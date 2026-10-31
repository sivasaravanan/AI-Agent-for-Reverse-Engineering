"""Phase 9: findings grid with severity filters, category filter, search and a detail panel."""
from __future__ import annotations

import tkinter as tk
from collections import Counter
from tkinter import ttk
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from models.finding import SEVERITIES, Finding


def filter_findings(findings: Iterable[Finding], severities: Optional[Iterable[str]] = None,
                    category: str = "", text: str = "") -> List[Finding]:
    """Pure filtering (no Tk). ``severities=None`` means all; ``text`` matches title/file/rule/evidence."""
    sev = set(severities) if severities is not None else None
    needle = text.strip().lower()
    out = []
    for f in findings:
        if sev is not None and f.severity not in sev:
            continue
        if category and f.category != category:
            continue
        if needle and needle not in " ".join(str(x or "") for x in (f.title, f.file, f.rule_id, f.evidence, f.id)).lower():
            continue
        out.append(f)
    return out


def format_detail(f: Finding) -> str:
    return (f"{f.id}  [{f.severity.upper()}]  {f.title}\n\nFile: {f.file}:{f.line or '-'}"
            f"{'  column ' + str(f.column) if f.column else ''}\nCategory: {f.category}   Rule: {f.rule_id}\n"
            f"Source: {f.source}\nConfidence: {f.confidence}   Verification: {f.verification}\n\n"
            f"Evidence:\n  {f.evidence}\n\nExplanation:\n  {f.explanation}\n\nSuggested remediation:\n  {f.remediation}\n")


class FindingsView(ttk.Frame):
    COLUMNS = (("id", "ID", 70), ("severity", "Severity", 70), ("category", "Category", 90), ("title", "Title", 330),
               ("loc", "Location", 230), ("conf", "Confidence", 80), ("ver", "Verification", 140))

    def __init__(self, parent, on_select: Optional[Callable[[Optional[Finding]], None]] = None, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_select = on_select
        self._all: List[Finding] = []
        self._by_iid: Dict[str, Finding] = {}
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        self.sev_vars: Dict[str, tk.BooleanVar] = {}
        self.sev_boxes: Dict[str, ttk.Checkbutton] = {}
        for s in SEVERITIES:
            v = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(bar, text=s, variable=v, command=self.refresh)
            cb.pack(side="left", padx=2)
            self.sev_vars[s], self.sev_boxes[s] = v, cb
        ttk.Label(bar, text="Category:").pack(side="left", padx=(12, 2))
        self.cat_var = tk.StringVar(value="All")
        self.cat_box = ttk.Combobox(bar, textvariable=self.cat_var, state="readonly", width=14, values=["All"])
        self.cat_box.pack(side="left")
        self.cat_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(bar, text="Search:").pack(side="left", padx=(12, 2))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.search_var, width=28)
        entry.pack(side="left")
        self.search_var.trace_add("write", lambda *_: self.refresh())
        self.count_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.count_var).pack(side="right")
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        pane.add(top, weight=3)
        self.grid_tree = ttk.Treeview(top, columns=[c[0] for c in self.COLUMNS], show="headings")
        for key, title, width in self.COLUMNS:
            self.grid_tree.heading(key, text=title)
            self.grid_tree.column(key, width=width, anchor="w")
        sb = ttk.Scrollbar(top, orient="vertical", command=self.grid_tree.yview)
        self.grid_tree.configure(yscrollcommand=sb.set)
        self.grid_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.grid_tree.bind("<<TreeviewSelect>>", self._selected)
        self.detail = tk.Text(pane, height=10, wrap="word", state="disabled")
        pane.add(self.detail, weight=2)

    def show(self, findings: Sequence[Finding]) -> None:
        self._all = list(findings)
        counts = Counter(f.severity for f in self._all)
        for s, cb in self.sev_boxes.items():
            cb.config(text=f"{s} ({counts[s]})")
            self.sev_vars[s].set(True)
        cats = sorted({f.category for f in self._all})
        self.cat_box.config(values=["All"] + cats)
        self.cat_var.set("All")
        self.search_var.set("")
        self.refresh()

    def current_filter(self) -> List[Finding]:
        sev = [s for s, v in self.sev_vars.items() if v.get()]
        cat = "" if self.cat_var.get() == "All" else self.cat_var.get()
        return filter_findings(self._all, sev, cat, self.search_var.get())

    def refresh(self) -> None:
        self.grid_tree.delete(*self.grid_tree.get_children())
        self._by_iid = {}
        rows = self.current_filter()
        for f in rows:
            loc = f"{f.file or ''}:{f.line}" if f.line else (f.file or "")
            iid = self.grid_tree.insert("", "end", values=(f.id, f.severity, f.category, f.title, loc, f.confidence, f.verification))
            self._by_iid[iid] = f
        self.count_var.set(f"{len(rows)} of {len(self._all)} findings")
        self._set_detail("")
        if self.on_select:
            self.on_select(None)

    def _set_detail(self, text: str) -> None:
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.config(state="disabled")

    def _selected(self, _evt=None) -> None:
        sel = self.grid_tree.selection()
        f = self._by_iid.get(sel[0]) if sel else None
        self._set_detail(format_detail(f) if f else "")
        if self.on_select:
            self.on_select(f)
