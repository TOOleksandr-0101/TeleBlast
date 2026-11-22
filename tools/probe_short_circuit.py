"""Development experiment: prove necessary execution sites for predicate rewrites."""
import ast
import argparse
import dis
import json
from pathlib import Path
import re
import sys
from types import CodeType


def graph(node):
    leaves = []

    def lower(expr):
        if isinstance(expr, ast.BoolOp):
            return type(expr.op).__name__, tuple(lower(v) for v in expr.values)
        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            return "Not", lower(expr.operand)
        leaves.append(expr)
        return len(leaves) - 1

    def compile_graph(expr, truth, false):
        if isinstance(expr, int):
            return (expr, truth, false)
        op, args = expr
        if op == "Not":
            return compile_graph(args, false, truth)
        next_step = compile_graph(args[-1], truth, false)
        for term in reversed(args[:-1]):
            next_step = (compile_graph(term, next_step, false) if op == "And"
                         else compile_graph(term, truth, next_step))
        return next_step

    expression = lower(node)
    return leaves, compile_graph(expression, True, False)


def divergence_sites(a, b):
    """Both formulas evaluated identical leaves and truths up to the first differing jump."""
    found, seen = set(), set()

    def visit(left, right):
        if isinstance(left, bool) or isinstance(right, bool) or (id(left), id(right)) in seen:
            return
        seen.add((id(left), id(right)))
        assert left[0] == right[0]
        for lnext, rnext in zip(left[1:], right[1:]):
            if isinstance(lnext, bool) or isinstance(rnext, bool):
                if lnext is not rnext:
                    found.add(left[0])
            elif lnext[0] != rnext[0]:
                found.add(left[0])
            else:
                visit(lnext, rnext)

    visit(a, b)
    return found


def unconditional_name(expr):
    # Restrict fallbacks to a load that actually runs before any action of the
    # leaf, including a potentially throwing call or overloaded comparison.
    while not isinstance(expr, ast.Name):
        if isinstance(expr, (ast.Attribute, ast.Subscript)):
            expr = expr.value
        elif isinstance(expr, ast.Call):
            expr = expr.func
        elif isinstance(expr, (ast.Compare, ast.BinOp)):
            expr = expr.left
        elif isinstance(expr, ast.NamedExpr):
            expr = expr.value
        else:
            return None
    return expr


def load_positions(code):
    positions = set()
    for instr in dis.get_instructions(code):
        if instr.opname.startswith(("LOAD_FAST", "LOAD_GLOBAL", "LOAD_DEREF", "LOAD_NAME")):
            p = instr.positions
            positions.add((p.lineno, p.col_offset, p.end_lineno, p.end_col_offset))
    for child in code.co_consts:
        if isinstance(child, CodeType):
            positions.update(load_positions(child))
    return positions


def predicate_lines(old, new, source, path, ranges):
    """None for unsupported edits, otherwise lines that must execute for impact."""
    kinds = (ast.If, ast.While, ast.IfExp, ast.Assert)
    matches = [n for n in ast.walk(old) if isinstance(n, kinds)
               and all(n.test.lineno <= first <= last <= n.test.end_lineno for first, last in ranges)]
    if len(matches) != 1:
        return None
    before = matches[0]
    after = next((n for n in ast.walk(new) if type(n) is type(before)
                  and n.lineno == before.lineno and n.col_offset == before.col_offset), None)
    if after is None:
        return None
    a, left = graph(before.test)
    b, right = graph(after.test)
    if len(a) > 48 or not any(isinstance(n, ast.BoolOp) for n in ast.walk(before.test)):
        return None
    if list(map(ast.dump, a)) != list(map(ast.dump, b)) or ast.dump(before.test) == ast.dump(after.test):
        return None
    saved = after.test
    try:
        after.test = before.test
        if ast.dump(old) != ast.dump(new):
            return None
    finally:
        after.test = saved
    positions = load_positions(compile(source, path, "exec", dont_inherit=True))
    lines = set()
    for index in divergence_sites(left, right):
        name = unconditional_name(a[index])
        if name is None or (name.lineno, name.col_offset, name.end_lineno, name.end_col_offset) not in positions:
            return None
        lines.add(name.lineno)
    return lines


def probe(data):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from impact import parse_patch, UnsupportedPatch
    from scheduler import schedule
    from score import evaluate_run
    from selector import Selector

    tests = json.loads((data / "tests.json").read_text())["tests"]
    # Labels enter only evaluate_run, after both complete sets of plans exist.
    selector = Selector(tests, "candidate")
    durations = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    original_plans, experimental_plans = [], []
    recognized = narrowed = 0
    for patch in sorted((data / "commits").glob("*.patch")):
        original = selector.select(patch)
        experimental = set()
        try:
            for change in parse_patch(patch.read_text(encoding="utf-8")):
                path = change.path
                selected = selector.file_tests(change)
                changed_logic = any(row[0] in "+-" and re.search(r"\b(and|or|not)\b", row)
                                    for hunk in change.hunks for row in hunk.body)
                if path.endswith(".py") and changed_logic:
                    source = selector.source(path)
                    updated = change.updated_python(source)
                    ranges = [r for h in change.hunks for r in h.changes()]
                    witnesses = predicate_lines(ast.parse(source), ast.parse(updated),
                                                source, path, ranges)
                    if witnesses is not None and selector.coverage.available and selector.collection:
                        recognized += 1
                        keep = (selector.source_observers(path) | selector.coverage.untraced
                                | selector.catalog.resolve([path])
                                | (selector.all - selector.catalog.resolve(selector.collection["complete"])))
                        for line in witnesses:
                            keep.update(selector.coverage.hits(path, line, line))
                        for start, end, modules in selector.collection["files"].get(path, ()):
                            if any(start <= line <= end for line in witnesses):
                                keep.update(selector.catalog.resolve(modules))
                        selected = selected & keep
                experimental.update(selected)
        except (SyntaxError, ValueError, UnsupportedPatch):
            experimental = original
        narrowed += len(original - experimental)
        for into, ids in [(original_plans, original), (experimental_plans, experimental)]:
            into.append(json.dumps({"commit": patch.stem, "workers": schedule(ids, durations)}))
    answers = json.loads((data / "answers.json").read_text())["commits"]
    print(json.dumps({"recognized_edits": recognized, "selections_removed": narrowed,
                      **{label: evaluate_run("\n".join(plans).encode(), tests, answers)
                         for label, plans in [("candidate", original_plans),
                                              ("short_circuit", experimental_plans)]}}, indent=2))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "sample")
    probe(cli.parse_args().data)
