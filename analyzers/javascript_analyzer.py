"""JavaScript/TypeScript analyzer adapter (Phase 3): ESLint and the TypeScript compiler when installed."""
from __future__ import annotations

from analyzers.base import PluginBackedAnalyzer


class JavaScriptAnalyzer(PluginBackedAnalyzer):
    name = "javascript"
    languages = ("JavaScript", "TypeScript")
    plugin_languages = ("javascript", "typescript")
    tools = ("eslint", "tsc")
    builtin = False
