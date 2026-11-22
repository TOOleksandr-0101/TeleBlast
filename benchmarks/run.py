"""Run from the repository root on a Linux Docker host with GNU make."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def scheduling_quality(plans):
    tests = json.loads(Path("sample/tests.json").read_text())["tests"]
    ms = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    gaps = []
    for line in plans.splitlines():
        groups = [g for g in json.loads(line)["workers"] if g]
        if not groups:
            gaps.append(0)
            continue
        loads = [sum(ms[j] for j in group) for group in groups]
        longest = max(ms[j] for group in groups for j in group)
        total = sum(loads)
        lower = min(2000 * k + max(longest, (total + k - 1) // k)
                    for k in range(1, min(8, sum(map(len, groups))) + 1))
        gaps.append(max(loads) + 2000 * len(loads) - lower)
    return {"commits_at_lower_bound": gaps.count(0),
            "max_gap_to_lower_bound_ms": max(gaps, default=0)}


def main():
    bench = Path(".bench")
    bench.mkdir(exist_ok=True)
    start = time.monotonic()
    run(["timeout", "600", "make", "build"])
    build_s = round(time.monotonic() - start, 3)
    run(["timeout", "180", "make", "sample"])
    official = Path("output.jsonl").read_bytes()
    if len(official) > 2_000_000:
        raise ValueError("stdout exceeds 2 MB")
    digest = hashlib.sha256(official).hexdigest()
    report = run([sys.executable, "score.py", "sample", "sample/answers.json",
                  "output.jsonl"], capture_output=True, text=True).stdout
    simulated = float(re.search(r"Simulated:\s*([\d.]+)", report)[1])
    misses = int(re.search(r"Misses:\s*(\d+)", report)[1])
    repetitions = []
    for _ in range(3):
        result = run(["timeout", "180", "make", "-s", "-f", "benchmarks/limits.mk",
                      "measured-run"], capture_output=True)
        if result.stdout != official:
            raise ValueError("output changed between runs")
        lines = result.stderr.decode().splitlines()
        repetitions.append(json.loads(next(line.removeprefix("TELEBLAST_TIMING ")
                                           for line in lines if line.startswith("TELEBLAST_TIMING "))))
    cpu = statistics.median(r["selector_cpu_s"] for r in repetitions)
    wall = statistics.median(r["selector_wall_s"] for r in repetitions)
    data = {"simulated_s": simulated, "misses": misses, "build_s": build_s,
            "runs": repetitions, "median_local_total_s": simulated + cpu,
            "stdout_bytes": len(official), "stdout_sha256": digest,
            **scheduling_quality(official)}
    (bench / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    summary = (f"{simulated:.3f}s simulated, {misses} misses, CPU {cpu:.6f}s, "
               f"wall {wall:.6f}s, build {build_s:.3f}s")
    print(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            print("result=" + summary, file=f)


if __name__ == "__main__":
    main()
