"""Controlled test runner (Phase 4): pytest, npm test, dotnet test, mvn/gradle test - in isolation only."""
from __future__ import annotations

from typing import List

from plugins.build_execution import Candidate
from services.build_runner import BuildRunner


class TestRunner(BuildRunner):
    __test__ = False  # not a pytest class
    kind = "test"

    def _wanted(self, c: Candidate) -> bool:
        return "test" in f"{c.id} {c.description}".lower()
