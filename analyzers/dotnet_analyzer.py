"""C# / VB.NET analyzer adapter (Phase 3): dotnet build diagnostics; skipped unless the user approves it."""
from __future__ import annotations

from analyzers.base import PluginBackedAnalyzer


class DotNetAnalyzer(PluginBackedAnalyzer):
    name = "dotnet"
    languages = ("C#", "VB.NET")
    plugin_languages = ("csharp", "vbnet")
    tools = ("dotnet",)
    builtin = False
