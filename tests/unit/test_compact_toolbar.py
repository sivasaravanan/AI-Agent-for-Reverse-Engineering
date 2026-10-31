"""Compact App Preview toolbars: icons + tooltips, the Aa option, and no widget cut off at any width."""
import tkinter as tk
from tkinter import ttk

import pytest

from ui import theme
from ui.app_preview_view import AppPreviewView
from ui.compact import Compactor, FlowFrame


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    yield r
    r.destroy()


def _pump(r, n=15):
    for _ in range(n):
        r.update()


def test_flowframe_wraps_and_keeps_every_widget_visible(root):
    root.geometry("200x200")
    f = FlowFrame(root)
    f.pack(fill="x")
    btns = [ttk.Button(f, text="button %d" % i) for i in range(8)]
    for b in btns:
        f.add(b)
    _pump(root)
    assert all(b.winfo_ismapped() for b in btns)
    assert all(b.winfo_rootx() + b.winfo_width() <= root.winfo_rootx() + 205 for b in btns)
    assert len({b.winfo_rooty() for b in btns}) > 1                      # more than one row
    root.geometry("1400x200")
    _pump(root)
    assert len({b.winfo_rooty() for b in btns}) == 1                     # one row again


def test_compactor_icon_text_tooltip_and_toggle(root):
    c = Compactor(root, compact=True)
    b = ttk.Button(root)
    c.reg(b, "⚡", "Run now", "One click")
    assert b["text"] == "⚡"
    c.set_suffix(b, "(2)")
    assert b["text"] == "⚡ (2)"
    c.set_compact(False)
    assert b["text"] == "⚡ Run now (2)"


@pytest.mark.parametrize("width", [620, 1000, 1600])
def test_panel_has_no_cut_off_widget_and_toggle_keeps_all(root, width):
    theme.apply_theme(root, "light")
    root.geometry(f"{width}x600+0+0")
    v = AppPreviewView(root)
    v.pack(fill="both", expand=True)
    _pump(root, 30)
    right = root.winfo_rootx() + width + 1

    def seen():
        out = {}

        def walk(w):
            for ch in w.winfo_children():
                if isinstance(ch, FlowFrame):                        # the toolbars: every widget in them must be on screen
                    for it in ch.winfo_children():
                        for x in ([it] + it.winfo_children() if isinstance(it, ttk.Frame) else [it]):
                            if x.winfo_class() in ("TButton", "TMenubutton", "TCheckbutton", "TEntry", "TCombobox", "TSpinbox", "TRadiobutton"):
                                out[str(x)] = bool(x.winfo_ismapped()) and x.winfo_rootx() + x.winfo_width() <= right
                walk(ch)
        walk(v)
        return out
    a = seen()
    assert a and all(a.values()), [k for k, ok in a.items() if not ok]
    assert v.cpt.compact and v.include_btn["text"].startswith("➕")
    v.compact_btn.invoke()
    _pump(root, 30)
    b = seen()
    assert not v.cpt.compact and "Include..." in v.include_btn["text"] and set(a) == set(b) and all(b.values())
