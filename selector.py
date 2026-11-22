"""Select sqlglot tests for independent patches against the pinned revision."""

import argparse
import ast
from bisect import bisect_left
from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
import math
from pathlib import Path
import sys
from zipfile import ZipFile

from impact import UnsupportedPatch, parse_patch
from scheduler import schedule


MAPS = Path(__file__).resolve().parent / "maps"
REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SUPPORTED_STRATEGIES = ("all", "static", "coverage", "hybrid", "precision", "candidate")


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (ast.ClassDef, *FUNCTIONS)
CONTROL_STATEMENTS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.Match,
)


def _span(node):
    return node.lineno, node.end_lineno


def _contains(node, first, last):
    return node.lineno <= first and last <= node.end_lineno


def _overlaps(node, first, last):
    return node.lineno <= last and first <= node.end_lineno


def _dump(value):
    return ast.dump(value, include_attributes=False)


def comparison_edit(before, after):
    """Identify an edit whose sole semantic AST difference is one comparison operator."""
    found = []

    def match(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, list):
            return len(a) == len(b) and all(match(x, y) for x, y in zip(a, b))
        if not isinstance(a, ast.AST):
            return a == b
        if isinstance(a, ast.Compare) and len(a.ops) == len(b.ops) == 1:
            old, new = type(a.ops[0]).__name__, type(b.ops[0]).__name__
            if old != new:
                if found:
                    return False
                found.append((a.lineno, a.col_offset, old, new))
            return match(a.left, b.left) and match(a.comparators, b.comparators)
        return all(match(getattr(a, key), getattr(b, key)) for key in a._fields)

    return found[0] if match(before, after) and found else None


def relation_result(op, relation):
    identity_only = {"identity_equal", "identity_unequal", "value_equal"}
    if relation not in {"same", "equal", "less", "greater", "disjoint"} | identity_only:
        return None
    if op in {"Is", "IsNot"}:
        return (relation in {"same", "identity_equal"}) == (op == "Is")
    if op in {"Eq", "NotEq"}:
        return (relation in {"same", "equal", "identity_equal", "value_equal"}) == (op == "Eq")
    # Two different builtin types can be unordered. 'same' can also refer
    # to None, which has no ordering even when both operands are identical.
    if relation in {"same", "disjoint"} | identity_only:
        return None
    truth = {"Lt": {"less"}, "LtE": {"less", "equal"},
             "Gt": {"greater"}, "GtE": {"greater", "equal"}}
    return relation in truth[op] if op in truth else None


def read_map(directory, name):
    path = directory / (name + ".json.gz")
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if data["revision"] != REVISION:
        raise ValueError(f"{name} map uses a different sqlglot revision")
    return data


class TestCatalog:
    def __init__(self, tests):
        self.by_name = defaultdict(set)
        self.numbers = set()
        for test in tests:
            number = test["number"]
            self.numbers.add(number)
            for name in (test["file"], test["id"], test["id"].split("[", 1)[0]):
                self.by_name[name].add(number)

    def resolve(self, names):
        result = set()
        for name in names:
            result.update(self.by_name.get(name, ()))
        return result


