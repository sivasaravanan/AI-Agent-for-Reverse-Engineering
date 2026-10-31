import ast
import pytest
from findings_plugin import (register, coerce_finding, coerce_many, from_syntax_error, Severity, Kind, Finding)
from findings_plugin.detail import format_detail
from findings_plugin.report import render_html_section, findings_to_json_report
from findings_plugin.util import redact, detect_language
from findings_plugin import make_ai_finding


def test_legacy_dict_mapping_and_no_fabrication():
    f = coerce_finding({"level": "warning", "message": "TODO found", "file": "a\\b.py", "lineno": 0})
    assert f.severity is Severity.MEDIUM and f.line is None and f.file_path == "a/b.py"
    assert f.language == "python" and f.kind is Kind.HEURISTIC
    g = coerce_finding({"message": "x"})
    assert g.extra["severity_assumed"] is True
    assert coerce_finding({"severity": "error", "msg": "m", "line": "7"}).line == 7


def test_bad_items_reported_not_dropped_silently():
    ok, errs = coerce_many([{"message": "a"}, {"severity": "high"}, 42, "plain"])
    assert len(ok) == 2 and len(errs) == 2


def test_syntax_error_adapter():
    try:
        ast.parse("def f(:\n  pass", filename="bad.py")
    except SyntaxError as e:
        f = from_syntax_error(e)
    assert f.kind is Kind.PARSER_ERROR and f.file_path == "bad.py" and f.line == 1 and f.confidence.value == "confirmed"
    assert f.explanation  # rule-table guidance


def test_plugin_headless_flow(tmp_path):
    p = register()
    assert p.add([{"message": "a", "severity": "high"}, object]) == 1 and p.last_errors
    res = p.analyze_log_text("ERROR boom in x.py:4\n", "t.log")
    assert len(res.findings) == 1 and len(p.store) == 2
    p.export_json(tmp_path / "o.json")
    p.export_html(tmp_path / "o.html")
    assert '"schema": "findings/1"' in (tmp_path / "o.json").read_text()
    assert "<section>" in p.html_section()


def test_html_escapes_and_flags_ai():
    ai = make_ai_finding("<script>alert(1)</script>", explanation="a & b")
    html = render_html_section([ai])
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "unverified" in html
    assert "ai_hypothesis" in html


def test_detail_marks_ai_and_unknown_location():
    ai = make_ai_finding("maybe")
    secs = dict(format_detail(ai))
    assert "!Notice" in secs and secs["Location"].startswith("unknown")


def test_redact_and_language():
    assert "hunter2" not in redact("Password=hunter2;Server=x")
    assert "s3cret" not in redact("postgres://bob:s3cret@host/db")
    assert "abcdefghijk1234" not in redact("Authorization: Bearer abcdefghijk1234")
    assert detect_language("X\\Y.CS") == "csharp" and detect_language("README") is None
