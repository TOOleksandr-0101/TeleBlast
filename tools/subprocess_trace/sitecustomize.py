"""Start a per-interpreter trace when the isolated profiling harness requests it."""
import os

if os.environ.get("TELEBLAST_CHILD_SPOOL"):
    import atexit
    import json
    from pathlib import Path
    import sys

    def start():
        root = os.path.realpath(os.environ["TELEBLAST_TRACE_ROOT"]) + os.sep
        dest = Path(os.environ["TELEBLAST_CHILD_SPOOL"]) / str(os.getpid())
        touched = set()
        launches = []
        opaque = []
        dest.with_suffix(".started").touch()

        def remember(filename):
            if isinstance(filename, str):
                full = os.path.abspath(filename)
                if full.startswith(root):
                    name = full[len(root):]
                    if name.startswith(("sqlglot/", "tests/")):
                        touched.add(name)

        def audit(event, args):
            if event == "open":
                remember(args[0])
            elif event == "exec":
                remember(args[0].co_filename)
            elif event == "subprocess.Popen":
                launches.append(str(args[0]))
            elif event in {"os.fork", "os.forkpty", "os.exec", "ctypes.dlopen"}:
                # Python cannot audit arbitrary native file reads. Normal
                # extension loading is covered separately by the pinned suite.
                if event != "ctypes.dlopen" or args[0] is not None:
                    opaque.append(event)

        def finish():
            dest.with_suffix(".done").write_text(json.dumps({
                "paths": sorted(touched), "children": launches, "opaque": sorted(set(opaque))
            }))
        sys.addaudithook(audit)
        atexit.register(finish)
    start()
