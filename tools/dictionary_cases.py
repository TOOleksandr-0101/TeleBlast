"""Predeclared dictionary-value changes on four classes in the pinned source."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.phase3_cases import Case


CASES = [
    Case("hive-type-literal", "dictionary", "sqlglot/generators/hive.py",
         'exp.DType.BIT: "BOOLEAN",', 'exp.DType.BIT: "BIT",'),
    Case("snowflake-column-tuple", "dictionary", "sqlglot/dialects/snowflake.py",
         'exp.SplitToTable: ("seq", "index", "value"),',
         'exp.SplitToTable: ("seq", "index", "element"),'),
    Case("mysql-registered-lambda", "dictionary", "sqlglot/generators/mysql.py",
         'exp.Chr: lambda self, e: self.chr_sql(e, "CHAR"),',
         'exp.Chr: lambda self, e: self.chr_sql(e, "CHR"),'),
    Case("presto-factory-result", "dictionary", "sqlglot/parsers/presto.py",
         '"BITWISE_AND": binary_from_function(exp.BitwiseAnd),',
         '"BITWISE_AND": binary_from_function(exp.BitwiseOr),'),
]


if __name__ == "__main__":
    from phase3_lab import main

    main(CASES)
