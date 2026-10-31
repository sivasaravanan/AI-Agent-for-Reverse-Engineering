"""Stage B: batch grading tab (instructor view). Analysis runs on a worker thread; the UI polls a queue."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional


class GradingPanel(ttk.Frame):
    COLS = ("Student", "Status", "Points", "%", "Final", "High+", "Similarity")

    def __init__(self, parent, out_dir, **kwargs):
        super().__init__(parent, **kwargs)
        self.out_dir = Path(out_dir)
        self.q: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel = threading.Event()
        self.batch = None
        self.folder_var, self.rubric_var, self.template_var, self.manual_var = (tk.StringVar() for _ in range(4))
        self.status_var = tk.StringVar(value="Choose a folder with one ZIP/tar/folder per student.")
        grid = ttk.LabelFrame(self, text="Inputs")               # click the title to fold / unfold
        grid.pack(fill="x", padx=3, pady=(3, 0))
        for r, (label, var, cmd) in enumerate((("Submissions folder", self.folder_var, self._pick_folder),
                                              ("Rubric (JSON, optional)", self.rubric_var, lambda: self._pick(self.rubric_var, "json")),
                                              ("Starter template (optional)", self.template_var, lambda: self._pick(self.template_var, "zip")),
                                              ("Manual scores CSV (optional)", self.manual_var, lambda: self._pick(self.manual_var, "csv")))):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w")
            ttk.Entry(grid, textvariable=var, width=60).grid(row=r, column=1, sticky="we", padx=4)
            ttk.Button(grid, text="Browse...", command=cmd).grid(row=r, column=2)
        grid.columnconfigure(1, weight=1)
        bar = ttk.Frame(self, padding=(3, 0))
        bar.pack(fill="x")
        self.run_btn = ttk.Button(bar, text="▶ Grade all", style="Accent.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="■ Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(bar, text="⎋ Summary", command=self.open_summary, state="disabled")
        self.open_btn.pack(side="left")
        self.folder_btn = ttk.Button(bar, text="❐ Results folder", command=self.open_folder, state="disabled")
        self.folder_btn.pack(side="left", padx=4)
        self.progress = ttk.Progressbar(bar, length=200, maximum=100)
        self.progress.pack(side="left", padx=4)
        ttk.Label(self, textvariable=self.status_var, padding=3).pack(fill="x")
        self.tree = ttk.Treeview(self, columns=self.COLS, show="headings")
        for c in self.COLS:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=110 if c != "Student" else 180, anchor="w")
        self.tree.pack(fill="both", expand=True)
        ttk.Label(self, text="Scores use static checks only (nothing is run). Similarity is a lead for review, not proof of copying.",
                  padding=3).pack(fill="x")

    def _pick_folder(self) -> None:
        p = filedialog.askdirectory(title="Folder containing the student submissions")
        if p:
            self.folder_var.set(p)

    def _pick(self, var, kind) -> None:
        types = {"json": [("JSON", "*.json")], "zip": [("Archives", "*.zip *.tar *.gz *.tgz")], "csv": [("CSV", "*.csv")]}[kind]
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            var.set(p)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showinfo("Batch grading", "Choose the submissions folder first.")
            return
        self.cancel.clear()
        self.tree.delete(*self.tree.get_children())
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        args = (folder, self.rubric_var.get().strip() or None, self.template_var.get().strip() or None, self.manual_var.get().strip() or None)
        self.worker = threading.Thread(target=self._work, args=args, daemon=True)
        self.worker.start()
        self.after(100, self._poll)

    def _work(self, folder, rubric, template, manual) -> None:
        try:
            from education import grade_batch, load_rubric
            rb = load_rubric(rubric) if rubric else None
            b = grade_batch(folder, rb, self.out_dir / "grading", progress=lambda f, m: self.q.put(("p", f, m)),
                            cancel=self.cancel, template=template, manual_scores=manual)
            self.q.put(("done", b))
        except Exception as exc:  # noqa: BLE001 - shown to the user, never crashes the UI
            self.q.put(("err", str(exc)))

    def _poll(self) -> None:
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self.progress["value"] = m[1] * 100
                    self.status_var.set(m[2])
                elif m[0] == "err":
                    self._finish()
                    self.status_var.set(f"Failed: {m[1]}")
                    messagebox.showerror("Batch grading", m[1])
                    return
                elif m[0] == "done":
                    self.batch = m[1]
                    self._fill()
                    self._finish()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self) -> None:
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _fill(self) -> None:
        b = self.batch
        for s in b.students:
            self.tree.insert("", "end", values=(s.student_id, s.status if s.status == "graded" else f"error: {s.error[:60]}",
                                                 f"{s.earned}/{s.total_possible}" if s.status == "graded" else "",
                                                 s.percent if s.percent is not None else "", "yes" if s.complete else "no",
                                                 s.serious_findings, f"{s.similarity_level} {s.similarity_max:.2f}" if s.similarity_level else ""))
        flagged = sum(1 for p in b.pairs if p.level != "low")
        self.status_var.set(f"Graded {sum(1 for s in b.students if s.status == 'graded')}/{len(b.students)}; "
                            f"{flagged} similar pair(s) to review. Reports: {b.out_dir}")
        self.open_btn.config(state="normal")
        self.folder_btn.config(state="normal")

    def open_summary(self) -> None:
        if self.batch and self.batch.files.get("summary_html"):
            webbrowser.open(Path(self.batch.files["summary_html"]).as_uri())

    def open_folder(self) -> None:
        if self.batch and self.batch.out_dir:
            webbrowser.open(Path(self.batch.out_dir).as_uri())
