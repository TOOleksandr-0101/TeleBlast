"""Predeclared operator-transfer suite. Anchors come from the pinned Python sources."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.phase3_cases import Case


CASES = [
    # enumerate() starts at zero. The predicates agree on the entire reachable domain.
    Case("enumerated-row", "transfer", "sqlglot/generator.py",
         "if i == 0 and column_names:", "if i <= 0 and column_names:"),
    Case("fractional-boundary", "transfer", "sqlglot/time.py",
         "if subsecond_digit_count > 3:", "if subsecond_digit_count >= 3:"),
    Case("lineage-arity", "transfer", "sqlglot/lineage.py",
         "if len(named_sources) == 1:", "if len(named_sources) >= 1:"),
    Case("diff-threshold", "transfer", "sqlglot/diff.py",
         "min(source_leaves_num, target_leaves_num) > 4",
         "min(source_leaves_num, target_leaves_num) >= 4"),
    # Two independently evaluated comparisons plus possible changes to later control flow.
    Case("timezone-arity-pair", "transfer", "sqlglot/dialects/dialect.py",
         "unit_based = len(args) >= 3\n        has_timezone = len(args) == 4",
         "unit_based = len(args) > 3\n        has_timezone = len(args) >= 4"),
    Case("alias-key", "transfer", "sqlglot/optimizer/scope.py",
         'column.parent.arg_key == "rename"', 'column.parent.arg_key != "rename"'),
    Case("interval-unit", "transfer", "sqlglot/generators/sqlite.py",
         'if unit == "MONTH":', 'if unit != "MONTH":'),
    # Additional harness control: tests importing this module must be accounted for
    # even if pytest cannot collect any of their methods.
    Case("module-import-check", "collection-control", "sqlglot/lineage.py",
         "from sqlglot.errors import SqlglotError",
         "from sqlglot.errors import MissingError as SqlglotError"),
]


if __name__ == "__main__":
    from phase3_lab import main

    main(CASES)
