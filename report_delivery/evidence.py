"""Build/test evidence: runs the APPLICATION'S OWN test suite (trusted code you are delivering) and records
the real command, exit code and output. Uploaded/untrusted projects are never run here (that is Phase 4)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .redaction import redact_text


def collect_test_evidence(root, command: Optional[Sequence[str]] = None, timeout: int = 600,
                          max_output: int = 20000) -> Dict[str, Any]:
    root = Path(root)
    cmd: List[str] = list(command) if command else [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    rec: Dict[str, Any] = {"command": " ".join(cmd), "cwd": root.name, "python": sys.version.split()[0],
                           "status": "not-run", "exit_code": None, "duration_s": None, "stdout": "", "stderr": ""}
    if not command and importlib.util.find_spec("pytest") is None:
        rec.update(status="unavailable", stderr="pytest is not installed; tests were NOT run. Install with: pip install pytest")
        return rec
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        rec.update(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr,
                   status="passed" if p.returncode == 0 else "failed")
    except subprocess.TimeoutExpired as exc:
        rec.update(status="timeout", stderr=f"Timed out after {timeout}s", stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
    except OSError as exc:
        rec.update(status="error", stderr=f"Could not start test runner: {exc.__class__.__name__}")
    rec["duration_s"] = round(time.perf_counter() - t0, 2)
    for k in ("stdout", "stderr"):
        s = redact_text(rec[k] or "")
        rec[k] = s if len(s) <= max_output else s[:max_output] + "\n...[truncated]"
    return rec
