"""Replay frozen patches inside the actual submission image and use the official scorer."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run
from tools.phase3_cases import CASES


def load_evidence(folder, cases=CASES):
    names = {case.name for case in cases} | {"baseline"}
    received = {}
    for path in folder.rglob("observed.json"):
        row = json.loads(path.read_text())
        name = row["name"]
        if name not in names or name in received or not row["valid"]:
            raise ValueError(f"unexpected or unusable labels: {path}")
        received[name] = (row, path.parent)
    if set(received) != names or received["baseline"][0]["failing"]:
        raise ValueError("incomplete frozen corpus or dirty control")
    return received


def summarize(lines, tests, labels, details):
    raw = b"".join(lines)
    ev = evaluate_run(raw, tests, labels)
    if not ev["ok"]:
        raise ValueError(ev)
    misses = len(ev["missed_commits"])
    missed_tests = sum(len(details[name]["missed_nodes"]) for name in labels)
    total = ev["simulated_milliseconds"] / 1000
    return {"cases": len(labels), "failure_bearing": sum(bool(v["failing"]) for v in labels.values()),
            "missed_cases": misses, "missed_tests": missed_tests, "penalty": 300 * misses,
            "raw_seconds": round(total - 300 * misses, 3), "penalized_seconds": total}


def evaluate(strategy, data, tests, labels, cases=CASES):
    args = ["make", "-s", "-f", "benchmarks/independent.mk"]
    if strategy == "default":
        args.append("independent-default")
    else:
        args += ["strategy-run", "STRATEGY=" + strategy]
    proc = subprocess.run(args + ["SAMPLE=" + str(data)], stdout=subprocess.PIPE,
                          cwd=ROOT, timeout=180, check=True)
    if len(proc.stdout) > 2_000_000:
        raise ValueError("oversized stdout")
    ev = evaluate_run(proc.stdout, tests, labels)
    if not ev["ok"]:
        raise ValueError((strategy, ev))
    by_number = {t["number"]: t["id"] for t in tests}
    lines, details = {}, {}
    for line in proc.stdout.splitlines(keepends=True):
        row = json.loads(line)
        name = row["commit"]
        selected = {n for worker in row["workers"] for n in worker}
        actual = set(labels[name]["failing"])
        cost = evaluate_run(line, tests, {name: labels[name]})["simulated_milliseconds"] / 1000
        lines[name] = line
        details[name] = {"selected": len(selected), "failing": len(actual),
                         "missed_nodes": [by_number[n] for n in sorted(actual - selected)],
                         "penalized_seconds": cost}
    groups = {"all": list(labels)}
    for group in dict.fromkeys(c.group for c in cases):
        groups[group] = [c.name for c in cases if c.group == group]
    scores = {g: summarize([lines[n] for n in names], tests,
                           {n: labels[n] for n in names}, details) for g, names in groups.items()}
    return {"scores": scores, "detail": details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--corpus", choices=("phase3", "transfer"), default="phase3")
    args = parser.parse_args()
    cases = CASES
    if args.corpus == "transfer":
        from tools.transfer_cases import CASES as transfer_cases
        cases = transfer_cases
    rows = load_evidence(args.evidence, cases)
    supported = json.loads(subprocess.check_output(
        ["make", "-s", "-f", "benchmarks/limits.mk", "strategies"], cwd=ROOT))
    strategies = ["default", *(s for s in ("hybrid", "coverage", "precision", "candidate")
                               if s in supported)]
    with TemporaryDirectory() as tmp:
        data = Path(tmp).resolve()
        (data / "commits").mkdir()
        shutil.copyfile(ROOT / "sample/tests.json", data / "tests.json")
        tests = json.loads((data / "tests.json").read_text())["tests"]
        labels = {}
        for case in cases:
            obs, directory = rows[case.name]
            shutil.copyfile(directory / "change.patch", data / "commits" / (case.name + ".patch"))
            labels[case.name] = {"failing": obs["failing"]}
        result = {s: evaluate(s, data, tests, labels, cases) for s in strategies}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    outputs = {}
    for group in ("all", *dict.fromkeys(case.group for case in cases)):
        values = []
        for name, row in result.items():
            s = row["scores"][group]
            values.append(f"{name} {s['missed_cases']}miss/{s['missed_tests']}tests/{s['penalized_seconds']:.3f}s")
        text = group + ": " + ", ".join(values)
        outputs[group.replace("-", "_")] = text
    if args.corpus == "transfer":
        for case in cases:
            values = []
            for strategy, row in result.items():
                d = row["detail"][case.name]
                values.append(f"{strategy}={d['selected']}/{len(d['missed_nodes'])}")
            outputs[case.name.replace("-", "_")] = (
                f"{case.name} {len(labels[case.name]['failing'])} failing, " + " ".join(values))
    print("\n".join(outputs.values()))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as f:
            for key, text in outputs.items():
                print(f"{key}={text}", file=f)


if __name__ == "__main__":
    main()
