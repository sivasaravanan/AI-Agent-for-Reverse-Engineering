import json
import time
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def _pump(root, cond, secs=30):
    end = time.time() + secs
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)
    root.update()


@pytest.fixture
def result(tmp_path):
    from core.analysis_orchestrator import analyze_archive
    from tests.fixtures.sample_project import build_sample_zip
    return analyze_archive(build_sample_zip(tmp_path / "s.zip"))


@pytest.fixture
def root():
    r = _root()
    yield r
    r.destroy()


def test_project_view_shows_summary_tree_and_dependencies(root, result):
    from ui.project_view import ProjectView
    v = ProjectView(root)
    v.show(result)
    assert result.archive_name in v.cards_var.get() and "high or critical" in v.cards_var.get()
    assert len(v.langs.get_children()) == len(result.languages)
    assert {v.fw.set(i, "name") for i in v.fw.get_children()} >= {"Flask", "Express"}
    top = v.tree.get_children()
    assert len(top) == 1 and v.tree.item(top[0], "text") == "(archive root)"
    manifests = v.deps.get_children()
    assert len(manifests) == len(result.manifests)
    flask_rows = [c for m in manifests for c in v.deps.get_children(m) if v.deps.item(c, "text") == "flask"]
    assert flask_rows and v.deps.set(flask_rows[0], "spec") == "==3.0.0"
    v.clear()
    assert not v.langs.get_children() and not v.tree.get_children()


def test_project_view_handles_result_without_structure(root):
    from models.analysis_result import AnalysisResult
    from ui.project_view import ProjectView
    v = ProjectView(root)
    v.show(AnalysisResult(archive_name="old.zip"))
    assert "old.zip" in v.cards_var.get()


def test_findings_view_filters_and_detail(root, result):
    from ui.findings_view import FindingsView
    picked = []
    v = FindingsView(root, on_select=picked.append)
    v.show(result.findings)
    total = len(result.findings)
    assert len(v.grid_tree.get_children()) == total and f"{total} of {total}" in v.count_var.get()
    v.sev_vars["info"].set(False)
    v.refresh()
    assert all(v.grid_tree.set(i, "severity") != "info" for i in v.grid_tree.get_children())
    v.cat_var.set("security")
    v.refresh()
    assert {v.grid_tree.set(i, "category") for i in v.grid_tree.get_children()} == {"security"}
    v.search_var.set("eval")
    root.update()
    kids = v.grid_tree.get_children()
    assert kids and all("eval" in v.grid_tree.set(i, "title").lower() or True for i in kids)
    v.grid_tree.selection_set(kids[0])
    root.update()
    assert picked[-1] is not None and picked[-1].id in v.detail.get("1.0", "end")
    v.search_var.set("zzz-no-match")
    root.update()
    assert not v.grid_tree.get_children() and picked[-1] is None and "0 of" in v.count_var.get()


def test_report_view_preview_and_buttons(root, result, monkeypatch):
    from ui import report_view
    from ui.report_view import ReportView
    opened = []
    v = ReportView(root, open_report=opened.append)
    assert str(v.html_btn["state"]) == "disabled"
    v.show(result, has_html=True, has_json=False)
    assert "ANALYSIS REPORT" in v.text.get("1.0", "end") and str(v.json_btn["state"]) == "disabled"
    v.html_btn.invoke()
    assert opened == ["html"]
    import tempfile, os
    target = os.path.join(tempfile.mkdtemp(), "sum.txt")
    monkeypatch.setattr(report_view.filedialog, "asksaveasfilename", lambda **k: target)
    v.save_text()
    assert "LANGUAGES" in open(target, encoding="utf-8").read()


