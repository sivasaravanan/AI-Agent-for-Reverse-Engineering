from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional

DEFAULT_SKIP_DIRS: FrozenSet[str] = frozenset({
    "node_modules", ".git", ".hg", ".svn", "venv", ".venv", "site-packages", "__pycache__",
    "bin", "obj", "dist", "build", "target", "vendor", ".idea", ".vs", "__macosx", ".tox",
    ".mypy_cache", ".pytest_cache", ".gradle",
})


@dataclass
class AnalysisOptions:
    """Tunable behaviour. All defaults are safe; nothing here enables network or execution."""
    max_file_bytes: int = 2_000_000
    max_findings_per_rule_per_file: int = 20
    max_evidence_per_kind: int = 50
    skip_dirs: FrozenSet[str] = DEFAULT_SKIP_DIRS
    # progress(done, total, current_path) - called from the worker thread
    progress: Optional[Callable[[int, int, str], None]] = None
    # cancel() -> True requests a clean stop between files
    cancel: Optional[Callable[[], bool]] = None
