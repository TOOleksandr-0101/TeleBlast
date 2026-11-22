"""Trace cold pytest collection in a fresh interpreter for each pinned test module."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def trace_one(root, module, target):
    import pytest
    os.chdir(root)
    sys.path.insert(0, str(root))
    lines = defaultdict(set)
    resources = set()

    @lru_cache(maxsize=8192)
    def relative(filename):
        if filename.startswith("<"):
            return ""
        path = Path(filename)
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else ""

    def record_line(code, lineno):
        name = relative(code.co_filename)
        if name.startswith(("sqlglot/", "tests/")):
            lines[name].add(lineno)
        return sys.monitoring.DISABLE

    def record_open(event, args):
        if event != "open":
            return
        filename, _, flags = args
        if not isinstance(filename, str) or flags & os.O_WRONLY:
            return
        name = relative(os.path.abspath(filename))
        if not name.startswith(("sqlglot/", "tests/")) or "__pycache__/" in name:
            return
        if name.endswith(".py") and sys._getframe(1).f_code.co_filename.startswith("<frozen importlib"):
            return
        resources.add(name)

    monitor = sys.monitoring
    monitor.use_tool_id(monitor.COVERAGE_ID, "teleblast-collection")
    monitor.register_callback(monitor.COVERAGE_ID, monitor.events.LINE, record_line)
    monitor.set_events(monitor.COVERAGE_ID, monitor.events.LINE)
    sys.addaudithook(record_open)
    status = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", module])
    monitor.set_events(monitor.COVERAGE_ID, 0)
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        raise SystemExit(int(status))
    target.write_text(json.dumps({
        "complete": status == pytest.ExitCode.OK,
        "lines": {p: sorted(nums) for p, nums in lines.items()},
        "resources": sorted(resources),
    }))


def build(root, output):
    from build_coverage import validate_checkout
    validate_checkout(root)
    modules = sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))
    complete, lines, resources = [], defaultdict(lambda: defaultdict(list)), defaultdict(list)
    with TemporaryDirectory() as tmp:
        def worker(pair):
            i, module = pair
            dest = Path(tmp) / f"{i}.json"
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(root),
                                   str(dest), "--module", module], timeout=120, capture_output=True,
                                  env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                       "PYTHONDONTWRITEBYTECODE": "1", "SKIP_INTEGRATION": "0"})
            if proc.returncode:
                raise RuntimeError(f"{module}: {(proc.stdout + proc.stderr)[-3000:]!r}")
            return module, json.loads(dest.read_text())
        with ThreadPoolExecutor(max_workers=4) as pool:
            for module, data in pool.map(worker, enumerate(modules)):
                if data["complete"]:
                    complete.append(module)
                for path, numbers in data["lines"].items():
                    for n in numbers:
                        lines[path][n].append(module)
                for path in data["resources"]:
                    resources[path].append(module)

    ranges = {}
    for path, entries in lines.items():
        rows = ranges[path] = []
        for n, owners in sorted(entries.items()):
            if rows and n == rows[-1][1] + 1 and owners == rows[-1][2]:
                rows[-1][1] = n
            else:
                rows.append([n, n, owners])
    data = {"revision": REVISION, "version": 1, "files": ranges,
            "resources": resources, "complete": complete}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
                                     compresslevel=9, mtime=0))
    print(f"{len(complete)}/{len(modules)} test files collected, {len(ranges)} executed source files, "
          f"{len(resources)} resources, {output.stat().st_size} bytes")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("checkout", type=Path)
    cli.add_argument("output", type=Path)
    cli.add_argument("--module")
    args = cli.parse_args()
    if args.module:
        trace_one(args.checkout.resolve(), args.module, args.output.resolve())
    else:
        build(args.checkout.resolve(), args.output.resolve())
