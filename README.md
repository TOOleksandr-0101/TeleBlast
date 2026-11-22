# TeleBlast

TeleBlast chooses sqlglot tests for independent unified diffs against commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The default selector combines per-test Python line coverage, observed file reads, and a static dependency index. It emits one JSON line per patch and assigns every selected test to one worker. The numbered catalog and recorded test durations come from the input batch.

## Run

On Linux with Docker, GNU make and Python 3.9+ installed on the host:

```sh
make sample                          # Build linux/amd64, run all 30 patches, score output.jsonl
make check                           # Validate the JSONL contract and commit coverage
python3 benchmarks/run.py             # Build, make sample, then time three identical selections
python3 benchmarks/compare.py         # Score four strategies using the same image and limits
```

The benchmark writes machine-readable reports to `.bench/`. For local development with Python 3.13, run `python -m unittest discover -s tests`. Run the packaged selector with `python selector.py --data-dir sample > output.jsonl` and score it with `python score.py sample sample/answers.json output.jsonl`. These commands use the host environment.

The root Dockerfile copies three Python modules and the prebuilt maps into the pinned starter image. The build context excludes sample answers and patches. At runtime only the read-only `/data/tests.json` and `/data/commits/` mounts are required. The official Makefile sets 2 CPUs, 4 GB RAM, a 64 MB tmpfs, 256 processes, no network, a read-only root and uid 65534. The benchmark adds a 180-second timeout and a conservative 2,000,000-byte stdout check. The image writes diagnostics to stderr.

## Selection

1. `impact.py` parses file headers and hunk coordinates and identifies changed ranges in the old revision. An insertion also touches its neighboring old lines. Python hunks must match the bundled pinned source and produce compilable Python. An unsupported diff, a file creation or rename, or an invalid Python module selects the full suite.
2. `tools/build_index.py` parses the pinned source and test ASTs. Its reverse index links implementation files with importing test modules, directly and transitively. It records definition spans, name references in tests, literal dialect uses and fixture readers, including paths constructed by sqlglot's fixture helpers.
3. `tools/build_coverage.py` runs pytest against the **clean base revision** with integration tests enabled. It attributes executed lines to pytest node IDs, records fixture and Python source reads, attributes shared setup/teardown to the test module, and collects process-pool work. It stores shared test sets once and compresses consecutive line ranges. A failed, skipped or incompletely traced test can still be selected if it appears in the runtime catalog.
4. `selector.py` translates node IDs and test-file paths into the batch's test numbers. For a function-body edit, hybrid selection takes the union of relevant line and function coverage, static symbol references and source readers. Structural or module-level edits widen selection to known dependent tests. When the compiled module matches byte for byte, including source positions, the selector retains observed source readers and incompletely traced tests. Fixtures use both observed reads and static links. The results for all changed files are combined. Unknown dependencies fall back to the full suite.
5. `scheduler.py` works in integer milliseconds. It compares up to eight worker counts, accounting for the 2-second charge per occupied worker. It tries longest-test-first placement, skips counts whose lower bound cannot improve the price, and refines small sets with bounded partition search. Zero-duration tests stay in the plan. The report counts sample plans that reach a mathematical lower bound; for arbitrary inputs the algorithm is a heuristic.

The static index was built from 250 Python modules and contains 6,650 definition spans and 62 fixture paths. The bundled source archive includes sqlglot's MIT license. The selector uses maps built from the pinned revision. The official sample labels are used only by the scorer and benchmark harness.

## Rebuild the maps

The submitted image already contains the files in `maps/`. To regenerate them, use CPython 3.13 and the same pinned checkout. For comparable coverage use the pinned Linux image in the Dockerfile and install `requirements.lock` in an isolated build environment.

```sh
git clone https://github.com/tobymao/sqlglot.git ../teleblast-sqlglot
git -C ../teleblast-sqlglot checkout --detach eb11aec4be8cb2122afe713a4816662c3f0fb2fa
python -m pip install -r requirements.lock
python tools/build_index.py ../teleblast-sqlglot maps/static.json.gz
python tools/pack_sources.py ../teleblast-sqlglot
PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 PYTHON_CPU_COUNT=2 python -X cpu_count=2 tools/build_coverage.py ../teleblast-sqlglot maps/coverage.json.gz
```

The **Rebuild Linux coverage** workflow performs this tracing step in Docker, checks completeness, runs the sample scorer with the new map, and fast-forwards the verified map onto `main`. A concurrent change to `main` causes the ordinary push to fail cleanly. Full test tracing is a development step outside the timed submission build.

## Results

The benchmark starts with the supplied `make sample`. Three further runs use the same Docker flags and input mounts. A measurement wrapper launches the original selector, measures its user and system CPU with Linux `wait4`, and checks that all three outputs match the first byte for byte. Wall time measures the selector process including Python startup. The official leaderboard measures all container processes and ranks the median of three valid totals. Local simulated seconds are exact for each recorded output.

| Strategy | Simulated seconds, 30 patches | Missed commits |
| --- | ---: | ---: |
| Supplied starter, all tests on three workers | 1210.470 | 0 |
| All tests, optimized worker count | 1104.510 | 0 |
| Static dependencies and fixtures | 767.372 | 0 |
| Changed-line coverage and file reads | 300.868 | 0 |
| **Hybrid (submission default)** | **491.903** | **0** |

The hybrid default retains a function's other callers and transitive importers when a change could affect initialization. Exact-line coverage was faster on these 30 patches. New control flow can enter a line that none of the original tests reached, so this narrower mode remains an experiment. The hybrid selector's plans attain the scheduling lower bound on **30/30** commits and occupy **65,414 bytes** of stdout.

The committed Linux map covers 234 executed Python files. Of the 1,620 numbered tests, 1,619 have complete traces. The remaining lazy-dialect-loading test starts its own interpreter and the default keeps it when coverage is used.

## Handoff and next experiments

The sample contains 30 independent patches. A clean sample run establishes correctness on those inputs. Changed import-time behavior, dynamic dispatch into unobserved code and readers running outside Python can still broaden the impact on other patches. Selection and scheduling can be changed independently. Keep the numeric test IDs supplied by the input catalog and the exact two JSON keys in the output. Update map readers and generators together when changing their schema. Keep the scorer and fixture labels outside the submitted image.

Priorities for the next iteration:

1. Validate changes on independent, generated mutations of the clean revision. Measure the 300-second miss penalty as well as savings before tightening selection. Producing fresh pytest labels costs development time.
2. Attribute collection-time imports and class initialization to their consuming test modules. This can narrow the broadest fallback, at the cost of more complex handling of cached imports.
3. Refine function-wide coverage using basic blocks when unchanged control flow can be established. This should help large parsers and generators. Retain the enclosing-function fallback for ambiguous insertions and changed branches.
4. Trace native and external file readers where Python audit events cannot observe them. Better coverage can reduce the pool of tests retained on every change. Instrumenting subprocesses increases collection time and depends on the platform.
5. Compare faster map encodings and lazy decompression on final-sized batches. This targets charged selector CPU. Retain the current format unless the CPU savings justify the added machinery.
