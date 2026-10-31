"""SQL analyzer adapter (Phase 3): sqlfluff when installed."""
from __future__ import annotations

from analyzers.base import PluginBackedAnalyzer


class SqlAnalyzer(PluginBackedAnalyzer):
    name = "sql"
    languages = ("SQL",)
    plugin_languages = ("sql",)
    tools = ("sqlfluff",)
    builtin = False
