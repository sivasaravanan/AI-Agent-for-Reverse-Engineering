"""Compact, modern look for the whole app: one place for colours, fonts, ttk styles, icons and tooltips.

Everything is plain Tk/ttk (no extra packages). Icons are BMP symbol glyphs (they render with the system's symbol fonts on
Windows, macOS and Linux and avoid astral emoji, which older Tk builds cannot draw).
"""
from __future__ import annotations

import json
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Dict, Optional

LIGHT = {"bg": "#f3f5f9", "panel": "#ffffff", "side": "#1f2a44", "side_fg": "#c9d3ea", "side_hi": "#2d3b5f", "side_sel": "#3b82f6",
         "fg": "#1b2333", "muted": "#6b7691", "accent": "#2f6fed", "accent_fg": "#ffffff", "accent_hi": "#1f5bd1", "border": "#d5dbe8",
         "field": "#ffffff", "row_alt": "#f7f9fd", "sel": "#dbe7ff", "sel_fg": "#0f1b3a", "good": "#1f9d57", "warn": "#d98a00", "bad": "#d64545",
         "btn": "#e8edf7", "btn_hi": "#d9e2f4"}
DARK = {"bg": "#12161f", "panel": "#1a2030", "side": "#0d111a", "side_fg": "#aeb8d0", "side_hi": "#1b2336", "side_sel": "#4c8dff",
        "fg": "#e6eaf4", "muted": "#8f9bb8", "accent": "#4c8dff", "accent_fg": "#0b1220", "accent_hi": "#6aa0ff", "border": "#2b3550",
        "field": "#131a28", "row_alt": "#1e2536", "sel": "#2b3f6e", "sel_fg": "#ffffff", "good": "#36c47a", "warn": "#f0b429", "bad": "#ff6b6b",
        "btn": "#252e44", "btn_hi": "#2f3a56"}

# BMP-only glyphs, keyed by tab title / action
ICONS: Dict[str, str] = {
    "Overview": "⌂", "Languages": "☰", "Frameworks": "⬡", "Databases": "▤", "Manifests": "❒", "Findings": "⚠", "Finding detail": "⌕",
    "Structure": "▦", "Entry points": "➜", "Imports": "⇩", "Config & Env": "⚒", "Confidence": "◍", "Config warnings": "△",
    "Static tools": "✔", "Code map": "⎇", "Findings Explorer": "⌕", "Build & Test": "▶", "Live Run": "◉", "App Preview": "▣", "AI Assistant": "✦", "Dashboard": "◔",
    "Findings (filter)": "≣", "Report preview": "▣", "History": "⟲", "Batch grading": "✎", "Compliance": "⚖", "Team": "☻", "Settings": "⚙",
    "archive": "⇪", "folder": "❐", "analyze": "▶", "cancel": "■", "html": "⎋", "json": "❴❵", "fix": "✎", "update": "⟳", "docs": "☰",
    "delivery": "⬒", "theme": "◐", "brand": "◆",
    "Portfolio": "▥", "Run Insights": "⌁", "Prerequisites": "⛭",
}
SIDEBAR_GROUPS = [
    ("Results", ["Dashboard", "Overview", "Findings", "Findings (filter)", "Findings Explorer", "Finding detail", "Report preview"]),
    ("Project", ["Languages", "Frameworks", "Databases", "Manifests", "Structure", "Entry points", "Imports", "Config & Env",
                 "Confidence", "Config warnings", "Static tools", "Code map"]),
    ("Actions", ["App Preview", "AI Assistant"]),      # App Preview = the one run view; Build & Test / Live Run stay reachable under More
    ("Institution", ["Batch grading", "Compliance", "Team"]),
    ("System", ["History", "Settings"]),
]


def icon(name: str, text: str = "") -> str:
    g = ICONS.get(name, "•")
    return f"{g} {text}" if text else g


def pick_font(root, candidates=("Segoe UI", "SF Pro Text", "Inter", "Noto Sans", "DejaVu Sans", "Helvetica")) -> str:
    try:
        fams = set(tkfont.families(root))
    except tk.TclError:
        return "TkDefaultFont"
    return next((c for c in candidates if c in fams), "TkDefaultFont")