class SyntaxModel:
    """Location-aware syntax facts used by the precision strategy.

    The model intentionally separates control-flow *shape* from expression
    values.  Replacing an operator in an existing predicate can be covered by
    the tests that evaluated that predicate; adding a branch, handler or early
    return widens to every observed caller of the enclosing function.
    """

    def __init__(self, source, path):
        self.tree = ast.parse(source, filename=path)
        self.nodes = [node for node in ast.walk(self.tree) if hasattr(node, "lineno")]
        self.scopes = {}
        self.scope_keys = {}
        self._visit(self.tree, ())

    def _visit(self, node, prefix):
        child_prefix = prefix
        if isinstance(node, DEFINITIONS):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            child_prefix = (*prefix, f"{kind}:{node.name}")
            self.scopes[child_prefix] = node
            self.scope_keys[id(node)] = child_prefix
        for child in ast.iter_child_nodes(node):
            self._visit(child, child_prefix)

    @staticmethod
    def _signature(node):
        if isinstance(node, FUNCTIONS):
            values = (
                node.args,
                node.decorator_list,
                node.returns,
                getattr(node, "type_params", ()),
            )
        else:
            values = (
                node.bases,
                node.keywords,
                node.decorator_list,
                getattr(node, "type_params", ()),
            )
        encoded = tuple(
            None
            if value is None
            else _dump(value)
            if isinstance(value, ast.AST)
            else tuple(map(_dump, value))
            for value in values
        )
        return (*encoded, ast.get_docstring(node, clean=False))

    @property
    def definition_signatures(self):
        return {key: self._signature(node) for key, node in self.scopes.items()}

    @property
    def imports(self):
        return tuple(
            _dump(node) for node in self.nodes if isinstance(node, (ast.Import, ast.ImportFrom))
        )

    @property
    def semantics(self):
        return _dump(self.tree)

    @property
    def exception_handlers(self):
        return tuple(
            _dump(node.type) if node.type is not None else None
            for node in self.nodes
            if isinstance(node, ast.ExceptHandler)
        )

    def scope(self, first, last, kind):
        choices = [
            node
            for key, node in self.scopes.items()
            if key[-1].startswith(kind + ":") and _contains(node, first, last)
        ]
        return min(choices, key=lambda node: node.end_lineno - node.lineno, default=None)

    def key(self, node):
        return self.scope_keys.get(id(node))

    def statement(self, first, last):
        choices = [
            node
            for node in self.nodes
            if isinstance(node, ast.stmt)
            and not isinstance(node, DEFINITIONS)
            and _contains(node, first, last)
        ]
        return min(choices, key=lambda node: node.end_lineno - node.lineno, default=None)

    def predicate_ranges(self, first, last):
        ranges = set()
        for node in self.nodes:
            targets = ()
            if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                targets = (node.test,)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets = (node.iter,)
            elif isinstance(node, ast.comprehension):
                targets = (node.iter, *node.ifs)
            elif isinstance(node, ast.Match):
                targets = (node.subject,)
            for target in targets:
                if _overlaps(target, first, last):
                    ranges.add(_span(target))
        return ranges

    @classmethod
    def _body_shape(cls, body):
        return tuple(cls._statement_shape(statement) for statement in body)

    @classmethod
    def _statement_shape(cls, node):
        if isinstance(node, ast.If):
            return ("if", cls._body_shape(node.body), cls._body_shape(node.orelse))
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return (type(node).__name__, cls._body_shape(node.body), cls._body_shape(node.orelse))
        if isinstance(node, (ast.Try, ast.TryStar)):
            handlers = tuple(
                (_dump(handler.type) if handler.type else None, cls._body_shape(handler.body))
                for handler in node.handlers
            )
            return (
                type(node).__name__,
                cls._body_shape(node.body),
                handlers,
                cls._body_shape(node.orelse),
                cls._body_shape(node.finalbody),
            )
        if isinstance(node, ast.Match):
            cases = tuple(
                (_dump(case.pattern), bool(case.guard), cls._body_shape(case.body))
                for case in node.cases
            )
            return ("match", cases)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return (type(node).__name__, cls._body_shape(node.body))
        # The presence and ordering of transfers is structural; their values
        # are handled by statement coverage instead of widening every edit.
        transfers = tuple(
            type(child).__name__
            for child in ast.walk(node)
            if isinstance(child, (ast.Return, ast.Yield, ast.YieldFrom, ast.Raise,
                                  ast.Break, ast.Continue))
        )
        return type(node).__name__, transfers

    def control_shape(self, scope):
        return self._body_shape(scope.body)


class Coverage:
    def __init__(self, data, catalog):
        self.available = bool(data)
        nodes = data.get("nodes", [])
        self.untraced = catalog.numbers - catalog.resolve(nodes[i] for i in data.get("complete", ()))
        self.groups = [catalog.resolve(nodes[i] for i in group) for group in data.get("groups", ())]
        self.lines = data.get("lines", {})
        self.ends = {path: [row[1] for row in rows] for path, rows in self.lines.items()}
        self.readers = {
            path: catalog.resolve(nodes[i] for i in group)
            for path, group in data.get("fixtures", {}).items()
        }
        self.source_readers = {
            path: catalog.resolve(nodes[i] for i in group)
            for path, group in data.get("source_readers", {}).items()
        }

    def hits(self, path, first, last):
        rows = self.lines.get(path, ())
        index = bisect_left(self.ends.get(path, ()), first)
        groups = set()
        while index < len(rows) and rows[index][0] <= last:
            groups.add(rows[index][2])
            index += 1
        hits = set()
        for group in groups:
            hits.update(self.groups[group])
        return hits


