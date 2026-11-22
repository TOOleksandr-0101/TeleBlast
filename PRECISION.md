# Precision strategy report

`precision` is an experimental selector. The default remains `hybrid` because
the independent mutation corpus is still too small to justify promotion.

## Over-selection diagnosis

`python benchmarks/diagnose.py` produces per-commit JSON and Markdown reports.
On the 30 sample patches, Hybrid selected 11,841 tests beyond Coverage-only.
Conservative file fallbacks contributed 11,670 of them (517.357 seconds of raw
test runtime); function-wide coverage contributed 141 and static symbols 30.
Fixture readers contributed no Hybrid-only selections. The largest fallback
regressions were class-body assignments and comments at definition boundaries.

## Algorithm

Precision parses the pinned and patched Python ASTs. It uses changed-statement
coverage for ordinary edits, expands a multi-line condition to the complete
predicate to account for short-circuit changes, and widens added branches,
returns, raises, loops and handlers to observed function callers. Signatures,
decorators, imports, class inheritance, module initialization and exception
handler structure retain the transitive dependency fallback. Class-body values
use exact import-time coverage, then class-symbol and direct-import fallbacks.
Fixture changes use observed readers plus the independent fixture index.

AST-equivalent source edits retain tests that explicitly read Python source or
inspect code positions. Coverage map version 2 distinguishes those reads from
ordinary import opens. Incompletely traced subprocess tests are retained when
the static dependency graph links them to a semantically changed Python module.

## Results

Host-equivalent sample scoring after rebuilding the maps:

| Strategy | Simulated seconds | Missed commits |
| --- | ---: | ---: |
| Hybrid | 491.903 | 0 |
| Coverage-only | 300.868 | 0 |
| Precision | 165.538 | 0 |

Precision emits 2,150 bytes for the sample. A local macOS three-run median used 0.615
CPU seconds; Linux/Docker timing could not be measured because Docker is not
installed on the development host. Selecting only the known failing sample
tests costs 94.351 seconds, so a 60–70 second sample score is below the scorer's
mathematical lower bound.

## Independent mutations

The pinned clean suite passed all 1,620 numbered tests. Two full-suite mutations
completed before the time-box ended:

- A constructor comparison mutation caused collection failures affecting 1,020
  numbered tests. Initial Precision missed the incomplete subprocess test; the
  dependency-linked incomplete-test rule fixed it (0 recorded misses).
- An exception-handler type mutation completed normally with 239 numbered test
  failures. Function coverage was insufficient, so handler-structure edits now
  use the transitive dependency closure (0 recorded misses after the fix).

These are useful failure-mode measurements, not evidence of general zero-miss
reliability. `tools/mutation_validate.py` generates fresh mutations in eleven
categories and records actual pytest outcomes separately from predictions.

## Known risks and next work

1. Run a substantially larger mutation corpus, especially new branches,
   defaults, dynamic dispatch, imports, dialect registries and fixture readers.
2. Attribute collection-time imports and subprocess code to individual tests,
   reducing the conservative incomplete-test and module-initialization closures.
3. Add expression-value profiles for comparisons and branch predicates; this
   can remove tests whose observed operands make an operator edit equivalent
   without using prohibited patch-answer lookup tables.
