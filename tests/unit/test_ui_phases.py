"""Tkinter tests for the merged UI (skipped without Tk or a display; run with xvfb-run)."""
import json
import time

import pytest

tk = pytest.importorskip("tkinter")

from core.ai_settings import load_settings, save_settings  # noqa: E402
from security.consent_policy import ConsentGrant  # noqa: E402
from tests.unit.test_phase7_ai import FakeTransport, ollama_ok, chat_ok  # noqa: E402


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def _pump(root, cond, timeout=20):
    end = time.time() + timeout
    while not cond():
        root.update()
        time.sleep(0.01)
        assert time.time() < end, "timed out waiting for UI condition"
    for _ in range(3):
        root.update()


def _tab_names(nb):
    return [nb.tab(i, "text") for i in range(nb.index("end"))]


@pytest.fixture
def window(tmp_path, monkeypatch):
    from tests.fixtures.sample_project import build_sample_zip
    from ui import main_window
    monkeypatch.setattr(main_window.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(main_window.messagebox, "showerror", lambda *a, **k: None)
    root = _root()
    win = main_window.MainWindow(root, tmp_path / "reports")
    win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
    yield root, win
    root.destroy()


def _analyze(root, win):
    win.start()
    _pump(root, lambda: not win.worker.is_alive() and win.q.empty() and win._last_result is not None)


def test_every_phase_has_a_tab(window):
    root, win = window
    names = _tab_names(win.nb)
    for expected in ("Static tools", "Databases", "Findings Explorer", "Build & Test", "AI Assistant", "Findings"):
        assert expected in names, names


def test_analysis_fills_phase_tabs_and_findings_explorer(window):
    root, win = window
    _analyze(root, win)
    assert win.dbt.get_children() and win.tools.get_children()
    assert len(win.findings_plugin.store) == len(win._last_result.findings) > 0
    assert str(win.html_btn["state"]) == "normal"


def test_ai_off_by_default_and_app_still_works(window):
    root, win = window
    assert win.ai_panel.settings["ai"]["enabled"] is False and win.ai_panel.router.providers == []
    _analyze(root, win)
    assert win._last_result.findings


def _enable_local_ai(win, tmp_path, transport):
    s = load_settings(tmp_path / "none.json")
    s["ai"]["enabled"] = True
    s["ai"]["providers"][0]["model"] = "llama3"
    panel = win.ai_panel
    panel.settings, panel.transport = s, transport
    panel.enabled.set(True)
    panel.prov.set("ollama")
    panel._load_provider_fields()
    panel._rebuild_router()
    return panel


def test_local_ai_explain_flow_marks_output_unverified(window, tmp_path):
    root, win = window
    _analyze(root, win)
    tr = FakeTransport(ollama_ok("Likely SQL injection (hypothesis)."))
    panel = _enable_local_ai(win, tmp_path, tr)
    win.fnd.selection_set(win.fnd.get_children()[0])
    panel.start("explain_finding")
    _pump(root, lambda: panel.last is not None)
    text = panel.out.get("1.0", "end")
    assert "NOT verified" in text and "Likely SQL injection" in text and panel.find_btn["state"].__str__() == "normal"
    assert win._last_result.ai_notes and win._last_result.ai_notes[0]["verified"] is False
    panel.to_findings()
    kinds = {f.kind.value for f in win.findings_plugin.store.all()}
    assert "ai_hypothesis" in kinds


def test_ask_ai_button_hook_from_findings_tab(window, tmp_path):
    root, win = window
    _analyze(root, win)
    tr = FakeTransport(ollama_ok("explained"))
    panel = _enable_local_ai(win, tmp_path, tr)
    win._ask_ai_about(win.findings_plugin.store.all()[0])
    _pump(root, lambda: panel.last is not None)
    assert panel.last.text == "explained"


def test_external_provider_shows_consent_dialog_and_cancel_sends_nothing(window, tmp_path, monkeypatch):
    root, win = window
    _analyze(root, win)
    tr = FakeTransport(chat_ok("never"))
    s = load_settings(tmp_path / "none.json")
    s["ai"]["enabled"] = True
    s["ai"]["providers"][1].update(enabled=True, model="gpt-x")
    s["ai"]["providers"][0]["enabled"] = False
    panel = win.ai_panel
    panel.settings, panel.transport = s, tr
    panel.enabled.set(True)
    panel.prov.set("openai_compatible")
    panel._load_provider_fields()
    panel.key.set("sk-session-key-123456")
    panel._rebuild_router()
    shown = {}

    class Declined:
        def __init__(self, parent, req):
            shown["hosts"], shown["text"] = req.external_hosts, req.prompt
            self.grant = None
    monkeypatch.setattr("ui.ai_panel.ConsentDialog", Declined)
    win.fnd.selection_set(win.fnd.get_children()[0])
    panel.start("explain_finding")
    root.update()
    assert shown["hosts"] == ["api.openai.com"] and "<<<ITEM" in shown["text"]
    assert tr.calls == [] and "no consent" in panel.status.get().lower()


def test_consent_dialog_requires_ticking_the_box(monkeypatch):
    from core.ai_assistant import PreparedRequest
    from security.consent_policy import SelectedItem, build_payload
    from ui.ai_panel import ConsentDialog
    root = _root()
    try:
        monkeypatch.setattr(tk.Toplevel, "wait_window", lambda self, *a: None)
        pv = build_payload([SelectedItem("a.py", "password = 'abc12345'\nx=1")])
        req = PreparedRequest("explain_finding", "PROMPT " + pv.text, "s", pv, external_hosts=["api.x.com"], needs_consent=True)
        dlg = ConsentDialog(root, req)
        assert str(dlg.ok["state"]) == "disabled" and dlg.grant is None
        dlg.var.set(True)
        dlg._sync()
        assert str(dlg.ok["state"]) == "normal"
        dlg._ok()
    finally:
        root.destroy()


def test_settings_save_never_writes_session_key(window, tmp_path):
    root, win = window
    panel = win.ai_panel
    panel.settings_path = tmp_path / "settings.json"
    panel.key.set("sk-should-never-be-saved-1234")
    panel.url.set("https://example.com/v1")
    panel.save()
    saved = (tmp_path / "settings.json").read_text()
    assert "sk-should-never-be-saved" not in saved


def test_exec_result_flows_to_report_and_findings_log(window):
    root, win = window
    _analyze(root, win)
    from plugins.build_execution import ExecutionResult
    before = len(win.findings_plugin.store)
    res = ExecutionResult(status="failed", candidate_id="python:pytest", exit_code=1,
                          stderr='Traceback (most recent call last):\n  File "t.py", line 3, in <module>\n    1/0\nZeroDivisionError: division by zero\n')
    win._exec_result(res)
    assert win._last_result.execution_results[0]["candidate_id"] == "python:pytest"
    assert len(win.findings_plugin.store) > before


def test_ai_patch_opens_review_dialog_with_ai_source(window, tmp_path, monkeypatch):
    from core.patch_manager import Workspace
    import ui.main_window as mw
    root, win = window
    _analyze(root, win)
    ws, _ = Workspace.create_from_zip(win.zip_path, tmp_path / "ws")
    win.workspace, win.workspace_zip = ws, win.zip_path
    captured = {}

    class FakeDialog:
        def __init__(self, parent, patch, finding, workspace, runner=None, on_close=None):
            captured.update(patch=patch, runner=runner)
    monkeypatch.setattr(mw, "PatchDialog", FakeDialog)
    original = ws.read_text("shop/app/main.py")
    win._ai_patch("shop/app/main.py", original.replace("DEBUG = True", "DEBUG = False"), "", "AI suggestion")
    assert captured["patch"].source == "ai-unverified" and "unverified" in captured["patch"].caution
    assert ws.read_text("shop/app/main.py") == original                 # nothing applied by opening the dialog
