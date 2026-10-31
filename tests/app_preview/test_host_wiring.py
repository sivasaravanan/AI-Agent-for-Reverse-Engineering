"""Regression: the main window must build with the App Preview tab, and a project picked in the panel reaches the host."""
import pytest

tk = pytest.importorskip("tkinter")


def test_main_window_builds_and_panel_announces_its_source(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from ui.main_window import MainWindow
        mw = MainWindow(root, out_dir=tmp_path / "out")
        titles = [mw.nb.tab(i, "text") for i in range(mw.nb.index("end"))]
        assert "App Preview" in titles
        seen = []
        mw.preview_view.on_source_changed = seen.append
        mw.preview_view._announce_source("x.zip")
        assert seen == ["x.zip"]
    finally:
        root.destroy()


def test_panel_accepts_the_host_callback(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from ui.app_preview_view import AppPreviewView
        got = []
        v = AppPreviewView(root, on_source_changed=got.append)
        v._announce_source("p.zip")
        assert got == ["p.zip"]
    finally:
        root.destroy()
