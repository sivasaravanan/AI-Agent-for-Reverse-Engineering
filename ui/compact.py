"""Compact toolbars for the App Preview panel (standard library only; new module, additive).

Two small helpers:

``FlowFrame``  a row of widgets that wraps onto more rows when the pane is narrow, so NO widget is ever cut off or hidden
               (a widget marked ``stretch`` takes the free room of its row, like an expanding entry).
``Compactor``  keeps every button / check box of the toolbars as ``icon + label + tooltip``. In compact mode (the default) only the
               icon is shown and the full name plus the explanation sit in the tooltip; the option "Icons only" switches the labels back
               on. Nothing is removed in either mode.

Both only change how existing widgets are laid out; no command, variable or state is touched.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from . import theme

ICON_STYLE = "Icon.Tool.TButton"


def ensure_styles(widget) -> None:
    """Icon buttons: the tool-button look with tighter padding. Re-applied after a theme change."""
    try:
        s = ttk.Style(widget)
        s.configure(ICON_STYLE, padding=(4, 1), width=0)
    except tk.TclError:
        pass


class FlowFrame(ttk.Frame):
    """Wrapping row. Create the children with this frame as parent, then ``add(child, stretch=..., padx=...)``."""

    def __init__(self, parent, pady: Tuple[int, int] = (0, 0), **kw):
        super().__init__(parent, **kw)
        self._items: List[Tuple[tk.Widget, bool, Tuple[int, int]]] = []
        self._rows: List[ttk.Frame] = []
        self._sig: Optional[tuple] = None
        self._pady = pady
        self.bind("<Configure>", self._on_configure, add="+")

    def add(self, w, stretch: bool = False, padx: Tuple[int, int] = (0, 2), side: str = "left") -> None:
        self._items.append((w, stretch, tuple(padx)))
        self._sig = None
        self.after_idle(self.reflow)

    def _row(self, i: int) -> ttk.Frame:
        while len(self._rows) <= i:
            self._rows.append(ttk.Frame(self))
        return self._rows[i]

    def _on_configure(self, _e=None) -> None:
        self.reflow()

    def reflow(self) -> None:
        try:
            width = self.winfo_width()
            if width <= 1:
                width = 100000                                   # not laid out yet: everything in one row, corrected by <Configure>
            plan: List[List[int]] = [[]]
            used = 0
            for i, (w, _s, px) in enumerate(self._items):
                need = w.winfo_reqwidth() + px[0] + px[1]
                if plan[-1] and used + need > width:
                    plan.append([])
                    used = 0
                plan[-1].append(i)
                used += need
            sig = (tuple(tuple(r) for r in plan), tuple(w.winfo_reqwidth() for w, _s, _p in self._items))
            if sig == self._sig:
                return
            self._sig = sig
            for r in self._rows:
                r.pack_forget()
            for w, _s, _p in self._items:
                w.pack_forget()
            for ri, idxs in enumerate(plan):
                row = self._row(ri)
                row.pack(fill="x", pady=self._pady if ri == 0 else (0, 0))
                for i in idxs:
                    w, stretch, px = self._items[i]
                    if stretch:
                        w.pack(in_=row, side="left", fill="x", expand=True, padx=px)
                    else:
                        w.pack(in_=row, side="left", padx=px)
                    w.lift(row)
        except tk.TclError:
            pass


class Compactor:
    """Registry of the toolbar buttons / check boxes: ``icon``, ``label`` (original text) and the tooltip."""

    def __init__(self, host, compact: bool = True, on_change: Optional[Callable[[bool], None]] = None):
        self.host = host
        self.compact = bool(compact)
        self.on_change = on_change
        self._w: List[Dict] = []
        ensure_styles(host)
        try:
            host.bind("<<ThemeChanged>>", lambda _e: ensure_styles(host), add="+")
        except tk.TclError:
            pass

    def reg(self, w, icon: str, label: str, tip: str = "", suffix: str = "") -> "tk.Widget":
        """Remember a widget. ``suffix`` is a short extra (a counter) shown after the icon / label."""
        rec = {"w": w, "icon": icon, "label": label, "suffix": suffix, "tip": None}
        full = f"{label}\n{tip}" if tip and tip != label else label
        rec["tip"] = theme.Tooltip(w, full)
        self._w.append(rec)
        self._paint(rec)
        return w

    def set_suffix(self, w, suffix: str) -> None:
        for rec in self._w:
            if rec["w"] is w:
                rec["suffix"] = suffix
                self._paint(rec)

    def text_of(self, w) -> str:
        for rec in self._w:
            if rec["w"] is w:
                return self._text(rec)
        return ""

    def _text(self, rec: Dict) -> str:
        suf = f" {rec['suffix']}" if rec["suffix"] else ""
        if self.compact:
            return f"{rec['icon']}{suf}"
        return f"{rec['icon']} {rec['label']}{suf}".strip() if rec["icon"] else f"{rec['label']}{suf}"

    def _paint(self, rec: Dict) -> None:
        w = rec["w"]
        try:
            w.configure(text=self._text(rec))
            if isinstance(w, ttk.Button) or isinstance(w, ttk.Menubutton):
                w.configure(style=ICON_STYLE if self.compact else "Tool.TButton")
        except tk.TclError:
            pass

    def set_compact(self, on: bool) -> None:
        self.compact = bool(on)
        ensure_styles(self.host)
        for rec in self._w:
            self._paint(rec)
        if self.on_change:
            self.on_change(self.compact)
