import time

import pytest

tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def _pump(root, cond, secs=5):
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)


def test_dialog_requires_approval_then_applies_and_reverts(tmp_path, make_zip, monkeypatch):
    from core.analysis_orchestrator import analyze_archive
    from core.patch_manager import Workspace, propose_from_finding
    from ui import patch_dialog
    z = make_zip({"a.py": "DEBUG = True\n"})
    ws, _ = Workspace.create_from_zip(z, tmp_path / "ws")
    f = next(x for x in analyze_archive(z).findings if x.rule_id == "sec-debug-enabled")
    patch = propose_from_finding(f, ws)
    root = _root()
    try:
        dlg = patch_dialog.PatchDialog(root, patch, f, ws)
        root.update()
        assert "+DEBUG = False" in dlg.diff_text.get("1.0", "end")
        assert str(dlg.apply_btn["state"]) == "disabled"
        dlg.apply_clicked()                                  # clicking without approval must do nothing
        root.update()
        assert (ws.root / "a.py").read_text() == "DEBUG = True\n" and patch.status == "proposed"
        dlg.approve_var.set(True)
        dlg._sync_buttons()
        assert str(dlg.apply_btn["state"]) == "normal"
        dlg.apply_clicked()
        _pump(root, lambda: dlg.outcome is not None)
        assert (ws.root / "a.py").read_text() == "DEBUG = False\n"
        assert "NOT VERIFIED" in dlg.result.get("1.0", "end")
        assert str(dlg.revert_btn["state"]) == "normal"
        dlg.revert_clicked()
        assert (ws.root / "a.py").read_text() == "DEBUG = True\n" and patch.status == "reverted"
        dlg.close()
    finally:
        root.destroy()


def test_dialog_reports_refusal_without_crashing(tmp_path, make_zip, monkeypatch):
    from core.analysis_orchestrator import analyze_archive
    from core.patch_manager import Workspace, propose_from_finding
    from ui import patch_dialog
    shown = []
    monkeypatch.setattr(patch_dialog.messagebox, "showerror", lambda t, m, **k: shown.append(m))
    z = make_zip({"a.py": "DEBUG = True\n"})
    ws, _ = Workspace.create_from_zip(z, tmp_path / "ws")
    f = next(x for x in analyze_archive(z).findings if x.rule_id == "sec-debug-enabled")
    patch = propose_from_finding(f, ws)
    root = _root()
    try:
        dlg = patch_dialog.PatchDialog(root, patch, f, ws)
        (ws.root / "a.py").write_text("DEBUG = True  # edited\n")      # file drifts after the diff was shown
        dlg.approve_var.set(True)
        dlg.apply_clicked()
        _pump(root, lambda: bool(shown))
        assert shown and "changed since" in shown[0]
        assert "edited" in (ws.root / "a.py").read_text()
        dlg.close()
    finally:
        root.destroy()


def test_main_window_enables_propose_fix_only_for_supported_findings(tmp_path):
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    root = _root()
    try:
        win = MainWindow(root, tmp_path / "r")
        win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
        win.start()
        _pump(root, lambda: win.report_paths and win.fnd.get_children(), 30)
        assert str(win.fix_btn["state"]) == "disabled"
        for iid, f in win._findings.items():
            win.fnd.selection_set(iid)
            root.update()
            expected = f.rule_id in ("sec-debug-enabled", "sec-eval-exec", "sec-hardcoded-secret",
                                     "sec-tls-verify-off", "sec-pickle") and bool(f.file.endswith(".py"))
            if f.rule_id == "sec-pickle":
                continue
            assert (str(win.fix_btn["state"]) == "normal") == expected, f.rule_id
    finally:
        root.destroy()
