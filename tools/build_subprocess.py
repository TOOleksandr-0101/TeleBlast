"""Re-run incompletely traced numbered tests, accounting for every new interpreter."""
import argparse
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from build_coverage import REVISION, validate_checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    validate_checkout(root)
    package = Path(__file__).resolve().parent.parent
    with gzip.open(package / "maps/coverage.json.gz", "rt") as f:
        base = json.load(f)
    catalog = {t["id"] for t in json.loads((package / "sample/tests.json").read_text())["tests"]}
    pending = catalog - {base["nodes"][i] for i in base["complete"]}
    complete, paths, counts = [], defaultdict(list), {}
    for node in sorted(pending):
        with TemporaryDirectory() as tmp:
            env = dict(os.environ, TELEBLAST_CHILD_SPOOL=tmp, TELEBLAST_TRACE_ROOT=str(root),
                       PYTHONPATH=os.pathsep.join([str(package / "tools/subprocess_trace"), str(root)]),
                       PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
                       PYTHONPYCACHEPREFIX=str(Path(tmp) / "empty-cache"),
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", OPENBLAS_NUM_THREADS="1")
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                               env=env, cwd=root, timeout=180)
            started = {p.stem for p in Path(tmp).glob("*.started")}
            done = {p.stem: json.loads(p.read_text()) for p in Path(tmp).glob("*.done")}
            n = sum(len(v["children"]) for v in done.values())
            counts[node] = {"starts": len(started), "exits": len(done), "children": n,
                            "opaque": sorted({e for v in done.values() for e in v["opaque"]})}
            if (r.returncode or started != set(done) or len(done) != n + 1
                    or counts[node]["opaque"]):
                continue
            complete.append(node)
            for path in sorted({p for v in done.values() for p in v["paths"]}):
                paths[path].append(node)
    result = {"revision": REVISION, "version": 1, "paths": paths, "complete": complete,
              "processes": counts}
    args.output.write_bytes(gzip.compress(json.dumps(result, sort_keys=True,
                                           separators=(",", ":")).encode(), mtime=0))
    print(f"{len(complete)}/{len(pending)} additional tests, {len(paths)} paths; {counts}")


if __name__ == "__main__":
    main()
