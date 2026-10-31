"""Switches for the optional analysis phases. Every phase is on by default but can be turned off; none runs project code."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set


@dataclass
class AnalysisOptions:
    static_plugins: bool = True          # Phase 3: AST/Ruff/Pyright/ESLint/tsc/javac/sqlfluff (tools only if installed)
    approved_tools: Set[str] = field(default_factory=set)   # e.g. {"dotnet"}: a build tool that runs project build logic
    database_analysis: bool = True       # Phase 5: drivers, connection config, ORM, migrations, DDL (no connections)
    discover_execution: bool = True      # Phase 4: DISCOVER build/test commands + isolation status (executes nothing)
    code_map: bool = True                # function/class map and call graph (Python exact; others names only)
    findings_overview: bool = True       # Phase 6: grouped counts
    sql_dialect: str = "ansi"
    tool_timeout_s: int = 180

    @classmethod
    def phase1_2_only(cls) -> "AnalysisOptions":
        return cls(static_plugins=False, database_analysis=False, discover_execution=False, findings_overview=False, code_map=False)