def _prefs_path(base: Optional[Path]) -> Optional[Path]:
    return (Path(base) / "data" / "ui_prefs.json") if base else None


def load_prefs(base: Optional[Path]) -> dict:
    p = _prefs_path(base)
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(base: Optional[Path], **changes) -> None:
    p = _prefs_path(base)
    try:
        if p:
            d = load_prefs(base)
            d.update(changes)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass                                    # a preference that cannot be saved is not an error


def load_pref_dark(base: Optional[Path]) -> bool:
    return bool(load_prefs(base).get("dark", False))


def save_pref_dark(base: Optional[Path], dark: bool) -> None:
    save_prefs(base, dark=bool(dark))


def apply_theme(root, dark: bool = False) -> Dict[str, str]:
    c = DARK if dark else LIGHT
    fam = pick_font(root)
    base = (fam, 9)
    for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(n).configure(family=fam, size=9)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(size=9)
    except tk.TclError:
        pass
    root.configure(bg=c["bg"])
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"], bordercolor=c["border"], lightcolor=c["bg"],
                darkcolor=c["bg"], troughcolor=c["btn"], focuscolor=c["accent"], font=base, padding=0)
    s.configure("TFrame", background=c["bg"])
    s.configure("Panel.TFrame", background=c["panel"])
    s.configure("TLabel", background=c["bg"], foreground=c["fg"], padding=(2, 1))
    s.configure("Muted.TLabel", foreground=c["muted"])
    s.configure("Title.TLabel", font=(fam, 11, "bold"))
    s.configure("Status.TLabel", background=c["panel"], foreground=c["muted"], padding=(6, 2))
    s.configure("TButton", background=c["btn"], foreground=c["fg"], bordercolor=c["border"], relief="flat", padding=(8, 3), borderwidth=1)
    s.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["btn_hi"]), ("active", c["btn_hi"])],
          foreground=[("disabled", c["muted"])], bordercolor=[("focus", c["accent"])])
    s.configure("Accent.TButton", background=c["accent"], foreground=c["accent_fg"], bordercolor=c["accent"], font=(fam, 9, "bold"))
    s.map("Accent.TButton", background=[("disabled", c["btn"]), ("pressed", c["accent_hi"]), ("active", c["accent_hi"])],
          foreground=[("disabled", c["muted"])])
    s.configure("TButton", width=0)
    s.configure("Accent.TButton", width=0)
    s.configure("Tool.TButton", padding=(6, 2), width=0)
    s.configure("TCheckbutton", background=c["bg"], foreground=c["fg"], padding=(2, 1), indicatorbackground=c["field"],
                indicatorforeground=c["accent_fg"], indicatormargin=(0, 0, 4, 0))
    s.map("TCheckbutton", background=[("active", c["bg"])], indicatorbackground=[("selected", c["accent"]), ("!selected", c["field"])],
          indicatorforeground=[("selected", c["accent_fg"])], indicatorcolor=[("selected", c["accent"]), ("!selected", c["field"])])
    s.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])
    s.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], padding=(4, 2), bordercolor=c["border"])
    s.map("TEntry", bordercolor=[("focus", c["accent"])])
    s.configure("TCombobox", fieldbackground=c["field"], foreground=c["fg"], padding=(4, 2), arrowcolor=c["muted"])
    s.map("TCombobox", fieldbackground=[("readonly", c["field"])], bordercolor=[("focus", c["accent"])])
    s.configure("TSpinbox", fieldbackground=c["field"], padding=(4, 2))
    s.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["btn"], thickness=4, borderwidth=0, lightcolor=c["accent"], darkcolor=c["accent"])
    s.configure("TLabelframe", background=c["bg"], bordercolor=c["border"], padding=4)
    s.configure("TLabelframe.Label", background=c["bg"], foreground=c["muted"], font=(fam, 9, "bold"))
    s.configure("TSeparator", background=c["border"])
    s.configure("TPanedwindow", background=c["bg"])
    # tables
    s.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"], rowheight=20, borderwidth=0, padding=0)
    s.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["sel_fg"])])
    s.configure("Treeview.Heading", background=c["btn"], foreground=c["fg"], font=(fam, 9, "bold"), padding=(6, 3), relief="flat", borderwidth=0)
    s.map("Treeview.Heading", background=[("active", c["btn_hi"])])
    s.configure("Vertical.TScrollbar", background=c["btn"], troughcolor=c["bg"], arrowsize=11, borderwidth=0, gripcount=0)
    s.configure("Horizontal.TScrollbar", background=c["btn"], troughcolor=c["bg"], arrowsize=11, borderwidth=0, gripcount=0)
    s.map("Vertical.TScrollbar", background=[("active", c["btn_hi"])])
    # notebook: the tab strip is replaced by the sidebar, so tabs are collapsed to nothing
    s.layout("TNotebook.Tab", [])
    s.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=0, padding=0)
    s.configure("TNotebook.Tab", padding=0)
    # sidebar
    s.configure("Side.TFrame", background=c["side"])
    s.configure("Brand.TLabel", background=c["side"], foreground="#ffffff", font=(fam, 11, "bold"), padding=(10, 8))
    root.option_add("*Menu.background", c["panel"])
    root.option_add("*Menu.foreground", c["fg"])
    root.option_add("*Listbox.background", c["field"])
    root.option_add("*Listbox.foreground", c["fg"])
    root.option_add("*Text.background", c["field"])
    root.option_add("*Text.foreground", c["fg"])
    restyle_tk_widgets(root, c)
    return c


