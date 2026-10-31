"""Log and traceback analysis (Phase 6). Line numbers come only from the log text; nothing is fabricated."""
from __future__ import annotations

from pathlib import Path
from typing import List

from findings_plugin import LogAnalysis, analyze_log_file as _file, analyze_log_text as _text


def analyze_log(text: str, source_name: str = "log") -> LogAnalysis:
    return _text(text, source_name)


def analyze_traceback(text: str, source_name: str = "traceback") -> List:
    """Findings parsed from a traceback / test output / log text."""
    return list(_text(text, source_name).findings)


def analyze_log_path(path) -> LogAnalysis:
    return _file(Path(path))
