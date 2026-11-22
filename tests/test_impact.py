import gzip
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from impact import UnsupportedPatch, parse_patch
from selector import REVISION, Selector


class UnifiedDiffTests(unittest.TestCase):
    def test_insertions_and_deletions_preserve_base_coordinates(self):
        patch = """--- a/source.py
+++ b/source.py
@@ -1,0 +2 @@
+middle
@@ -3 +3,0 @@
-last
"""
        change, = parse_patch(patch)
        self.assertEqual(change.updated_python("first\nsecond\nlast\n"), "first\nmiddle\nsecond\n")
        self.assertEqual([list(h.changes()) for h in change.hunks], [[(1, 2)], [(3, 3)]])
        self.assertEqual([list(h.edited_lines("old")) for h in change.hunks], [[], [(3, 3)]])
        self.assertEqual([list(h.edited_lines("new")) for h in change.hunks], [[(2, 2)], []])

    def test_file_headers_inside_hunks_are_content(self):
        change, = parse_patch("--- a/data\n+++ b/data\n@@ -1 +1 @@\n--- old comment\n+++ new comment\n")
        self.assertEqual(change.updated_python("-- old comment\n"), "++ new comment\n")

    def test_reject_truncated_hunk_and_extra_mode_only_file(self):
        with self.assertRaises(UnsupportedPatch):
            parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,5 @@\n-a\n+b\n")
        with self.assertRaises(UnsupportedPatch):
            parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/z b/z\nold mode 100644\nnew mode 100755")


class HybridSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.patch = root / "change.patch"
        tests = [
            {"number": num, "file": f"tests/test_{name}.py", "id": f"tests/test_{name}.py::test_it"}
            for num, name in [(2, "parser"), (0, "fixture"), (1, "untraced"), (3, "other")]
        ]
        nodes = [t["id"] for t in tests]
        static = {"spans": {"src.py": [[1, 3, "parse", "function", 2]]},
                  "fixtures": {"data.sql": [nodes[1]]}, "files": {"src.py": [t["file"] for t in tests]}}
        coverage = {"nodes": nodes, "complete": [0, 1, 3], "groups": [[0]],
                    "lines": {"src.py": [[2, 3, 0]]}, "fixtures": {}}
        for name, obj in (("static", static), ("coverage", coverage)):
            with gzip.open(root / (name + ".json.gz"), "wt") as out:
                json.dump({"revision": REVISION, **obj}, out)
        with ZipFile(root / "sources.zip", "w") as out:
            out.writestr("src.py", "def parse():\n    value = 1\n    return value\n")
        self.selector = Selector(tests, "hybrid", root)
        self.addCleanup(self.selector.source_zip.close)

    def select(self, old, new, extra=""):
        self.patch.write_text(f"--- a/src.py\n+++ b/src.py\n@@ -2 +2 @@\n-{old}\n+{new}\n{extra}", encoding="utf-8")
        return self.selector.select(self.patch)

    def test_coverage_uses_node_ids_and_keeps_incomplete_tests(self):
        self.assertEqual(self.select("    value = 1", "    value = 2"), {2, 1})

    def test_identical_compiled_code_still_selects_source_readers(self):
        self.selector.coverage.readers["src.py"] = {3}
        self.assertEqual(self.select("    value = 1", "    value = 1  # note"), {1, 3})

    def test_multifile_patch_combines_coverage_and_fixture_readers(self):
        fixture = "--- a/data.sql\n+++ b/data.sql\n@@ -1 +1 @@\n-A\n+B\n"
        self.assertEqual(self.select("    value = 1", "    value = 2", fixture), {0, 1, 2})

    def test_unapplicable_patch_and_syntax_errors_select_everything(self):
        self.assertEqual(self.select("wrong baseline", "    value = 2"), {0, 1, 2, 3})
        self.assertEqual(self.select("    value = 1", "    value = ("), {0, 1, 2, 3})


class PrecisionSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.patch = self.root / "change.patch"
        names = ("left", "right", "caller", "class", "observer")
        self.tests = [
            {"number": number, "file": f"tests/test_{name}.py",
             "id": f"tests/test_{name}.py::test_it"}
            for number, name in enumerate(names)
        ]
        nodes = [test["id"] for test in self.tests]
        static = {
            "spans": {"src.py": [[1, 7, "choose", "function", 2],
                                    [10, 11, "Config", "class", 11]]},
            "files": {"src.py": nodes[:4]},
            "direct": {"src.py": nodes[:2]},
            "symbols": {"choose": [nodes[2]], "Config": [nodes[3]]},
            "fixtures": {"data.sql": [nodes[0]]},
            "observers": [nodes[4]],
        }
        coverage = {
            "nodes": nodes,
            "complete": list(range(len(nodes))),
            "groups": [[0], [1], [0, 1, 2]],
            "lines": {"src.py": [[3, 3, 0], [4, 4, 1], [6, 7, 2]]},
            "fixtures": {"data.sql": [0]},
        }
        for name, obj in (("static", static), ("coverage", coverage)):
            with gzip.open(self.root / (name + ".json.gz"), "wt") as out:
                json.dump({"revision": REVISION, **obj}, out)
        source = (
            "def choose(left, right):\n"
            "    if (\n"
            "        left\n"
            "        and right\n"
            "    ):\n"
            "        return 1\n"
            "    return 0\n"
            "\n"
            "\n"
            "class Config:\n"
            "    VALUE = 1\n"
        )
        with ZipFile(self.root / "sources.zip", "w") as out:
            out.writestr("src.py", source)
        self.selector = Selector(self.tests, "precision", self.root)
        self.addCleanup(self.selector.source_zip.close)

    def select(self, patch):
        self.patch.write_text(patch, encoding="utf-8")
        return self.selector.select(self.patch)

    def test_short_circuit_change_covers_the_complete_predicate(self):
        selected = self.select(
            "--- a/src.py\n+++ b/src.py\n@@ -4 +4 @@\n"
            "-        and right\n+        or right\n"
        )
        self.assertEqual(selected, {0, 1, 4})

    def test_added_control_flow_widens_to_observed_callers(self):
        selected = self.select(
            "--- a/src.py\n+++ b/src.py\n@@ -1,0 +2,2 @@\n"
            "+    if left is None:\n+        return -1\n"
        )
        self.assertEqual(selected, {0, 1, 2, 4})

    def test_class_assignment_uses_symbol_consumers_when_untraced(self):
        selected = self.select(
            "--- a/src.py\n+++ b/src.py\n@@ -11 +11 @@\n"
            "-    VALUE = 1\n+    VALUE = 2\n"
        )
        self.assertEqual(selected, {3, 4})

    def test_source_only_edit_keeps_explicit_source_observers(self):
        selected = self.select(
            "--- a/src.py\n+++ b/src.py\n@@ -0,0 +1 @@\n+# source note\n"
        )
        self.assertEqual(selected, {4})

    def test_signature_change_retains_transitive_dependents(self):
        selected = self.select(
            "--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n"
            "-def choose(left, right):\n+def choose(left, right=False):\n"
        )
        self.assertEqual(selected, {0, 1, 2, 3, 4})

    def test_fixture_selection_does_not_add_unrelated_incomplete_tests(self):
        selected = self.select(
            "--- a/data.sql\n+++ b/data.sql\n@@ -1 +1 @@\n-old\n+new\n"
        )
        self.assertEqual(selected, {0})

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            Selector(self.tests, "typo", self.root)


if __name__ == "__main__":
    unittest.main()
