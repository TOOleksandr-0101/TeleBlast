"""Index AST dependencies of the unmodified sqlglot checkout (Python 3.13+)."""

import argparse
import ast
from collections import defaultdict
import gzip
from importlib.util import resolve_name
import json
from pathlib import Path, PurePosixPath
import subprocess


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_name(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def dependencies(tree, path, modules):
    """Resolve local imports, their package initializers, and literal dialect lookups."""
    name = module_name(path)
    package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            source = resolve_name("." * node.level + (node.module or ""), package)
            imports.add(source)
            for alias in node.names:
                imports.add(f"{source}.{alias.name}")
                if source == "sqlglot.dialects":
                    imports.add(f"{source}.{alias.name.lower()}")
        elif path.startswith("tests/") and isinstance(node, ast.Constant):
            # Dialects are lazy-loaded from their registered string name.
            if isinstance(node.value, str) and node.value.isidentifier():
                imports.add("sqlglot.dialects." + node.value.lower())
    result = set()
    for target in imports & modules.keys():
        while target:
            if target in modules:
                result.add(modules[target])
            target = target.rpartition(".")[0]
    result.discard(path)
    return result


def scoped_functions(tree, prefix=()):
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            yield from scoped_functions(node, (*prefix, node.name))
        elif isinstance(node, FUNCTIONS):
            yield prefix, node


def literal_path(expr, bindings):
    """Evaluate only strings, basic joins and f-strings from fixture helpers."""
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.FormattedValue):
        return literal_path(expr.value, bindings)
    if isinstance(expr, ast.JoinedStr):
        parts = [literal_path(part, bindings) for part in expr.values]
        return "".join(parts) if None not in parts else None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = (literal_path(part, bindings) for part in (expr.left, expr.right))
        return left + right if left is not None and right is not None else None
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "join":
        parts = [literal_path(part, bindings) for part in expr.args]
        return "/".join(parts) if parts and None not in parts else None
    return None


def fixture_arguments(func):
    """Find the actual filenames consumed by this function or method."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
            if node.func.id in {"load_sql_fixture_pairs", "load_sql_fixtures", "open"}:
                yield node.args[0]


def bind_literals(helper, call, namespace):
    params = [arg.arg for arg in helper.args.args if arg.arg not in {"self", "cls"}]
    arguments = dict(zip(params, call.args))
    arguments.update((kw.arg, kw.value) for kw in call.keywords if kw.arg)
    return {**namespace, **{k: literal_path(v, namespace) for k, v in arguments.items()}}


def fixture_links(tree, path, existing):
    # Module-scoped fixture directories also capture readers that construct paths
    # from schema table names, as in TPC-H/TPC-DS class setup.
    namespace = {"FIXTURES_DIR": "tests/fixtures"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(child, ast.Name) and child.id == "FIXTURES_DIR" for child in ast.walk(node.value)
        ):
            directory = literal_path(node.value, namespace)
            if directory:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        namespace[target.id] = directory
                yield from ((f, path) for f in existing if f.startswith(directory.rstrip("/") + "/"))

    funcs = list(scoped_functions(tree))
    readers = {(scope, f.name): (f, list(fixture_arguments(f))) for scope, f in funcs}
    for scope, func in funcs:
        owner = "::".join((path, *scope, func.name)) if func.name.startswith("test_") else path
        values = [literal_path(expr, namespace) for expr in readers[scope, func.name][1]]
        for call in ast.walk(func):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Attribute):
                key = (scope, call.func.attr)
            elif isinstance(call.func, ast.Name):
                key = ((), call.func.id)
            else:
                continue
            if key in readers:
                helper, expressions = readers[key]
                bound = bind_literals(helper, call, namespace)
                values.extend(literal_path(expr, bound) for expr in expressions)
        for value in filter(None, values):
            fixture = value if value.startswith("tests/fixtures/") else f"tests/fixtures/{value}"
            if fixture in existing:
                yield fixture, owner


def build(root):
    if (root / ".git").exists():
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        if head != REVISION:
            raise ValueError(f"expected {REVISION}, got {head}")
    files = sorted(p.relative_to(root).as_posix() for folder in ("sqlglot", "tests")
                   for p in (root / folder).rglob("*.py"))
    modules = {module_name(p): p for p in files}
    fixtures = {p.relative_to(root).as_posix() for p in (root / "tests/fixtures").rglob("*") if p.is_file()}
    if "sqlglot/parser.py" not in files or not fixtures:
        raise ValueError("expected a complete pinned sqlglot checkout")
    graph, spans = {}, {}
    owners, direct, readers, symbol_uses = (defaultdict(set) for _ in range(4))
    defined = set()
    for path in files:
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        graph[path] = dependencies(tree, path, modules)
        declarations = []
        for node in ast.walk(tree):
            if isinstance(node, DEFINITIONS):
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                declarations.append((first, node.end_lineno, node.name,
                                     "class" if isinstance(node, ast.ClassDef) else "function",
                                     node.body[0].lineno))
                if path.startswith("sqlglot/"):
                    defined.add(node.name)
        spans[path] = sorted(declarations)
        if PurePosixPath(path).name.startswith("test_"):
            for fixture, owner in fixture_links(tree, path, fixtures):
                readers[fixture].add(owner)
                owners[fixture].add(path)
            for scope, func in scoped_functions(tree):
                if func.name.startswith("test_"):
                    nodeid = "::".join((path, *scope, func.name))
                    for expr in ast.walk(func):
                        if isinstance(expr, ast.Name):
                            symbol_uses[expr.id].add(nodeid)
                        elif isinstance(expr, ast.Attribute):
                            symbol_uses[expr.attr].add(nodeid)
    # Reverse reachability from each test module follows imports of base classes,
    # test helpers, re-exports and imported implementation modules.
    for test in files:
        if not PurePosixPath(test).name.startswith("test_"):
            continue
        for source in graph[test]:
            direct[source].add(test)
        seen, work = set(), [test]
        while work:
            source = work.pop()
            if source not in seen:
                owners[source].add(test)
                seen.add(source)
                work.extend(graph[source] - seen)

    def ordered(index):
        return {k: sorted(v) for k, v in index.items()}

    return {"revision": REVISION, "files": ordered(owners), "direct": ordered(direct),
            "fixtures": ordered(readers), "spans": spans,
            "symbols": ordered({name: uses for name, uses in symbol_uses.items() if name in defined})}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source", type=Path, help="checkout or source archive extracted at the pinned revision")
    cli.add_argument("output", type=Path, nargs="?", default=Path("maps/static.json.gz"))
    args = cli.parse_args()
    index = build(args.source)
    payload = json.dumps(index, separators=(",", ":"), sort_keys=True).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    print(f"{len(index['spans'])} modules, {len(index['fixtures'])} fixture paths, {args.output.stat().st_size} bytes")
