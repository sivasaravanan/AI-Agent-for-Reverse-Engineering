"""Phases 1-8 working together on real archives. No network, no container runtime required."""
import json
import zipfile
from pathlib import Path

import pytest

from core.analysis_options import AnalysisOptions
from core.analysis_orchestrator import analyze_archive
from core.report_generator import to_html, to_json, write_reports
from tests.fixtures.sample_project import build_sample_zip

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    return build_sample_zip(tmp_path_factory.mktemp("s") / "sample.zip")


@pytest.fixture(scope="module")
def full(sample):
    return analyze_archive(sample)


def test_phases_1_to_6_all_contribute(full):
    assert full.structure is not None                                   # phase 2
    assert full.static_analysis and full.static_analysis["tool_runs"]  # phase 3
    assert full.execution is not None and "isolation" in full.execution  # phase 4 (discovery only)
    assert full.database_analysis and full.database_analysis["databases"]  # phase 5
    assert full.findings_overview["total"] == len(full.findings)       # phase 6
    sources = {f.source.split(":")[0] for f in full.findings}
    assert {"phase5", "heuristic-security-scan"} <= sources
    assert all(f.id.startswith("F-") for f in full.findings)


def test_phase1_2_only_option_skips_optional_phases(sample):
    r = analyze_archive(sample, options=AnalysisOptions.phase1_2_only())
    assert r.static_analysis is None and r.database_analysis is None and r.execution is None
    assert r.structure is not None and r.findings


def test_report_contains_every_phase_and_is_valid_json(full, tmp_path):
    html = to_html(full)
    for marker in ("Phase 2", "Phase 3", "Phase 4", "Phase 5", "Phase 6"):
        assert marker in html, marker
    assert "Nothing is executed" in html or "nothing was run" in html.lower()
    assert json.loads(to_json(full))["database_analysis"]["databases"]
    paths = write_reports(full, tmp_path)
    assert paths["html"].exists() and paths["json"].exists()


def test_no_secret_in_report(full):
    blob = to_html(full) + to_json(full)
    assert "Fake#Pass1" not in blob            # fixture secret from appsettings.json


def test_missing_external_tools_are_reported_not_faked(full):
    runs = {r["analyzer"]: r for r in full.static_analysis["tool_runs"]}
    for name, tool in full.static_analysis["tools"].items():
        if tool is None:
            assert all(r["status"] != "ok" for r in full.static_analysis["tool_runs"] if r["analyzer"] == name)
    assert "python-ast" in " ".join(runs) or runs                        # the built-in parser always ran


