#!/usr/bin/env python3
"""Validate TeleBlast selectors on fresh mutations of pinned sqlglot.

This is development tooling, not part of the submitted selector image. Each
mutation is generated from syntax in the pinned checkout, applied to a fresh
detached worktree, measured with pytest, and then passed to each requested
selection strategy. Actual failures stay separate from predictions and cost.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selector as selector_module  # noqa: E402
from scheduler import schedule  # noqa: E402


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SQLGLOT_URL = "https://github.com/tobymao/sqlglot.git"
MUTATION_CATEGORIES = (
    "arithmetic_operator",
    "comparison_operator",
    "boolean_condition",
    "removed_branch",
    "return_value",
    "function_call",
    "import_change",
    "class_level",
    "exception_handling",
    "dialect_behavior",
    "fixture_content",
)
PYTEST_OK = 0
PYTEST_FAILED = 1


@dataclass(frozen=True)
class SourceEdit:
    category: str
    path: str
    start: int
    end: int
    original: str
    replacement: str
    line: int
    description: str
    covered_tests: int = 0

    @property
    def mutation_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.category}\0{self.path}\0{self.start}\0{self.original}\0{self.replacement}".encode()
        ).hexdigest()[:10]
        return f"{self.category}-{digest}"

    def apply(self, source: str) -> str:
        if source[self.start : self.end] != self.original:
            raise ValueError(
                f"{self.path}:{self.line} no longer contains {self.original!r}"
            )
        return source[: self.start] + self.replacement + source[self.end :]

    def manifest(self) -> dict[str, Any]:
        def preview(value: str) -> str:
            return value if len(value) <= 240 else value[:237] + "..."

        return {
            "id": self.mutation_id,
            "category": self.category,
            "path": self.path,
            "line": self.line,
            "description": self.description,
            "original": preview(self.original),
            "replacement": preview(self.replacement),
            "covered_tests": self.covered_tests,
        }


@dataclass
class PytestResult:
    exit_code: int
    wall_seconds: float
    records: list[dict[str, Any]]
    stdout_tail: str


class OffsetTable:
    """Translate AST UTF-8 byte columns into Python string offsets."""

    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.starts: list[int] = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line)
        if not self.lines:
            self.starts.append(0)

    def offset(self, line: int, utf8_column: int) -> int:
        text = self.lines[line - 1]
        prefix = text.encode("utf-8")[:utf8_column].decode("utf-8")
        return self.starts[line - 1] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.offset(node.lineno, node.col_offset),
            self.offset(node.end_lineno, node.end_col_offset),
        )


class CoveragePopularity:
    """Cheap ranking signal; it is never used to determine expected failures."""

    def __init__(self, coverage_map: Path | None):
        self.rows: dict[str, list[tuple[int, int, int]]] = {}
        if not coverage_map or not coverage_map.exists():
            return
        with gzip.open(coverage_map, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        group_sizes = [len(group) for group in data.get("groups", ())]
        for path, rows in data.get("lines", {}).items():
            self.rows[path] = [
                (first, last, group_sizes[group]) for first, last, group in rows
            ]

    def count(self, path: str, first: int, last: int) -> int:
        return max(
            (size for begin, end, size in self.rows.get(path, ()) if begin <= last and first <= end),
            default=0,
        )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _literal_replacement(node: ast.AST, original: str) -> str:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return "False" if value else "True"
        if isinstance(value, int):
            return repr(value + 1)
        if isinstance(value, float):
            return repr(value + 1.0)
        if isinstance(value, str):
            return repr(value + "__teleblast_mutation__")
        if value is None:
            return "False"
    return f"not ({original})"


def _candidate(
    category: str,
    path: str,
    source: str,
    offsets: OffsetTable,
    node: ast.AST,
    replacement: str,
    description: str,
    popularity: CoveragePopularity,
) -> SourceEdit | None:
    required = ("lineno", "end_lineno", "col_offset", "end_col_offset")
    if not all(hasattr(node, attr) for attr in required):
        return None
    start, end = offsets.span(node)
    original = source[start:end]
    if not original or original == replacement:
        return None
    return SourceEdit(
        category=category,
        path=path,
        start=start,
        end=end,
        original=original,
        replacement=replacement,
        line=node.lineno,
        description=description,
        covered_tests=popularity.count(path, node.lineno, node.end_lineno),
    )


def _python_candidates(path: str, source: str, popularity: CoveragePopularity) -> Iterator[SourceEdit]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return
    offsets = OffsetTable(source)
    parents = _parents(tree)
    def segment(node: ast.AST) -> str:
        start, end = offsets.span(node)
        return source[start:end]

    nodes = sorted(
        ast.walk(tree),
        key=lambda node: (getattr(node, "lineno", sys.maxsize), getattr(node, "col_offset", 0)),
    )
    arithmetic = {
        ast.Add: "-", ast.Sub: "+", ast.Mult: "//",
        ast.FloorDiv: "*", ast.Div: "*", ast.Mod: "+",
    }
    comparisons = {
        ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.LtE: ">",
        ast.Gt: "<=", ast.GtE: "<", ast.In: "not in", ast.NotIn: "in",
        ast.Is: "is not", ast.IsNot: "is",
    }

    for node in nodes:
        edit = None
        if isinstance(node, ast.BinOp) and type(node.op) in arithmetic:
            left = segment(node.left)
            right = segment(node.right)
            if left and right:
                operator = arithmetic[type(node.op)]
                edit = _candidate(
                    "arithmetic_operator", path, source, offsets, node,
                    f"(({left}) {operator} ({right}))",
                    f"replace arithmetic operator with {operator}", popularity,
                )
        elif isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1 and type(node.ops[0]) in comparisons:
            left = segment(node.left)
            right = segment(node.comparators[0])
            if left and right:
                operator = comparisons[type(node.ops[0])]
                edit = _candidate(
                    "comparison_operator", path, source, offsets, node,
                    f"({left} {operator} {right})", f"replace comparison operator with {operator}", popularity,
                )
        elif isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            parts = [segment(value) for value in node.values]
            if all(parts):
                operator = "or" if isinstance(node.op, ast.And) else "and"
                edit = _candidate(
                    "boolean_condition", path, source, offsets, node,
                    f"({f' {operator} '.join(parts)})", f"replace boolean connective with {operator}", popularity,
                )
        elif isinstance(node, ast.If):
            edit = _candidate(
                "removed_branch", path, source, offsets, node.test, "False",
                "make branch unreachable", popularity,
            )
        elif isinstance(node, ast.Return) and node.value and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            original = segment(node.value)
            if original:
                edit = _candidate(
                    "return_value", path, source, offsets, node.value,
                    _literal_replacement(node.value, original), "change returned value", popularity,
                )
        elif isinstance(node, ast.Call) and node.args:
            argument = node.args[0]
            original = segment(argument)
            if original and not isinstance(argument, ast.Starred):
                edit = _candidate(
                    "function_call", path, source, offsets, argument,
                    _literal_replacement(argument, original), "change first call argument", popularity,
                )
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and node.names:
            alias = node.names[-1]
            if hasattr(alias, "lineno"):
                replacement = "__teleblast_missing_symbol"
                if alias.asname:
                    replacement += f" as {alias.asname}"
                edit = _candidate(
                    "import_change", path, source, offsets, alias, replacement,
                    "replace imported symbol with a missing name", popularity,
                )
        elif isinstance(node, ast.ExceptHandler) and node.type is not None:
            original = segment(node.type)
            replacement = "TypeError" if original != "TypeError" else "ValueError"
            edit = _candidate(
                "exception_handling", path, source, offsets, node.type, replacement,
                f"catch {replacement} instead", popularity,
            )

        if edit is not None:
            yield edit

        if isinstance(node, ast.ClassDef):
            if node.bases:
                base = node.bases[0]
                original = segment(base)
                if original and original != "object":
                    edit = _candidate(
                        "class_level", path, source, offsets, base, "object",
                        "replace first base class", popularity,
                    )
                    if edit:
                        yield edit
            else:
                for statement in node.body:
                    value = (
                        statement.value
                        if isinstance(statement, (ast.Assign, ast.AnnAssign))
                        else None
                    )
                    if value is not None:
                        literal = next(
                            (
                                child
                                for child in ast.walk(value)
                                if isinstance(child, ast.Constant)
                                and isinstance(child.value, (bool, int, float, str))
                                and len(segment(child)) <= 120
                            ),
                            None,
                        )
                        if literal is not None:
                            original = segment(literal)
                            edit = _candidate(
                                "class_level", path, source, offsets, literal,
                                _literal_replacement(literal, original),
                                "change class-level value", popularity,
                            )
                            if edit:
                                yield edit
                            break

        if (
            path.startswith("sqlglot/dialects/")
            and isinstance(node, ast.Constant)
            and isinstance(node.value, (str, int))
        ):
            parent = parents.get(node)
            if not isinstance(parent, ast.Expr):
                original = segment(node)
                if original:
                    edit = _candidate(
                        "dialect_behavior", path, source, offsets, node,
                        _literal_replacement(node, original),
                        "change dialect constant", popularity,
                    )
                    if edit:
                        yield edit


def _fixture_candidates(
    checkout: Path,
    fixture_readers: dict[str, Sequence[str]],
) -> Iterator[SourceEdit]:
    token = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
    ordered = sorted(fixture_readers, key=lambda item: (-len(fixture_readers[item]), item))
    for path in ordered:
        target = checkout / path
        suffixes = {".sql", ".json", ".yaml", ".yml", ".txt"}
        if not target.is_file() or target.suffix.lower() not in suffixes:
            continue
        try:
            source = target.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        match = next(
            (
                candidate
                for candidate in token.finditer(source)
                if candidate.group(0).upper() not in {"DESCRIPTION", "SOURCE"}
            ),
            None,
        )
        if not match:
            continue
        line = source.count("\n", 0, match.start()) + 1
        original = match.group(0)
        yield SourceEdit(
            category="fixture_content",
            path=path,
            start=match.start(),
            end=match.end(),
            original=original,
            replacement=original + "_MUTATED",
            line=line,
            description="change fixture token",
            covered_tests=len(fixture_readers[path]),
        )


def _compiles(edit: SourceEdit, checkout: Path) -> bool:
    if not edit.path.endswith(".py"):
        return True
    source = (checkout / edit.path).read_text(encoding="utf-8")
    try:
        compile(edit.apply(source), edit.path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, OverflowError, RecursionError):
        return False
    return True


def generate_mutations(
    checkout: Path,
    maps: Path,
    categories: Iterable[str] = MUTATION_CATEGORIES,
    per_category: int = 1,
) -> list[SourceEdit]:
    wanted = set(categories)
    unknown = wanted - set(MUTATION_CATEGORIES)
    if unknown:
        raise ValueError(f"unknown mutation categories: {sorted(unknown)}")
    popularity = CoveragePopularity(maps / "coverage.json.gz")
    candidates: dict[str, list[SourceEdit]] = defaultdict(list)
    for target in sorted((checkout / "sqlglot").rglob("*.py")):
        path = target.relative_to(checkout).as_posix()
        source = target.read_text(encoding="utf-8")
        for candidate in _python_candidates(path, source, popularity):
            if candidate.category in wanted:
                candidates[candidate.category].append(candidate)

    static_map = maps / "static.json.gz"
    if "fixture_content" in wanted and static_map.exists():
        with gzip.open(static_map, "rt", encoding="utf-8") as stream:
            fixture_readers = json.load(stream).get("fixtures", {})
        candidates["fixture_content"].extend(_fixture_candidates(checkout, fixture_readers))

    chosen: list[SourceEdit] = []
    for category in MUTATION_CATEGORIES:
        if category not in wanted:
            continue
        ordered = sorted(
            candidates.get(category, ()),
            key=lambda edit: (-edit.covered_tests, edit.path, edit.line, edit.start),
        )
        seen: set[tuple[str, int, int]] = set()
        valid: list[SourceEdit] = []
        for edit in ordered:
            identity = (edit.path, edit.start, edit.end)
            if identity in seen:
                continue
            seen.add(identity)
            if _compiles(edit, checkout):
                valid.append(edit)
            if len(valid) == per_category:
                break
        if len(valid) < per_category:
            raise RuntimeError(
                f"generated {len(valid)} valid {category} mutations, wanted {per_category}"
            )
        chosen.extend(valid)
    return chosen


def _run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=True, **kwargs)


def prepare_checkout(path: Path, offline: bool) -> None:
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"offline checkout does not exist: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", SQLGLOT_URL, str(path)])
        _run(["git", "-C", str(path), "checkout", "--detach", REVISION])
    revision = _run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True
    ).stdout.strip()
    if revision != REVISION:
        raise ValueError(f"sqlglot checkout is {revision}, expected {REVISION}")
    dirty = _run(
        ["git", "-C", str(path), "status", "--porcelain"], capture_output=True
    ).stdout
    if dirty:
        raise ValueError(f"sqlglot checkout is not clean:\n{dirty}")


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def run_pytest(
    checkout: Path,
    targets: Sequence[str],
    python: str,
    log_path: Path,
    timeout_seconds: float = 300.0,
) -> PytestResult:
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = log_path.with_suffix(".failures.jsonl")
    report_path.unlink(missing_ok=True)
    env = dict(os.environ)
    python_path = [str(ROOT / "tools"), str(checkout)]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(python_path),
            "PYTHONHASHSEED": "0",
            "SKIP_INTEGRATION": "0",
            "PYTHON_CPU_COUNT": "2",
            "OPENBLAS_NUM_THREADS": "1",
            "TELEBLAST_MUTATION_REPORT": str(report_path),
        }
    )
    command = [
        python,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "--disable-warnings",
        "--continue-on-collection-errors",
        "-p",
        "mutation_pytest_plugin",
        *targets,
    ]
    started = time.monotonic()
    try:
        process = subprocess.run(
            command,
            cwd=checkout,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code = process.returncode
        stdout = process.stdout
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        stdout = error.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
    wall = time.monotonic() - started
    log_path.write_text(stdout, encoding="utf-8")
    return PytestResult(
        exit_code=exit_code,
        wall_seconds=wall,
        records=_load_records(report_path),
        stdout_tail="\n".join(stdout.splitlines()[-30:]),
    )


def resolve_failure_records(
    records: Iterable[dict[str, Any]],
    catalog: selector_module.TestCatalog,
) -> tuple[set[int], list[str]]:
    numbers: set[int] = set()
    unresolved: list[str] = []
    for record in records:
        if record.get("kind") not in {"test", "collect"}:
            continue
        nodeid = str(record.get("nodeid", ""))
        if record.get("kind") == "collect":
            candidates = (nodeid.split("::", 1)[0],)
        else:
            # Exact runtime failures must not be expanded to every test in the
            # same file. TestCatalog also indexes the unparameterized base ID.
            candidates = (nodeid, nodeid.split("[", 1)[0])
        resolved = catalog.resolve(candidates)
        if resolved:
            numbers.update(resolved)
        else:
            unresolved.append(nodeid)
    return numbers, sorted(set(unresolved))


def _simulated_milliseconds(
    workers: Sequence[Sequence[int]], durations: dict[int, int]
) -> int:
    occupied = [worker for worker in workers if worker]
    if not occupied:
        return 0
    largest = max(sum(durations[number] for number in worker) for worker in occupied)
    return largest + 2_000 * len(occupied)


def predict(
    patch: Path,
    tests: list[dict[str, Any]],
    strategies: Sequence[str],
    actual_failures: set[int],
    maps: Path,
) -> dict[str, dict[str, Any]]:
    durations = {
        test["number"]: math.floor(test["seconds"] * 1000 + 0.5) for test in tests
    }
    results: dict[str, dict[str, Any]] = {}
    supported = set(
        getattr(
            selector_module,
            "SUPPORTED_STRATEGIES",
            ("all", "static", "coverage", "hybrid"),
        )
    )
    for strategy in strategies:
        if strategy not in supported:
            raise ValueError(
                f"selector does not advertise strategy {strategy!r}; supported: {sorted(supported)}"
            )
        started = time.process_time()
        instance = selector_module.Selector(tests, strategy, maps)
        try:
            selected = instance.select(patch)
        finally:
            if instance.source_zip is not None:
                instance.source_zip.close()
        cpu = time.process_time() - started
        workers = schedule(selected, durations)
        results[strategy] = {
            "selected_count": len(selected),
            "selected_runtime_seconds": sum(durations[number] for number in selected) / 1000,
            "simulated_seconds": _simulated_milliseconds(workers, durations) / 1000,
            "selector_cpu_seconds": cpu,
            "missed_test_numbers": sorted(actual_failures - selected),
            "miss": bool(actual_failures - selected),
        }
    return results


def _unique_test_files(tests: Sequence[dict[str, Any]]) -> list[str]:
    return sorted({str(test["file"]) for test in tests})


def validate_mutation(
    base: Path,
    edit: SourceEdit,
    tests: list[dict[str, Any]],
    baseline_failures: set[int],
    strategies: Sequence[str],
    maps: Path,
    targets: Sequence[str],
    python: str,
    output: Path,
    pytest_timeout: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="teleblast-mutation-") as temporary:
        worktree = Path(temporary) / "checkout"
        _run(
            ["git", "-C", str(base), "worktree", "add", "--detach", str(worktree), REVISION],
            stdout=subprocess.DEVNULL,
        )
        try:
            target = worktree / edit.path
            source = target.read_text(encoding="utf-8")
            target.write_text(edit.apply(source), encoding="utf-8")
            patch_bytes = subprocess.run(
                [
                    "git", "-C", str(worktree), "diff", "--no-ext-diff",
                    "--no-color", "--", edit.path,
                ],
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
            patch = output / "patches" / f"{edit.mutation_id}.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_bytes(patch_bytes)
            pytest_result = run_pytest(
                worktree,
                targets,
                python,
                output / "logs" / f"{edit.mutation_id}.log",
                pytest_timeout,
            )
            catalog = selector_module.TestCatalog(tests)
            observed, unresolved = resolve_failure_records(pytest_result.records, catalog)
            actual = observed - baseline_failures
            prediction = predict(patch, tests, strategies, actual, maps)
            collection_failure = any(
                record.get("kind") == "collect" for record in pytest_result.records
            )
            valid_test_run = (
                pytest_result.exit_code in {PYTEST_OK, PYTEST_FAILED}
                or (pytest_result.exit_code == 2 and collection_failure)
            ) and not unresolved
            return {
                **edit.manifest(),
                "pytest_exit_code": pytest_result.exit_code,
                "pytest_wall_seconds": pytest_result.wall_seconds,
                "actual_failing_test_numbers": sorted(actual),
                "baseline_failing_test_numbers_seen": sorted(observed & baseline_failures),
                "unresolved_failure_nodeids": unresolved,
                "collection_failure": collection_failure,
                "complete_test_execution": pytest_result.exit_code in {PYTEST_OK, PYTEST_FAILED},
                "valid_test_run": valid_test_run,
                "predictions": prediction,
            }
        finally:
            subprocess.run(
                ["git", "-C", str(base), "worktree", "remove", "--force", str(worktree)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _parse_args() -> argparse.Namespace:
    supported = tuple(
        getattr(
            selector_module,
            "SUPPORTED_STRATEGIES",
            ("all", "static", "coverage", "hybrid"),
        )
    )
    defaults = [name for name in ("hybrid", "coverage", "precision") if name in supported]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout", type=Path, default=ROOT / ".bench" / "sqlglot-mutations"
    )
    parser.add_argument("--maps", type=Path, default=ROOT / "maps")
    parser.add_argument("--tests", type=Path, default=ROOT / "sample" / "tests.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".bench" / "mutations")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--pytest-timeout", type=float, default=300.0)
    parser.add_argument("--offline", action="store_true", help="never clone a missing checkout")
    parser.add_argument(
        "--list-only", action="store_true", help="write manifest without running pytest"
    )
    parser.add_argument("--per-category", type=int, default=1)
    parser.add_argument("--category", action="append", choices=MUTATION_CATEGORIES)
    parser.add_argument("--strategy", action="append", choices=supported, dest="strategies")
    parser.add_argument(
        "--pytest-target",
        action="append",
        help="override the default of every test file represented in tests.json",
    )
    parser.set_defaults(strategies=defaults)
    return parser.parse_args()


def main() -> int:
    options = _parse_args()
    if options.per_category < 1:
        raise ValueError("--per-category must be positive")
    if options.pytest_timeout <= 0:
        raise ValueError("--pytest-timeout must be positive")
    prepare_checkout(options.checkout, options.offline)
    categories = options.category or list(MUTATION_CATEGORIES)
    mutations = generate_mutations(
        options.checkout, options.maps, categories, options.per_category
    )
    options.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "revision": REVISION,
        "mutations": [mutation.manifest() for mutation in mutations],
    }
    (options.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if options.list_only:
        print(f"wrote {len(mutations)} mutations to {options.output / 'manifest.json'}")
        return 0

    tests = json.loads(options.tests.read_text(encoding="utf-8"))["tests"]
    targets = options.pytest_target or _unique_test_files(tests)
    baseline = run_pytest(
        options.checkout,
        targets,
        options.python,
        options.output / "logs" / "baseline.log",
        options.pytest_timeout,
    )
    catalog = selector_module.TestCatalog(tests)
    baseline_failures, baseline_unresolved = resolve_failure_records(baseline.records, catalog)
    if baseline.exit_code not in {PYTEST_OK, PYTEST_FAILED} or baseline_unresolved:
        print(baseline.stdout_tail, file=sys.stderr)
        raise RuntimeError(
            f"baseline pytest run is unusable: exit={baseline.exit_code}, "
            f"unresolved={baseline_unresolved}"
        )

    results = []
    for index, mutation in enumerate(mutations, start=1):
        print(
            f"[{index}/{len(mutations)}] {mutation.mutation_id} "
            f"{mutation.path}:{mutation.line}",
            file=sys.stderr,
        )
        results.append(
            validate_mutation(
                options.checkout,
                mutation,
                tests,
                baseline_failures,
                options.strategies,
                options.maps,
                targets,
                options.python,
                options.output,
                options.pytest_timeout,
            )
        )
        partial = {
            "revision": REVISION,
            "strategies": options.strategies,
            "baseline": {
                "pytest_exit_code": baseline.exit_code,
                "pytest_wall_seconds": baseline.wall_seconds,
                "failing_test_numbers": sorted(baseline_failures),
            },
            "results": results,
        }
        (options.output / "report.json").write_text(
            json.dumps(partial, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    valid_results = [result for result in results if result["valid_test_run"]]
    invalid_count = len(results) - len(valid_results)
    misses = {
        strategy: sum(result["predictions"][strategy]["miss"] for result in valid_results)
        for strategy in options.strategies
    }
    print(
        ", ".join(
            f"{strategy}: {count}/{len(valid_results)} valid mutation misses"
            for strategy, count in misses.items()
        ) + (f", invalid runs: {invalid_count}" if invalid_count else "")
    )
    if invalid_count:
        return 2
    return 0 if not any(misses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
