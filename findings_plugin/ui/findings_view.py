"""Tkinter findings panel: filters, search, severity grouping, detail + related views."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from ..detail import format_detail
from ..logparse import analyze_log_file
from ..models import Finding, Kind, Severity
from ..report import findings_to_json_report, render_html_page
from ..store import FindingStore

SEV_COLORS = {"critical": "#8b0000", "high": "#c9302c", "medium": "#b8860b", "low": "#31708f", "info": "#666666"}
SEV_CHOICES = [("All severities", None), ("Low and above", Severity.LOW), ("Medium and above", Severity.MEDIUM),
               ("High and above", Severity.HIGH), ("Critical only", Severity.CRITICAL)]
ALL = "All"


class FindingsView(ttk.Frame):
    """Drop-in panel. Safe to update the store from any thread; the view refreshes on the Tk thread."""

    def __init__(self, master, store: FindingStore, *, on_open_file: Optional[Callable[[str, Optional[int]], None]] = None,
                 on_request_ai: Optional[Callable[[Finding], None]] = None, **kw) -> None:
        super().__init__(master, **kw)
        self.store, self.on_open_file, self.on_request_ai = store, on_open_file, on_request_ai
        self._q: "queue.Queue" = queue.Queue()
        self._closed = False
        self._debounce = None
        self._current: Optional[Finding] = None
        self._build()
        self._unsub = store.subscribe(lambda: self._q.put(("refresh", None)))
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.after(150, self._poll)
        self.refresh()

    # ---- construction -----------------------------------------------------
    def _build(self) -> None:
        self.search_var, self.sev_var = tk.StringVar(), tk.StringVar(value=SEV_CHOICES[0][0])
        self.cat_var, self.lang_var, self.kind_var = (tk.StringVar(value=ALL) for _ in range(3))
        self.group_var = tk.BooleanVar(value=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text="Search:").pack(side="left")
        ttk.Entry(bar, textvariable=self.search_var, width=22).pack(side="left", padx=(2, 8))
        self.search_var.trace_add("write", lambda *_: self._schedule_refresh())
        self.sev_cb = self._combo(bar, self.sev_var, [c[0] for c in SEV_CHOICES], 16)
        self.cat_cb = self._combo(bar, self.cat_var, [ALL], 12)
        self.lang_cb = self._combo(bar, self.lang_var, [ALL], 11)
        self.kind_cb = self._combo(bar, self.kind_var, [ALL] + [k.value for k in Kind], 14)
        for cb in (self.sev_cb, self.cat_cb, self.lang_cb, self.kind_cb):
            cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Checkbutton(bar, text="Group by severity", variable=self.group_var, command=self.refresh).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear filters", command=self.clear_filters).pack(side="left")
        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", padx=6, pady=2)
        ttk.Button(bar2, text="Analyze log file…", command=self._load_log).pack(side="left")
        ttk.Button(bar2, text="Export JSON…", command=lambda: self._export("json")).pack(side="left", padx=4)
        ttk.Button(bar2, text="Export HTML…", command=lambda: self._export("html")).pack(side="left")
        self.status = ttk.Label(bar2, text="")
        self.status.pack(side="right")

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=6, pady=4)
        top = ttk.Frame(pane)
        cols = ("sev", "id", "cat", "loc", "title", "kind")
        self.tree = ttk.Treeview(top, columns=cols, show="tree headings", selectmode="browse", height=10)
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=18, stretch=False)
        for c, t, w in (("sev", "Severity", 70), ("id", "ID", 80), ("cat", "Category", 90),
                        ("loc", "Location", 200), ("title", "Finding", 360), ("kind", "Kind", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "title"))
        for sev, color in SEV_COLORS.items():
            self.tree.tag_configure(sev, foreground=color)
        ys = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        pane.add(top, weight=3)

        bottom = ttk.Frame(pane)
        btns = ttk.Frame(bottom)
        btns.pack(fill="x")
        self.open_btn = ttk.Button(btns, text="Open file", command=self._open_file, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(btns, text="Copy details", command=self._copy).pack(side="left", padx=4)
        if self.on_request_ai:
            ttk.Button(btns, text="Ask AI to explain",
                       command=lambda: self._current and self.on_request_ai(self._current)).pack(side="left")
        self.detail = tk.Text(bottom, height=12, wrap="word", state="disabled", relief="flat", background="#fafafa")
        self.detail.tag_configure("h", font=("Segoe UI", 9, "bold"), spacing1=6)
        self.detail.tag_configure("n", background="#fff3cd")
        self.detail.tag_configure("m", font=("Consolas", 9), background="#f0f0f0")
        self.detail.pack(fill="both", expand=True, pady=(2, 2))
        self.rel = ttk.Treeview(bottom, columns=("id", "why", "title"), show="headings", height=3)
        for c, t, w in (("id", "Related", 80), ("why", "Why related", 190), ("title", "Finding", 400)):
            self.rel.heading(c, text=t)
            self.rel.column(c, width=w, stretch=(c == "title"))
        self.rel.pack(fill="x")
        self.rel.bind("<Double-1>", self._goto_related)
        pane.add(bottom, weight=2)

    @staticmethod
    def _combo(parent, var, values, width):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly")
        cb.pack(side="left", padx=2)
        return cb

    # ---- plumbing ---------------------------------------------------------
    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._closed = True
            self._unsub()

    def _poll(self) -> None:
        if self._closed:
            return
        need_refresh = False
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "refresh":
                    need_refresh = True
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        if need_refresh:
            self.refresh()
        try:
            self.after(150, self._poll)
        except tk.TclError:
            self._closed = True

    def _schedule_refresh(self) -> None:
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = self.after(200, self.refresh)

    # ---- public -----------------------------------------------------------
    def clear_filters(self) -> None:
        self.search_var.set("")
        self.sev_var.set(SEV_CHOICES[0][0])
        for v in (self.cat_var, self.lang_var, self.kind_var):
            v.set(ALL)
        self.refresh()

    def select(self, fid: str) -> None:
        if not self.tree.exists(fid):
            self.clear_filters()
        if self.tree.exists(fid):
            self.tree.selection_set(fid)
            self.tree.see(fid)

    def refresh(self) -> None:
        self._debounce = None
        self.cat_cb["values"] = [ALL] + self.store.categories()
        self.lang_cb["values"] = [ALL] + self.store.languages()
        min_sev = dict(SEV_CHOICES).get(self.sev_var.get())
        results = self.store.query(
            min_severity=min_sev, text=self.search_var.get(),
            categories=None if self.cat_var.get() == ALL else [self.cat_var.get()],
            languages=None if self.lang_var.get() == ALL else [self.lang_var.get()],
            kinds=None if self.kind_var.get() == ALL else [self.kind_var.get()])
        keep = self._current.id if self._current else None
        self.tree.delete(*self.tree.get_children())

        def ins(parent, f: Finding) -> None:
            self.tree.insert(parent, "end", iid=f.id, tags=(f.severity.value,), values=(
                f.severity.value.upper(), f.id, f.category, f.location or "-", f.title, f.kind.value))
        if self.group_var.get():
            for sev, items in self.store.group_by_severity(results).items():
                gid = f"grp:{sev.value}"
                self.tree.insert("", "end", iid=gid, open=True, tags=(sev.value,),
                                 values=(sev.value.upper(), "", "", "", f"{len(items)} finding(s)", ""))
                for f in items:
                    ins(gid, f)
        else:
            for f in results:
                ins("", f)
        c = self.store.counts()
        self.status.configure(text=f"Showing {len(results)} of {len(self.store)}  |  " +
                              ", ".join(f"{v} {k}" for k, v in c.items() if v))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    # ---- detail -----------------------------------------------------------
    def _on_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        f = self.store.get(sel[0]) if sel and not sel[0].startswith("grp:") else None
        self._current = f
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.rel.delete(*self.rel.get_children())
        self.open_btn.configure(state="normal" if f and f.file_path and self.on_open_file else "disabled")
        if f:
            related = self.store.related(f.id)
            for head, body in format_detail(f, related):
                if head.startswith("!"):
                    self.detail.insert("end", body + "\n", "n")
                    continue
                self.detail.insert("end", head + "\n", "h")
                self.detail.insert("end", body + "\n", "m" if head == "Evidence" else ())
            for r, why in related:
                self.rel.insert("", "end", iid=r.id, values=(r.id, why, r.title))
        self.detail.configure(state="disabled")

    def _goto_related(self, _evt=None) -> None:
        sel = self.rel.selection()
        if sel:
            self.select(sel[0])

    def _open_file(self) -> None:
        if self._current and self._current.file_path and self.on_open_file:
            self.on_open_file(self._current.file_path, self._current.line)

    def _copy(self) -> None:
        text = self.detail.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    # ---- actions ----------------------------------------------------------
    def _load_log(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Select log / test output",
                                          filetypes=[("Logs", "*.log *.txt *.out"), ("All files", "*.*")])
        if not path:
            return
        self.status.configure(text="Analyzing log...")

        def work() -> None:
            try:
                res = analyze_log_file(path)
                self._q.put(("call", lambda: self._log_done(res)))
            except Exception as exc:  # surface to the user instead of dying inside the thread
                err = f"{type(exc).__name__}: {exc}"
                self._q.put(("call", lambda: self._log_failed(err)))
        threading.Thread(target=work, daemon=True).start()

    def _log_failed(self, err: str) -> None:
        messagebox.showerror("Log analysis failed", err, parent=self)
        self.refresh()

    def _log_done(self, res) -> None:
        added = self.store.add_many(res.findings)
        self.refresh()
        messagebox.showinfo("Log analysis", f"{added} new finding(s) from {res.line_count} log lines.\n\n" +
                            "\n".join(res.notes), parent=self)

    def _export(self, fmt: str) -> None:
        ext = ".json" if fmt == "json" else ".html"
        path = filedialog.asksaveasfilename(parent=self, defaultextension=ext, filetypes=[(fmt.upper(), "*" + ext)])
        if not path:
            return
        items = self.store.query(min_severity=dict(SEV_CHOICES).get(self.sev_var.get()), text=self.search_var.get())
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(findings_to_json_report(items) if fmt == "json" else render_html_page(items))
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
