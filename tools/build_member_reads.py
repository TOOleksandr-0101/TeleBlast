"""Join source attribute reads to test and isolated collection coverage."""
import argparse
import ast
from bisect import bisect_left
from collections import defaultdict
import gzip
import json
from pathlib import Path
from zipfile import ZipFile

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def build(maps):
    with gzip.open(maps / "coverage.json.gz", "rt") as f:
        coverage = json.load(f)
    with gzip.open(maps / "collection.json.gz", "rt") as f:
        collection = json.load(f)
    if coverage["revision"] != REVISION or collection["revision"] != REVISION:
        raise ValueError("input maps must target the pinned revision")
    names = set()
    by_name = defaultdict(set)
    with ZipFile(maps / "sources.zip") as sources:
        for path in sources.namelist():
            if not path.startswith("sqlglot/"):
                continue
            tree = ast.parse(sources.read(path))
            for owner in ast.walk(tree):
                if isinstance(owner, ast.ClassDef):
                    for stmt in owner.body:
                        if isinstance(stmt, (ast.AnnAssign, ast.Assign)):
                            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                            names.update(t.id for t in targets if isinstance(t, ast.Name))
        for path in sources.namelist():
            if not path.endswith(".py"):
                continue
            rows = coverage["lines"].get(path, [])
            ends = [row[1] for row in rows]
            collected = collection["files"].get(path, [])
            collect_ends = [row[1] for row in collected]
            for expr in ast.walk(ast.parse(sources.read(path))):
                if isinstance(expr, ast.Attribute) and isinstance(expr.ctx, ast.Load):
                    name = expr.attr
                elif (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                      and expr.func.id in {"getattr", "hasattr"} and len(expr.args) >= 2
                      and isinstance(expr.args[1], ast.Constant)):
                    name = expr.args[1].value
                else:
                    continue
                if name not in names:
                    continue
                lo, hi = expr.lineno, expr.end_lineno
                index = bisect_left(ends, lo)
                while index < len(rows) and rows[index][0] <= hi:
                    by_name[name].update(coverage["nodes"][i] for i in coverage["groups"][rows[index][2]])
                    index += 1
                index = bisect_left(collect_ends, lo)
                while index < len(collected) and collected[index][0] <= hi:
                    by_name[name].update(collected[index][2])
                    index += 1
    nodes = sorted(set().union(*by_name.values()))
    index = {name: i for i, name in enumerate(nodes)}
    groups = {}
    uses = {}
    for name, readers in sorted(by_name.items()):
        group = tuple(sorted(index[r] for r in readers))
        uses[name] = groups.setdefault(group, len(groups))
    data = {"revision": REVISION, "nodes": nodes, "groups": list(groups), "uses": uses}
    dest = maps / "member_reads.json.gz"
    dest.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(by_name)} class member names with observed readers, {dest.stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("maps", type=Path)
    build(parser.parse_args().maps)
