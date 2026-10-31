"""Phase 9: project dashboard - summary cards, language/framework/database summaries, dependency inventory, file tree."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List

from models.analysis_result import AnalysisResult

MAX_DEP_ROWS = 2000


def _fmt_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


class ProjectView(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=3)
        ttk.Label(left, text="File tree", padding=4).pack(anchor="w")
        self.tree = self._make_tree(left, (("size", "Size", 95),), show="tree headings", tree_width=150)
        self.tree.heading("#0", text="Name")
        self.cards_var = tk.StringVar(value="Run an analysis to see the project overview.")
        ttk.Label(right, textvariable=self.cards_var, padding=8, justify="left").pack(fill="x")
        grid = ttk.Frame(right)
        grid.pack(fill="x")
        self.langs = self._labeled(grid, "Languages", (("language", "Language", 110), ("files", "Files", 50), ("pct", "%", 50), ("conf", "Confidence", 90)), 0)
        self.fw = self._labeled(grid, "Frameworks / runtimes", (("name", "Name", 150), ("kind", "Kind", 100), ("conf", "Evidence", 80)), 1)
        self.dbs = self._labeled(grid, "Databases", (("name", "Database", 140), ("conf", "Evidence", 80), ("note", "Note", 160)), 2)
        for c in range(3):
            grid.columnconfigure(c, weight=1)
        ttk.Label(right, text="Dependency inventory", padding=(4, 8, 4, 0)).pack(anchor="w")
        self.deps = self._make_tree(right, (("eco", "Ecosystem", 90), ("spec", "Version constraint", 120),
                                            ("scope", "Scope", 70), ("loc", "Declared at", 220)),
                                    show="tree headings", tree_width=220, height=10)
        self.deps.heading("#0", text="Dependency")

    @staticmethod
    def _make_tree(parent, cols, show="headings", tree_width=0, height=8) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=4, pady=2)
        t = ttk.Treeview(frame, columns=[c[0] for c in cols], show=show, height=height)
        if "tree" in show:
            t.column("#0", width=tree_width, stretch=True)
        for key, title, width in cols:
            t.heading(key, text=title)
            t.column(key, width=width, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    def _labeled(self, grid, title, cols, column) -> ttk.Treeview:
        holder = ttk.LabelFrame(grid, text=title)
        holder.grid(row=0, column=column, sticky="nsew", padx=3, pady=3)
        return self._make_tree(holder, cols, height=6)

    def clear(self) -> None:
        for t in (self.tree, self.langs, self.fw, self.dbs, self.deps):
            t.delete(*t.get_children())
        self.cards_var.set("Run an analysis to see the project overview.")

    def show(self, r: AnalysisResult) -> None:
        self.clear()
        a = r.archive
        high = sum(1 for f in r.findings if f.severity in ("critical", "high"))
        self.cards_var.set(
            f"{r.archive_name}    Files: {a.file_count:,}    Size: {_fmt_size(a.total_uncompressed)}    "
            f"Text files scanned: {a.scanned_text_files:,}\nFindings: {len(r.findings):,} ({high} high or critical)    "
            f"Skipped unsafe entries: {a.skipped_total:,}    Warnings: {len(r.warnings)}")
        for l in r.languages:
            self.langs.insert("", "end", values=(l.language, l.files, l.percent_of_bytes, l.confidence))
        classes = {c.subject: c.evidence_class for c in (r.structure.confidence if r.structure else [])}
        for d in r.frameworks:
            self.fw.insert("", "end", values=(d.name, d.kind, classes.get(d.name, d.confidence)))
        for d in r.databases:
            self.dbs.insert("", "end", values=(d.name, classes.get(d.name, d.confidence), d.note[:80]))
        rows = 0
        for m in r.manifests:
            parent = self.deps.insert("", "end", text=f"{m.path}", open=False,
                                      values=(m.ecosystem, "", m.kind, f"{len(m.dependencies)} dependencies"))
            for d in m.dependencies:
                if rows >= MAX_DEP_ROWS:
                    break
                self.deps.insert(parent, "end", text=d.name,
                                 values=(d.ecosystem, d.spec or "(none)", d.scope, f"{d.file}:{d.line or '-'}"))
                rows += 1
        if r.structure and r.structure.tree:
            stack = [("", r.structure.tree)]
            while stack:                                   # iterative: no recursion limit issues
                parent, node = stack.pop()
                note = f"{node.file_count} files" if node.is_dir else _fmt_size(node.size)
                if node.vendored:
                    note += " (vendored)"
                iid = self.tree.insert(parent, "end", text=node.name, open=(parent == ""), values=(note,))
                stack.extend((iid, c) for c in reversed(node.children))
