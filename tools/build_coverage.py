"""Collect test-specific line and file-read coverage from the clean pinned sqlglot checkout.

Run under CPython 3.13 with the starter test dependencies installed:
    python -X cpu_count=2 tools/build_coverage.py /path/to/sqlglot maps/coverage.json.gz
"""

import argparse
import functools
import gzip
import hashlib
import json
import multiprocessing.util
import os
from pathlib import Path
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from tempfile import NamedTemporaryFile, TemporaryDirectory

import pytest


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
INPUT_DIGEST = "607a7d709c02b6b22c42d7ae11733414b0c8129af3f360fba00e9c390387b8a2"


def validate_checkout(root):
    """Refuse patched inputs, including cases where an archive has no Git metadata."""
    paths = set((root / "sqlglot").rglob("*.py")) | set((root / "tests").rglob("*.py"))
    paths.update(p for p in (root / "tests/fixtures").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts)
    digest = hashlib.sha256()
    for p in sorted(paths, key=lambda path: path.as_posix()):
        data = p.read_bytes()
        if p.suffix != ".gz":
            data = data.replace(b"\r\n", b"\n")
        digest.update(p.relative_to(root).as_posix().encode() + b"\x00" + hashlib.sha256(data).digest())
    if digest.hexdigest() != INPUT_DIGEST:
        raise SystemExit(f"Source/tests differ from the pinned checkout {REVISION}.")


class Recorder:
    def __init__(self, root):
        self.root = Path(root)
        self.lines = defaultdict(dict)
        self.files = {}
        self.mask = 0
        self.untraced_children = 0
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID

    @functools.lru_cache(maxsize=8192)
    def relative(self, filename):
        if filename.startswith("<"):
            return ""
        path = Path(os.path.abspath(filename))
        if path.is_relative_to(self.root):
            return path.relative_to(self.root).as_posix()
        return ""

    def line(self, code, number):
        path = self.relative(code.co_filename)
        if self.mask and path.startswith(("sqlglot/", "tests/")):
            lines = self.lines[path]
            lines[number] = lines.get(number, 0) | self.mask
        # CPython disables this bytecode location until the active test changes.
        return self.monitor.DISABLE

    def audit(self, event, args):
        if not self.mask:
            return
        if event == "subprocess.Popen":
            self.untraced_children |= self.mask
        if event == "open":
            filename, mode, flags = args
            if not isinstance(filename, str) or flags & os.O_WRONLY:
                return
            path = self.relative(filename)
            if path.startswith(("sqlglot/", "tests/")) and "__pycache__/" not in path:
                self.files[path] = self.files.get(path, 0) | self.mask

    def start(self):
        self.monitor.use_tool_id(self.tool, "teleblast")
        self.monitor.register_callback(self.tool, self.monitor.events.LINE, self.line)
        self.monitor.set_events(self.tool, self.monitor.events.LINE)
        sys.addaudithook(self.audit)

    def context(self, mask):
        if mask != self.mask:
            self.mask = mask
            self.monitor.restart_events()

    def stop(self):
        self.mask = 0
        self.monitor.set_events(self.tool, 0)
        self.monitor.free_tool_id(self.tool)


_worker_recorder = None


def traced_job(root, spool, mask, func, args, kwargs):
    """Run the actual process-pool job, attributing the child interpreter to its caller."""
    global _worker_recorder
    if _worker_recorder is None:
        rec = _worker_recorder = Recorder(root)
        rec.start()

        def flush():
            rec.stop()
            with gzip.open(Path(spool) / f"{os.getpid()}.json.gz", "wt", encoding="utf-8") as f:
                json.dump([rec.lines, rec.files, rec.untraced_children], f)

        multiprocessing.util.Finalize(None, flush, exitpriority=1)
    _worker_recorder.context(mask)
    return func(*args, **kwargs)


