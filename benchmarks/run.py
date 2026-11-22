"""Run from the repository root on a Linux Docker host with GNU make."""
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import evaluate_run, run_total_microseconds

STDOUT_LIMIT = 2_000_000
RUNTIME_LIMIT_S = 180


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def target(name, strategy=None, **kwargs):
    args = ["make", "-s", "-f", "benchmarks/limits.mk", name]
    if strategy:
        args.append("STRATEGY=" + strategy)
    return run(["timeout", str(RUNTIME_LIMIT_S), *args], **kwargs)


def available_strategies():
    return json.loads(target("strategies", capture_output=True).stdout)


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


def measure(strategy, reference, tests, answers, build_s):
    if len(reference) > STDOUT_LIMIT:
        raise ValueError(f"{strategy or 'default'} stdout exceeds {STDOUT_LIMIT} bytes")
    evaluation = evaluate_run(reference, tests, answers)
    if not evaluation["ok"]:
        raise ValueError((strategy, evaluation))
    simulated_ms = evaluation["simulated_milliseconds"]
    repetitions = []
    for _ in range(3):
        result = target("measured-run", strategy, capture_output=True)
        if result.stdout != reference:
            raise ValueError(f"{strategy or 'default'} output changed between runs")
        lines = result.stderr.decode().splitlines()
        sample = json.loads(next(line.removeprefix("TELEBLAST_TIMING ")
                                 for line in lines if line.startswith("TELEBLAST_TIMING ")))
        sample["local_total_s"] = run_total_microseconds(
            simulated_ms, round(sample["selector_cpu_s"] * 1_000_000)) / 1_000_000
        repetitions.append(sample)
    cpu = statistics.median(r["selector_cpu_s"] for r in repetitions)
    wall = statistics.median(r["selector_wall_s"] for r in repetitions)
    return {"strategy": strategy or "image default", "simulated_s": simulated_ms / 1000,
            "misses": len(evaluation["missed_commits"]), "build_s": build_s,
            "runs": repetitions, "median_cpu_s": cpu, "median_wall_s": wall,
            "median_local_total_s": statistics.median(r["local_total_s"] for r in repetitions),
            "runtime_timeout_s": RUNTIME_LIMIT_S, "stdout_limit_bytes": STDOUT_LIMIT,
            "commits": len(answers), "stdout_bytes": len(reference),
            "stdout_sha256": hashlib.sha256(reference).hexdigest(), **scheduling_quality(reference)}


def publish(bench, key, data):
    filename = "report" if key == "result" else key
    (bench / (filename + ".json")).write_text(json.dumps(data, indent=2) + "\n")
    summary = (f"{data['strategy']} {data['simulated_s']:.3f}s simulated, {data['misses']} misses, "
               f"CPU {data['median_cpu_s']:.6f}s, wall {data['median_wall_s']:.6f}s, "
               f"total {data['median_local_total_s']:.6f}s, {data['stdout_bytes']} bytes, "
               f"build {data['build_s']:.3f}s")
    print(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            print(key + "=" + summary, file=f)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            print(f"### {summary}\n", file=f)
            print("| Run | Selector CPU (s) | Wall (s) | Local total (s) | Peak RSS (KiB) |", file=f)
            print("|---:|---:|---:|---:|---:|", file=f)
            for idx, r in enumerate(data["runs"], 1):
                print(f"| {idx} | {r['selector_cpu_s']:.6f} | {r['selector_wall_s']:.6f} | "
                      f"{r['local_total_s']:.6f} | {r['selector_peak_rss_kib']} |", file=f)
            print(file=f)


def main():
    bench = Path(".bench")
    bench.mkdir(exist_ok=True)
    start = time.monotonic()
    run(["timeout", "600", "make", "-s", "-f", "benchmarks/limits.mk", "cold-build"])
    build_s = round(time.monotonic() - start, 3)
    run(["timeout", str(RUNTIME_LIMIT_S), "make", "sample"])
    tests = json.loads(Path("sample/tests.json").read_text())["tests"]
    answers = json.loads(Path("sample/answers.json").read_text())["commits"]
    reference = Path("output.jsonl").read_bytes()
    publish(bench, "result", measure(None, reference, tests, answers, build_s))
    for strategy in available_strategies():
        if strategy in {"all", "static", "coverage", "hybrid"}:
            continue
        reference = target("strategy-run", strategy, capture_output=True).stdout
        publish(bench, strategy, measure(strategy, reference, tests, answers, build_s))


if __name__ == "__main__":
    main()
