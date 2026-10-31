"""Layout behaviour: maximized start, resizable/collapsible sidebar, scrolling pages, collapsible sections."""
import time

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")


def _pump(root, n=12):
    for _ in range(n):
        root.update()
        time.sleep(0.03)


def test_labelframe_collapses_and_restores_pack_and_grid():
    from ui.widgets import make_labelframes_collapsible
    root = _root()
    try:
        root.geometry("500x400")
        g = ttk.LabelFrame(root, text="Grid box")
        g.pack(fill="x")
        for r in range(4):
            ttk.Label(g, text=f"row {r}").grid(row=r, column=0)
        p = ttk.LabelFrame(root, text="Pack box")
        p.pack(fill="x")
        for r in range(4):
            ttk.Label(p, text=f"line {r}").pack()
        assert make_labelframes_collapsible(root) == 2
        _pump(root)
        full_g, full_p = g.winfo_height(), p.winfo_height()
        g._ase_toggle(); p._ase_toggle(); _pump(root)
        assert g.winfo_height() < full_g / 2 and p.winfo_height() < full_p / 2
        g._ase_toggle(); p._ase_toggle(); _pump(root)
        assert g.winfo_height() == full_g and p.winfo_height() == full_p
        assert [c.winfo_manager() for c in p.winfo_children() if isinstance(c, ttk.Label)] == ["pack"] * 4
    finally:
        root.destroy()


def test_scrollframe_shows_bars_only_when_needed():
    from ui.widgets import ScrollFrame
    root = _root()
    try:
        root.geometry("400x200")
        sf = ScrollFrame(root, min_width=600)
        sf.pack(fill="both", expand=True)
        for i in range(30):
            ttk.Label(sf.inner, text=f"item {i}").pack()
        _pump(root)
        assert sf.vbar.winfo_ismapped() and sf.hbar.winfo_ismapped()          # too small both ways
        root.geometry("900x900"); _pump(root, 20)
        assert not sf.vbar.winfo_ismapped() and not sf.hbar.winfo_ismapped()  # fits: no bars
        assert sf.inner.winfo_height() >= sf.canvas.winfo_height() - 2        # content stretches to the page
    finally:
        root.destroy()


def test_main_window_layout_features(tmp_path):
    from ui.main_window import MainWindow
    root = _root()
    try:
        win = MainWindow(root, tmp_path / "r", base_dir=tmp_path)
        _pump(root, 20)
        try:
            assert root.attributes("-zoomed") in (1, True) or root.state() == "zoomed"      # starts maximized
        except tk.TclError:
            pass
        assert win.paned.sashpos(0) >= 150                                    # sidebar shown at a sensible width
        win.paned.sashpos(0, 260); _pump(root)
        assert win.sidebar.winfo_width() == 260                                 # splitter resizes the sidebar
        win.sidebar.set_rail(True); _pump(root)
        assert win.sidebar.winfo_width() < 80                                   # collapsed to an icon rail
        win.sidebar.set_rail(False); _pump(root)
        assert win.sidebar.winfo_width() >= 150
        win._show_page(win.ai_panel)                                            # wrapped pages can still be selected by panel
        assert win.nb.tab(win.nb.select(), "text") == "AI Assistant"
        win._show_page(win.detail)
        assert win.nb.tab(win.nb.select(), "text") == "Finding detail"
        opts = win.opts_section
        opts.toggle(False); _pump(root)
        assert not opts.body.winfo_ismapped()
        opts.toggle(True); _pump(root)
        assert opts.body.winfo_ismapped()
    finally:
        root.destroy()


def test_sidebar_state_is_remembered(tmp_path):
    from ui.main_window import MainWindow
    root = _root()
    try:
        win = MainWindow(root, tmp_path / "r", base_dir=tmp_path)
        _pump(root, 15)
        win.sidebar.set_rail(True); _pump(root)
        root.update_idletasks()
    finally:
        root.destroy()
    root = _root()
    try:
        win2 = MainWindow(root, tmp_path / "r", base_dir=tmp_path)
        _pump(root, 25)
        assert win2.sidebar.rail is True and win2.sidebar.winfo_width() < 80
    finally:
        root.destroy()