class Selector:
    def __init__(self, tests, strategy, maps=MAPS):
        if strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(f"unknown selection strategy: {strategy}")
        self.catalog = TestCatalog(tests)
        self.all = self.catalog.numbers
        self.strategy = strategy
        self.static = read_map(maps, "static") if strategy != "all" else {}
        data = read_map(maps, "coverage") if strategy in ("coverage", "hybrid", "precision", "candidate") else {}
        self.coverage = Coverage(data, self.catalog)
        self.source_zip = ZipFile(maps / "sources.zip") if strategy != "all" else None
        if strategy == "candidate":
            obs = read_map(maps, "observations")
            nodes = obs.get("nodes", ())
            self.observed_sites = obs.get("sites", {})
            self.observed_groups = [self.catalog.resolve(nodes[i] for i in grp)
                                    for grp in obs.get("groups", ())]
            self.observed_complete = self.catalog.resolve(nodes[i] for i in obs.get("complete", ()))
            self.collection = read_map(maps, "collection")
            members = read_map(maps, "member_reads")
            self.member_reads = members.get("uses", {})
            self.member_groups = members.get("groups", ())
            self.member_nodes = members.get("nodes", ())

    @lru_cache(maxsize=32)
    def source(self, path):
        try:
            return self.source_zip.read(path).decode("utf-8")
        except KeyError as exc:
            raise UnsupportedPatch("source missing from pinned revision") from exc

    def static_tests(self, field, key):
        return self.catalog.resolve(self.static.get(field, {}).get(key, ()))

    def source_observers(self, path):
        names = self.static.get("observers", ())
        if isinstance(names, dict):
            names = (*names.get("*", ()), *names.get(path, ()))
        return self.catalog.resolve(names) | self.coverage.source_readers.get(path, set())

    @lru_cache(maxsize=32)
    def syntax(self, path):
        return SyntaxModel(self.source(path), path)

    @lru_cache(maxsize=32)
    def original_code_digest(self, path):
        code = compile(self.source(path).encode("utf-8"), path, "exec", dont_inherit=True)
        return hashlib.sha256(marshal.dumps(code)).digest()

    def function(self, path, first, last):
        owners = (
            span for span in self.static.get("spans", {}).get(path, ())
            if span[3] == "function" and span[0] < first and last <= span[1]
            and (len(span) < 5 or span[4] <= first)
        )
        return min(owners, key=lambda span: span[1] - span[0], default=None)

    def _precision_structural(self, path, observers):
        dependent = self.static_tests("files", path) | self.catalog.resolve([path])
        observed = self.coverage.hits(path, 1, sys.maxsize)
        return dependent | observed | observers or self.all

    @staticmethod
    def _locations(model, ranges, kind):
        result = []
        for first, last in ranges:
            owner = model.scope(first, last, kind)
            if owner is not None and owner not in result:
                result.append(owner)
        return result

    def precision_python_tests(self, change):
        """Select by executed statements, widening only for structural risk.

        Existing expression changes cannot affect a test that never evaluates
        the containing statement.  Multi-line predicates are special: changing
        a short-circuited operand can make another operand reachable, so every
        test that evaluated any part of the predicate is retained.  Added
        control flow and altered exception structure widen to observed callers.
        """
        path = change.path
        before = self.source(path)
        after = change.updated_python(before)
        if before == after:
            return set()
        try:
            compile(after.encode("utf-8"), path, "exec", dont_inherit=True)
            original = self.syntax(path)
            updated = SyntaxModel(after, path)
        except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
            raise UnsupportedPatch("invalid patched Python") from exc

        selected = self._precision_select(change, original, updated)
        if self.strategy == "candidate":
            return self._candidate_select(change, original, updated, selected)
        return selected

    def _precision_select(self, change, original, updated):
        path = change.path
        observers = self.source_observers(path)
        direct_test = self.catalog.resolve([path])
        if original.semantics == updated.semantics:
            # Comments and formatting can still be observed through source
            # reads, code-object inspection or tracebacks.  The index records
            # tests with those behaviours instead of treating all import opens
            # as source introspection.
            return observers | direct_test

        if (
            original.definition_signatures != updated.definition_signatures
            or original.imports != updated.imports
            or original.exception_handlers != updated.exception_handlers
        ):
            return self._precision_structural(path, observers)

        hunks = []
        touched_functions = set()
        module_level = False
        for hunk in change.hunks:
            old_edits = list(hunk.edited_lines("old"))
            new_edits = list(hunk.edited_lines("new"))
            old_ranges = old_edits or list(hunk.changes())
            new_ranges = new_edits or list(hunk.new_changes())
            old_functions = self._locations(original, old_ranges, "function")
            new_functions = self._locations(updated, new_ranges, "function")
            old_classes = self._locations(original, old_ranges, "class")
            new_classes = self._locations(updated, new_ranges, "class")
            for function in (*old_functions, *new_functions):
                model = original if function in old_functions else updated
                key = model.key(function)
                if key:
                    touched_functions.add(key)
            if not old_functions and not new_functions and not old_classes and not new_classes:
                module_level = True
            hunks.append(
                (hunk, old_edits, old_ranges, old_functions, new_functions,
                 old_classes, new_classes)
            )

        # Executable module initialization is shared state established before
        # individual tests can be attributed.  Imports and definition headers
        # were handled above; other module statements retain the transitive
        # dependency fallback.
        if module_level:
            return self._precision_structural(path, observers)

        risky_functions = set()
        for key in touched_functions:
            old_scope = original.scopes.get(key)
            new_scope = updated.scopes.get(key)
            if (
                old_scope is None
                or new_scope is None
                or original.control_shape(old_scope) != updated.control_shape(new_scope)
            ):
                risky_functions.add(key)

        # Tests with incomplete traces (notably subprocess-based tests) cannot
        # appear in statement coverage.  Retain only the incomplete tests that
        # the static import graph links to this implementation module.
        incomplete = self.coverage.untraced & self.static_tests("files", path)
        result = observers | direct_test | incomplete
        for (
            hunk,
            old_edits,
            old_ranges,
            old_functions,
            new_functions,
            old_classes,
            new_classes,
        ) in hunks:
            exact = set()
            for first, last in hunk.changes():
                exact.update(self.coverage.hits(path, first, last))

            # A changed right-hand operand may have been short-circuited in the
            # old run.  Covering the complete predicate retains tests that only
            # evaluated its left-hand side (and loop dispatch equivalents).
            for first, last in old_edits:
                for begin, end in original.predicate_ranges(first, last):
                    exact.update(self.coverage.hits(path, begin, end))

            # Continuation lines do not always receive their own line event.
            # If an edit has no direct hit, retry at the containing statement's
            # first executable line before concluding it was unobserved.
            if not exact:
                for first, last in old_edits or old_ranges:
                    statement = original.statement(first, last)
                    if statement is not None:
                        exact.update(self.coverage.hits(path, statement.lineno, statement.lineno))

            functions = old_functions or new_functions
            if functions:
                for function in functions:
                    model = original if function in old_functions else updated
                    key = model.key(function)
                    old_scope = original.scopes.get(key)
                    if key in risky_functions and old_scope is not None:
                        callers = self.coverage.hits(path, old_scope.lineno, old_scope.end_lineno)
                        callers.update(self.static_tests("symbols", old_scope.name))
                        result.update(callers)
                    else:
                        result.update(exact)
                        if old_scope is not None:
                            # Incomplete tests have no usable line map.  Retain
                            # only those that statically name this function.
                            result.update(
                                self.coverage.untraced
                                & self.static_tests("symbols", old_scope.name)
                            )
                continue

            classes = old_classes or new_classes
            if classes:
                if exact:
                    result.update(exact)
                    continue
                symbols = set()
                for owner in classes:
                    symbols.update(self.static_tests("symbols", owner.name))
                result.update(symbols)
                if not symbols:
                    direct = self.static_tests("direct", path)
                    result.update(direct or self.static_tests("files", path) or self.all)
                continue

            # This should only be reachable for syntax whose owner disappeared
            # without changing a recorded signature.  Fail closed.
            return self._precision_structural(path, observers)
        return result

    def _candidate_select(self, change, original, updated, selected):
        path = change.path
        if not self.coverage.available:
            return self.static_tests("files", path) or self.all
        # AST-equal edits can still alter source text or positions. Incomplete
        # subprocess traces cannot rule out observation.
        protected = self.source_observers(path) | self.catalog.resolve([path]) | self.coverage.untraced
        if original.semantics == updated.semantics:
            return selected | protected
        ranges = [r for h in change.hunks for r in h.changes()]
        # Isolated collection detects function calls made while importing
        # test modules, before ordinary per-test line attribution began.
        collection_modules = set()
        for start, end, modules in self.collection.get("files", {}).get(path, ()):
            if any(first <= end and start <= last for first, last in ranges):
                collection_modules.update(modules)
        protected.update(self.catalog.resolve(collection_modules))
        if self.collection:
            protected.update(self.all - self.catalog.resolve(self.collection.get("complete", ())))

        for first, last in ranges:
            scope = original.scope(first, last, "function")
            owner = original.scope(first, last, "class")
            stmt = original.statement(first, last)
            if scope is None and owner and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in self.member_reads:
                        group = self.member_groups[self.member_reads[target.id]]
                        protected.update(self.catalog.resolve(self.member_nodes[i] for i in group))

        if self.observed_sites:
            edit = comparison_edit(original.tree, updated.tree)
            if edit:
                line, col, old, new = edit
                row = self.observed_sites.get(f"{path}:{line}:{col}", {})
                equal, uncertain = set(), set()
                for relation, group in row.items():
                    a, b = relation_result(old, relation), relation_result(new, relation)
                    bucket = equal if a is not None and a == b else uncertain
                    bucket.update(self.observed_groups[group])
                # Require that every observation for a test agrees. A single
                # opaque/changed operand or untraced child prevents removal.
                selected = selected - ((equal - uncertain) & self.observed_complete)
        return selected | protected

    def python_tests(self, change):
        path = change.path
        before = self.source(path)
        after = change.updated_python(before)
        if before == after:
            return set()
        try:
            # Syntax errors in a function can prevent collection of every importer.
            code = compile(after.encode("utf-8"), path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
            raise UnsupportedPatch("invalid patched Python") from exc

        dependent = self.static_tests("files", path) | self.catalog.resolve([path])
        coverage = self.coverage
        readers = coverage.readers.get(path, set())
        if not coverage.available or self.strategy == "static":
            return dependent or self.all
        if self.strategy == "hybrid" and hashlib.sha256(marshal.dumps(code)).digest() == self.original_code_digest(path):
            # Bytecode, constants and debugging positions matched. Source-text
            # readers and incompletely observed tests may still see the edit.
            return readers | coverage.untraced

        result = readers | self.catalog.resolve([path])
        intervals = [interval for hunk in change.hunks for interval in hunk.changes()]
        structural = any(
            row[0] in "+-" and row[1:].lstrip().startswith(("def ", "async def ", "class ", "@", "import ", "from "))
            for hunk in change.hunks for row in hunk.body
        )
        for first, last in intervals:
            exact = coverage.hits(path, first, last)
            if self.strategy == "coverage":
                result.update(exact)
                continue
            owner = self.function(path, first, last)
            if structural or not owner:
                # Module/class initialization may run before pytest attributes
                # coverage to a test. Include the transitive import closure.
                if not dependent:
                    return self.all
                return dependent | readers | coverage.untraced | coverage.hits(path, 1, sys.maxsize)

            scope = coverage.hits(path, owner[0], owner[1])
            result.update(exact)
            result.update(scope)
            result.update(self.static_tests("symbols", owner[2]))
            if not scope:
                result.update(dependent or self.all)
        return (result | coverage.untraced) if result else (dependent or self.all)

    def file_tests(self, change):
        path = change.path
        if path.endswith(".py"):
            if self.strategy in {"precision", "candidate"}:
                return self.precision_python_tests(change)
            return self.python_tests(change)
        readers = self.coverage.readers.get(path, set())
        owners = self.static_tests("fixtures", path)
        if self.strategy in {"precision", "candidate"}:
            if self.strategy == "candidate":
                collecting = self.catalog.resolve(self.collection.get("resources", {}).get(path, ()))
                if collecting:
                    return owners | readers | collecting | self.coverage.untraced
            if owners or readers:
                return owners | readers
            return self.static_tests("files", path) or self.all
        if self.strategy == "coverage" and readers:
            return readers | self.coverage.untraced
        if self.strategy == "static" or not self.coverage.available:
            return owners or self.static_tests("files", path) or self.all
        if owners or readers:
            # Coverage can be partial. The independent fixture index also sees
            # missing tests when it can resolve their opens from Python source.
            return owners | readers | self.coverage.untraced
        return self.static_tests("files", path) or self.all

    def select(self, patch):
        if self.strategy == "all":
            return self.all
        try:
            changes = parse_patch(patch.read_text(encoding="utf-8"))
            selected = set()
            for change in changes:
                selected.update(self.file_tests(change))
                if selected == self.all:
                    break
            return selected
        except (UnicodeError, UnsupportedPatch) as exc:
            print(f"{patch.name}: selecting all tests ({exc})", file=sys.stderr)
            return self.all


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--strategy", choices=SUPPORTED_STRATEGIES, default="hybrid")
    args = parser.parse_args()
    tests = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]
    selector = Selector(tests, args.strategy)
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    for patch in sorted((args.data_dir / "commits").glob("*.patch")):
        workers = schedule(selector.select(patch), durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
