"""Record operand relations for simple comparisons on the clean pinned revision.

Only exact builtin types are inspected. No patches, alternate operators,
test failure labels or sample files are input to this profiler.
"""
import argparse
import ast
import builtins
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
from importlib.machinery import PathFinder, SourceFileLoader
import json
import math
import multiprocessing.util
import operator
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest

from build_coverage import Attribution, REVISION, indices, validate_checkout


OPS = {name: getattr(operator, fn) for name, fn in (
    ("Lt", "lt"), ("LtE", "le"), ("Gt", "gt"), ("GtE", "ge"),
    ("Eq", "eq"), ("NotEq", "ne"), ("Is", "is_"), ("IsNot", "is_not"))}
NUMERIC = {id(int), id(bool), id(float)}
ORDERED = {id(str), id(bytes)}
SIMPLE = NUMERIC | ORDERED | {id(type(None))}
HELPER = "__teleblast_observe_cmp__"


def relation(a, b):
    ta, tb = id(type(a)), id(type(b))
    if ta not in SIMPLE or tb not in SIMPLE:
        return "opaque"
    if ((ta == id(float) and math.isnan(a)) or (tb == id(float) and math.isnan(b))):
        return "opaque"
    if a is b:
        return "same"
    if (ta in NUMERIC and tb in NUMERIC) or (ta == tb and ta in ORDERED):
        return "equal" if a == b else ("less" if a < b else "greater")
    return "disjoint"


class Rewrite(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        op = type(node.ops[0]).__name__
        if len(node.ops) != 1 or op not in OPS:
            return node
        key = f"{self.path}:{node.lineno}:{node.col_offset}"
        call = ast.Call(ast.Name(HELPER, ast.Load()),
                        [ast.Constant(key), ast.Constant(op), node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)


class Loader(SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        data = self.get_data(path)
        tree = ast.parse(data, filename=path)
        tree = Rewrite(Path(path).relative_to(REC.root).as_posix()).visit(tree)
        return compile(ast.fix_missing_locations(tree), path, "exec", dont_inherit=True)


class Finder:
    @staticmethod
    def find_spec(fullname, path, target=None):
        if fullname.split(".", 1)[0] != "sqlglot":
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if (spec and isinstance(spec.loader, SourceFileLoader) and
                Path(spec.origin).is_relative_to(REC.root)):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Recorder:
    def __init__(self, root):
        self.root = root
        self.mask = 0
        self.batch = set()
        self.rows = defaultdict(dict)
        self.outside = set()
        self.untraced_children = 0

    def observe(self, key, op, a, b):
        if self.mask:
            self.batch.add((key, relation(a, b)))
        else:
            self.outside.add(key)
        return OPS[op](a, b)

    def context(self, mask):
        if self.mask != mask:
            for key, bucket in self.batch:
                row = self.rows[key]
                row[bucket] = row.get(bucket, 0) | self.mask
            self.batch.clear()
            self.mask = mask

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.untraced_children |= self.mask


REC = None
WORKER_PID = None


def install(root):
    global REC
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise RuntimeError("instrumentation must precede sqlglot import")
    REC = Recorder(Path(root))
    setattr(builtins, HELPER, REC.observe)
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(REC.audit)


# With multiprocessing spawn, Python executes this file in the new interpreter
# before unpickling the function and its sqlglot arguments.
if __name__ == "__mp_main__" and os.environ.get("TELEBLAST_PROFILE_ROOT"):
    install(os.environ["TELEBLAST_PROFILE_ROOT"])


def profile_job(root, spool, mask, fn, args, kwargs):
    global WORKER_PID
    if REC is None:
        install(root)
    pid = os.getpid()
    if WORKER_PID != pid:
        WORKER_PID = pid
        spool = Path(spool)
        (spool / f"{pid}.started").touch()
        def flush():
            REC.context(0)
            with gzip.open(spool / f"{pid}.done.gz", "wt") as out:
                json.dump([REC.rows, sorted(REC.outside), REC.untraced_children], out)
        multiprocessing.util.Finalize(None, flush, exitpriority=1)
    REC.context(mask)
    return fn(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    root = options.checkout.resolve()
    output = options.output.resolve()
    validate_checkout(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(TELEBLAST_PROFILE_ROOT=str(root), SKIP_INTEGRATION="0",
                      PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    install(root)
    plugin = Attribution(REC)
    submit = ProcessPoolExecutor.submit
    with TemporaryDirectory() as spool:
        def wrapped(pool, fn, /, *args, **kwargs):
            return submit(pool, profile_job, str(root), spool, REC.mask, fn, args, kwargs)
        ProcessPoolExecutor.submit = wrapped
        try:
            status = pytest.main(["tests", "-q", "--tb=line", "-p", "no:cacheprovider"],
                                 plugins=[plugin])
        finally:
            ProcessPoolExecutor.submit = submit
            REC.context(0)
        if status != pytest.ExitCode.OK or not plugin.nodes:
            raise SystemExit(f"instrumented base suite failed: {status}")
        for marker in Path(spool).glob("*.started"):
            with gzip.open(marker.with_suffix(".done.gz"), "rt") as f:
                rows, outside, incomplete = json.load(f)
            REC.untraced_children |= incomplete
            REC.outside.update(outside)
            for key, values in rows.items():
                dest = REC.rows[key]
                for bucket, mask in values.items():
                    dest[bucket] = dest.get(bucket, 0) | mask
    groups = {}
    # A relation observed outside a test can affect import-time or global
    # state. Leave the whole site to the ordinary selector in that case.
    sites = {key: {bucket: groups.setdefault(mask, len(groups))
                   for bucket, mask in sorted(values.items())}
             for key, values in sorted(REC.rows.items())
             if key not in REC.outside and set(values) != {"opaque"}}
    complete = [i for i, node in enumerate(plugin.nodes)
                if plugin.phases[node] == {"setup", "call", "teardown"}
                and node not in plugin.incomplete and not (REC.untraced_children >> i & 1)]
    payload = {"revision": REVISION, "version": 1, "nodes": plugin.nodes,
               "complete": complete, "groups": [indices(mask) for mask in groups], "sites": sites}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(),
                                     compresslevel=9, mtime=0))
    print(f"{len(sites)} comparison sites, {len(complete)} complete tests, {output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
