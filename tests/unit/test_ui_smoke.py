import queue
import time

import pytest

tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def test_main_window_runs_analysis_without_blocking(tmp_path):
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    root = _root()
    try:
        win = MainWindow(root, tmp_path / "reports")
        win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
        win.start()
        ticks = 0
        deadline = time.time() + 30
        while win.worker.is_alive() or not win.q.empty():
            root.update()           # UI stays responsive while the worker runs
            ticks += 1
            assert time.time() < deadline
            time.sleep(0.01)
        for _ in range(5):
            root.update()
            time.sleep(0.12)
        assert ticks > 0
        assert win.report_paths["html"].exists() and win.report_paths["json"].exists()
        assert len(win.fnd.get_children()) > 0
        assert str(win.html_btn["state"]) == "normal"
    finally:
        root.destroy()


def test_main_window_reports_invalid_zip_gracefully(tmp_path, monkeypatch):
    from ui import main_window
    from ui.main_window import MainWindow
    shown = []
    monkeypatch.setattr(main_window.messagebox, "showerror", lambda t, m: shown.append(m))
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope" * 50)
    root = _root()
    try:
        win = MainWindow(root, tmp_path / "r")
        win.zip_path = str(bad)
        win.start()
        end = time.time() + 10
        while time.time() < end and not shown:
            root.update()
            time.sleep(0.02)
        assert shown and "not a valid ZIP" in shown[0]
        assert str(win.analyze_btn["state"]) == "normal"
    finally:
        root.destroy()
