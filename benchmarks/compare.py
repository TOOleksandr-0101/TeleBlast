"""Compare selection strategies on identical Docker inputs and recorded test times."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import evaluate_run


def main():
    tests = json.loads(Path("sample/tests.json").read_text())["tests"]
    answers = json.loads(Path("sample/answers.json").read_text())["commits"]
    records = {}
    for strategy in ("all", "static", "coverage", "hybrid"):
        proc = subprocess.run(["make", "-s", "-f", "benchmarks/limits.mk",
                               "strategy-run", "STRATEGY=" + strategy],
                              stdout=subprocess.PIPE, check=True, timeout=180)
        result = evaluate_run(proc.stdout, tests, answers)
        if not result["ok"] or len(proc.stdout) > 2_000_000:
            raise ValueError((strategy, result))
        records[strategy] = {"simulated_s": result["simulated_milliseconds"] / 1000,
                             "misses": len(result["missed_commits"])}
    bench = Path(".bench")
    bench.mkdir(exist_ok=True)
    (bench / "strategies.json").write_text(json.dumps(records, indent=2) + "\n")
    summary = ", ".join(f"{name} {r['simulated_s']:.3f}s/{r['misses']} misses"
                        for name, r in records.items())
    print(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            print("result=" + summary, file=f)


if __name__ == "__main__":
    main()
