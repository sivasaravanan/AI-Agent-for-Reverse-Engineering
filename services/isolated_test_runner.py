"""Adapter that lets Phase 8 (patch verification) use the Phase 4 sandbox.

``isolated`` is True only when a container runtime really answers. Every run needs the approver callback to say
yes for the exact plan (the UI shows the approval dialog); with no approver the runner refuses.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional

from models.patch import RunSummary
from plugins.build_execution import Approval, ExecutionResult, Options, Plan, custom_candidate
from services.environment_service import detect_isolation
from services.test_runner import TestRunner

_PYTEST_LINE = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)", re.M)


def parse_pytest_report(output: str) -> RunSummary:
    passed, failed = set(), set()
    for status, name in _PYTEST_LINE.findall(output):
        (passed if status == "PASSED" else failed).add(name)
    return RunSummary(exit_code=0 if not failed else 1, passed=passed, failed=failed, output=output[-20000:])


class IsolatedSandboxRunner:
    """Implements ``core.patch_manager.IsolatedTestRunner``."""

    def __init__(self, approver: Optional[Callable[[Plan], bool]] = None, options: Optional[Options] = None,
                 on_result: Optional[Callable[[ExecutionResult], None]] = None):
        self.approver, self.options, self.on_result = approver, options or Options(), on_result
        self.last_result: Optional[ExecutionResult] = None
        self._isolated: Optional[bool] = None

    @property
    def isolated(self) -> bool:
        if self._isolated is None:
            info = detect_isolation(self.options)
            ok = info.available
            if not ok and getattr(self.options, "auto_start", False) and info.executable:
                from plugins.build_execution.runtime_start import ensure_runtime      # installed but stopped: start it
                ok = bool(ensure_runtime(self.options).get("available"))
            self._isolated = ok
        return self._isolated

    def _pick(self, root: Path):
        runner = TestRunner(root, self.options)
        cands = [c for c in runner.candidates() if not c.needs_network]
        if not cands:
            raise RuntimeError("No offline test command was discovered for this project.")
        c = cands[0]
        if c.id == "python:pytest":
            c = custom_candidate(["python", "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider"], "python")
        return runner, runner.plan(c)

    def run(self, workspace_root: Path) -> RunSummary:
        runner, plan = self._pick(Path(workspace_root))
        if self.approver is None or not self.approver(plan):
            raise RuntimeError("Test execution was not approved by the user.")
        res = runner.run(plan, Approval.for_plan(plan))
        self.last_result = res
        if self.on_result:
            self.on_result(res)
        if res.status in ("skipped", "refused", "error", "cancelled"):
            raise RuntimeError(f"Tests did not run ({res.status}): {' '.join(res.notes)[:200]}")
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        if plan.candidate.ecosystem == "python" and "-rA" in plan.candidate.argv:
            summary = parse_pytest_report(out)
            summary.exit_code = res.exit_code if res.exit_code is not None else summary.exit_code
        else:   # no per-test detail available: one pseudo-test for the whole command
            ok = res.exit_code == 0
            key = " ".join(plan.candidate.argv)
            summary = RunSummary(exit_code=res.exit_code or 0, passed={key} if ok else set(),
                                 failed=set() if ok else {key}, output=out[-20000:])
        summary.timed_out = res.status == "timeout"
        return summary
