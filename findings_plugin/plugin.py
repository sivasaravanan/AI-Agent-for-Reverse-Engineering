"""Public entry point: `from findings_plugin import register`."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from .adapters import coerce_many
from .logparse import LogAnalysis, analyze_log_file, analyze_log_text
from .models import Finding
from .report import findings_to_json_report, render_html_page, render_html_section
from .store import FindingStore

PLUGIN_INFO = {"name": "findings", "title": "Findings", "version": "1.0.0", "phase": 6,
               "requires": "Python 3.11+, standard library only (tkinter only for the optional UI)"}


class FindingsPlugin:
    """Facade the main app talks to. Works headless; mount() adds the Tk panel."""

    def __init__(self, store: Optional[FindingStore] = None) -> None:
        self.store = store or FindingStore()
        self.view = None
        self.last_errors: list = []

    # -- feeding findings
    def add(self, items: Iterable[Any], source: str = "legacy") -> int:
        """Accept Finding objects, dicts, SyntaxErrors or strings. Returns number newly added.
        Items that cannot be converted are listed in `last_errors` (not silently dropped)."""
        findings, self.last_errors = coerce_many(items, default_source=source)
        return self.store.add_many(findings)

    def analyze_log_text(self, text: str, source_name: str = "log") -> LogAnalysis:
        res = analyze_log_text(text, source_name)
        self.store.add_many(res.findings)
        return res

    def analyze_log_file(self, path: str | Path) -> LogAnalysis:
        res = analyze_log_file(path)
        self.store.add_many(res.findings)
        return res

    # -- UI
    def mount(self, parent, *, tab_text: str = "Findings", on_open_file=None, on_request_ai=None,
              pack: bool = True, **frame_kw):
        """Create the panel in `parent`. If parent is a ttk.Notebook it becomes a tab."""
        from tkinter import ttk  # lazy: headless use never needs Tk
        from .ui.findings_view import FindingsView
        self.view = FindingsView(parent, self.store, on_open_file=on_open_file, on_request_ai=on_request_ai, **frame_kw)
        if isinstance(parent, ttk.Notebook):
            parent.add(self.view, text=tab_text)
        elif pack:
            self.view.pack(fill="both", expand=True)
        return self.view

    # -- output
    def export_json(self, path: str | Path, meta: Optional[dict] = None) -> None:
        Path(path).write_text(findings_to_json_report(self.store.all(), meta), encoding="utf-8")

    def export_html(self, path: str | Path, title: str = "Findings Report", meta: Optional[dict] = None) -> None:
        Path(path).write_text(render_html_page(self.store.all(), title, meta), encoding="utf-8")

    def html_section(self, heading: str = "Findings") -> str:
        """HTML fragment for embedding in the main app's existing report."""
        return render_html_section(self.store.all(), heading)


def register(host=None, *, store: Optional[FindingStore] = None, **mount_kw) -> FindingsPlugin:
    """One-call integration. `register()` is headless; `register(notebook)` adds a tab."""
    plugin = FindingsPlugin(store)
    if host is not None:
        plugin.mount(host, **mount_kw)
    return plugin
