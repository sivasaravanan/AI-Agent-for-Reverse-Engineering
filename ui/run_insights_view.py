"""Run Insights tab: timeline, located diagnostics with source lines, call trace and hints for a sandboxed run."""
from __future__ import annotations

import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Callable, Optional

from run_insights import RunTrace, build_trace, render_html, render_text, source_context


class RunInsightsView(ttk.Frame):
    def __init__(self, parent, get_source: Optional[Callable[[], str]] = None, out_dir=None, **kw):
        super().__init__(parent, **kw)
        self.get_source = get_source or (lambda: "")
        self.out_dir = Path(out_dir) if out_dir else None
        self.trace: Optional[RunTrace] = None
        self._source = ""
        top = ttk.Frame(self, padding=(0, 6))
        top.pack(fill="x")
        self.summary = tk.StringVar(value="Run a command on the Build & Test tab; the trace appears here.")
        ttk.Label(top, textvariable=self.summary, font=("Segoe UI", 10, "bold"), wraplength=900).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Copy as text", command=self._copy).pack(side="right")
        ttk.Button(top, text="Save trace (HTML)...", command=self._save).pack(side="right", padx=6)
        pan = ttk.PanedWindow(self, orient="vertical")
        pan.pack(fill="both", expand=True)
        cols = ("sev", "tool", "code", "loc", "msg")
        self.diag = ttk.Treeview(pan, columns=cols, show="headings", height=7, selectmode="browse")
        for c, t, w in (("sev", "Severity", 70), ("tool", "Tool", 80), ("code", "Code", 110), ("loc", "Location", 240), ("msg", "Message", 520)):
            self.diag.heading(c, text=t)
            self.diag.column(c, width=w, anchor="w", stretch=c == "msg")
        self.diag.tag_configure("error", foreground="#b91c1c")
        self.diag.tag_configure("warning", foreground="#b45309")
        self.diag.bind("<<TreeviewSelect>>", self._on_diag)
        pan.add(self.diag, weight=2)
        mid = ttk.PanedWindow(pan, orient="horizontal")
        self.src = tk.Text(mid, height=10, wrap="none", font=("Consolas", 10), state="disabled")
        self.src.tag_configure("hit", background="#fde8e8")
        self.src.tag_configure("note", foreground="#6b7280")
        mid.add(self.src, weight=3)
        self.frames = ttk.Treeview(mid, columns=("loc", "fn"), show="headings", height=6, selectmode="browse")
        self.frames.heading("loc", text="Call trace (oldest first)")
        self.frames.heading("fn", text="Function")
        self.frames.column("loc", width=220)
        self.frames.column("fn", width=120)
        self.frames.bind("<<TreeviewSelect>>", self._on_frame)
        mid.add(self.frames, weight=1)
        pan.add(mid, weight=2)
        self.notes = tk.Text(pan, height=8, wrap="word", state="disabled")
        pan.add(self.notes, weight=2)
        self._frame_data = {}

    # ------------------------------------------------------------------ API
    def show_result(self, result) -> None:
        """Called from the Build & Test tab when a run finishes (Tk main thread)."""
        self._source = (self.get_source() or "")
        self.load_trace(build_trace(result))

    def load_trace(self, t: RunTrace) -> None:
        self.trace = t
        self.summary.set(t.summary)
        self.diag.delete(*self.diag.get_children())
        self._frame_data.clear()
        for i, d in enumerate(t.diagnostics):
            self.diag.insert("", "end", iid=str(i), tags=(d.severity,), values=(d.severity, d.tool, d.code, d.location, d.message[:300]))
        lines = ["TIMELINE"] + [f"  {e['label']}: {e['detail']}" for e in t.events] + ["", "HINTS (heuristics, not verified)"]
        lines += [f"  [{h['level']}] {h['text']}" for h in t.hints] or ["  (none)"]
        self._set(self.notes, "\n".join(lines))
        self._clear_src("Select a diagnostic to see its source lines.")
        self.frames.delete(*self.frames.get_children())
        kids = self.diag.get_children()
        if kids:
            self.diag.selection_set(kids[0])

    # ------------------------------------------------------------------ events
    def _on_diag(self, _e=None) -> None:
        sel = self.diag.selection()
        if not sel or not self.trace:
            return
        d = self.trace.diagnostics[int(sel[0])]
        self.frames.delete(*self.frames.get_children())
        self._frame_data.clear()
        for k, (f, ln, fn) in enumerate(d.frames):
            iid = f"f{k}"
            self._frame_data[iid] = (f, ln)
            self.frames.insert("", "end", iid=iid, values=(f"{f}:{ln}", fn))
        self._show_source(d.file, d.line)

    def _on_frame(self, _e=None) -> None:
        sel = self.frames.selection()
        if sel and sel[0] in self._frame_data:
            self._show_source(*self._frame_data[sel[0]])

    def _show_source(self, rel: str, line: int) -> None:
        c = source_context(self._source, rel, line)
        self.src.config(state="normal")
        self.src.delete("1.0", "end")
        if not c["lines"]:
            self.src.insert("end", c["note"] or "No source lines available.", "note")
        else:
            self.src.insert("end", f"{c['path']}\n", "note")
            for x in c["lines"]:
                self.src.insert("end", f"{x['n']:>5}  {x['text']}\n", "hit" if x["target"] else "")
        self.src.config(state="disabled")

    def _clear_src(self, msg: str) -> None:
        self.src.config(state="normal")
        self.src.delete("1.0", "end")
        self.src.insert("end", msg, "note")
        self.src.config(state="disabled")

    @staticmethod
    def _set(widget: tk.Text, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.config(state="disabled")

    def _copy(self) -> None:
        if self.trace:
            self.clipboard_clear()
            self.clipboard_append(render_text(self.trace))

    def _save(self) -> None:
        if not self.trace:
            messagebox.showinfo("Run trace", "There is no run yet.", parent=self)
            return
        init = str(self.out_dir) if self.out_dir else None
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".html", initialdir=init,
                                         initialfile=f"run_trace_{datetime.now():%Y%m%d_%H%M%S}.html",
                                         filetypes=[("HTML", "*.html")])
        if p:
            Path(p).write_text(render_html(self.trace, self._source), encoding="utf-8")
            webbrowser.open(Path(p).resolve().as_uri())