class Attribution:
    def __init__(self, rec):
        self.rec = rec
        self.nodes = []
        self.index = {}
        self.module_masks = defaultdict(int)
        self.phases = defaultdict(set)
        self.incomplete = set()

    def pytest_collection_finish(self, session):
        self.nodes = sorted(item.nodeid for item in session.items)
        self.index = {node: i for i, node in enumerate(self.nodes)}
        for node, i in self.index.items():
            self.module_masks[node.split("::")[0]] |= 1 << i

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        # Module/class fixtures can be cached after the first test of their scope.
        self.rec.context(self.module_masks[item.nodeid.split("::")[0]])

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        self.rec.context(1 << self.index[item.nodeid])

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        self.rec.context(self.module_masks[item.nodeid.split("::")[0]])

    def pytest_runtest_logreport(self, report):
        if report.passed:
            self.phases[report.nodeid].add(report.when)
        else:
            self.incomplete.add(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid):
        self.rec.context(0)


def indices(mask):
    result = []
    while mask:
        low = mask & -mask
        result.append(low.bit_length() - 1)
        mask ^= low
    return result


def encode(rec, plugin):
    groups = {}
    ranges = {}
    for path, observations in sorted(rec.lines.items()):
        rows = ranges[path] = []
        for line, mask in sorted(observations.items()):
            group = groups.setdefault(mask, len(groups))
            if rows and rows[-1][1] + 1 == line and rows[-1][2] == group:
                rows[-1][1] = line
            else:
                rows.append([line, line, group])
    return {
        "version": 1,
        "revision": REVISION,
        "nodes": plugin.nodes,
        "complete": [i for i, node in enumerate(plugin.nodes)
                     if plugin.phases[node] == {"setup", "call", "teardown"}
                     and node not in plugin.incomplete and not (rec.untraced_children >> i & 1)],
        "groups": [indices(mask) for mask in groups],
        "lines": ranges,
        "fixtures": {p: indices(mask) for p, mask in sorted(rec.files.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    validate_checkout(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    rec = Recorder(root)
    plugin = Attribution(rec)
    with TemporaryDirectory() as spool:
        original_submit = ProcessPoolExecutor.submit
        def submit(pool, func, /, *args, **kwargs):
            return original_submit(pool, traced_job, str(root), spool, rec.mask, func, args, kwargs)

        ProcessPoolExecutor.submit = submit
        try:
            rec.start()
            status = pytest.main(["tests", "-q", "--tb=line", "-p", "no:cacheprovider",
                                  "--continue-on-collection-errors"], plugins=[plugin])
        finally:
            rec.stop()
            ProcessPoolExecutor.submit = original_submit
        if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) or not plugin.nodes:
            raise SystemExit(f"Collection failed: {status}")
        for shard in sorted(Path(spool).glob("*.json.gz")):
            with gzip.open(shard, "rt", encoding="utf-8") as f:
                lines, files, incomplete = json.load(f)
            rec.untraced_children |= incomplete
            for path, rows in lines.items():
                dest = rec.lines[path]
                for line, mask in rows.items():
                    dest[int(line)] = dest.get(int(line), 0) | mask
            for path, mask in files.items():
                rec.files[path] = rec.files.get(path, 0) | mask
    data = encode(rec, plugin)
    output.parent.mkdir(exist_ok=True, parents=True)
    packed = json.dumps(data, separators=(",", ":")).encode()
    # Preserve the previous map if collection or compression is interrupted.
    with NamedTemporaryFile(dir=output.parent, delete=False) as pending:
        pending_path = Path(pending.name)
    try:
        pending_path.write_bytes(gzip.compress(packed, compresslevel=9, mtime=0))
        pending_path.chmod(0o644)
        os.replace(pending_path, output)
    finally:
        pending_path.unlink(missing_ok=True)
    print(f"{len(data['complete'])}/{len(plugin.nodes)} complete tests, "
          f"{len(rec.lines)} traced files, {len(rec.files)} opened resources, "
          f"{output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
