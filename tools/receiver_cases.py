"""Frozen scalar-setting mutations from clean sqlglot, independent of sample patches."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.phase3_cases import Case


CASES = [
    Case("base-dialect-index", "receiver", "sqlglot/dialects/dialect.py",
         "    INDEX_OFFSET = 0", "    INDEX_OFFSET = 1"),
    Case("base-generator-values", "receiver", "sqlglot/generator.py",
         "    WRAP_DERIVED_VALUES = True", "    WRAP_DERIVED_VALUES = False"),
    Case("postgres-placeholder", "receiver", "sqlglot/generators/postgres.py",
         '    PARAMETER_TOKEN = "$"', '    PARAMETER_TOKEN = "?"'),
    Case("bigquery-tokenizer", "receiver", "sqlglot/dialects/bigquery.py",
         "        NESTED_COMMENTS = False", "        NESTED_COMMENTS = True"),
]


if __name__ == "__main__":
    from phase3_lab import main

    main(CASES)
