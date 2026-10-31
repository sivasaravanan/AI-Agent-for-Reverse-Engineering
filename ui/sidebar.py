"""Left navigation: icon + label rows in collapsible groups, scrollable, and itself collapsible to an icon-only rail.
Drives a ttk.Notebook whose own tab strip is hidden. Width is set by dragging the splitter next to it (see MainWindow)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from ui import theme

FULL_W, RAIL_W = 190, 46


class Sidebar(tk.Frame):
    def __init__(self, parent, notebook: ttk.Notebook, groups: List[Tuple[str, List[str]]], colors: Dict[str, str],
                 collapsed=("Project",), on_width: Optional[Callable[[int], None]] = None, rail: bool = False):
        super().__init__(parent, width=FULL_W, bg=colors["side"])
        self.pack_propagate(False)
        self.nb, self.c, self.groups = notebook, colors, groups
        self.on_width = on_width or (lambda w: None)
        self.rows: Dict[str, tk.Label] = {}
        self.tips: Dict[str, theme.Tooltip] = {}
        self.heads: Dict[str, tk.Label] = {}
        self.group_frames: Dict[str, tk.Frame] = {}
        self.collapsed = set(collapsed)
        self.rail = rail
        top = tk.Frame(self, bg=colors["side"])
        top.pack(fill="x")
        self.toggle_btn = tk.Label(top, text="«", bg=colors["side"], fg="#ffffff", cursor="hand2", padx=8, pady=6, font=("TkDefaultFont", 11, "bold"))
        self.toggle_btn.pack(side="right")
        self.toggle_btn.bind("<Button-1>", lambda _e: self.set_rail(not self.rail))
        self.brand = tk.Label(top, text=theme.icon("brand", "AI Engineering Agent"), bg=colors["side"], fg="#ffffff", anchor="w",
                              font=("TkDefaultFont", 10, "bold"), padx=10, pady=8)
        self.brand.pack(side="left", fill="x", expand=True)
        self._top = top
        wrap = tk.Frame(self, bg=colors["side"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=colors["side"], highlightthickness=0, borderwidth=0)
        self.canvas._keep_colors = True
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=colors["side"])
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for w in (self.canvas, self.body):
            w.bind("<Enter>", self._bind_wheel, add="+")
            w.bind("<Leave>", self._unbind_wheel, add="+")
        notebook.bind("<<NotebookTabChanged>>", lambda _e: self.sync(), add="+")

    # ---- scrolling (only when the list is taller than the window)
    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda _e: self._scroll(-3))
        self.canvas.bind_all("<Button-5>", lambda _e: self._scroll(3))

    def _unbind_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _wheel(self, e):
        self._scroll(-3 if e.delta > 0 else 3)

    def _scroll(self, n):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(n, "units")

    # ---- building
    def build(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.rows.clear(); self.tips.clear(); self.heads.clear(); self.group_frames.clear()
        titles = {self.nb.tab(i, "text"): i for i in range(self.nb.index("end")) if self.nb.tab(i, "state") != "hidden"}
        used = set()
        for gname, items in self.groups:
            present = [t for t in items if t in titles]
            if present:
                used.update(present)
                self._group(gname, present)
        rest = [t for t in titles if t not in used]
        if rest:
            self._group("More", rest)
        self._apply_rail()
        self.sync()

    def _group(self, name: str, items: List[str]) -> None:
        c = self.c
        head = tk.Label(self.body, bg=c["side"], fg="#7f8db3", anchor="w", font=("TkDefaultFont", 8, "bold"), padx=10, pady=3, cursor="hand2")
        head.pack(fill="x", pady=(4, 0))
        self.heads[name] = head
        frame = tk.Frame(self.body, bg=c["side"])
        self.group_frames[name] = frame
        if name not in self.collapsed or self.rail:
            frame.pack(fill="x")
        head.bind("<Button-1>", lambda _e, n=name: self._toggle(n))
        for t in items:
            row = tk.Label(frame, bg=c["side"], fg=c["side_fg"], anchor="w", padx=14, pady=3, cursor="hand2")
            row.pack(fill="x")
            row.bind("<Button-1>", lambda _e, tt=t: self.select(tt))
            row.bind("<Enter>", lambda _e, r=row: r.configure(bg=c["side_hi"]) if r is not self._current_row() else None)
            row.bind("<Leave>", lambda _e, r=row: r.configure(bg=self.c["side"]) if r is not self._current_row() else None)
            self.rows[t] = row
            self.tips[t] = theme.Tooltip(row, "")

    def _toggle(self, name: str) -> None:
        if self.rail:
            return
        if name in self.collapsed:
            self.collapsed.discard(name)
            self.group_frames[name].pack(fill="x", after=self.heads[name])
        else:
            self.collapsed.add(name)
            self.group_frames[name].pack_forget()
        self._label_heads()

    def _label_heads(self) -> None:
        for n, h in self.heads.items():
            h.configure(text=("─" if self.rail else ("▸ " if n in self.collapsed else "▾ ") + n.upper()), anchor="center" if self.rail else "w")

    def set_rail(self, on: bool) -> None:
        self.rail = bool(on)
        self._apply_rail()
        self.on_width(RAIL_W if self.rail else FULL_W)

    def _apply_rail(self) -> None:
        """Rail = icons only. Every group is shown (no hidden pages), labels become tooltips."""
        self.toggle_btn.configure(text="»" if self.rail else "«")
        if self.rail:
            self.brand.pack_forget()
            self.toggle_btn.pack_forget()
            self.toggle_btn.pack(fill="x")
        else:
            self.toggle_btn.pack_forget()
            self._top.pack_forget()
            self._top.pack(fill="x", before=self.canvas.master)
            self.toggle_btn.pack(side="right")
            self.brand.pack(side="left", fill="x", expand=True)
        for n, f in self.group_frames.items():
            f.pack_forget()
            if self.rail or n not in self.collapsed:
                f.pack(fill="x", after=self.heads[n])
        for t, row in self.rows.items():
            row.configure(text=theme.icon(t) if self.rail else theme.icon(t, t), anchor="center" if self.rail else "w",
                          padx=0 if self.rail else 14, font=("TkDefaultFont", 11 if self.rail else 9))
            self.tips[t].text = t if self.rail else ""
        self._label_heads()
        self.sync()

    def _current_title(self) -> str:
        try:
            return self.nb.tab(self.nb.select(), "text")
        except tk.TclError:
            return ""

    def _current_row(self):
        return self.rows.get(self._current_title())

    def select(self, title: str) -> None:
        for i in range(self.nb.index("end")):
            if self.nb.tab(i, "text") == title:
                if self.nb.tab(i, "state") == "hidden":          # a hidden page that something needs: show it again
                    self.nb.add(self.nb.tabs()[i])
                    self.build()
                self.nb.select(i)
                return

    def sync(self) -> None:
        cur = self._current_title()
        for t, row in self.rows.items():
            sel = t == cur
            size = 11 if self.rail else 9
            row.configure(bg=self.c["side_sel"] if sel else self.c["side"], fg="#ffffff" if sel else self.c["side_fg"],
                          font=("TkDefaultFont", size, "bold") if sel else ("TkDefaultFont", size))

    def recolor(self, colors: Dict[str, str]) -> None:
        self.c = colors
        for w in (self, self._top, self.brand, self.toggle_btn, self.body, self.canvas, self.canvas.master):
            w.configure(bg=colors["side"])
        for f in self.group_frames.values():
            f.configure(bg=colors["side"])
        for h in self.heads.values():
            h.configure(bg=colors["side"])
        self.sync()
