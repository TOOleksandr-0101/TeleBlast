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

    def test_multifile_patch_combines_coverage_and_fixture_readers(self):
        fixture = "--- a/data.sql\n+++ b/data.sql\n@@ -1 +1 @@\n-A\n+B\n"
        self.assertEqual(self.select("    value = 1", "    value = 2", fixture), {0, 1, 2})

    def test_unapplicable_patch_and_syntax_errors_select_everything(self):
        self.assertEqual(self.select("wrong baseline", "    value = 2"), {0, 1, 2, 3})
        self.assertEqual(self.select("    value = 1", "    value = ("), {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
