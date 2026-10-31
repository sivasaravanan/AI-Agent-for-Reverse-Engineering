"""Java analyzer adapter (Phase 3): javac diagnostics when a JDK is installed."""
from __future__ import annotations

from analyzers.base import PluginBackedAnalyzer


class JavaAnalyzer(PluginBackedAnalyzer):
    name = "java"
    languages = ("Java",)
    plugin_languages = ("java",)
    tools = ("javac",)
    builtin = False
