import ast
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from selector import REVISION, Selector, comparison_edit, relation_result


class CandidateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = "def within(a, b):\n    return a < b\n"
        with ZipFile(self.root / "sources.zip", "w") as f:
            f.writestr("src.py", self.source)
        files = [f"tests/test_{n}.py" for n in range(6)]
        ids = [f"{p}::test_it" for p in files]
        catalog = [{"number": n, "id": name, "file": files[n]} for n, name in enumerate(ids)]
        maps = {
            "static": {"files": {"src.py": files}},
            "coverage": {"nodes": ids, "complete": list(range(5)), "groups": [list(range(6))],
                         "lines": {"src.py": [[2, 2, 0]]}},
            "collection": {"complete": files, "files": {"src.py": [[2, 2, [files[3]]]]}},
            "observations": {"nodes": ids, "complete": list(range(5)),
                             "sites": {"src.py:2:11": {"less": 0, "equal": 1, "opaque": 2}},
                             "groups": [[0, 1, 2, 3, 5], [1], [2]]},
        }
        for name, data in maps.items():
            with gzip.open(self.root / (name + ".json.gz"), "wt") as f:
                json.dump({"revision": REVISION, **data}, f)
        self.catalog = catalog
        self.selector = Selector(catalog, "candidate", self.root)
        self.addCleanup(self.selector.source_zip.close)

    def plan(self, new):
        patch = self.root / "change.patch"
        patch.write_text("--- a/src.py\n+++ b/src.py\n@@ -2 +2 @@\n-    return a < b\n+    " + new + "\n")
        return self.selector.select(patch)

    def test_all_observations_must_agree_and_be_complete(self):
        # 0 has only strictly ordered primitive operands. Others had an equal
        # or opaque value, ran at collection, or had missing/incomplete data.
        self.assertEqual(self.plan("return a <= b"), {1, 2, 3, 4, 5})

    def test_second_semantic_change_disables_value_pruning(self):
        self.assertEqual(self.plan("return not (a <= b)"), set(range(6)))

    def test_identity_and_ordering_uncertainty(self):
        self.assertFalse(relation_result("Is", "equal"))
        self.assertTrue(relation_result("Eq", "equal"))
        self.assertIsNone(relation_result("LtE", "same"))  # possibly None
        self.assertIsNone(relation_result("Lt", "disjoint"))
        self.assertIsNone(relation_result("Gt", "identity_unequal"))
        self.assertIsNone(relation_result("Eq", "opaque"))
        self.assertFalse(relation_result("Is", "value_equal"))
        self.assertTrue(relation_result("Eq", "value_equal"))

    def test_bool_and_int_constants_are_semantically_distinct(self):
        self.assertIsNone(comparison_edit(ast.parse("f(True, a < b)"), ast.parse("f(1, a <= b)")))

    def test_class_field_uses_receivers_only_with_complete_clean_evidence(self):
        nodes = [t["id"] for t in self.catalog]
        with ZipFile(self.root / "sources.zip", "w") as f:
            f.writestr("src.py", "class Engine:\n    ENABLED = False\n")
        site = "src.Engine:ENABLED"
        for name, data in [
            ("member_reads", {"nodes": nodes, "groups": [list(range(6))], "uses": {"ENABLED": 0}}),
            ("receivers", {"nodes": nodes, "complete": [0, 1, 2, 3, 4], "tracked": [site],
                           "groups": [[1, 2]], "sites": {site: {"read": 0}}}),
        ]:
            with gzip.open(self.root / (name + ".json.gz"), "wt") as out:
                json.dump({"revision": REVISION, **data}, out)
        sel = Selector(self.catalog, "candidate", self.root)
        self.addCleanup(sel.source_zip.close)
        patch = self.root / "field.patch"
        patch.write_text("--- a/src.py\n+++ b/src.py\n@@ -2 +2 @@\n-    ENABLED = False\n+    ENABLED = True\n")
        self.assertEqual(sel.select(patch), {1, 2, 5})
        sel.receiver_fields.clear()  # observed at import or unprofiled
        self.assertEqual(sel.select(patch), set(range(6)))


if __name__ == "__main__":
    unittest.main()
