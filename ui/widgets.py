"""Reusable layout helpers: collapsible sections, scrolled text, automatic scrollbars, page scrolling."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


class Collapsible(ttk.Frame):
    """A header row (▾/▸ title) that shows or hides ``self.body``. Put the section's widgets in ``body``."""

    def __init__(self, parent, title: str, expanded: bool = True, on_toggle: Optional[Callable[[bool], None]] = None, **kw):
        super().__init__(parent, **kw)
        self.title, self.on_toggle = title, on_toggle
        self.expanded = expanded
        self.head = tk.Label(self, anchor="w", cursor="hand2", padx=4, pady=1, font=("TkDefaultFont", 9, "bold"))
        self.head.pack(fill="x")
        self.head.bind("<Button-1>", lambda _e: self.toggle())
        self.body = ttk.Frame(self)
        self._paint()
        if expanded:
            self.body.pack(fill="x")

    def _paint(self) -> None:
        self.head.configure(text=("▾ " if self.expanded else "▸ ") + self.title)
        try:
            st = ttk.Style(self)
            self.head.configure(bg=st.lookup("TFrame", "background") or self.head.cget("bg"), fg=st.lookup("Muted.TLabel", "foreground") or "#6b7691")
        except tk.TclError:
            pass

    def toggle(self, expanded: Optional[bool] = None) -> None:
        want = (not self.expanded) if expanded is None else bool(expanded)
        if want == self.expanded:
            return
        self.expanded = want
        if want:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()
        self._paint()
        if self.on_toggle:
            self.on_toggle(want)


def scrolled_text(parent, **kw):
    """Return (frame, text) where the text has vertical + horizontal scrollbars. Pack/add the frame, use the text."""
    frame = ttk.Frame(parent)
    text = tk.Text(frame, **kw)
    sy = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sy.set)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    text.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    if str(kw.get("wrap", "char")) == "none":
        sx = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(xscrollcommand=sx.set)
        sx.grid(row=1, column=0, sticky="ew")
    return frame, text


def _bar(widget, orient: str):
    return ttk.Scrollbar(widget.master, orient=orient, command=widget.yview if orient == "vertical" else widget.xview)


def ensure_scrollbars(root) -> int:
    """Give every Treeview/Text that has no vertical scrollbar one (and a horizontal bar to Treeviews / no-wrap Text).
    Works for widgets laid out with pack or grid; others (notebook/paned children, place) are left alone. Returns how many were added."""
    added = 0
    for w in list(root.winfo_children()):
        added += ensure_scrollbars(w)
        if not isinstance(w, (ttk.Treeview, tk.Text)) or getattr(w, "_ase_scrolled", False):
            continue
        if isinstance(w.master, (ttk.Notebook, ttk.PanedWindow)):
            continue
        mgr = w.winfo_manager()
        if mgr not in ("pack", "grid"):
            continue
        has_y = bool(str(w.cget("yscrollcommand")))
        want_x = isinstance(w, ttk.Treeview) or str(w.cget("wrap")) == "none"
        has_x = bool(str(w.cget("xscrollcommand")))
        w._ase_scrolled = True
        if has_y and (has_x or not want_x):
            continue
        try:
            if mgr == "pack":
                if not has_y:
                    sy = _bar(w, "vertical")
                    w.configure(yscrollcommand=sy.set)
                    sy.pack(side="right", fill="y", before=w)
                    added += 1
                if want_x and not has_x:
                    sx = _bar(w, "horizontal")
                    w.configure(xscrollcommand=sx.set)
                    sx.pack(side="bottom", fill="x", before=w)
                    added += 1
            else:
                gi = w.grid_info()
                r, c = int(gi.get("row", 0)), int(gi.get("column", 0))
                if not has_y:
                    sy = _bar(w, "vertical")
                    w.configure(yscrollcommand=sy.set)
                    sy.grid(row=r, column=c + int(gi.get("columnspan", 1)), sticky="ns")
                    added += 1
                if want_x and not has_x:
                    sx = _bar(w, "horizontal")
                    w.configure(xscrollcommand=sx.set)
                    sx.grid(row=r + int(gi.get("rowspan", 1)), column=c, sticky="ew")
                    added += 1
        except tk.TclError:
            continue
    return added


_SELF_SCROLLING = (ttk.Treeview, tk.Text, tk.Listbox, ttk.Combobox)


def _owner_scrolls(widget) -> bool:
    """True when the pointer is over a widget that scrolls itself (table, text box, list)."""
    while widget is not None:
        if isinstance(widget, _SELF_SCROLLING):
            return True
        widget = getattr(widget, "master", None)
        if widget is not None and isinstance(widget, (tk.Tk, tk.Toplevel)):
            return False
    return False


