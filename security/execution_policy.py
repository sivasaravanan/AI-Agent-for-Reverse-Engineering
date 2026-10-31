"""Execution policy (Phase 4): project code may run ONLY with explicit approval AND an isolation backend.

There is no host fallback. The decision function is pure so it can be tested without Docker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

DEFAULT_LIMITS = {"wall_seconds": 300, "memory_mb": 1024, "processes": 256, "cpus": 1.0, "network": False}


@dataclass
class ExecutionDecision:
    allowed: bool
    reasons: List[str]


def decide(approved: bool = False, isolation_available: bool = False, network_requested: bool = False,
           network_approved: bool = False) -> ExecutionDecision:
    reasons: List[str] = []
    if not approved:
        reasons.append("No explicit user approval for this exact command.")
    if not isolation_available:
        reasons.append("No isolation backend (Docker/Podman) is available; execution is skipped, never run on the host.")
    if network_requested and not network_approved:
        reasons.append("The command needs network access and the user has not approved network access.")
    return ExecutionDecision(allowed=not reasons, reasons=reasons)


def is_execution_allowed(approved: bool = False, isolation_available: bool = False) -> bool:
    """Fail closed: only True when both approval and isolation exist."""
    return decide(approved, isolation_available).allowed
