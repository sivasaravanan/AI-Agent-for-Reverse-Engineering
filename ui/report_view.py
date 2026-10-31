"""Phase 9: report preview (plain-text rendering of the analysis) with open/save actions."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from core.report_preview import build_text_report
from models.analysis_result import AnalysisResult


class ReportView(ttk.Frame):
    def __init__(self, parent, open_report: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(parent, **kwargs)
        self._open = open_report
        self.result: Optional[AnalysisResult] = None
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        self.html_btn = ttk.Button(bar, text="Open HTML report", command=lambda: self._call("html"), state="disabled")
        self.html_btn.pack(side="left")
        self.json_btn = ttk.Button(bar, text="Open JSON report", command=lambda: self._call("json"), state="disabled")
        self.json_btn.pack(side="left", padx=3)
        self.save_btn = ttk.Button(bar, text="Save text summary...", command=self.save_text, state="disabled")
        self.save_btn.pack(side="left")
        self.text = tk.Text(self, wrap="none", font=("Consolas", 10), state="disabled")
        sy = ttk.Scrollbar(self, command=self.text.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sx.pack(side="bottom", fill="x")
        sy.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

    def _call(self, kind: str) -> None:
        if self._open:
            self._open(kind)

    def set_text(self, value: str) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.config(state="disabled")

    def show(self, result: AnalysisResult, has_html: bool = True, has_json: bool = True) -> None:
        self.result = result
        self.set_text(build_text_report(result))
        self.html_btn.config(state="normal" if has_html else "disabled")
        self.json_btn.config(state="normal" if has_json else "disabled")
        self.save_btn.config(state="normal")

    def save_text(self) -> None:
        if not self.result:
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                            initialfile="analysis_summary.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(build_text_report(self.result, max_findings=100000))
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
