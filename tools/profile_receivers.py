"""Instrument reads of scalar class settings while preserving normal lookup."""
import ast
import builtins
from collections import defaultdict
import json
import os
from pathlib import Path


HELPER = "__teleblast_read_setting__"
NAMES = set()


def manifest(root):
    fields = defaultdict(set)
    def visit(body, prefix):
        for obj in body:
            if not isinstance(obj, ast.ClassDef):
                continue
            cls = prefix + "." + obj.name
            visit(obj.body, cls)
            for stmt in obj.body:
                if (isinstance(stmt, (ast.Assign, ast.AnnAssign))
                        and isinstance(stmt.value, ast.Constant)):
                    names = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    for n in names:
                        if isinstance(n, ast.Name) and n.id.isupper():
                            fields[cls].add(n.id)
    for path in (root / "sqlglot").rglob("*.py"):
        module = ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
        visit(ast.parse(path.read_bytes()).body, module)
    return {k: sorted(v) for k, v in fields.items()}


def probe(expr, kind, receiver, name):
    return ast.copy_location(ast.Call(ast.Name(HELPER, ast.Load()),
                                     [ast.Constant(kind), receiver, name], []), expr)


class Rewrite(ast.NodeTransformer):
    def __init__(self, path):
        pass

    def visit_Call(self, expr):
        # Preserve short-circuiting and the order in which function/argument
        # expressions are evaluated. A wrapped argument returns its original
        # object, letting the existing getattr/hasattr/vars call do the lookup.
        f = expr.func
        if isinstance(f, ast.Name) and f.id in {"getattr", "hasattr", "vars"} and expr.args:
            if f.id == "vars" and len(expr.args) == 1:
                expr.args[0] = probe(expr.args[0], "namespace", self.visit(expr.args[0]), ast.Constant("*"))
                return expr
            if len(expr.args) >= 2 and isinstance(expr.args[1], ast.Constant):
                key = expr.args[1].value
                if isinstance(key, str) and key in NAMES:
                    self.generic_visit(expr)
                    expr.args[0] = probe(expr.args[0], "input", expr.args[0], ast.Constant(key))
                    return expr
            elif len(expr.args) >= 2 and not any(isinstance(a, ast.Starred) for a in expr.args):
                # A computed key may name any setting. Broad attribution
                # preserves correctness even for an overridden helper.
                self.generic_visit(expr)
                expr.args[0] = probe(expr.args[0], "input", expr.args[0], ast.Constant("*"))
                return expr
        if (isinstance(f, ast.Attribute) and f.attr == "get"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "__dict__"
                and expr.args and isinstance(expr.args[0], ast.Constant)
                and isinstance(expr.args[0].value, str)):
            f.value.value = probe(f.value.value, "namespace", self.visit(f.value.value),
                                  ast.Constant(expr.args[0].value))
            expr.args = [self.visit(arg) for arg in expr.args]
            expr.keywords = [self.visit(kw) for kw in expr.keywords]
            return expr
        return self.generic_visit(expr)

    def visit_Subscript(self, expr):
        if (isinstance(expr.value, ast.Attribute) and expr.value.attr == "__dict__"
                and isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str)):
            receiver = expr.value.value
            expr.value.value = probe(receiver, "namespace", self.visit(receiver), expr.slice)
            return expr
        return self.generic_visit(expr)

    def visit_Attribute(self, expr):
        self.generic_visit(expr)
        if isinstance(expr.ctx, ast.Load) and (expr.attr in NAMES or expr.attr == "__dict__"):
            key = "*" if expr.attr == "__dict__" else expr.attr
            expr.value = probe(expr.value, "input", expr.value, ast.Constant(key))
        return expr


def bind(rec):
    fields = json.loads(Path(os.environ["TELEBLAST_FIELDS"]).read_bytes())
    NAMES.update(name for names in fields.values() for name in names)
    cache = {}

    def inputs(kind, obj, member):
        # Preserve original dispatch. Attribute access, descriptor calls and
        # arbitrary custom metaclass code still run at the original bytecode.
        cls = obj if isinstance(obj, type) else type(obj)
        item = cache.get(id(cls))
        if item is None:
            by_name = defaultdict(list)
            for t in type.__getattribute__(cls, "__mro__"):
                qualified = (type.__getattribute__(t, "__module__") + "."
                             + type.__getattribute__(t, "__qualname__"))
                for name in fields.get(qualified, ()):
                    site = qualified + ":" + name
                    by_name[name].append(site)
                    by_name["*"].append(site)
            item = cache[id(cls)] = (cls, by_name)
        for site in item[1].get(member, ()):
            if rec.mask and not rec.import_depth:
                rec.batch.add((site, "read"))
            else:
                rec.outside.add(site)
        return obj

    setattr(builtins, HELPER, inputs)
