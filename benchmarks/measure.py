"""Linux-only measurement wrapper, mounted only for benchmark repetitions."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

def read_limit(v2, legacy):
    root = Path("/sys/fs/cgroup")
    for name in (v2, legacy):
        path = root / name
        if path.exists():
            return path.read_text().strip()
    raise RuntimeError("cannot verify cgroup setting " + v2)


def runtime_limits():
    quota = read_limit("cpu.max", "cpu/cpu.cfs_quota_us").split()
    period = quota[1] if len(quota) == 2 else read_limit("cpu.max", "cpu/cpu.cfs_period_us")
    tmp = os.statvfs("/tmp")
    return {
        "cpus": int(quota[0]) / int(period),
        "memory_bytes": int(read_limit("memory.max", "memory/memory.limit_in_bytes")),
        "pids": int(read_limit("pids.max", "pids/pids.max")),
        "uid": os.getuid(),
        "root_read_only": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        "tmpfs_bytes": tmp.f_blocks * tmp.f_frsize,
        "network_interfaces": sorted(name for _, name in socket.if_nameindex()),
    }


limits = runtime_limits()
expected = {"cpus": 2, "memory_bytes": 4096 * 2**20, "pids": 256, "uid": 65534,
            "root_read_only": True, "tmpfs_bytes": 64 * 2**20, "network_interfaces": ["lo"]}
if limits != expected:
    raise RuntimeError(f"unexpected submission runtime settings: {limits}")
start = time.monotonic_ns()
proc = subprocess.Popen([sys.executable, "/app/selector.py", *sys.argv[1:]])
_, status, usage = os.wait4(proc.pid, 0)
proc.returncode = os.waitstatus_to_exitcode(status)
print("TELEBLAST_TIMING " + json.dumps({
    "selector_cpu_s": round(usage.ru_utime + usage.ru_stime, 6),
    "selector_wall_s": round((time.monotonic_ns() - start) / 1e9, 6),
    "selector_peak_rss_kib": usage.ru_maxrss,
    "runtime_limits": limits,
}), file=sys.stderr)
sys.exit(proc.returncode)
