# Independent mutation validation

## Phase 3 corpus

The fourteen edits in `tools/phase3_cases.py` were fixed against clean sqlglot
before running pytest. Eight discovery cases probe collection/imports, defaults,
new branches, dispatch, registry lookup, handlers, class attributes and source
introspection. Six separate holdout cases probe SQL fixtures, a predicate, a
module initializer, a keyword-only signature, a fixture reader and a dialect
generator. These strata sample known risk areas and are not a random sample of
future changes.

The **Independent mutation corpus** workflow checks out the pinned sqlglot SHA
and copies the clean source into a separate temporary directory per case. It
tests all files in the numbered catalog using the starter Docker image,
`requirements.lock` and `SKIP_INTEGRATION=0`. Collection continues after import
errors. A label set is usable only if every numbered test finished or its file
failed to import, the reporter recorded the session exit, and no numbered tests
were silently skipped. The independent clean control must pass. Artifacts keep
the exact diff, pytest log, raw reporter events and numbered failures.

To generate a single observation locally in the pinned Python environment:

```sh
python tools/phase3_lab.py measure baseline /path/to/pinned-sqlglot .bench/labels/baseline
python tools/phase3_lab.py measure new-branch /path/to/pinned-sqlglot .bench/labels/new-branch
```

The **Replay independent mutations** workflow downloads the frozen development
artifacts, builds the actual submission image and passes all fourteen patches
as one batch under the official runtime flags. The supplied `score.py` computes
costs including the 300-second penalty once per missed mutation. The job
publishes separate discovery, holdout and combined totals. Its JSON report
names every omitted failing test. Rerun with the original evidence run ID, or
generate fresh observations using **Independent mutation corpus** and pass that
run ID to **Replay independent mutations**. Artifacts expire according to the
repository retention setting. The independent labels are never copied into the
submission image.

The [initial Linux run](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45019915124)
finished all 1,620 numbered tests for every mutation and for the clean control.
Eleven edits caused a total of 189 failing-test observations, and three edits
caused no observed failure. The original strategies produced these totals:

| Strategy | Missed mutations | Missed failing-test observations | Schedule seconds | Penalty seconds | Penalized seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Hybrid | 0/14 | 0 | 366.412 | 0 | 366.412 |
| Coverage | 2/14 | 130 | 267.232 | 600 | 867.232 |
| Precision | 1/14 | 6 | 343.383 | 300 | 643.383 |

Precision omitted all six failures caused by a class configuration value shared
across dialects. Coverage also missed many tests after a lazy dialect import
broke. These are mutation results, not sample or leaderboard scores.

## Generic AST mutation generator

`tools/mutation_validate.py` evaluates selectors on changes generated from the
pinned sqlglot source instead of from the public sample. It is a development
experiment and is not copied into the submission image.

The default run creates one mutation in each of eleven categories: arithmetic,
comparison, boolean conditions, removed branches, return values, call
arguments, imports, class-level behavior, exception handlers, dialect
constants, and fixture contents. Candidate locations are ranked by old-revision
coverage only to make an observable mutation more likely. Coverage never
decides the actual outcome: a fresh detached worktree is tested with pytest.

```sh
python tools/mutation_validate.py --list-only
python tools/mutation_validate.py \
  --strategy hybrid --strategy coverage --strategy precision
```

The first command clones and verifies sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa` when the checkout is absent, then
writes `.bench/mutations/manifest.json`. Add `--offline` to prohibit cloning.
The full command first runs the clean baseline and then every numbered test file
for every mutation. It writes patches, complete pytest logs, and an incremental
`.bench/mutations/report.json`. `--category`, `--per-category`, and
`--pytest-target` support smaller diagnostic runs; restricted pytest targets
are not evidence about the full numbered suite.

The pytest plugin records exact failing node IDs, including setup and collection
failures. Baseline failures are subtracted. Mutation-caused collection errors
are retained but marked as incomplete test execution; all collection failures
are evidence of impact, while later runtime failures are necessarily unknown.
A run with an unresolved node ID or an unrelated pytest infrastructure exit is
marked unusable instead of being counted as a selector success. The report records failing test numbers, selection
counts, selected runtime, scheduled simulated cost, selector CPU time, and
missed tests for every advertised strategy.

Important limitations:

- A mutation that produces no numbered-test failure tests selection cost but
  supplies no evidence about recall.
- The operators are intentionally simple. They probe representative failure
  modes; they are not a model of the final hidden commit distribution.
- Changing an import can cause a collection failure and therefore represents a
  high-impact structural change. It should not dominate the mutation corpus.
- Host dependency or platform differences can create baseline failures. The
  report preserves them, but a clean pinned environment is preferred.
- One mutation per category is too small for a reliability claim. Increase
  `--per-category` and retain generated reports for reproducibility.
