# Phase 3 evaluation

## Submission decision

The Docker default is **`registry`**. On Linux/amd64 it scored **213.975 simulated seconds**, caught every failing test across the 30 sample patches and produced 15,737 bytes of JSONL. Before promotion, three identical outputs had median selector CPU of 1.159346 s and a measured total of **215.134346 s**. The cold build took 6.704 s. This reduces simulated cost by **56.5%** relative to the previous Hybrid default. The 100–120 s sample target remains open. [Pre-promotion Docker verification](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45022311907)

All five strategies were replayed on the same 30 independently labelled mutations. The table gives total scorer cost including the 300-second penalty per missed mutation. CPU is measured separately on the submission batch.

| Strategy | Missed mutations | Omitted failing tests | Penalties | Independent simulated total |
| --- | ---: | ---: | ---: | ---: |
| Hybrid | 0 | 0 | 0 s | 735.277 s |
| Coverage | 7 | 144 | 2,100 s | 2,567.849 s |
| Precision | 8 | 67 | 2,400 s | 2,881.429 s |
| Candidate | 0 | 0 | 0 s | 563.551 s |
| **Registry** | **0** | **0** | **0 s** | **532.967 s** |

The cheapest sample-only strategy, Precision, missed real faults across shared dialects, inherited settings and mapping entries. Registry had the lowest cost among the strategies with zero misses on both the public and independent sets. Four Docker reports cover the [original 14](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45022311813), [comparison changes and collection failure](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45022311812), [receiver controls](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45022311803) and [dictionary controls](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45022311996).

The registry map distinguishes the execution of a stored lambda from creation of the dictionary that holds it. Narrowing requires a matching code contract, positive observations and unchanged surrounding AST. Changing a scalar/tuple table value can also use observed class readers when the table keys and surrounding construction are unchanged. Changes to factories and tables consumed during initialization retain conservative fallbacks. Lambda attribution saved 25.414 simulated seconds on the sample. Dictionary-reader attribution narrowed a separate tuple mutation from 1,029 tests to three while catching its failing test.

The main remaining opportunity is to trace **which dictionary entries survive copying, inversion and factory construction**, then attribute reads of those derived tables to tests. Whole-table fallbacks still pull in long tests. Indirect native readers, reflective namespace access and changed import order remain generalization risks. The release is technically ready for submission. No official leaderboard run has been requested or scored here.

The incoming revision `657b6f0` produced working plans, but its CI stopped before the strategy comparison. A development test imported `tools.mutation_validate` from the submission image. Runtime regressions now run against the packaged modules, and development tests run separately against a read-only checkout using the same Python image. Both sets passed on the [first repaired run](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45019719786).

## Precision reproduced on Linux

The [independent three-run benchmark](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45019816187) confirmed the incoming Precision result on all 30 sample patches. It produced 165.538 simulated seconds, zero missed commits and 2,150 bytes of stdout. Median selector CPU was 1.397475 s, median wall time was 1.401145 s and the measured median total was 166.935475 s. The no-cache image build took 3.423 s. The three runs emitted identical bytes. Hybrid on the same runner used 491.903 simulated seconds and had no misses.

These totals add the selector's Linux process-tree CPU to the official scorer's simulated cost. Official ranking uses CPU from every process in the submission container and a separate hidden batch. These experiments have no leaderboard score.

## Ground truth and separate evaluation

The [primary corpus](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45019915124) uses 14 source edits fixed before labelling, partitioned into eight discovery and six holdout examples. Each was applied to a separate clean copy of the pinned upstream revision. A clean control and every mutant were run against every numbered test file with integration tests enabled. A pytest plugin records failures, subtest failures, setup errors and file collection failures. The report checks session exit, test accounting and unresolved node IDs before accepting a run. The scorer sees the observed failures only after the selector has emitted its plan. Artifacts contain the manifest, diffs, observed failures and logs.

All 1,620 numbered tests passed on the control. Eleven of the 14 mutations caused failures, with 189 failing (mutation, test) pairs. For the original strategies, the scorer recorded:

| Strategy | Missed mutations | Omitted failing tests | Penalty | Cost with penalty |
| --- | ---: | ---: | ---: | ---: |
| Hybrid | 0 | 0 | 0 s | 366.412 s |
| Coverage | 2 | 130 | 600 s | 867.232 s |
| Precision | 1 | 6 | 300 s | 643.383 s |

For example, changing a SQLite class attribute affected six tests across dialect, executor and shared SQL behavior. The original Precision selected none of the six. The statement assigning the attribute ran during initialization; subsequent tests read the resulting value elsewhere. A broken lazy import also failed 127 tests even though pytest collected the entire numbered suite. Line-only coverage omitted 124 of those failures.

