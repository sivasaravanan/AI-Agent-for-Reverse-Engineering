import pytest

pytest.importorskip("tkinter")   # ui.findings_view imports Tk at module level

from core.analysis_orchestrator import analyze_archive
from core.report_preview import build_text_report
from models.finding import Finding
from tests.fixtures.sample_project import build_sample_zip
from ui.findings_view import filter_findings, format_detail


def F(i, sev, cat, title, file="a.py", ev=""):
    return Finding(rule_id="r" + i, severity=sev, category=cat, title=title, file=file, line=1, evidence=ev, id=i)


FS = [F("1", "high", "syntax", "Syntax error"), F("2", "medium", "security", "eval used", "b.py", "x = eval(s)"),
      F("3", "info", "maintenance", "TODO marker", "c.py")]


def test_filter_by_severity_category_and_text():
    assert [f.id for f in filter_findings(FS, ["high", "info"])] == ["1", "3"]
    assert [f.id for f in filter_findings(FS, None, "security")] == ["2"]
    assert [f.id for f in filter_findings(FS, None, "", "EVAL")] == ["2"]            # case-insensitive, matches title/evidence
    assert [f.id for f in filter_findings(FS, None, "", "c.py")] == ["3"]
    assert filter_findings(FS, []) == [] and filter_findings(FS, None, "nope") == []
    assert len(filter_findings(FS)) == 3


def test_format_detail_contains_all_sections():
    t = format_detail(F("2", "medium", "security", "eval used", ev="x = eval(s)"))
    assert "[MEDIUM]" in t and "Evidence" in t and "Suggested remediation" in t and "Verification" in t


def test_text_report_preview(tmp_path):
    r = analyze_archive(build_sample_zip(tmp_path / "s.zip"))
    t = build_text_report(r)
    for needle in ("LANGUAGES", "FRAMEWORKS", "DATABASE INDICATORS", "ENTRY POINTS", "FINDINGS", "KNOWN LIMITATIONS", "Python"):
        assert needle in t
    assert "Sup3rS3cret" not in t and "Fake#Pass1" not in t
    assert "more (see the JSON/HTML report)" in build_text_report(r, max_findings=2)


def test_text_report_for_phase1_style_result_without_structure():
    from models.analysis_result import AnalysisResult
    t = build_text_report(AnalysisResult(archive_name="x.zip"))
    assert "FINDINGS  (none)" in t and "ENTRY POINTS" not in t
