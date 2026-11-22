"""Verify the committed source tree and the actual submitted application's contents."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run import target
from tools.pack_sources import REVISION, SOURCE_DIGEST


def checked_tree():
    entries = subprocess.check_output(["git", "ls-tree", "--full-tree", "-rlz", "HEAD"])
    sizes = {}
    for row in entries.split(b"\0"):
        if row:
            info, path = row.split(b"\t", 1)
            _, kind, _, size = info.split()
            if kind != b"blob":
                raise ValueError(f"submission cannot depend on submodules: {path!r}")
            sizes[path.decode()] = int(size)
    total = sum(sizes.values())
    if "Dockerfile" not in sizes or total > 20_000_000:
        raise ValueError(f"submission tree missing root Dockerfile or exceeds 20 MB: {total}")
    return sizes


def verify_app(app_files, tracked):
    required = {"selector.py", "scheduler.py", "impact.py", "maps/static.json.gz",
                "maps/coverage.json.gz", "maps/sources.zip"}
    if not required.issubset(app_files):
        raise ValueError(f"missing required files: {required - app_files.keys()}")
    for name, sha256 in app_files.items():
        path = Path(name)
        is_map = path.parent == Path("maps") and path.name.endswith(".json.gz")
        if name not in tracked or (name not in required and not is_map):
            raise ValueError(f"unexpected file in /app: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise ValueError(f"image differs from checkout: {name}")
        if is_map:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                if json.load(stream).get("revision") != REVISION:
                    raise ValueError(f"wrong sqlglot revision: {name}")
    digest = hashlib.sha256()
    with ZipFile("maps/sources.zip") as archive:
        for name in sorted(archive.namelist()):
            digest.update(name.encode() + b"\0" + hashlib.sha256(archive.read(name)).digest())
    if digest.hexdigest() != SOURCE_DIGEST:
        raise ValueError("pinned sources and LICENSE do not match their recorded digest")


def main():
    tracked = checked_tree()
    image = os.environ.get("IMAGE", "blast-radius-entry")
    spec, = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))
    config = spec["Config"]
    command = (config.get("Entrypoint") or []) + (config["Cmd"] or [])
    if (spec["Os"], spec["Architecture"], command) != ("linux", "amd64", ["python", "/app/selector.py"]):
        raise ValueError("image must run the benchmarked submission command on linux/amd64")
    app_files = json.loads(target("image-manifest", capture_output=True).stdout)
    verify_app(app_files, tracked)
    total = sum(tracked.values())
    result = {"tracked_tree_bytes": total, "git_files": len(tracked), "command": command,
              "platform": "linux/amd64", "app_files_sha256": app_files, "sqlglot_revision": REVISION}
    output = Path(".bench/readiness.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    summary = (f"Ready checks: {total} tracked bytes, {len(app_files)} application files, "
               f"linux/amd64, pinned {REVISION[:12]}")
    print(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as target_file:
            print("result=" + summary, file=target_file)


if __name__ == "__main__":
    main()
