"""Compare measured clean-base profiles, holding the candidate algorithm fixed."""
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scheduler import schedule
from score import evaluate_run
from selector import Selector


def emit_plans(tests, durations, patches, maps):
    selector = Selector(tests, "candidate", maps)
    try:
        rows = []
        for patch in patches:
            workers = schedule(selector.select(patch), durations)
            rows.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        return ("\n".join(rows) + "\n").encode()
    finally:
        selector.source_zip.close()


def main():
    data = ROOT / "sample"
    tests = json.loads((data / "tests.json").read_text())["tests"]
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + .5) for t in tests}
    answers = json.loads((data / "answers.json").read_text())["commits"]
    patches = sorted((data / "commits").glob("*.patch"))
    optional = {"observations.json.gz", "receivers.json.gz"}
    choices = {"guard": set(), "comparisons": {"observations.json.gz"},
               "receivers": {"receivers.json.gz"}, "both": optional}
    scores = {}
    for name, enabled in choices.items():
        if any(not (ROOT / "maps" / path).exists() for path in enabled):
            continue
        with TemporaryDirectory() as tmp:
            maps = Path(tmp)
            for path in (ROOT / "maps").iterdir():
                if path.name not in optional or path.name in enabled:
                    # Private file copies work on Windows as well as Linux.
                    (maps / path.name).write_bytes(path.read_bytes())
            output = emit_plans(tests, durations, patches, maps)
        score = evaluate_run(output, tests, answers)
        if not score["ok"]:
            raise ValueError(score)
        scores[name] = (score["simulated_milliseconds"] / 1000, len(score["missed_commits"]))
    print(", ".join(f"{name} {sec:.3f}s/{misses} misses" for name, (sec, misses) in scores.items()))


if __name__ == "__main__":
    main()
