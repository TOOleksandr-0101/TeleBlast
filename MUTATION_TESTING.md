# Independent mutation validation

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
