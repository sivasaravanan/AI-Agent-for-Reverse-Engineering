"""Phase 9: local analysis history (open, delete, compare)."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from database.db_manager import DbManager, HistoryError


class HistoryView(ttk.Frame):
    COLS = (("id", "#", 50), ("when", "Analyzed (UTC)", 150), ("name", "Archive", 220), ("files", "Files", 60),
            ("findings", "Findings", 70), ("high", "High+", 60), ("langs", "Languages", 220), ("saved", "Full result", 80))

    def __init__(self, parent, db: Optional[DbManager] = None, on_open: Optional[Callable[[int], None]] = None, **kwargs):
        super().__init__(parent, **kwargs)
        self.db, self.on_open = db, on_open
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        self.open_btn = ttk.Button(bar, text="Open selected", command=self.open_selected)
        self.open_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Compare 2 selected", command=self.compare_selected).pack(side="left")
        ttk.Button(bar, text="Delete selected", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left")
        ttk.Label(bar, text="Search:").pack(side="left", padx=(12, 2))
        self.search_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search_var, width=22).pack(side="left")
        self.search_var.trace_add("write", lambda *_: self.refresh())
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, padding=(3, 0)).pack(fill="x")
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())
        self.refresh()

    def set_db(self, db: Optional[DbManager]) -> None:
        self.db = db
        self.refresh()

    def _ids(self):
        return [int(self.tree.set(i, "id")) for i in self.tree.selection()]

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.db is None:
            self.status_var.set("History is turned off (Settings > Keep local analysis history).")
            return
        try:
            rows = self.db.list_history(search=self.search_var.get())
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a damaged database
            self.status_var.set(f"History unavailable: {exc}")
            return
        for r in rows:
            self.tree.insert("", "end", values=(r.id, r.analyzed_at.replace("T", " ")[:19], r.archive_name, r.file_count,
                                                r.finding_count, r.high_count, r.languages, "yes" if r.stored else "summary only"))
        self.status_var.set(f"{len(rows)} saved analysis(es). Double-click to open.")

    def open_selected(self) -> None:
        ids = self._ids()
        if ids and self.on_open:
            self.on_open(ids[0])

    def delete_selected(self) -> None:
        ids = self._ids()
        if not ids or self.db is None:
            return
        if messagebox.askyesno("Delete history", f"Delete {len(ids)} saved analysis(es)?"):
            for i in ids:
                self.db.delete(i)
            self.refresh()

    def clear_all(self) -> None:
        if self.db is not None and messagebox.askyesno("Clear history", "Delete ALL saved analyses?"):
            self.db.clear()
            self.refresh()

    def compare_selected(self) -> None:
        ids = sorted(self._ids())
        if len(ids) != 2 or self.db is None:
            messagebox.showinfo("Compare", "Select exactly two analyses to compare.")
            return
        try:
            d = self.db.compare(ids[0], ids[1])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Compare failed", str(exc))
            return
        lines = [f"Comparing #{ids[0]} (older) with #{ids[1]} (newer)", "",
                 f"New findings: {d['new_count']}    Resolved: {d['resolved_count']}    Unchanged: {d['unchanged']}", ""]
        lines += [f"+ {x['rule_id']}  {x['file']}" for x in d["new"][:15]] + [f"- {x['rule_id']}  {x['file']}" for x in d["resolved"][:15]]
        messagebox.showinfo("Compare analyses", "\n".join(lines))
