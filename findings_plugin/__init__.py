"""Findings plugin - Phase 6 of the AI Software Engineering Agent.

    from findings_plugin import register
    plugin = register(notebook)          # adds a "Findings" tab
    plugin.add(old_style_findings)       # dicts / objects / SyntaxError / str
    plugin.analyze_log_file("run.log")   # tracebacks, test output, ERROR lines
"""
from .adapters import coerce_finding, coerce_many, from_syntax_error
from .logparse import LogAnalysis, analyze_log_file, analyze_log_text
from .models import Category, Confidence, Finding, Kind, Severity, Verification, make_ai_finding
from .plugin import PLUGIN_INFO, FindingsPlugin, register
from .store import FindingStore

__version__ = PLUGIN_INFO["version"]
__all__ = ["register", "FindingsPlugin", "FindingStore", "Finding", "Severity", "Kind", "Confidence",
           "Verification", "Category", "make_ai_finding", "coerce_finding", "coerce_many",
           "from_syntax_error", "analyze_log_text", "analyze_log_file", "LogAnalysis", "PLUGIN_INFO"]