Cold collection profiles now record source ranges and resource reads separately for each of the 58 test modules in a fresh interpreter. Member-reader profiles link class assignments to sites that later consume their values. The [first protected candidate](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45020355589) caught all 189 failures, including the six missed by Precision. Its cost on these 14 mutations was 366.959 s.

The [second independent corpus](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45020404382) includes seven fresh boundary, equality and compound-predicate changes, plus a separate import-error control. Five of the seven changes caused 56 failing-test observations. The control affected 53 tests, including 50 numbered tests whose modules could not be collected. All were accounted for by pytest and the scorer. Across both corpora there were **22 mutations, 17 failure-bearing cases, and 298 failing (mutation, test) pairs**. Tests of the same sqlglot subsystem can be correlated. Zero observed misses on this set cannot establish a hidden-commit miss rate.

Supplemental subprocess tracing accounted for the parent and the new Python interpreter used by the incompletely traced lazy-load test. Both exited normally and the trace recorded 63 paths. Source-only changes can now exclude a fully observed child that never read or executed the changed file. A missing exit record or an opaque child keeps the conservative fallback.

The four additional [inherited class-setting controls](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45021152687) changed a base dialect setting, a base generator flag, a Postgres override and a tokenizer override. Three changes caused 50 failing-test observations. On their [Docker replay](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45021536606), Hybrid caught all 50 at 147.268 s. Coverage omitted 3 tests on one mutation (379.232 s including its penalty), and Precision omitted all 50 on three mutations (917.465 s including penalties). The profiled candidate caught all 50 at 85.724 s. Two fields had positive receiver evidence and two exercised the conservative fallback.

The clean comparison and receiver runs record input relations and attribute access by existing test node ID. The selector checks that the proposed edit has the supported AST shape and preserves the other source semantics before using a profile. Mixed or opaque operand behavior, import-time observations and untraced tests remain protected. The [component comparison](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45021536423) measured 262.992 simulated seconds with the collection/member guards and 239.389 with comparison observations, at zero misses in both cases. Concrete scalar receivers helped on independent settings changes without changing the sample cost.

The [four dictionary controls](https://github.com/TOOleksandr-0101/TeleBlast/actions/runs/45021866693) changed a type mapping, a tuple element, a deferred callable and a parser factory. Their clean baseline passed. All four mutations caused failures (2, 1, 6 and 2), and the original Precision and Coverage strategies each missed all four. In total, **30 independent mutations** were tested across the four groups. Of those, **24 caused at least one failure**, producing **359 failing (mutation, test) pairs**. None of the independent Python edit anchors overlaps a changed line in the public patches. The 30 public sample patches are scored separately.

We also tested a short-circuit proof that required the same order of operand evaluations, including possible side effects. It removed zero selections on the 30 sample commits and stays in `tools/probe_short_circuit.py`. Computing the sample's oracle outside the submission yields **94.351 s**. On every commit the schedule of just the known failures meets the mathematical lower bound given by the longest job, average load and per-worker fee. The oracle requires labels unavailable to the selector.

## Reproduction

```sh
make sample
python3 benchmarks/run.py
python3 benchmarks/compare.py
python3 benchmarks/readiness.py
python3 -m unittest discover -s tests
python3 -m unittest discover -s dev_tests
```

Run the selector tests in Python 3.13. The benchmark and readiness commands require GNU make and Linux Docker. The benchmark applies the official runtime flags. For each timed run it inspects the CPU, memory and process quotas, read-only root, tmpfs size, user and network interfaces. It also validates the actual JSONL against the scorer and the output budget. The readiness check verifies the packaged source snapshot, map revisions, application file digests, default Docker command and the 20 MB repository budget.

The Actions workflows can regenerate clean-base profiles and mutation labels independently. Replaying previously generated labels avoids re-running pytest while comparing selector revisions. Sample answers, mutation implementations and labelling artifacts are outside the submitted image. Replay jobs take an optional `evidence_run` input. When GitHub's retained artifacts expire, dispatch the matching full-suite measurement workflow with an empty evidence-run input, then use the resulting run ID in the replay workflow. An absent or expired artifact cannot be counted as a passing validation.

The cold collection, operand, scalar receiver, registry and subprocess workflows reproduce their maps from the pinned checkout and requirements lock. To regenerate the static member-read index after changing base coverage or cold collection, run `python tools/build_member_reads.py maps`. The submission Docker build copies the verified maps directly. Its time and resource budget is independent of the one-time development profiling jobs.
