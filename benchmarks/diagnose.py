"""Attribute sample selection cost to the inputs used by the hybrid selector.

This is development-only tooling.  It deliberately reads the sample answers only
to report whether each strategy includes the known failing tests; selector.py and
the submitted image do not import it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import marshal
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from impact import UnsupportedPatch, parse_patch  # noqa: E402
from scheduler import WORKER_MS, schedule  # noqa: E402
from selector import Selector  # noqa: E402


COMPONENTS = (
    "changed_line_coverage",
    "function_wide_coverage",
    "static_symbol_dependencies",
    "fixture_readers",
    "conservative_fallbacks",
    "direct_test_file",
)

FALLBACK_SOURCES = (
    "transitive_file_dependencies",
    "whole_file_coverage",
    "untraced_tests",
    "full_suite",
)


def _schedule_cost(selected: set[int], durations: dict[int, int]) -> tuple[int, list[list[int]]]:
    workers = schedule(selected, durations)
    longest = max(
        (sum(durations[number] for number in worker) for worker in workers),
        default=0,
    )
    return longest + WORKER_MS * len(workers), workers


def _ranges(numbers: list[int]) -> str:
    """Return a compact, lossless representation of sorted integer IDs."""
    if not numbers:
        return "-"
    result = []
    first = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        result.append(str(first) if first == previous else f"{first}-{previous}")
        first = previous = number
    result.append(str(first) if first == previous else f"{first}-{previous}")
    return ",".join(result)


class HybridTrace:
    """Mirror hybrid selection while retaining the provenance of every test."""

    def __init__(self, selector: Selector):
        self.selector = selector

    @staticmethod
    def _sets(names):
        return {name: set() for name in names}

    def _python(self, change):
        selector = self.selector
        coverage = selector.coverage
        components = self._sets(COMPONENTS)
        fallback_sources = self._sets(FALLBACK_SOURCES)
        events = []
        path = change.path
        before = selector.source(path)
        after = change.updated_python(before)
        if before == after:
            return components, fallback_sources, events

        code = compile(after.encode("utf-8"), path, "exec", dont_inherit=True)
        dependent = selector.static_tests("files", path) | selector.catalog.resolve([path])
        readers = coverage.readers.get(path, set())
        components["fixture_readers"].update(readers)
        components["direct_test_file"].update(selector.catalog.resolve([path]))

        if hashlib.sha256(marshal.dumps(code)).digest() == selector.original_code_digest(path):
            components["conservative_fallbacks"].update(coverage.untraced)
            fallback_sources["untraced_tests"].update(coverage.untraced)
            events.append({"path": path, "reason": "equivalent_compiled_code"})
            return components, fallback_sources, events

        intervals = [interval for hunk in change.hunks for interval in hunk.changes()]
        structural = any(
            row[0] in "+-"
            and row[1:].lstrip().startswith(
                ("def ", "async def ", "class ", "@", "import ", "from ")
            )
            for hunk in change.hunks
            for row in hunk.body
        )
        for first, last in intervals:
            components["changed_line_coverage"].update(coverage.hits(path, first, last))
            owner = selector.function(path, first, last)
            if structural or not owner:
                full_file = coverage.hits(path, 1, sys.maxsize)
                fallback = dependent or selector.all
                components["conservative_fallbacks"].update(fallback)
                components["conservative_fallbacks"].update(coverage.untraced)
                components["conservative_fallbacks"].update(full_file)
                fallback_sources[
                    "transitive_file_dependencies" if dependent else "full_suite"
                ].update(fallback)
                fallback_sources["whole_file_coverage"].update(full_file)
                fallback_sources["untraced_tests"].update(coverage.untraced)
                events.append(
                    {
                        "path": path,
                        "reason": "structural_change" if structural else "no_function_owner",
                        "interval": [first, last],
                        "dependent_tests": len(dependent),
                        "whole_file_coverage_tests": len(full_file),
                    }
                )
                return components, fallback_sources, events

            scope = coverage.hits(path, owner[0], owner[1])
            symbols = selector.static_tests("symbols", owner[2])
            components["function_wide_coverage"].update(scope)
            components["static_symbol_dependencies"].update(symbols)
            events.append(
                {
                    "path": path,
                    "reason": "function_body",
                    "interval": [first, last],
                    "owner": owner[2],
                    "owner_span": [owner[0], owner[1]],
                    "function_coverage_tests": len(scope),
                    "symbol_tests": len(symbols),
                }
            )
            if not scope:
                fallback = dependent or selector.all
                components["conservative_fallbacks"].update(fallback)
                fallback_sources[
                    "transitive_file_dependencies" if dependent else "full_suite"
                ].update(fallback)

        components["conservative_fallbacks"].update(coverage.untraced)
        fallback_sources["untraced_tests"].update(coverage.untraced)
        return components, fallback_sources, events

    def _fixture(self, change):
        selector = self.selector
        path = change.path
        components = self._sets(COMPONENTS)
        fallback_sources = self._sets(FALLBACK_SOURCES)
        readers = selector.coverage.readers.get(path, set())
        owners = selector.static_tests("fixtures", path)
        components["fixture_readers"].update(readers)
        components["static_symbol_dependencies"].update(owners)
        if owners or readers:
            components["conservative_fallbacks"].update(selector.coverage.untraced)
            fallback_sources["untraced_tests"].update(selector.coverage.untraced)
            reason = "fixture_indexes"
        else:
            fallback = selector.static_tests("files", path) or selector.all
            components["conservative_fallbacks"].update(fallback)
            fallback_sources[
                "transitive_file_dependencies" if fallback != selector.all else "full_suite"
            ].update(fallback)
            reason = "unresolved_fixture"
        return components, fallback_sources, [{"path": path, "reason": reason}]

    def patch(self, patch: Path):
        components = self._sets(COMPONENTS)
        fallback_sources = self._sets(FALLBACK_SOURCES)
        events = []
        try:
            changes = parse_patch(patch.read_text(encoding="utf-8"))
            for change in changes:
                traced = self._python(change) if change.path.endswith(".py") else self._fixture(change)
                change_components, change_fallbacks, change_events = traced
                for name, values in change_components.items():
                    components[name].update(values)
                for name, values in change_fallbacks.items():
                    fallback_sources[name].update(values)
                events.extend(change_events)
        except (UnicodeError, UnsupportedPatch, SyntaxError, ValueError, OverflowError, RecursionError) as error:
            components["conservative_fallbacks"].update(self.selector.all)
            fallback_sources["full_suite"].update(self.selector.all)
            events.append({"path": None, "reason": "unsupported_patch", "detail": str(error)})
        return components, fallback_sources, events


def _selection_record(selected, durations, failing):
    cost, workers = _schedule_cost(selected, durations)
    return {
        "selected_count": len(selected),
        "selected_test_runtime_ms": sum(durations[number] for number in selected),
        "simulated_worker_cost_ms": cost,
        "worker_count": len(workers),
        "missed_failing_tests": sorted(set(failing) - selected),
    }


def _attribution_record(candidates, hybrid_only, durations, all_components=None):
    selected = candidates & hybrid_only
    record = {
        "candidate_count": len(candidates),
        "hybrid_only_count": len(selected),
        "hybrid_only_runtime_ms": sum(durations[number] for number in selected),
        "hybrid_only_tests": sorted(selected),
    }
    if all_components is not None:
        exclusive = {
            number
            for number in selected
            if sum(number in values for values in all_components.values()) == 1
        }
        record.update(
            {
                "exclusive_hybrid_only_count": len(exclusive),
                "exclusive_hybrid_only_runtime_ms": sum(
                    durations[number] for number in exclusive
                ),
                "exclusive_hybrid_only_tests": sorted(exclusive),
            }
        )
    return record


def diagnose(data_dir: Path):
    tests = json.loads((data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]
    answers_path = data_dir / "answers.json"
    answers = (
        json.loads(answers_path.read_text(encoding="utf-8"))["commits"]
        if answers_path.exists()
        else {}
    )
    durations = {
        test["number"]: math.floor(test["seconds"] * 1000 + 0.5) for test in tests
    }
    coverage_selector = Selector(tests, "coverage")
    hybrid_selector = Selector(tests, "hybrid")
    trace = HybridTrace(hybrid_selector)
    commits = []

    for patch in sorted((data_dir / "commits").glob("*.patch")):
        coverage = coverage_selector.select(patch)
        hybrid = hybrid_selector.select(patch)
        components, fallback_sources, events = trace.patch(patch)
        attributed = set().union(*components.values())
        if attributed != hybrid:
            raise AssertionError(
                f"trace drift for {patch.name}: "
                f"{len(attributed - hybrid)} extra, {len(hybrid - attributed)} missing"
            )
        hybrid_only = hybrid - coverage
        coverage_only = coverage - hybrid
        failing = answers.get(patch.stem, {}).get("failing", [])
        coverage_record = _selection_record(coverage, durations, failing)
        hybrid_record = _selection_record(hybrid, durations, failing)
        commits.append(
            {
                "commit": patch.stem,
                "events": events,
                "coverage": coverage_record,
                "hybrid": hybrid_record,
                "simulated_cost_delta_ms": (
                    hybrid_record["simulated_worker_cost_ms"]
                    - coverage_record["simulated_worker_cost_ms"]
                ),
                "hybrid_only": {
                    "count": len(hybrid_only),
                    "runtime_ms": sum(durations[number] for number in hybrid_only),
                    "tests": sorted(hybrid_only),
                },
                "coverage_only": {
                    "count": len(coverage_only),
                    "runtime_ms": sum(durations[number] for number in coverage_only),
                    "tests": sorted(coverage_only),
                },
                "components": {
                    name: _attribution_record(
                        components[name], hybrid_only, durations, components
                    )
                    for name in COMPONENTS
                },
                "fallback_sources": {
                    name: _attribution_record(
                        fallback_sources[name], hybrid_only, durations
                    )
                    for name in FALLBACK_SOURCES
                },
            }
        )

    return {
        "data_dir": str(data_dir),
        "commit_count": len(commits),
        "totals": {
            "coverage_simulated_worker_cost_ms": sum(
                row["coverage"]["simulated_worker_cost_ms"] for row in commits
            ),
            "hybrid_simulated_worker_cost_ms": sum(
                row["hybrid"]["simulated_worker_cost_ms"] for row in commits
            ),
            "hybrid_minus_coverage_ms": sum(
                row["simulated_cost_delta_ms"] for row in commits
            ),
            "hybrid_only_test_selections": sum(row["hybrid_only"]["count"] for row in commits),
            "hybrid_only_test_runtime_ms": sum(row["hybrid_only"]["runtime_ms"] for row in commits),
            "coverage_only_test_selections": sum(row["coverage_only"]["count"] for row in commits),
            "coverage_only_test_runtime_ms": sum(row["coverage_only"]["runtime_ms"] for row in commits),
        },
        "commits": commits,
    }


def render_markdown(report):
    lines = [
        "# Hybrid vs coverage diagnostic",
        "",
        (
            "Component columns show tests selected only by Hybrid that were attributed "
            "to that component. Components can overlap; the JSON report also records "
            "raw candidates, exclusive candidates, exact test IDs and fallback sources."
        ),
        "",
        "| Commit | Coverage count/runtime/cost (ms) | Hybrid count/runtime/cost (ms) | Cost delta (ms) | Hybrid-only tests | Function-wide | Static symbols | Fixture readers | Conservative fallback |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in report["commits"]:
        coverage = row["coverage"]
        hybrid = row["hybrid"]
        only = row["hybrid_only"]
        component = row["components"]
        lines.append(
            "| {commit} | {cc}/{cr}/{cs} | {hc}/{hr}/{hs} | {delta:+} | "
            "{only} | {function} | {static} | {readers} | {fallback} |".format(
                commit=row["commit"],
                cc=coverage["selected_count"],
                cr=coverage["selected_test_runtime_ms"],
                cs=coverage["simulated_worker_cost_ms"],
                hc=hybrid["selected_count"],
                hr=hybrid["selected_test_runtime_ms"],
                hs=hybrid["simulated_worker_cost_ms"],
                delta=row["simulated_cost_delta_ms"],
                only=_ranges(only["tests"]),
                function=component["function_wide_coverage"]["hybrid_only_count"],
                static=component["static_symbol_dependencies"]["hybrid_only_count"],
                readers=component["fixture_readers"]["hybrid_only_count"],
                fallback=component["conservative_fallbacks"]["hybrid_only_count"],
            )
        )

    totals = report["totals"]
    lines.extend(
        [
            "",
            "## Totals",
            "",
            f"- Coverage simulated worker cost: {totals['coverage_simulated_worker_cost_ms']} ms",
            f"- Hybrid simulated worker cost: {totals['hybrid_simulated_worker_cost_ms']} ms",
            f"- Hybrid minus coverage: {totals['hybrid_minus_coverage_ms']:+} ms",
            f"- Hybrid-only test selections/runtime: {totals['hybrid_only_test_selections']} / {totals['hybrid_only_test_runtime_ms']} ms",
            f"- Coverage-only test selections/runtime: {totals['coverage_only_test_selections']} / {totals['coverage_only_test_runtime_ms']} ms",
            "",
            "## Largest Hybrid cost increases",
            "",
        ]
    )
    ranked = sorted(
        report["commits"], key=lambda row: row["simulated_cost_delta_ms"], reverse=True
    )
    for row in ranked:
        if row["simulated_cost_delta_ms"] <= 0:
            break
        reasons = ", ".join(
            event["reason"] for event in row["events"] if event["reason"] != "function_body"
        ) or "function expansion"
        lines.append(
            f"- `{row['commit']}`: +{row['simulated_cost_delta_ms']} ms, "
            f"{row['hybrid_only']['count']} Hybrid-only tests ({reasons})."
        )
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sample")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".bench")
    args = parser.parse_args()
    report = diagnose(args.data_dir.resolve())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "selection-diagnostics.json"
    markdown_path = args.output_dir / "selection-diagnostics.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(markdown_path)


if __name__ == "__main__":
    main()
