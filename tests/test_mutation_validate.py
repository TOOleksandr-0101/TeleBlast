import ast
import unittest

from selector import TestCatalog
from tools.mutation_validate import (
    CoveragePopularity,
    OffsetTable,
    SourceEdit,
    _python_candidates,
    resolve_failure_records,
)


class SourceEditTests(unittest.TestCase):
    def test_ast_byte_columns_work_with_unicode(self):
        source = 'value = "λ"\nresult = value + 1\n'
        tree = ast.parse(source)
        expression = tree.body[1].value
        start, end = OffsetTable(source).span(expression)
        self.assertEqual(source[start:end], "value + 1")

    def test_edit_refuses_a_different_source(self):
        edit = SourceEdit("return_value", "x.py", 0, 1, "a", "b", 1, "change")
        self.assertEqual(edit.apply("abc"), "bbc")
        with self.assertRaises(ValueError):
            edit.apply("xbc")


class CandidateTests(unittest.TestCase):
    def test_python_generator_covers_control_flow_categories(self):
        source = '''\
from pkg import thing

class Child(Base):
    FLAG = 1

def run(a, b):
    try:
        if a and b == 1:
            return thing(a + b)
    except ValueError:
        return 0
'''
        candidates = list(
            _python_candidates("sqlglot/dialects/demo.py", source, CoveragePopularity(None))
        )
        categories = {candidate.category for candidate in candidates}
        self.assertTrue(
            {
                "arithmetic_operator",
                "comparison_operator",
                "boolean_condition",
                "removed_branch",
                "return_value",
                "function_call",
                "import_change",
                "class_level",
                "exception_handling",
                "dialect_behavior",
            }.issubset(categories)
        )
        for candidate in candidates:
            compile(candidate.apply(source), candidate.path, "exec")


class FailureResolutionTests(unittest.TestCase):
    def test_parametrized_and_collection_failures_resolve_to_numbers(self):
        tests = [
            {"number": 4, "file": "tests/test_a.py", "id": "tests/test_a.py::test_x[p]"},
            {"number": 6, "file": "tests/test_a.py", "id": "tests/test_a.py::test_z"},
            {"number": 5, "file": "tests/test_b.py", "id": "tests/test_b.py::test_y"},
        ]
        records = [
            {"kind": "test", "nodeid": "tests/test_a.py::test_x[p]", "when": "call"},
            {"kind": "collect", "nodeid": "tests/test_b.py"},
            {"kind": "session", "exitstatus": 1},
        ]
        numbers, unresolved = resolve_failure_records(records, TestCatalog(tests))
        self.assertEqual(numbers, {4, 5})
        self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