def restyle_tk_widgets(widget, c: Dict[str, str]) -> None:
    """Classic tk widgets (Text, Listbox, Canvas) ignore ttk styles; recolour them in place, recursively."""
    for w in widget.winfo_children():
        try:
            if isinstance(w, tk.Text):
                w.configure(bg=c["field"], fg=c["fg"], insertbackground=c["fg"], selectbackground=c["sel"], selectforeground=c["sel_fg"],
                            relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=c["border"], highlightcolor=c["accent"],
                            padx=6, pady=4)
            elif isinstance(w, tk.Listbox):
                w.configure(bg=c["field"], fg=c["fg"], selectbackground=c["sel"], selectforeground=c["sel_fg"], relief="flat",
                            highlightthickness=1, highlightbackground=c["border"])
            elif isinstance(w, tk.Canvas) and not getattr(w, "_keep_colors", False):
                w.configure(bg=c["panel"], highlightthickness=0)
        except tk.TclError:
            pass
        restyle_tk_widgets(w, c)


class Tooltip:
    """Small hover hint, used where a compact label hides detail."""

    def __init__(self, widget, text: str, delay: int = 450):
        self.widget, self.text, self.delay, self.tip, self._job = widget, text, delay, None, None
        widget.bind("<Enter>", self._arm, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _arm(self, _e=None):
        self._job = self.widget.after(self.delay, self._show)

    def _show(self):
        if self.tip or not self.text:
            return
        try:
            x, y = self.widget.winfo_rootx() + 10, self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg="#222b40", fg="#ffffff", padx=6, pady=3, font=("TkDefaultFont", 9), justify="left",
                     wraplength=360).pack()
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def make_app_icon(size: int = 32) -> tk.PhotoImage:
    """Draw the window icon in code (no image files): rounded blue square with a white chevron and bar."""
    img = tk.PhotoImage(width=size, height=size)
    r = size // 5
    for y in range(size):
        for x in range(size):
            cx, cy = min(x, size - 1 - x), min(y, size - 1 - y)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                continue
            t = y / size
            img.put("#%02x%02x%02x" % (int(47 + 30 * t), int(111 - 20 * t), int(237 - 10 * t)), (x, y))
    u = size / 32.0

    def box(x0, y0, x1, y1):
        img.put("#ffffff", (int(x0 * u), int(y0 * u), max(int(x1 * u), int(x0 * u) + 1), max(int(y1 * u), int(y0 * u) + 1)))
    for i in range(7):                       # chevron  >
        box(9 + i, 9 + i, 12 + i, 11 + i)
        box(9 + i, 23 - i, 12 + i, 25 - i)
    box(18, 22, 25, 25)                      # prompt bar
    return img
