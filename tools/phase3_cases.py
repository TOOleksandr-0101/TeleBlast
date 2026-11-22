"""Predeclared, independent changes to the clean pinned sources; no failure labels."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    group: str
    path: str
    before: str
    after: str

    def apply(self, source):
        if not self.before:  # Append a new case to a text fixture.
            return source.rstrip("\n") + "\n" + self.after + "\n"
        if source.count(self.before) != 1:
            raise ValueError(f"{self.name}: expected exactly one anchor in {self.path}")
        return source.replace(self.before, self.after)


CASES = [
    Case("collection-import", "discovery", "sqlglot/dialects/athena.py",
         "from sqlglot.parsers.athena import AthenaParser",
         "from sqlglot.parsers.athena import MissingAthenaParser as AthenaParser"),
    Case("argument-default", "discovery", "sqlglot/optimizer/simplify.py",
         "def simplify(\n    expression: exp.Expr,\n    constant_propagation: bool = False,",
         "def simplify(\n    expression: exp.Expr,\n    constant_propagation: bool = True,"),
    Case("new-branch", "discovery", "sqlglot/serde.py",
         "    module: ModuleType", "    if class_name == \"Literal\":\n        return exp.Null()\n    module: ModuleType"),
    Case("dynamic-dispatch", "discovery", "sqlglot/serde.py",
         "expression = getattr(module, class_name)()", "expression = getattr(module, class_name.lower())()"),
    Case("dialect-registry", "discovery", "sqlglot/dialects/__init__.py",
         "DIALECT_MODULE_NAMES = MODULE_BY_DIALECT.values()",
         "DIALECT_MODULE_NAMES = tuple(name for name in MODULE_BY_DIALECT.values() if name != 'drill')"),
    Case("exception-handler", "discovery", "sqlglot/dialects/athena.py",
         "except TokenError:\n                trino_tokens = None", "except ValueError:\n                trino_tokens = None"),
    Case("class-attribute", "discovery", "sqlglot/dialects/sqlite.py",
         "    TYPED_DIVISION = True", "    TYPED_DIVISION = False"),
    Case("source-introspection", "discovery", "sqlglot/typing/spark.py",
         "    **EXPRESSION_METADATA,", "    **{kind: dict(metadata) for kind, metadata in EXPRESSION_METADATA.items()},"),
    Case("fixture-append", "holdout", "tests/fixtures/identity.sql", "",
         "selECT cast(1 as INTEGER)"),
    Case("changed-predicate", "holdout", "sqlglot/typing/postgres.py",
         "if right.is_type(exp.DType.INTERVAL):", "if not right.is_type(exp.DType.INTERVAL):"),
    Case("module-initialization", "holdout", "sqlglot/errors.py",
         "ERROR_MESSAGE_CONTEXT_DEFAULT = 100", "ERROR_MESSAGE_CONTEXT_DEFAULT = 1"),
    Case("signature-keyword", "holdout", "sqlglot/errors.py",
         "    context_length: int = ERROR_MESSAGE_CONTEXT_DEFAULT,",
         "    *,\n    context_length: int = ERROR_MESSAGE_CONTEXT_DEFAULT,"),
    Case("fixture-reader", "holdout", "tests/helpers.py",
         "                expected = statements[i + 1].strip()",
         "                expected = statements[i + 1].strip().removesuffix(' LIMIT 1')"),
    Case("generator-dispatch", "holdout", "sqlglot/generators/drill.py",
         'return self.func("TO_DATE", this, time_format)', 'return self.func("TO_CHAR", this, time_format)'),
]
