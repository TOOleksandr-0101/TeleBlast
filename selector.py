"""Select sqlglot tests for independent patches against the pinned revision."""

import argparse
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
        self.catalog = TestCatalog(tests)
        self.all = self.catalog.numbers
        self.strategy = strategy
        self.static = read_map(maps, "static") if strategy != "all" else {}
        data = read_map(maps, "coverage") if strategy in ("coverage", "hybrid") else {}
        self.coverage = Coverage(data, self.catalog)
        self.source_zip = ZipFile(maps / "sources.zip") if strategy != "all" else None

    @lru_cache(maxsize=32)
    def source(self, path):
        try:
            return self.source_zip.read(path).decode("utf-8")
        except KeyError as exc:
            raise UnsupportedPatch("source missing from pinned revision") from exc

    def static_tests(self, field, key):
        return self.catalog.resolve(self.static.get(field, {}).get(key, ()))

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
            return self.python_tests(change)
        readers = self.coverage.readers.get(path, set())
        owners = self.static_tests("fixtures", path)
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
    parser.add_argument("--strategy", choices=("all", "static", "coverage", "hybrid"), default="hybrid")
    args = parser.parse_args()
    tests = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]
    selector = Selector(tests, args.strategy)
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    for patch in sorted((args.data_dir / "commits").glob("*.patch")):
        workers = schedule(selector.select(patch), durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
