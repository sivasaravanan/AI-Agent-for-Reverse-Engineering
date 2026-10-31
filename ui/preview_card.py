"""VirtualBox-style "Preview" card: a framed live thumbnail of the running container.

GUI apps show their virtual screen; web apps a page thumbnail; console apps a terminal view of the latest output.
Idle shows a dark "Powered off" screen; paused/finished runs keep the last picture under a status badge (like a saved VM).
Plain Tk canvas drawing, no extra packages."""
from __future__ import annotations

import base64
import re
import textwrap
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from tkinter import ttk
from typing import Callable, Dict, Optional

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TAIL_LINES = 40


class PreviewCard(ttk.Frame):
    def __init__(self, parent, on_click: Optional[Callable[[str], None]] = None, **kw):
        super().__init__(parent, **kw)
        self.on_click = on_click
        self.run_state = "idle"
        self.badge = ""
        self.png = b""
        self.tail: "deque[str]" = deque(maxlen=TAIL_LINES)
        self._partial = ""
        self.web: Optional[Dict] = None
        self.dirty = True
        self._img = None
        self._img_key = None
        self._c: Dict[str, str] = {"panel": "#f3f5f9", "fg": "#1b2333", "muted": "#6b7691", "accent": "#2f6fed"}
        self.canvas = tk.Canvas(self, highlightthickness=0, width=300, height=270, cursor="hand2")
        self.canvas._keep_colors = True
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", lambda _e: self.on_click and self.on_click(self.mode()))
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        self.mono.configure(size=7)
        self.small = tkfont.nametofont("TkDefaultFont").copy()
        self.small.configure(size=7)
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold", size=9)
        self.big = tkfont.nametofont("TkDefaultFont").copy()
        self.big.configure(size=11, weight="bold")

    # ------------------------------------------------------------------ feed
    def reset(self) -> None:
        self.run_state, self.badge, self.png, self.web, self._img_key = "starting", "", b"", None, None
        self.tail.clear()
        self._partial = ""
        self.dirty = True

    def set_state(self, state: str, badge: str = "") -> None:
        self.run_state, self.badge, self.dirty = state, badge, True

    def set_frame(self, png: bytes) -> None:
        self.png, self.dirty = png, True

    def add_text(self, text: str) -> None:
        text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        parts = (self._partial + text).split("\n")
        self._partial = parts.pop()
        for p in parts:
            self.tail.append(p)
        self.dirty = True

    def set_web(self, up: bool, title: str, text: str, url: str) -> None:
        self.web = {"up": up, "title": title, "text": text, "url": url}
        self.dirty = True

    def mode(self) -> str:
        """Which full-size tab this thumbnail stands for: gui | web | console."""
        return "gui" if self.png else ("web" if self.web and self.web.get("up") else "console")

    def recolor(self, c: Dict[str, str]) -> None:
        self._c, self.dirty = c, True
        self.redraw()

    def tick(self) -> None:
        if self.dirty:
            self.redraw()

    # ------------------------------------------------------------------ drawing
    def _on_configure(self, e) -> None:
        h = 34 + int((e.width - 32) * 0.75) + 20
        if abs(h - e.height) > 3 and e.width > 60:
            self.canvas.configure(height=h)
        self.dirty = True
        self.redraw()

    @staticmethod
    def _rr(c, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    def redraw(self) -> None:
        self.dirty = False
        c, col = self.canvas, self._c
        w, h = max(c.winfo_width(), 120), max(c.winfo_height(), 120)
        c.delete("all")
        c.configure(bg=col["panel"])
        # header: monitor icon + "Preview"
        c.create_rectangle(8, 6, 24, 17, fill="#2f6fed", outline="#1b3f94")
        c.create_rectangle(10, 8, 22, 15, fill="#8fc1ff", outline="")
        c.create_rectangle(13, 18, 19, 20, fill="#6b7691", outline="")
        c.create_text(32, 13, text="Preview", anchor="w", font=self.bold, fill=col["fg"])
        # bezel
        x1, y1, x2, y2 = 6, 28, w - 6, h - 6
        self._rr(c, x1, y1, x2, y2, 14, fill="#8d8d8d", outline="")
        self._rr(c, x1 + 3, y1 + 3, x2 - 3, y2 - 3, 12, fill="#2b2b2b", outline="")
        aw, ah = (x2 - x1) - 24, (y2 - y1) - 24
        sw = int(min(aw, ah * 4 / 3))
        sh = int(sw * 3 / 4)
        sx, sy = x1 + ((x2 - x1) - sw) // 2, y1 + ((y2 - y1) - sh) // 2
        self._screen(sx, sy, sw, sh)
        if self.run_state in ("paused", "ended") or (self.run_state == "starting"):
            if self.run_state != "starting":
                c.create_rectangle(sx, sy, sx + sw, sy + sh, fill="#000000", stipple="gray50", outline="")
            label = self.badge or {"paused": "Paused", "ended": "Finished", "starting": "Starting..."}[self.run_state]
            tw = self.big.measure(label) + 24
            self._rr(c, sx + (sw - tw) // 2, sy + sh // 2 - 16, sx + (sw + tw) // 2, sy + sh // 2 + 16, 8, fill="#1b2333", outline="#6b7691")
            c.create_text(sx + sw // 2, sy + sh // 2, text=label, font=self.big, fill="#ffffff")

    def _screen(self, sx: int, sy: int, sw: int, sh: int) -> None:
        c, col = self.canvas, self._c
        c.create_rectangle(sx, sy, sx + sw, sy + sh, fill="#000000", outline="#555555")
        if self.run_state == "idle":
            c.create_text(sx + sw // 2, sy + sh // 2, text="Powered off", fill="#8a94ad", font=self.bold)
            return
        if self.png:
            img = self._scaled(sw, sh)
            if img is not None:
                c.create_image(sx + sw // 2, sy + sh // 2, image=img)
                return
        if self.web and self.web.get("up"):
            self._web_page(sx, sy, sw, sh)
        elif self.tail or self._partial:
            self._terminal(sx, sy, sw, sh)
        elif self.run_state == "running":
            c.create_text(sx + sw // 2, sy + sh // 2, text="Running - no output yet", fill="#8a94ad", font=self.small)

    def _scaled(self, sw: int, sh: int):
        key = (len(self.png), hash(self.png), sw, sh)
        if key == self._img_key:
            return self._img
        try:
            img = tk.PhotoImage(data=base64.b64encode(self.png).decode("ascii"))
        except tk.TclError:
            return None
        w, h = img.width(), img.height()
        fit = min(sw / max(w, 1), sh / max(h, 1))
        if fit >= 1:
            z, sub = max(1, min(int(fit), 6)), 1
        else:                                  # best integer ratio z/sub that still fits: finer than subsample alone
            z, sub, best = 1, 1, 0.0
            for zz in (1, 2, 3, 4):
                ss = -(-zz // 1) if fit >= 1 else int(-(-zz // fit))
                if zz / ss > best and zz / ss <= fit + 1e-9:
                    z, sub, best = zz, ss, zz / ss
        if sub > 1:
            img = img.subsample(sub, sub)
        if z > 1:
            img = img.zoom(z, z)
        self._img, self._img_key = img, key
        return img

    def _terminal(self, sx: int, sy: int, sw: int, sh: int) -> None:
        c = self.canvas
        cw, rh = max(self.mono.measure("M"), 1), max(self.mono.metrics("linespace"), 1)
        cols, rows = max((sw - 8) // cw, 10), max((sh - 8) // rh, 2)
        lines = list(self.tail) + ([self._partial] if self._partial else [])
        shown = []
        for ln in lines[-rows * 2:]:
            shown += textwrap.wrap(ln.expandtabs(4), cols) or [""]
        for i, ln in enumerate(shown[-rows:]):
            c.create_text(sx + 5, sy + 4 + i * rh, text=ln, anchor="nw", font=self.mono, fill="#b8f5b8")

    def _web_page(self, sx: int, sy: int, sw: int, sh: int) -> None:
        c, wb = self.canvas, self.web or {}
        c.create_rectangle(sx, sy, sx + sw, sy + sh, fill="#ffffff", outline="")
        c.create_rectangle(sx, sy, sx + sw, sy + 14, fill="#e4e7ee", outline="")
        c.create_text(sx + 6, sy + 7, text=(wb.get("url") or "")[:60], anchor="w", font=self.small, fill="#44506b")
        c.create_text(sx + 8, sy + 22, text=(wb.get("title") or "")[:50], anchor="nw", font=self.bold, fill="#1b2333")
        cw = max(self.small.measure("M"), 1)
        rh = max(self.small.metrics("linespace"), 1)
        rows = max((sh - 44) // rh, 1)
        lines = []
        for ln in (wb.get("text") or "").splitlines():
            lines += textwrap.wrap(ln, max((sw - 16) // cw, 10)) or [""]
            if len(lines) >= rows:
                break
        for i, ln in enumerate(lines[:rows]):
            c.create_text(sx + 8, sy + 40 + i * rh, text=ln, anchor="nw", font=self.small, fill="#2b3550")
