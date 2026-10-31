"""Read-only source viewer used by the App Preview panel: line-number gutter, breakpoint dots, current-line marker,
error marks, execution heat map and light Python syntax colouring. Plain Tk, no extra packages."""
from __future__ import annotations

import bisect
import keyword
import re
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Iterable, Optional, Set

MAX_HIGHLIGHT_CHARS = 300_000
MAX_HEAT_LINES = 4000
_PY = re.compile(r'(?P<com>#[^\n]*)|(?P<str>[rRbBuUfF]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'))'
                 r'|(?P<kw>\b(?:' + "|".join(keyword.kwlist) + r')\b)|(?P<def>(?<=\bdef )\w+|(?<=\bclass )\w+)|(?P<num>\b\d[\d_.]*\b)')


class CodeView(ttk.Frame):
    def __init__(self, parent, on_toggle_break: Optional[Callable[[int], None]] = None,
                 on_line_click: Optional[Callable[[int], None]] = None, **kw):
        super().__init__(parent, **kw)
        self.on_toggle_break, self.on_line_click = on_toggle_break, on_line_click
        self.path = ""
        self.lines = 0
        self.breaks: Set[int] = set()
        self.current = 0
        self._heat: Dict[int, int] = {}
        self.gutter = tk.Text(self, width=7, wrap="none", state="disabled", cursor="arrow", takefocus=0, font="TkFixedFont",
                              padx=2, pady=4, borderwidth=0, highlightthickness=0)
        self.text = tk.Text(self, wrap="none", state="disabled", font="TkFixedFont", undo=False)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._on_scroll, xscrollcommand=self.hsb.set)
        self.gutter.pack(side="left", fill="y")
        self.vsb.pack(side="right", fill="y")
        self.hsb.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)
        self.gutter.bind("<Button-1>", self._gutter_click)
        self.gutter.bind("<MouseWheel>", lambda e: self._wheel(e))
        self.gutter.bind("<Button-4>", lambda e: self._wheel(e, -1))
        self.gutter.bind("<Button-5>", lambda e: self._wheel(e, 1))
        self.text.bind("<ButtonRelease-1>", self._text_click, add="+")
        self.recolor()

    # ------------------------------------------------------------------ colours
    def recolor(self, c: Optional[Dict[str, str]] = None) -> None:
        c = c or {"field": "#ffffff", "fg": "#1b2333", "muted": "#6b7691", "sel": "#dbe7ff", "bad": "#d64545", "warn": "#d98a00",
                  "good": "#1f9d57", "accent": "#2f6fed", "panel": "#f3f5f9", "row_alt": "#f7f9fd"}
        self._c = c
        dark = sum(int(c["field"][i:i + 2], 16) for i in (1, 3, 5)) < 384
        self.gutter.configure(bg=c.get("row_alt", c["panel"]), fg=c["muted"])
        self.text.tag_configure("cur", background="#3a4a1f" if dark else "#fff3b0")
        self.text.tag_configure("err", background="#4a2327" if dark else "#fde8e8")
        for i, col in enumerate(("#2b3f2f", "#3b4a2a", "#5a4a1f", "#6b3b1f") if dark else ("#e6f4ea", "#d7efdc", "#fff0c2", "#ffd9b8"), 1):
            self.text.tag_configure(f"heat{i}", background=col)
        self.text.tag_configure("kw", foreground="#c586c0" if dark else "#8a2be2")
        self.text.tag_configure("str", foreground="#ce9178" if dark else "#a31515")
        self.text.tag_configure("com", foreground="#6a9955" if dark else "#2e7d32")
        self.text.tag_configure("def", foreground="#dcdcaa" if dark else "#0b5cad")
        self.text.tag_configure("num", foreground="#b5cea8" if dark else "#098658")
        self.gutter.tag_configure("bp", foreground=c["bad"])
        self.gutter.tag_configure("gcur", background="#3a4a1f" if dark else "#fff3b0", foreground=c["fg"])
        for t in ("heat1", "heat2", "heat3", "heat4", "err", "cur"):     # priority: current line wins over errors over heat
            self.text.tag_raise(t)
        self.text.tag_raise("cur")

    # ------------------------------------------------------------------ content
    def show(self, path: str, text: str, line: int = 0) -> None:
        self.path = path
        self.lines = text.count("\n") + 1
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.config(state="disabled")
        self._highlight(path, text)
        self.gutter.config(state="normal")
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", "\n".join(self._fmt(n) for n in range(1, self.lines + 1)))
        for n in self.breaks:
            if n <= self.lines:
                self.gutter.tag_add("bp", f"{n}.0", f"{n}.1")
        self.gutter.config(state="disabled")
        self.current = 0
        self._heat = {}
        self.text.tag_remove("cur", "1.0", "end")
        self.goto(line) if line else self.text.yview_moveto(0)

    def _fmt(self, n: int) -> str:
        return f"{'●' if n in self.breaks else ' '}{n:>5} "

    def _highlight(self, path: str, text: str) -> None:
        for t in ("kw", "str", "com", "def", "num"):
            self.text.tag_remove(t, "1.0", "end")
        if not path.lower().endswith((".py", ".pyw")) or len(text) > MAX_HIGHLIGHT_CHARS:
            return
        starts = [0]
        for m in re.finditer("\n", text):
            starts.append(m.end())
        for m in _PY.finditer(text):
            kind = m.lastgroup
            for pos, tag_end in ((m.start(), m.end()),):
                ln = bisect.bisect_right(starts, pos) - 1
                ln_end = bisect.bisect_right(starts, tag_end - 1) - 1
                if ln == ln_end:
                    self.text.tag_add(kind, f"{ln + 1}.{pos - starts[ln]}", f"{ln + 1}.{tag_end - starts[ln]}")
                else:                                   # multi-line string: tag it line by line
                    self.text.tag_add(kind, f"{ln + 1}.{pos - starts[ln]}", f"{ln + 2}.0")
                    for k in range(ln + 1, ln_end):
                        self.text.tag_add(kind, f"{k + 1}.0", f"{k + 2}.0")
                    self.text.tag_add(kind, f"{ln_end + 1}.0", f"{ln_end + 1}.{tag_end - starts[ln_end]}")

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    # ------------------------------------------------------------------ marks
    def goto(self, line: int) -> None:
        line = max(1, min(int(line), max(self.lines, 1)))
        self.text.see(f"{line}.0")
        first, last = self.text.yview()
        span = last - first
        self.text.yview_moveto(max(0.0, (line - 1) / max(self.lines, 1) - span / 3))

    def set_current(self, line: int, scroll: bool = True) -> None:
        self.text.tag_remove("cur", "1.0", "end")
        self.gutter.tag_remove("gcur", "1.0", "end")
        self.current = int(line or 0)
        if self.current:
            self.text.tag_add("cur", f"{self.current}.0", f"{self.current}.end+1c")
            self.gutter.tag_add("gcur", f"{self.current}.0", f"{self.current}.end")
            if scroll:
                self.text.see(f"{self.current}.0")
                self.gutter.yview_moveto(self.text.yview()[0])

    def set_errors(self, lines: Iterable[int]) -> None:
        self.text.tag_remove("err", "1.0", "end")
        for n in set(lines):
            if 1 <= n <= self.lines:
                self.text.tag_add("err", f"{n}.0", f"{n}.end+1c")

    def set_heat(self, counts: Dict[int, int]) -> None:
        for i in range(1, 5):
            self.text.tag_remove(f"heat{i}", "1.0", "end")
        if not counts:
            return
        top = max(counts.values())
        for n, v in sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_HEAT_LINES]:
            if 1 <= n <= self.lines:
                tier = 1 + min(3, int(4 * v / (top + 1)))
                self.text.tag_add(f"heat{tier}", f"{n}.0", f"{n}.end+1c")

    def set_breaks(self, lines: Iterable[int]) -> None:
        old, self.breaks = self.breaks, set(int(n) for n in lines)
        self.gutter.config(state="normal")
        for n in old ^ self.breaks:
            if 1 <= n <= self.lines:
                self.gutter.delete(f"{n}.0", f"{n}.end")
                self.gutter.insert(f"{n}.0", self._fmt(n))
                self.gutter.tag_remove("bp", f"{n}.0", f"{n}.end")
                if n in self.breaks:
                    self.gutter.tag_add("bp", f"{n}.0", f"{n}.1")
        self.gutter.config(state="disabled")

    # ------------------------------------------------------------------ interaction
    def _line_at(self, widget, e) -> int:
        return int(widget.index(f"@{e.x},{e.y}").split(".")[0])

    def _gutter_click(self, e) -> str:
        n = self._line_at(self.gutter, e)
        if 1 <= n <= self.lines and self.on_toggle_break:
            self.on_toggle_break(n)
        return "break"

    def _text_click(self, e) -> None:
        if self.on_line_click:
            self.on_line_click(self._line_at(self.text, e))

    def _on_scroll(self, first, last) -> None:
        self.vsb.set(first, last)
        self.gutter.yview_moveto(first)

    def _yview(self, *a) -> None:
        self.text.yview(*a)
        self.gutter.yview_moveto(self.text.yview()[0])

    def _wheel(self, e, direction: int = 0) -> str:
        d = direction or (-1 if e.delta > 0 else 1)
        self.text.yview_scroll(d * 3, "units")
        return "break"
