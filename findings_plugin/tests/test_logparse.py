from pathlib import Path
from findings_plugin import analyze_log_text, analyze_log_file, Kind, Severity

FIX = Path(__file__).parent / "fixtures" / "mixed.log"


def by_rule(res, rule):
    return [f for f in res.findings if f.rule_id == rule]


def res():
    return analyze_log_file(FIX)


def test_python_traceback_location_and_chain():
    r = res()
    ke, rt = by_rule(r, "KeyError")[0], by_rule(r, "RuntimeError")[0]
    assert (ke.file_path, ke.line) == ("C:/proj/app/handlers.py", 12)   # innermost frame
    assert ke.kind is Kind.RUNTIME_ERROR and ke.language == "python"
    assert ke.trace_id == rt.trace_id            # chained exceptions linked
    assert ke.debug_steps[0].startswith("Open C:/proj/app/handlers.py at line 12")
    assert ke.extra["guidance_source"] and ke.explanation


def test_java_dotnet_node():
    r = res()
    npe = by_rule(r, "java.lang.NullPointerException")[0]
    assert (npe.file_path, npe.line, npe.language) == ("Service.java", 88, "java")
    assert npe.extra["caused_by"][0].startswith("Caused by: java.io.IOException")
    nre = by_rule(r, "System.NullReferenceException")[0]
    assert (nre.file_path, nre.line, nre.language) == ("C:/src/Shop/Orders.cs", 57, "csharp")
    te = by_rule(r, "TypeError")[0]
    assert (te.file_path, te.line, te.column, te.language) == ("/app/index.js", 3, 9, "javascript")


def test_pytest_failure_has_no_invented_line():
    f = by_rule(res(), "pytest-failed")[0]
    assert f.kind is Kind.TEST_FAILURE and f.file_path == "tests/test_calc.py" and f.line is None


def test_standalone_syntax_error():
    f = by_rule(res(), "SyntaxError")[0]
    assert f.kind is Kind.PARSER_ERROR and (f.file_path, f.line) == ("broken.py", 3)


def test_generic_error_lines_and_redaction():
    r = res()
    crit = [f for f in r.findings if f.rule_id == "log-critical"][0]
    assert crit.severity is Severity.HIGH and (crit.file_path, crit.line) == ("jobs/runner.py", 77)
    for f in r.findings:
        assert "hunter2" not in f.title + f.evidence and "abc123secret" not in f.title + f.evidence
    err = [f for f in r.findings if f.rule_id == "log-error"][0]
    assert "[REDACTED]" in err.evidence and err.file_path is None and err.line is None
    assert not [f for f in r.findings if "ERROR_COUNT" in f.title]  # not a log level


def test_duplicates_counted_not_repeated():
    tb = "Traceback (most recent call last):\n  File \"a.py\", line 1, in f\n    x\nValueError: bad\n"
    r = analyze_log_text(tb * 3)
    assert len(r.findings) == 1 and r.findings[0].extra["occurrences"] == 3


def test_empty_and_truncated_are_honest():
    assert "does not prove" in " ".join(analyze_log_text("all fine\n").notes)
    r = analyze_log_text("Traceback (most recent call last):\n  File \"a.py\", line 1, in f\n")
    assert r.findings == [] and "Truncated traceback" in r.notes[0]


def test_limit_and_big_file_tail(tmp_path):
    lines = "\n".join(f"ERROR unique problem {i}" for i in range(20))
    r = analyze_log_text(lines, max_findings=5)
    assert len(r.findings) == 5 and r.truncated
    big = tmp_path / "big.log"
    big.write_text("ERROR old\n" * 1000 + "ERROR newest\n")
    r2 = analyze_log_file(big, max_bytes=200)
    assert r2.truncated and "last 200 bytes" in r2.notes[0]
    assert any("newest" in f.title for f in r2.findings)


def test_utf16_log(tmp_path):
    p = tmp_path / "u16.log"
    p.write_bytes("ERROR disk full\n".encode("utf-16"))
    assert analyze_log_file(p).findings[0].title == "disk full"