class ScrollFrame(ttk.Frame):
    """A scrollable page (vertical, plus horizontal when the window is narrower than the content's minimum width).
    Build content in ``self.inner``. When the window is larger than the content, ``inner`` stretches to fill it, so
    expanding tables and text boxes still use the whole page; when smaller, scrollbars appear."""

    def __init__(self, parent, min_width: int = 0, **kw):
        super().__init__(parent, **kw)
        self.min_width = min_width
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.canvas._keep_colors = False
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._set_y, xscrollcommand=self._set_x)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._fit())
        self.canvas.bind("<Configure>", lambda _e: self._fit())
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._bind_wheel, add="+")
            w.bind("<Leave>", self._unbind_wheel, add="+")

    def _set_y(self, lo, hi):                       # scrollbars only when needed
        self.vbar.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.vbar.grid_remove()
        else:
            self.vbar.grid(row=0, column=1, sticky="ns")

    def _set_x(self, lo, hi):
        self.hbar.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.hbar.grid_remove()
        else:
            self.hbar.grid(row=1, column=0, sticky="ew")

    def _fit(self) -> None:
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        rw = max(self.inner.winfo_reqwidth(), self.min_width)
        self.canvas.itemconfigure(self._win, width=max(cw, rw), height=max(ch, self.inner.winfo_reqheight()))
        self.canvas.configure(scrollregion=(0, 0, max(cw, rw), max(ch, self.inner.winfo_reqheight())))

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Shift-MouseWheel>", lambda e: self._wheel(e, "x"))
        self.canvas.bind_all("<Button-4>", lambda e: self._wheel(e, "y", -3))
        self.canvas.bind_all("<Button-5>", lambda e: self._wheel(e, "y", 3))

    def _unbind_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _wheel(self, e, axis: str = "y", step: int = 0):
        try:
            under = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            under = None
        if _owner_scrolls(under):                  # tables / text boxes scroll themselves
            return
        n = step or (int(-e.delta / 40) or (-1 if e.delta > 0 else 1))
        (self.canvas.xview_scroll if axis == "x" else self.canvas.yview_scroll)(n, "units")


# ------------------------------------------------------------------ collapsible group boxes
def make_labelframes_collapsible(root, start_collapsed=()) -> int:
    """Turn every ttk.LabelFrame under ``root`` into a collapsible section: click its title to fold/unfold.
    Children are hidden with their own geometry manager (pack or grid) and restored in the original order."""
    n = 0
    for w in list(root.winfo_children()):
        n += make_labelframes_collapsible(w, start_collapsed)
        if isinstance(w, ttk.LabelFrame) and not getattr(w, "_ase_collapsible", False):
            try:
                _enhance(w, w.cget("text") in start_collapsed)
                n += 1
            except tk.TclError:
                pass
    return n


def _enhance(frame: ttk.LabelFrame, collapsed: bool) -> None:
    title = frame.cget("text")
    frame._ase_collapsible = True
    frame._ase_hidden = []
    head = tk.Label(frame, anchor="w", cursor="hand2", padx=2, font=("TkDefaultFont", 9, "bold"))
    st = ttk.Style(frame)
    head.configure(bg=st.lookup("TLabelframe.Label", "background") or st.lookup("TFrame", "background") or "#f3f5f9",
                   fg=st.lookup("TLabelframe.Label", "foreground") or "#6b7691")

    def paint():
        head.configure(text=("▸ " if frame._ase_hidden else "▾ ") + title)

    def toggle(_e=None):
        if frame._ase_hidden:                        # expand: restore in original order
            for w, mgr, info in frame._ase_hidden:
                try:
                    (w.pack if mgr == "pack" else w.grid)(**info)
                except tk.TclError:
                    pass
            frame._ase_hidden = []
            sp = getattr(frame, "_ase_spacer", None)
            if sp is not None:
                sp.destroy()
                frame._ase_spacer = None
        else:
            hid = []
            for w in frame.winfo_children():
                if w is head:
                    continue
                mgr = w.winfo_manager()
                if mgr == "pack":
                    hid.append((w, "pack", w.pack_info()))
                    w.pack_forget()
                elif mgr == "grid":
                    hid.append((w, "grid", w.grid_info()))
                    w.grid_remove()
            frame._ase_hidden = hid
            if hid:      # Tk keeps a container's old size when it has no children left, so leave a 2px spacer to let it shrink
                sp = tk.Frame(frame, height=2, width=1, bd=0, highlightthickness=0)
                (sp.pack if hid[0][1] == "pack" else sp.grid)()
                frame._ase_spacer = sp
        paint()
    head.bind("<Button-1>", toggle)
    frame.configure(labelwidget=head)
    frame._ase_toggle = toggle
    paint()
    if collapsed:
        frame.after_idle(toggle)
