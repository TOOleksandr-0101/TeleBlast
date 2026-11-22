"""Minimal pytest reporter used by ``tools/mutation_validate.py``.

The plugin deliberately uses only pytest's public hook surface and JSONL so it
can run in the pinned sqlglot checkout without adding another dependency.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


REPORT_ENV = "TELEBLAST_MUTATION_REPORT"


def _append(payload: dict[str, Any]) -> None:
    target = os.environ.get(REPORT_ENV)
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def pytest_configure(config: Any) -> None:
    target = os.environ.get(REPORT_ENV)
    if target:
        Path(target).write_text("", encoding="utf-8")


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        _append({"kind": "test", "nodeid": report.nodeid, "when": report.when})
    elif report.skipped:
        _append({"kind": "skip", "nodeid": report.nodeid, "when": report.when})


def pytest_collectreport(report: Any) -> None:
    if report.failed:
        _append({"kind": "collect", "nodeid": report.nodeid})


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    _append({"kind": "finished", "nodeid": nodeid})


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    _append({"kind": "session", "exitstatus": int(exitstatus)})