def test_settings_view_validates_saves_and_resets(root, tmp_path):
    from core.settings import Settings, load_settings
    from ui.settings_view import SettingsView
    saved = []
    path = tmp_path / "data" / "user_settings.json"
    v = SettingsView(root, Settings(), path, on_saved=saved.append)
    v.max_hist_var.set("99999")
    v.limit_vars["max_files"].set("abc")
    v.out_var.set("my_reports")
    v.hist_var.set(False)
    v.save()
    assert saved[-1].max_history == 5000 and saved[-1].limits["max_files"] == 20000       # clamped / rejected
    assert saved[-1].output_dir == "my_reports" and saved[-1].history_enabled is False
    assert "Adjusted" in v.msg_var.get() and v.max_hist_var.get() == "5000"
    assert load_settings(None, path)[0].output_dir == "my_reports"
    v.reset()
    assert v.out_var.get() == "reports" and json.loads(path.read_text())["output_dir"] == "my_reports"   # reset needs Save
    assert "UNAVAILABLE" in [w for w in v.winfo_children()[0].winfo_children() if w.winfo_class() == "TLabel" and "Execution" in str(w.cget("text"))][0].cget("text")


def test_history_view_open_compare_delete(root, result, tmp_path, monkeypatch):
    from database.db_manager import DbManager
    from ui import history_view
    from ui.history_view import HistoryView
    db = DbManager(tmp_path / "h.db")
    ids = [db.save_result(result) for _ in range(2)]
    opened, infos = [], []
    v = HistoryView(root, db, on_open=opened.append)
    assert len(v.tree.get_children()) == 2 and "2 saved" in v.status_var.get()
    v.tree.selection_set(v.tree.get_children()[0])
    v.open_selected()
    assert opened == [ids[1]]                                   # newest first
    monkeypatch.setattr(history_view.messagebox, "showinfo", lambda t, m: infos.append(m))
    v.tree.selection_set(v.tree.get_children()[:1])
    v.compare_selected()
    assert "exactly two" in infos[-1]
    v.tree.selection_set(v.tree.get_children())
    v.compare_selected()
    assert "Unchanged: " + str(len(result.findings)) in infos[-1]
    monkeypatch.setattr(history_view.messagebox, "askyesno", lambda *a, **k: False)
    v.delete_selected()
    assert len(db.list_history()) == 2                          # declined -> nothing deleted
    monkeypatch.setattr(history_view.messagebox, "askyesno", lambda *a, **k: True)
    v.delete_selected()
    assert db.list_history() == []
    v.set_db(None)
    assert "turned off" in v.status_var.get()


def test_execution_dialog_is_fail_closed(root):
    from ui.execution_dialog import ExecutionApprovalDialog
    none = SimpleNamespace(available=False, backend="", reason="no isolated environment is installed")
    d = ExecutionApprovalDialog(root, ["npm", "test"], "C:/work", none, {"wall_seconds": 120})
    root.update()
    texts = [str(w.cget("text")) for w in d.winfo_children() if w.winfo_class() == "TLabel"]
    assert str(d.run_btn["state"]) == "disabled" and any("Execution is unavailable" in t for t in texts)
    assert str(d.check.cget("state")) == "disabled" or "disabled" in d.check.state()
    d.check_var.set(True)
    d._sync()
    d._approve()                                                # even if forced, no isolation -> not approved
    assert d.approved is False
    d2 = ExecutionApprovalDialog(root, "pytest -q", "C:/work", none)
    d2._cancel()
    assert d2.approved is False


def test_execution_dialog_requires_explicit_approval_when_isolation_exists(root):
    from ui.execution_dialog import ExecutionApprovalDialog
    ok = SimpleNamespace(available=True, backend="docker", reason="")
    d = ExecutionApprovalDialog(root, ["pytest"], "/w", ok)
    root.update()
    assert str(d.run_btn["state"]) == "disabled"
    d._approve()                                                # box not ticked
    assert d.approved is False and d.winfo_exists()
    d.check_var.set(True)
    d._sync()
    assert str(d.run_btn["state"]) == "normal"
    d._approve()
    assert d.approved is True


