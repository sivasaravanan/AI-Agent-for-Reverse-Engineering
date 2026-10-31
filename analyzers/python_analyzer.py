"""Python analyzer adapter (Phase 3): AST parsing (built in), Ruff, optional Pyright."""
from __future__ import annotations

from analyzers.base import PluginBackedAnalyzer


class PythonAnalyzer(PluginBackedAnalyzer):
    name = "python"
    languages = ("Python",)
    plugin_languages = ("python",)
    tools = ("ruff", "pyright")
    builtin = True
