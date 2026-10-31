"""Portfolio tab: analyse a folder of ZIP archives and compare them (new tab; existing tabs are unchanged)."""
from __future__ import annotations

import os
import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Callable, Dict, Optional

from core.analysis_options import AnalysisOptions
from security.archive_validation import ArchiveLimits

COLUMNS = (("project", "Project", 260), ("verdict", "Verdict", 70), ("risk", "Risk", 50), ("files", "Files", 55),
           ("crit", "Crit", 45), ("high", "High", 45), ("secrets", "Secrets", 60), ("similar", "Most similar", 200),
           ("why", "Why", 360))


def _portfolio_worker(folder, out_dir, profile, recursive, limits, profiles, cancel, q) -> None:
    """Thread body. Deliberately holds no Tk object (see ui.setup_dialog)."""
    from institution.portfolio import analyze_portfolio, write_portfolio_reports
    try:
        res = analyze_portfolio(
            folder, out_dir, profile=profile, recursive=recursive, limits=limits, options=AnalysisOptions(),
            profiles=profiles, cancel=cancel, progress=lambda f, m: q.put(("progress", f, m)),
            row_done=lambda r: q.put(("row", r)))
        paths = write_portfolio_reports(res, out_dir)
        q.put(("done", res, paths))
    except Exception as exc:  # noqa: BLE001
        q.put(("error", f"{type(exc).__name__}: {str(exc)[:200]}"))


class PortfolioView(ttk.Frame):
    def __init__(self, parent, out_dir, limits: Optional[ArchiveLimits] = None, profiles_path: Optional[Path] = None,
                 default_folder: str = "", **kwargs):
        super().__init__(parent, **kwargs)
        from institution.portfolio import load_profiles
        self.out_dir = Path(out_dir)
        self.limits = limits
        self.profiles, self._profile_warnings = load_profiles(profiles_path)
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._result = None
        self._paths: Dict[str, Path] = {}
        self._row_reports: Dict[str, str] = {}

        top = ttk.Frame(self, padding=(0, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Folder of ZIPs:").pack(side="left")
        self.folder_var = tk.StringVar(value=default_folder)
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse...", command=self._browse).pack(side="left")
        opts = ttk.Frame(self, padding=(0, 2))
        opts.pack(fill="x")
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Include sub-folders", variable=self.recursive_var).pack(side="left")
        ttk.Label(opts, text="Profile:").pack(side="left", padx=(12, 2))
        self.profile_var = tk.StringVar(value="general")
        self.profile_box = ttk.Combobox(opts, textvariable=self.profile_var, state="readonly", width=14,
                                        values=sorted(self.profiles))
        self.profile_box.pack(side="left")
        self.start_btn = ttk.Button(opts, text="Analyze folder", command=self.start)
        self.start_btn.pack(side="left", padx=10)
        self.cancel_btn = ttk.Button(opts, text="Cancel", command=self._cancel.set, state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_html_btn = ttk.Button(opts, text="Open portfolio report", command=lambda: self._open("html"), state="disabled")
        self.open_html_btn.pack(side="right")
        self.open_csv_btn = ttk.Button(opts, text="Open CSV", command=lambda: self._open("csv"), state="disabled")
        self.open_csv_btn.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", pady=4)
        self.status_var = tk.StringVar(value="Choose a folder that contains project ZIP files. Nothing from them is extracted or run.")
        ttk.Label(self, textvariable=self.status_var).pack(fill="x")
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=key in ("project", "why"))
        sy = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        sx = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._open_row)
        for verdict, color in (("PASS", "#15803d"), ("REVIEW", "#b45309"), ("FAIL", "#b91c1c")):
            self.tree.tag_configure(verdict, foreground=color)
        self.after(150, self._poll)

    # ------------------------------------------------------------------ actions
    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Folder that contains the project ZIP files")
        if d:
            self.folder_var.set(d)

    def start(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showinfo("Portfolio", "Choose an existing folder that contains ZIP files.")
            return
        if self._worker and self._worker.is_alive():
            return
        # read every Tk variable HERE; the worker thread must not touch Tk objects
        profile, recursive = self.profile_var.get(), bool(self.recursive_var.get())
        self.tree.delete(*self.tree.get_children())
        self._row_reports.clear()
        self._cancel.clear()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_html_btn.configure(state="disabled")
        self.open_csv_btn.configure(state="disabled")
        self.status_var.set("Scanning folder...")
        self._worker = threading.Thread(target=_portfolio_worker, daemon=True, args=(
            folder, self.out_dir, profile, recursive, self.limits, self.profiles, self._cancel, self._q))
        self._worker.start()

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.progress["value"] = msg[1] * 100
                    self.status_var.set(msg[2])
                elif kind == "row":
                    self._add_row(msg[1])
                elif kind == "done":
                    self._finish(msg[1], msg[2])
                elif kind == "error":
                    self._idle(f"Portfolio analysis failed: {msg[1]}")
        except queue.Empty:
            pass
        try:
            self.after(150, self._poll)
        except tk.TclError:
            pass                                    # window closed

    def _add_row(self, r) -> None:
        top = r.similar_to[0] if r.similar_to else None
        iid = self.tree.insert("", "end", tags=(r.verdict,), values=(
            r.name, r.verdict, r.risk_score, r.files, r.severity_counts.get("critical", 0),
            r.severity_counts.get("high", 0), r.secrets,
            f"{top['name']} ({int(round(top['similarity'] * 100))}%)" if top else "", "; ".join(r.reasons)))
        if r.report_html:
            self._row_reports[iid] = r.report_html

    def _finish(self, res, paths) -> None:
        self._result, self._paths = res, paths
        t = res.totals
        self._idle(f"Done: {t.get('analysed', 0)} of {t.get('archives', 0)} analysed. Verdicts: {t.get('verdicts', {})}. "
                   f"Reports in {self.out_dir}")
        self.open_html_btn.configure(state="normal")
        self.open_csv_btn.configure(state="normal")

    def _idle(self, status: str) -> None:
        self.status_var.set(status)
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.progress["value"] = 0

    def _open(self, kind: str) -> None:
        p = self._paths.get(kind)
        if p and Path(p).is_file():
            try:
                os.startfile(str(p)) if hasattr(os, "startfile") else webbrowser.open(Path(p).as_uri())   # noqa: S606
            except OSError as exc:
                messagebox.showerror("Portfolio", f"Could not open the file: {exc}")

    def _open_row(self, _evt=None) -> None:
        sel = self.tree.selection()
        rel = self._row_reports.get(sel[0]) if sel else None
        if rel:
            webbrowser.open((self.out_dir / rel).resolve().as_uri())
