"""Run the frozen independent corpus and replay strategies against pytest observations."""
import argparse
import base64
import difflib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zlib

from mutation_validate import ROOT, REVISION, _unique_test_files, predict, resolve_failure_records, run_pytest
from phase3_cases import CASES
from selector import SUPPORTED_STRATEGIES, TestCatalog


def emit(name, value):
    print(f"{name}={value}")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as output:
            print(f"{name}={value}", file=output)


def measure(name, checkout, output, tests, cases=CASES):
    case = next((case for case in cases if case.name == name), None)
    if case is None and name != "baseline":
        raise ValueError(name)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp) / "sqlglot"
        shutil.copytree(checkout, work, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "*.pyc"))
        if case:
            target = work / case.path
            before = target.read_text(encoding="utf-8")
            after = case.apply(before)
            if case.path.endswith(".py"):
                compile(after, case.path, "exec")
            target.write_text(after, encoding="utf-8")
            diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        "a/" + case.path, "b/" + case.path)
            (output / "change.patch").write_text("".join(diff), encoding="utf-8")
        result = run_pytest(work, _unique_test_files(tests), sys.executable,
                            output / "pytest.log", timeout_seconds=540)
    catalog = TestCatalog(tests)
    failures, unknown = resolve_failure_records(result.records, catalog)
    ended = catalog.resolve(r["nodeid"] for r in result.records if r["kind"] == "finished")
    skipped = catalog.resolve(r["nodeid"] for r in result.records if r["kind"] == "skip")
    collections = [r for r in result.records if r["kind"] == "collect"]
    collection_tests, _ = resolve_failure_records(collections, catalog)
    missing = catalog.numbers - ended - collection_tests
    completed_session = any(r["kind"] == "session" and r["exitstatus"] == result.exit_code
                            for r in result.records)
    valid = (completed_session and not missing and not (skipped - failures) and not unknown
             and result.exit_code in {0, 1} and (result.exit_code == 0) == (not failures))
    record = dict(name=name, group=case.group if case else "control", revision=REVISION,
                  valid=valid, pytest_exit=result.exit_code, seconds=round(result.wall_seconds, 3),
                  failing=sorted(failures), collected_errors=[r["nodeid"] for r in collections],
                  missing=sorted(missing), skipped=sorted(skipped), unresolved=unknown,
                  finished=len(ended), total=len(catalog.numbers))
    (output / "observed.json").write_text(json.dumps(record, indent=2) + "\n")
    # A compact, lossless copy of labels in the public jobs API permits replay.
    labels = base64.urlsafe_b64encode(zlib.compress(json.dumps(sorted(failures)).encode())).decode()
    emit("case_result", f"{name} valid={int(valid)} completed={len(ended)} "
         f"import_failed={len(collection_tests)} failing={len(failures)}")
    emit("labels", labels)
    if not valid or (name == "baseline" and failures):
        print(record)
        print(result.stdout_tail, file=sys.stderr)
        raise SystemExit("incomplete evidence or non-clean baseline")


def compare(artifacts, output, tests, cases=CASES):
    strategies = [s for s in ("hybrid", "coverage", "precision", "candidate")
                  if s in SUPPORTED_STRATEGIES]
    labels = {}
    for path in artifacts.rglob("observed.json"):
        record = json.loads(path.read_text())
        if not record["valid"] or record["name"] in labels or record["revision"] != REVISION:
            raise ValueError((path, record))
        labels[record["name"]] = (record, path.parent)
    if set(labels) != {"baseline", *(c.name for c in cases)} or labels["baseline"][0]["failing"]:
        raise ValueError("expected all frozen cases and clean baseline")
    result = []
    sums = {s: [0, 0, 0] for s in strategies}  # raw cost in ms, missed commits, missed tests
    lines = []
    for case in cases:
        obs, directory = labels[case.name]
        actual = set(obs["failing"])
        predictions = predict(directory / "change.patch", tests, strategies, actual, ROOT / "maps")
        case_steps = []
        for s, p in predictions.items():
            n = len(p["missed_test_numbers"])
            v = sums[s]
            v[0] += round(p["simulated_seconds"] * 1000)
            v[1] += bool(n)
            v[2] += n
            p["penalty_seconds"] = 300 * bool(n)
            p["penalized_seconds"] = round(p["simulated_seconds"] + p["penalty_seconds"], 3)
            case_steps.append(f"{s}={p['selected_count']}/{n}/{p['simulated_seconds']:.3f}")
        line = f"{case.name} {len(actual)} failures, " + " ".join(case_steps)
        emit(case.name.replace("-", "_"), line)
        lines.append(line)
        result.append({**obs, "predictions": predictions})
    totals = {s: dict(raw_seconds=ms / 1000, misses=miss, missed_tests=n,
                      penalty=300 * miss, total_seconds=ms / 1000 + 300 * miss)
              for s, (ms, miss, n) in sums.items()}
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(json.dumps({"baseline": labels["baseline"][0],
        "cases": result, "strategies": totals}, indent=2) + "\n")
    emit("summary", " ".join(f"{s}={v['misses']}miss/{v['missed_tests']}tests/{v['total_seconds']:.3f}s"
                             for s, v in totals.items()))
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as summary:
            print("Selected tests / missed tests / raw schedule seconds per case\n\n```",
                  *lines, "```", sep="\n", file=summary)


def main(cases=CASES):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure")
    m.add_argument("case")
    m.add_argument("checkout", type=Path)
    m.add_argument("output", type=Path)
    c = sub.add_parser("compare")
    c.add_argument("artifacts", type=Path)
    c.add_argument("output", type=Path)
    args = parser.parse_args()
    catalog = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    if args.command == "measure":
        measure(args.case, args.checkout, args.output, catalog, cases)
    else:
        compare(args.artifacts, args.output, catalog, cases)


if __name__ == "__main__":
    main()
