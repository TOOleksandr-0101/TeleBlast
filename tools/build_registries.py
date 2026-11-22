"""Trace clean-base users of class dictionaries and calls of lambdas stored in dictionaries.

This development-only entry uses the same pytest/process-pool harness as
build_observations, with its receiver plugin specialized for dictionary fields.
It reads only the pinned checkout and writes a separate map.
"""
import ast
import builtins
from collections import defaultdict
import os
import sys

import build_observations as harness
import profile_receivers as receiver

LAMBDA_HELPER = "__teleblast_registry_lambda_call__"
original_bind = receiver.bind


def manifest(root):
    fields = defaultdict(set)

    def visit(body, prefix):
        for cls in body:
            if not isinstance(cls, ast.ClassDef):
                continue
            name = prefix + "." + cls.name
            visit(cls.body, name)
            for stmt in cls.body:
                if (not isinstance(stmt, (ast.Assign, ast.AnnAssign))
                        or not isinstance(stmt.value, ast.Dict)):
                    continue
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for attr in targets:
                    if isinstance(attr, ast.Name):
                        fields[name].add(attr.id)

    for path in (root / "sqlglot").rglob("*.py"):
        module = ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
        visit(ast.parse(path.read_bytes()).body, module)
    return {k: sorted(v) for k, v in fields.items()}


class RegistryRewrite(receiver.Rewrite):
    def __init__(self, path):
        self.path = path
        self.dictionaries = 0

    def visit_Dict(self, node):
        self.dictionaries += 1
        try:
            return self.generic_visit(node)
        finally:
            self.dictionaries -= 1

    def visit_Lambda(self, node):
        self.generic_visit(node)
        if self.dictionaries:
            site = f"lambda:{self.path}:{node.lineno}:{node.col_offset}"
            marker = ast.Call(ast.Name(LAMBDA_HELPER, ast.Load()), [ast.Constant(site)], [])
            seq = ast.Tuple([marker, node.body], ast.Load())
            node.body = ast.copy_location(ast.Subscript(seq, ast.Constant(1), ast.Load()), node.body)
        return node


def bind(rec):
    original_bind(rec)
    def called(site):
        if rec.mask and not rec.import_depth:
            rec.batch.add((site, "called"))
        else:
            rec.outside.add(site)
    setattr(builtins, LAMBDA_HELPER, called)


# Spawned workers run the same entry point before unpickling a sqlglot job.
# Install the specialized plugin in both the driver and these interpreters.
receiver.manifest = manifest
receiver.Rewrite = RegistryRewrite
receiver.bind = bind
if __name__ == "__mp_main__" and os.environ.get("TELEBLAST_PROFILE_ROOT"):
    harness.install(os.environ["TELEBLAST_PROFILE_ROOT"])
if __name__ == "__main__":
    sys.argv.extend(["--kind", "receivers"])
    harness.main()
