import ast
import itertools
import unittest

from tools.probe_short_circuit import divergence_sites, graph, predicate_lines


def simulate(graph_root, values):
    cur = graph_root
    trace = []
    while not isinstance(cur, bool):
        leaf, true, false = cur
        trace.append(leaf)
        cur = true if values[leaf] else false
    return trace, cur


class WitnessTests(unittest.TestCase):
    def test_first_different_side_effect_is_never_skipped(self):
        expressions = ["a and b and c", "a or b and c", "a or b or c", "a and (b or c)",
                       "(a or b) and c", "a or (not b and c)", "not (a and b) or c"]
        for a, b in itertools.product(expressions, repeat=2):
            ga = graph(ast.parse(a, mode="eval").body)[1]
            gb = graph(ast.parse(b, mode="eval").body)[1]
            sites = divergence_sites(ga, gb)
            for values in itertools.product((False, True), repeat=3):
                trace, result = simulate(ga, values)
                if sites.isdisjoint(trace):
                    self.assertEqual((trace, result), simulate(gb, values))

    def test_reject_other_module_changes_and_side_effecting_new_leaves(self):
        before = "def run(a, b, c):\n    if (a()\n        or (b()\n            and c())):\n        return 1\n"
        after = before.replace("and c()", "or c()")
        def lines(new):
            return predicate_lines(ast.parse(before), ast.parse(new), before, "test.py", [(4, 4)])
        self.assertEqual(lines(after), {3})
        self.assertIsNone(lines(after + "x = 1\n"))
        self.assertIsNone(lines(after.replace("c()", "c(1)")))


if __name__ == "__main__":
    unittest.main()
