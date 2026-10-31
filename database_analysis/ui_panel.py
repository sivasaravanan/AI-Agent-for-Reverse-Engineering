"""Optional Tkinter tab for the main window. Import is lazy so headless use never needs Tk.

    from database_analysis.ui_panel import add_database_tab
    panel = add_database_tab(notebook)               # adds a "Databases" tab
    panel.analyze_async(zip_path)                    # runs off the UI thread
    panel.show(result)                               # or show an existing result
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from .models import DatabaseAnalysisResult
from .options import AnalysisOptions
from .plugin import run


def add_database_tab(notebook, text: str = "Databases") -> "DatabasePanel":
    panel = DatabasePanel(notebook)
    notebook.add(panel, text=text)
    return panel


def _base():
    import tkinter.ttk as ttk
    return ttk.Frame


class DatabasePanel(_base()):  # type: ignore[misc]
    def __init__(self, master, on_status: Optional[Callable[[str], None]] = None):
        import tkinter as tk
        from tkinter import ttk
        super().__init__(master)
        self._tk, self._ttk = tk, ttk
        self._on_status = on_status or (lambda s: None)
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self.result: Optional[DatabaseAnalysisResult] = None
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        self.status = ttk.Label(top, text="No analysis yet. Static analysis only - nothing is executed.")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(top, length=160, mode="determinate")
        self.progress.pack(side="right")
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=6, pady=4)
        self.dbs = ttk.Treeview(pw, columns=("level", "evidence"), height=6)
        for c, t, w in (("#0", "Database", 200), ("level", "Evidence level", 110), ("evidence", "Evidence", 520)):
            self.dbs.heading(c, text=t)
            self.dbs.column(c, width=w, anchor="w")
        self.finds = ttk.Treeview(pw, columns=("sev", "title", "loc"), height=8)
        for c, t, w in (("#0", "ID", 90), ("sev", "Severity", 80), ("title", "Title", 380), ("loc", "Location", 300)):
            self.finds.heading(c, text=t)
            self.finds.column(c, width=w, anchor="w")
        self.detail = tk.Text(pw, height=7, wrap="word", state="disabled")
        pw.add(self.dbs, weight=1)
        pw.add(self.finds, weight=2)
        pw.add(self.detail, weight=1)
        self.finds.bind("<<TreeviewSelect>>", self._on_select)

    # ------------------------------------------------------------- API
    def analyze_async(self, source, options: Optional[AnalysisOptions] = None) -> None:
        self._cancel.clear()
        opts = options or AnalysisOptions()
        opts.cancel = self._cancel.is_set
        opts.progress = lambda d, t, p: self._q.put(("progress", d, t))
        self.status.config(text="Analyzing...")

        def work():
            try:
                self._q.put(("done", run(source, opts)))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, never crashes the UI
                self._q.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def cancel(self) -> None:
        self._cancel.set()

    def show(self, result: DatabaseAnalysisResult) -> None:
        self.result = result
        self.dbs.delete(*self.dbs.get_children())
        self.finds.delete(*self.finds.get_children())
        for d in result.databases:
            self.dbs.insert("", "end", text=d.display, values=(d.status, d.rationale))
        for f in result.findings:
            self.finds.insert("", "end", iid=f.id, text=f.id,
                              values=(f.severity, f.title, f"{f.file or ''}:{f.line or ''}"))
        self.status.config(text=f"{len(result.databases)} database(s), {len(result.findings)} finding(s)"
                                + (" (cancelled)" if result.cancelled else ""))
        self._on_status(self.status.cget("text"))

    # ------------------------------------------------------------- internals
    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                if msg[0] == "progress":
                    self.progress.config(maximum=max(msg[2], 1), value=msg[1])
                elif msg[0] == "done":
                    self.show(msg[1])
                    return
                elif msg[0] == "error":
                    self.status.config(text=f"Analysis failed: {msg[1]}")
                    return
        except queue.Empty:
            self.after(100, self._poll)

    def _on_select(self, _evt) -> None:
        sel = self.finds.selection()
        if not sel or not self.result:
            return
        f = next((x for x in self.result.findings if x.id == sel[0]), None)
        if not f:
            return
        text = (f"{f.title}\n{f.file}:{f.line}\nSeverity: {f.severity}   Confidence: {f.confidence}\n"
                f"Evidence: {f.evidence}\n\n{f.explanation}\n\nRemediation: {f.remediation}\n"
                f"Verification: {f.verification_status}")
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.config(state="disabled")
