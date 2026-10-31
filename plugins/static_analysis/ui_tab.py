"""Optional Tkinter tab. Analysis runs in a worker thread; the UI only polls a queue (thread-safe)."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .models import ALL_LANGUAGES, Options
from .runner import ArchiveRejected, analyze, available_tools

SEV_TAGS = {"critical": "#b00020", "high": "#c62828", "medium": "#b26a00", "low": "#555555", "info": "#777777"}


class StaticAnalysisTab(ttk.Frame):
    def __init__(self, master, get_source=None):
        super().__init__(master)
        self._get_source = get_source
        self._q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._thread = None
        self._report = None
        self._build()
        self.after(150, self._poll)

    # ---- layout -----------------------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="Source (ZIP or folder):").pack(side="left")
        self.src = tk.StringVar(value=(self._get_source() or "") if self._get_source else "")
        ttk.Entry(top, textvariable=self.src).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="ZIP...", command=self._pick_zip).pack(side="left")
        ttk.Button(top, text="Folder...", command=self._pick_dir).pack(side="left", padx=(4, 0))

        opt = ttk.LabelFrame(self, text="Analyzers")
        opt.pack(fill="x", padx=8, pady=4)
        self.lang_vars = {}
        for lang in ALL_LANGUAGES:
            v = tk.BooleanVar(value=True)
            self.lang_vars[lang] = v
            ttk.Checkbutton(opt, text=lang, variable=v).pack(side="left", padx=4)
        self.pyright = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="pyright", variable=self.pyright).pack(side="left", padx=4)
        ttk.Label(opt, text="SQL dialect:").pack(side="left", padx=(12, 2))
        self.dialect = tk.StringVar(value="ansi")
        ttk.Combobox(opt, textvariable=self.dialect, width=10, values=(
            "ansi", "tsql", "postgres", "mysql", "sqlite", "oracle", "bigquery")).pack(side="left")

        ap = ttk.Frame(self)
        ap.pack(fill="x", padx=8)
        self.allow_dotnet = tk.BooleanVar(value=False)
        ttk.Checkbutton(ap, variable=self.allow_dotnet, text=(
            "Approve 'dotnet build' (runs MSBuild logic from the uploaded project ON THIS MACHINE; "
            "never restores packages)")).pack(side="left")

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        self.run_btn = ttk.Button(bar, text="Run analysis", command=self._run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self._cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Tools...", command=self._show_tools).pack(side="left", padx=4)
        ttk.Button(bar, text="Export JSON", command=lambda: self._export("json")).pack(side="left", padx=4)
        ttk.Button(bar, text="Export HTML", command=lambda: self._export("html")).pack(side="left")
        self.status = tk.StringVar(value="Idle.")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=12)
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=120)
        self.prog.pack(side="right")

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("severity", "kind", "file", "line", "rule", "analyzer", "conf", "message")
        self.tree = ttk.Treeview(pw, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (70, 110, 220, 50, 90, 90, 60, 500)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w", stretch=(c == "message"))
        for sev, colr in SEV_TAGS.items():
            self.tree.tag_configure(sev, foreground=colr)
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        pw.add(self.tree, weight=3)

        lower = ttk.Notebook(pw)
        self.detail = tk.Text(lower, height=6, wrap="word")
        self.runs_tree = ttk.Treeview(lower, columns=("analyzer", "status", "exit", "version", "note"),
                                      show="headings", height=6)
        for c, w in zip(("analyzer", "status", "exit", "version", "note"), (110, 90, 50, 150, 600)):
            self.runs_tree.heading(c, text=c.title())
            self.runs_tree.column(c, width=w, anchor="w")
        self.runs_tree.bind("<Double-1>", self._show_run)
        lower.add(self.detail, text="Finding detail")
        lower.add(self.runs_tree, text="Tool runs (double-click for command/output)")
        pw.add(lower, weight=1)

    # ---- actions ----------------------------------------------------------------------------
    def _pick_zip(self):
        p = filedialog.askopenfilename(filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")])
        if p:
            self.src.set(p)

    def _pick_dir(self):
        p = filedialog.askdirectory()
        if p:
            self.src.set(p)

    def _show_tools(self):
        lines = [f"{k:10s} {v or 'NOT FOUND (not installed by this plugin)'}" for k, v in available_tools().items()]
        messagebox.showinfo("External analyzers", "\n".join(lines), parent=self)

    def _run(self):
        src = self.src.get().strip()
        if not src:
            messagebox.showwarning("Static analysis", "Choose a ZIP file or folder first.", parent=self)
            return
        langs = {k for k, v in self.lang_vars.items() if v.get()}
        approved = set()
        if self.allow_dotnet.get() and langs & {"csharp", "vbnet"}:
            if not messagebox.askyesno(
                    "Approve dotnet build",
                    "'dotnet build' will execute build logic contained in the uploaded project on this computer "
                    "(MSBuild tasks, build events, analyzers). Only continue if you trust this project.\n\nContinue?",
                    icon="warning", parent=self):
                return
            approved.add("dotnet")
        opts = Options(languages=langs, approved_tools=approved, use_pyright=self.pyright.get(),
                       sql_dialect=self.dialect.get() or "ansi")
        self._cancel.clear()
        self.tree.delete(*self.tree.get_children())
        self.runs_tree.delete(*self.runs_tree.get_children())
        self.detail.delete("1.0", "end")
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog.start(12)
        self.status.set("Starting...")

        def work():
            try:
                rep = analyze(src, opts, progress=lambda m: self._q.put(("progress", m)), cancel=self._cancel)
                self._q.put(("done", rep))
            except (ArchiveRejected, FileNotFoundError) as exc:
                self._q.put(("error", str(exc)))
            except Exception as exc:  # unexpected: surface it, never crash the host app
                self._q.put(("error", f"Unexpected error: {type(exc).__name__}: {exc}"))

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _cancel_run(self):
        self._cancel.set()
        self.status.set("Cancelling...")

    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "progress":
                    self.status.set(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._idle()
                    self.status.set("Failed.")
                    messagebox.showerror("Static analysis", payload, parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _idle(self):
        self.prog.stop()
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _finish(self, rep):
        self._report = rep
        self._idle()
        for f in rep.findings:
            self.tree.insert("", "end", iid=f.id + str(len(self.tree.get_children())), tags=(f.severity,), values=(
                f.severity, f.kind, f.file, f.line or "", f.rule, f.analyzer, f.confidence, f.message))
        for t in rep.tool_runs:
            self.runs_tree.insert("", "end", values=(t.analyzer, t.status, "" if t.exit_code is None else t.exit_code,
                                                     t.version, t.note))
        s = rep.summary()
        self.status.set(f"{s['total']} findings. " + ("(partial - cancelled)" if self._cancel.is_set() else ""))

    def _show_detail(self, _evt=None):
        sel = self.tree.selection()
        if not sel or not self._report:
            return
        idx = self.tree.index(sel[0])
        f = self._report.findings[idx]
        self.detail.delete("1.0", "end")
        self.detail.insert("end", (
            f"[{f.severity}] {f.kind}  ({f.analyzer} {f.analyzer_version})\n{f.file}:{f.line or '?'}:{f.column or '?'}"
            f"  rule={f.rule or '-'}  confidence={f.confidence}  verification={f.verification}\n\n{f.message}\n"
            + (f"\nEvidence: {f.evidence}\n" if f.evidence else "")))

    def _show_run(self, _evt=None):
        sel = self.runs_tree.selection()
        if not sel or not self._report:
            return
        t = self._report.tool_runs[self.runs_tree.index(sel[0])]
        win = tk.Toplevel(self)
        win.title(f"{t.analyzer} - command and output")
        txt = tk.Text(win, wrap="none", width=120, height=30)
        txt.pack(fill="both", expand=True)
        txt.insert("end", f"Command: {' '.join(t.command)}\nExit code: {t.exit_code}\nDuration: {t.duration_s}s\n"
                          f"Note: {t.note}\n\n--- stdout ---\n{t.stdout}\n\n--- stderr ---\n{t.stderr}\n")

    def _export(self, fmt):
        if not self._report:
            messagebox.showinfo("Static analysis", "Run an analysis first.", parent=self)
            return
        p = filedialog.asksaveasfilename(defaultextension="." + fmt, filetypes=[(fmt.upper(), "*." + fmt)])
        if p:
            (self._report.write_json if fmt == "json" else self._report.write_html)(p)
            self.status.set(f"Saved {p}")
