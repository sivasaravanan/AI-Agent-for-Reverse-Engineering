"""Exact container command lines for the preview. Same hardening as the Phase 4 sandbox, plus what a preview needs:
a published port on 127.0.0.1 only, stdin for the debugger, and a read-only helper mount. Never uses a host shell."""
from __future__ import annotations

from pathlib import Path
from typing import List

from .models import DISPLAY_NUM, PreviewPlan
from .planner import HELPER


def container_command(exe: str, plan: PreviewPlan, workspace, helper_dir, name: str) -> List[str]:
    lim = plan.limits
    cmd = [exe, "run", "--rm", "--name", name, "--pull=never"]
    if plan.interactive:
        cmd.append("-i")
    cmd += ["--network", "bridge" if lim.network else "none"]
    for host, cont in plan.ports:
        cmd += ["--publish", f"127.0.0.1:{int(host)}:{int(cont)}"]
    cmd += ["--cpus", str(lim.cpus), "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m",
            "--pids-limit", str(lim.pids), "--ulimit", "nofile=1024:1024",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", f"/tmp:rw,exec,nosuid,size={lim.tmp_mb}m",
            "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "CI=1"]
    for k, v in sorted(plan.env.items()):
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--mount", f"type=bind,source={workspace},target=/work"]
    if plan.mount_helper:
        cmd += ["--mount", f"type=bind,source={helper_dir},target={HELPER},readonly"]
    cmd += ["-w", "/work", plan.image]
    return cmd + list(plan.argv)


def exec_command(exe: str, name: str, argv: List[str], display: bool = True) -> List[str]:
    """``<runtime> exec`` into the running preview container (screen capture, synthetic input)."""
    pre = [exe, "exec"] + (["-e", f"DISPLAY={DISPLAY_NUM}"] if display else []) + [name]
    return pre + list(argv)


def runtime_dir() -> Path:
    return Path(__file__).resolve().parent / "runtime"
