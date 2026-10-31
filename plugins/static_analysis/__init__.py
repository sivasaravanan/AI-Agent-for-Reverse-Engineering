r"""Phase 3 plugin - language-specific static analysis for the AI Software Engineering Agent.

Drop this folder into <project>/plugins/ and use ONE of:

    from plugins.static_analysis import analyze, Options
    report = analyze(r"C:\path\project.zip", Options())          # core API, no GUI needed

    from plugins.static_analysis import attach_tab
    attach_tab(notebook)                                          # adds a Tkinter tab to a ttk.Notebook
"""
from .models import AnalysisReport, Finding, Options, ToolRun, ALL_LANGUAGES, KINDS, SEVERITIES
from .runner import ArchiveRejected, analyze, available_tools, register_adapter
from .base import Adapter

PLUGIN = {"name": "static_analysis", "title": "Static Analysis (Phase 3)", "version": "0.1.0",
          "api": ["analyze", "available_tools", "register_adapter", "attach_tab"]}

__all__ = ["analyze", "available_tools", "register_adapter", "attach_tab", "Adapter", "AnalysisReport",
           "Finding", "Options", "ToolRun", "ArchiveRejected", "PLUGIN", "ALL_LANGUAGES", "KINDS", "SEVERITIES"]


def attach_tab(notebook, get_source=None, title: str = "Static Analysis"):
    """Add the analysis tab to an existing ttk.Notebook. `get_source` is an optional callable returning the
    path (ZIP/folder) currently loaded in the host app, used to pre-fill the source box."""
    from .ui_tab import StaticAnalysisTab  # imported lazily so the core works without Tkinter
    tab = StaticAnalysisTab(notebook, get_source=get_source)
    notebook.add(tab, text=title)
    return tab
