"""Linux-only measurement wrapper, mounted only for benchmark repetitions."""
import json
import os
import subprocess
import sys
import time

start = time.monotonic_ns()
proc = subprocess.Popen([sys.executable, "/app/selector.py"])
_, status, usage = os.wait4(proc.pid, 0)
proc.returncode = os.waitstatus_to_exitcode(status)
print("TELEBLAST_TIMING " + json.dumps({
    "selector_cpu_s": round(usage.ru_utime + usage.ru_stime, 6),
    "selector_wall_s": round((time.monotonic_ns() - start) / 1e9, 6),
}), file=sys.stderr)
sys.exit(proc.returncode)