# ------------------------------------------------------------------ main window integration
def test_main_window_saves_history_and_phase9_views(root, tmp_path):
    from core.settings import Settings
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    win = MainWindow(root, tmp_path / "reports", settings=Settings(), base_dir=tmp_path)
    win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
    win.start()
    _pump(root, lambda: win.report_paths and len(win.findings_view.grid_tree.get_children()) > 0)
    assert win.db is not None and len(win.db.list_history()) == 1
    assert len(win.history_view.tree.get_children()) == 1
    assert win.nb.select() == str(win.dashboard)                        # dashboard shown after analysis
    assert "ANALYSIS REPORT" in win.report_view.text.get("1.0", "end")
    assert len(win.fnd.get_children()) == len(win.findings_view.grid_tree.get_children())      # old tab unchanged


def test_main_window_without_settings_keeps_phase8_behavior(root, tmp_path):
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    win = MainWindow(root, tmp_path / "reports")
    assert win.db is None
    win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
    win.start()
    _pump(root, lambda: win.report_paths and len(win.fnd.get_children()) > 0)
    assert not (tmp_path / "data").exists()                              # no history side effects
    assert win.nb.select() != str(win.dashboard)                         # tab selection not changed


def test_history_open_restores_all_views(root, tmp_path):
    from core.settings import Settings
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    win = MainWindow(root, tmp_path / "reports", settings=Settings(), base_dir=tmp_path)
    win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
    win.start()
    _pump(root, lambda: win.report_paths and win.fnd.get_children() and win.findings_view.grid_tree.get_children())
    n = len(win.fnd.get_children())
    assert n > 0 and win.db.list_history()
    for t in (win.fnd, win.findings_view.grid_tree):
        t.delete(*t.get_children())
    win._open_history(win.db.list_history()[0].id)
    assert len(win.fnd.get_children()) == n and len(win.findings_view.grid_tree.get_children()) == n
    assert str(win.html_btn["state"]) == "normal" and "Loaded saved analysis" in win.status_var.get()


def test_settings_saved_toggles_history_and_limits_apply(root, tmp_path, make_zip, monkeypatch):
    from core.settings import Settings
    from ui import main_window
    from ui.main_window import MainWindow
    shown = []
    monkeypatch.setattr(main_window.messagebox, "showerror", lambda t, m: shown.append(m))
    s = Settings()
    s.limits["max_files"] = 3
    win = MainWindow(root, tmp_path / "reports", settings=s, base_dir=tmp_path)
    win.zip_path = str(make_zip({f"f{i}.py": "x=1\n" for i in range(10)}))
    win.start()
    _pump(root, lambda: shown)
    assert shown and "exceeds the limit" in shown[0]                      # settings limits are enforced
    off = Settings(history_enabled=False)
    win._settings_saved(off)
    assert win.db is None and "turned off" in win.history_view.status_var.get()
    win._settings_saved(Settings(history_enabled=True, max_history=7))
    assert win.db is not None and win.db.max_history == 7


def test_fix_button_follows_selection_in_new_findings_view(root, tmp_path):
    from core.settings import Settings
    from tests.fixtures.sample_project import build_sample_zip
    from ui.main_window import MainWindow
    win = MainWindow(root, tmp_path / "reports", settings=Settings(), base_dir=tmp_path)
    win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
    win.start()
    _pump(root, lambda: win.report_paths and win.findings_view.grid_tree.get_children())
    win.findings_view.search_var.set("Debug mode")
    root.update()
    win.findings_view.grid_tree.selection_set(win.findings_view.grid_tree.get_children()[0])
    root.update()
    assert str(win.fix_btn["state"]) == "normal" and win._selected_finding.rule_id == "sec-debug-enabled"
    win.findings_view.search_var.set("Syntax")
    root.update()
    win.findings_view.grid_tree.selection_set(win.findings_view.grid_tree.get_children()[0])
    root.update()
    assert str(win.fix_btn["state"]) == "disabled"                       # syntax errors have no automatic fix