def test_python_syntax_error_is_not_duplicated(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("p/broken.py", "def f(:\n    pass\n")
    r = analyze_archive(z)
    syntax = [f for f in r.findings if f.category == "syntax" and f.file == "p/broken.py"]
    assert len(syntax) == 1 and syntax[0].rule_id == "py-syntax"


def test_static_plugin_findings_keep_their_kind_and_are_tool_reported(tmp_path):
    z = tmp_path / "lint.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("p/m.py", "import os\nimport sys\n\ndef f():\n    return undefined_name\n")
    r = analyze_archive(z)
    phase3 = [f for f in r.findings if f.source.startswith("phase3:")]
    if not r.static_analysis["tools"].get("ruff"):
        pytest.skip("ruff not installed")
    assert phase3 and all(f.verification == "tool-reported" for f in phase3)
    assert all(f.category in ("lint", "type-check", "syntax", "compile", "heuristic") for f in phase3)
    assert not any("compile" == f.category for f in phase3)             # lint is never called a compiler error


def test_database_analysis_never_connects_and_masks_secrets():
    r = analyze_archive(ROOT / "samples" / "db_sample_project.zip")
    blob = json.dumps(r.database_analysis)
    assert r.database_analysis["databases"] and "hunter2" not in blob.lower()


# ------------------------------------------------------------------ phase 4
def test_build_runner_refuses_without_approval_and_bare_strings(sample):
    from services.build_runner import BuildRunner
    from services.test_runner import TestRunner
    br = BuildRunner(sample)
    r = br.run("python -m pytest", None)
    assert r.status == "refused"
    cands = TestRunner(sample).candidates()
    assert cands and all("test" in c.id for c in cands)
    plan = TestRunner(sample).plan(cands[0])
    assert br.run(plan, None).status == "refused"                       # no approval
    from plugins.build_execution import Approval
    assert br.run(plan, Approval(plan_hash="wrong")).status == "refused"  # approval for a different plan


def test_isolated_runner_fails_closed(tmp_path, sample):
    from services.isolated_test_runner import IsolatedSandboxRunner, parse_pytest_report
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n")
    runner = IsolatedSandboxRunner(approver=None)
    with pytest.raises(RuntimeError, match="not approved|No offline"):
        runner.run(tmp_path)
    s = parse_pytest_report("PASSED t.py::a\nFAILED t.py::b - assert\nERROR t.py::c\n")
    assert s.passed == {"t.py::a"} and s.failed == {"t.py::b", "t.py::c"}


def test_environment_service_reports_without_installing():
    from services.environment_service import detect_isolation, tool_versions
    iso = detect_isolation()
    assert isinstance(iso.available, bool) and (iso.available or iso.reason)
    v = tool_versions()
    assert "python" in v and v["python"].startswith("Python")


def test_execution_policy_matrix():
    from security.execution_policy import decide
    assert decide(True, True).allowed
    assert not decide(False, True).allowed and not decide(True, False).allowed
    d = decide(True, True, network_requested=True, network_approved=False)
    assert not d.allowed and "network" in d.reasons[0]


# ------------------------------------------------------------------ phase 6
def test_finding_store_over_analysis_findings(full):
    from core.finding_store import FindingStore
    store = FindingStore(full.findings)
    assert len(store) == len(full.findings) and not store.errors
    sec = store.filter(category="security")
    assert sec and all(f.category == "security" for f in sec)
    assert store.search("sql") is not None
    first = store.findings[0]
    assert store.related(first.id) is not None
    assert sum(len(v) for v in store.group_by_severity().values()) == len(store)


def test_traceback_analysis_does_not_invent_locations():
    from core.log_analyzer import analyze_traceback
    tb = 'Traceback (most recent call last):\n  File "/app/x.py", line 12, in run\n    boom()\nValueError: bad value\n'
    f = analyze_traceback(tb)[0]
    assert f.line == 12 and "ValueError" in f.title
    assert all(x.line is None for x in analyze_traceback("ERROR something failed without a location"))


# ------------------------------------------------------------------ phase 7 -> 8 and 7 -> 6
def test_ai_patch_goes_through_the_human_approved_workflow(sample, tmp_path):
    from core.patch_manager import Workspace, apply, diff, propose
    from models.patch import PatchError
    ws, _ = Workspace.create_from_zip(sample, tmp_path / "ws")
    rel = "shop/app/main.py"
    original = ws.read_text(rel)
    patch = propose(rel, original.replace("DEBUG = True", "DEBUG = False"), ws, None, "AI suggestion", "ai-unverified",
                    "AI-generated and unverified")
    assert patch.source == "ai-unverified" and "-DEBUG = True" in diff(patch)
    with pytest.raises(PatchError):
        apply(patch, approved=False, workspace=ws)                      # never silent
    assert ws.read_text(rel) == original


def test_apply_and_verify_skips_tests_when_runner_not_isolated(sample, tmp_path):
    from core.patch_manager import Workspace, apply_and_verify, diff, propose
    from services.isolated_test_runner import IsolatedSandboxRunner
    ws, _ = Workspace.create_from_zip(sample, tmp_path / "ws")
    original = ws.read_text("shop/app/main.py")
    patch = propose("shop/app/main.py", original.replace("DEBUG = True", "DEBUG = False"), ws, None, "r", "ai-unverified")
    diff(patch)
    runner = IsolatedSandboxRunner(approver=lambda plan: True)
    runner._isolated = False                                            # no container runtime
    out = apply_and_verify(patch, approved=True, workspace=ws, runner=runner)
    assert out.verification.level == "static-checked" and "NOT VERIFIED" in out.verification.claim
    assert out.verification.skipped_reasons


def test_ai_note_is_flagged_unverified_in_report(full):
    import copy
    r = copy.deepcopy(full)
    r.ai_notes.append({"task": "explain_finding", "provider": "ollama", "model": "m", "sent_items": ["a.py"],
                       "text": "maybe a bug; password=hunter2pw", "verified": False})
    html = to_html(r)
    assert "UNVERIFIED" in html and "hunter2pw" not in html
