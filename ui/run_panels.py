"""Resource panel shared by Live Run and App Preview: CPU / memory sparklines, the container's process list and a phase timeline.

One implementation for every run view. It only draws what the caller passes in (events come from ``BUS``); nothing here controls a run.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import tkinter as tk
from tkinter import ttk

SPARK_POINTS = 60


class ResourcePanel(ttk.Frame):
    def __init__(self, parent, colors: Optional[Dict[str, str]] = None, **kw):
        super().__init__(parent, **kw)
        from ui import theme
        self._c = colors or theme.LIGHT
        self._cpu: Sequence[float] = []
        self._mem: Sequence[float] = []
        res = ttk.LabelFrame(self, text="Resources (container)")
        res.pack(fill="x", padx=2, pady=(0, 3))
        self.cpu_canvas = self._canvas(res, (2, 0))
        self.mem_canvas = self._canvas(res, (2, 3))
        procs = ttk.LabelFrame(self, text="Processes in the container")
        procs.pack(fill="both", expand=True, padx=2, pady=(0, 3))
        self.procs = ttk.Treeview(procs, show="headings", height=6)
        self.procs.pack(fill="both", expand=True, padx=3, pady=3)
        tl = ttk.LabelFrame(self, text="Timeline")
        tl.pack(fill="both", expand=True, padx=2)
        self.timeline = tk.Listbox(tl, height=6, activestyle="none")
        self.timeline.pack(fill="both", expand=True, padx=3, pady=3)
        self.recolor(self._c)

    def _canvas(self, parent, pady) -> tk.Canvas:
        cv = tk.Canvas(parent, height=56, highlightthickness=0)
        cv._keep_colors = True
        cv.pack(fill="x", padx=3, pady=pady)
        cv.bind("<Configure>", lambda _e: self.draw(self._cpu, self._mem))
        return cv

    def recolor(self, c: Dict[str, str]) -> None:
        self._c = c
        for cv in (self.cpu_canvas, self.mem_canvas):
            cv.configure(bg=c["field"])
        self.draw(self._cpu, self._mem)

    def draw(self, cpu: Sequence[float], mem: Sequence[float]) -> None:
        self._cpu, self._mem = cpu, mem
        for cv, data, label, colour in ((self.cpu_canvas, cpu, "CPU %", self._c["accent"]), (self.mem_canvas, mem, "Memory MiB", self._c["good"])):
            cv.delete("all")
            w, h = max(cv.winfo_width(), 120), max(cv.winfo_height(), 46)
            cv.create_text(4, 1, anchor="nw", text=f"{label}  (max {max(data):.0f})" if data else label, fill=self._c["muted"], font=("TkDefaultFont", 8))
            if len(data) >= 2:
                top = max(max(data), 1.0)
                pts: List[float] = []
                for i, v in enumerate(data):
                    pts += [4 + i * (w - 8) / (len(data) - 1), h - 4 - (h - 20) * v / top]
                cv.create_line(*pts, fill=colour, width=2)

    def set_top(self, top) -> None:
        self.procs.delete(*self.procs.get_children())
        if not top:
            self.procs.configure(columns=())
            return
        header, rows = top
        self.procs.configure(columns=header)
        for h in header:
            self.procs.heading(h, text=h)
            self.procs.column(h, width=70 if h.upper() in ("PID", "PPID", "UID") else 120, anchor="w")
        for r in rows:
            self.procs.insert("", "end", values=r)

    def set_phases(self, phases: Sequence[str]) -> None:
        self.timeline.delete(0, "end")
        for p in phases:
            self.timeline.insert("end", p)

    def add_phase(self, text: str) -> None:
        self.timeline.insert("end", text)
        self.timeline.see("end")

    def reset(self) -> None:
        self.draw([], [])
        self.set_top(None)
        self.set_phases([])
