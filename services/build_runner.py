"""Controlled build/test runner (Phase 4).

Flow: ``candidate_commands`` (read-only discovery) -> ``plan`` (exact command, image, limits, network) ->
the UI/CLI shows the plan -> ``approve`` creates an Approval bound to that plan's hash -> ``run``.
``run`` refuses without a matching approval and never falls back to the host.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from plugins.build_execution import (Approval, Candidate, ExecutionResult, Options, Plan, build_plan,
                                     custom_candidate, discover_candidates, run_plan)

BUILD_ECOSYSTEM_HINTS = ("build", "compile", "install", "restore", "package")


class BuildRunner:
    kind = "build"

    def __init__(self, source, options: Optional[Options] = None):
        self.source = str(source)
        self.options = options or Options()
        self.notes: List[str] = []

    # -- discovery (executes nothing)
    def _all(self) -> List[Candidate]:
        cands, notes = discover_candidates(self.source)
        self.notes = list(notes)
        return list(cands)

    def candidates(self) -> List[Candidate]:
        return [c for c in self._all() if self._wanted(c)]

    def _wanted(self, c: Candidate) -> bool:
        return "test" not in c.id.lower() and any(h in f"{c.id} {c.description}".lower() for h in BUILD_ECOSYSTEM_HINTS)

    def candidate_commands(self, manifests=None) -> List[str]:
        """Display strings of candidate commands (``manifests`` is accepted for compatibility and ignored)."""
        return [c.display for c in self.candidates()]

    # -- planning / approval / execution
    def plan(self, candidate: Candidate, image: str = "") -> Plan:
        return build_plan(self.source, candidate, self.options, image=image)

    def custom_plan(self, argv: List[str], ecosystem: str = "python", needs_network: bool = False) -> Plan:
        return build_plan(self.source, custom_candidate(argv, ecosystem, needs_network), self.options)

    @staticmethod
    def approve(plan: Plan) -> Approval:
        """Call ONLY after the user has reviewed ``plan`` in an approval dialog / CLI prompt."""
        return Approval.for_plan(plan)

    def run(self, plan: Plan, approval: Optional[Approval], progress: Optional[Callable[[str], None]] = None,
            cancel: Optional[threading.Event] = None) -> ExecutionResult:
        if not isinstance(plan, Plan):
            return ExecutionResult(status="refused", notes=["Refused: run() needs a reviewed Plan, not a bare command string."])
        return run_plan(plan, approval, self.options, progress=progress, cancel=cancel)
