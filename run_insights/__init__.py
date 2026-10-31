"""Run insights: turns the output of a sandboxed build/test run into a trace, located diagnostics and plain-language hints.

Read-only and offline: it only parses text that a run already produced and reads source lines from the ZIP/folder.
Hints are rule-based heuristics, not verified diagnoses.
"""
from run_insights.hints import hints_for  # noqa: F401
from run_insights.parsers import Diagnostic, parse_output  # noqa: F401
from run_insights.trace import RunTrace, build_trace, render_html, render_text, source_context  # noqa: F401
