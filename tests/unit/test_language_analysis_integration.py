"""Phase 3 integration: analyzers/*, core.language_analysis, orchestrator, report, UI wiring."""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from analyzers import ANALYZERS, PythonAnalyzer, SqlAnalyzer, DotNetAnalyzer
from analyzers.base import ToolResult, convert_finding, materialize, safe_relative
from core.analysis_orchestrator import analyze_archive
from core.language_analysis import ANALYZABLE_EXT, run_language_analysis, select_sources
from core.report_generator import to_html, to_json

ROOT = Path(__file__).resolve().parents[2]


def make_zip(tmp_path, files: dict, name="p.zip") -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return p


# ------------------------------------------------------------------ analyzers package
def test_five_analyzers_registered_with_languages():
    names = {a.name for a in ANALYZERS}
    assert names == {"python", "javascript", "dotnet", "java", "sql"}
    assert "TypeScript" in next(a for a in ANALYZERS if a.name == "javascript").languages
    assert "VB.NET" in DotNetAnalyzer.languages


def test_python_analyzer_always_available_and_reports_syntax_error_as_parser_error():
    a = PythonAnalyzer()
    assert a.is_available() is True            # ast is built in
    res = a.analyze({"pkg/bad.py": "def f(:\n  pass\n"})
    assert isinstance(res, ToolResult)
    syn = [f for f in res.findings if f.category == "syntax"]
    assert syn and syn[0].file == "pkg/bad.py" and syn[0].line == 1
    assert syn[0].verification == "tool-reported"


def test_missing_tool_is_reported_not_faked(monkeypatch):
    import plugins.static_analysis.base as pb
    monkeypatch.setattr(pb.shutil, "which", lambda _n: None)
    res = SqlAnalyzer().analyze({"q.sql": "select 1;\n"})
    assert res.status == "unavailable" and res.findings == []
    assert "nothing was installed" in res.note.lower()


def test_dotnet_skipped_without_approval():
    res = DotNetAnalyzer().analyze({"A.cs": "class A {}", "A.csproj": "<Project/>"})
    assert res.status in ("skipped", "unavailable")
    assert res.findings == []


# ------------------------------------------------------------------ safety helpers
@pytest.mark.parametrize("bad", ["../x.py", "/etc/passwd", "C:/x.py", "a/../../b.py", "a\x00b.py", ""])
def test_safe_relative_rejects(bad):
    assert safe_relative(bad) is None


def test_materialize_skips_unsafe_names(tmp_path):
    n = materialize({"ok/a.py": "x=1", "../evil.py": "x=2", "/abs.py": "x=3"}, tmp_path / "d")
    assert n == 1 and (tmp_path / "d/ok/a.py").exists()
    assert not (tmp_path / "evil.py").exists()


def test_select_sources_filters():
    s = select_sources({"a.py": "", "README.md": "", "tsconfig.json": "{}", "x.csproj": "", "n.txt": ""})
    assert set(s) == {"a.py", "tsconfig.json", "x.csproj"}


def test_convert_finding_never_upgrades_certainty():
    from plugins.static_analysis import Finding as PF
    pf = PF(analyzer="ruff", kind="lint_warning", severity="low", message="unused", file="a.py", line=3, rule="F401",
            confidence="high")
    f = convert_finding(pf)
    assert f.category == "lint" and f.verification == "tool-reported" and f.line == 3
    assert "lint warning" in f.explanation and "compiler" not in f.explanation.lower()


# ------------------------------------------------------------------ orchestrator + reports
def test_no_analyzable_sources_is_not_run():
    findings, summary = run_language_analysis({"README.md": "hi"})
    assert findings == [] and summary["ran"] is False


def test_pipeline_adds_language_analysis_and_dedupes_phase1_syntax(tmp_path):
    z = make_zip(tmp_path, {"app/main.py": "def broken(:\n    pass\n", "q.sql": "select 1;\n"})
    r = analyze_archive(z)
    assert r.language_analysis and r.language_analysis["ran"] is True
    assert any(t["analyzer"] == "python-ast" for t in r.language_analysis["tool_runs"])
    syntax = [f for f in r.findings if f.category == "syntax" and f.file == "app/main.py" and f.line == 1]
    assert len(syntax) == 1 and syntax[0].rule_id == "py-syntax"      # Phase 1 finding kept, duplicate dropped
    assert all(f.id.startswith("F-") for f in r.findings)


def test_pipeline_can_disable_language_analysis(tmp_path):
    r = analyze_archive(make_zip(tmp_path, {"a.py": "x = 1\n"}), language_analysis=False)
    assert r.language_analysis is None


def test_reports_contain_phase3_section(tmp_path):
    r = analyze_archive(make_zip(tmp_path, {"a.py": "import os\nx = 1\n"}))
    data = json.loads(to_json(r))
    assert data["language_analysis"]["ran"] is True
    assert data["structure"] is not None and data["schema_version"] == "1.0"      # Phase 1/2 keys intact
    html = to_html(r)
    assert "Language-specific static analysis (Phase 3)" in html and "python-ast" in html


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_ruff_findings_are_lint_not_compiler(tmp_path):
    z = make_zip(tmp_path, {"a.py": "import os\nimport sys\nprint(1)\n"})
    r = analyze_archive(z)
    lint = [f for f in r.findings if f.rule_id.startswith("ruff:")]
    assert lint and all(f.category == "lint" for f in lint)
    run = next(t for t in r.language_analysis["tool_runs"] if t["analyzer"] == "ruff")
    assert run["version"] and run["command"] and run["exit_code"] is not None


def test_secrets_in_tool_output_are_redacted(tmp_path):
    z = make_zip(tmp_path, {"a.py": 'password = "hunter2hunter2"\ndef f(:\n  pass\n'})
    r = analyze_archive(z)
    blob = to_json(r)
    assert "hunter2hunter2" not in blob


def test_malformed_zip_still_graceful(tmp_path):
    from core.archive_manager import ArchiveError
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ArchiveError):
        analyze_archive(bad)


def test_ui_has_phase3_tabs():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    try:
        from ui.main_window import MainWindow
        mw = MainWindow(root, "reports")
        titles = [mw.nb.tab(i, "text") for i in range(mw.nb.index("end"))]
        assert "Static analysis" in titles and "Static analysis (tools)" in titles
    finally:
        root.destroy()


# ------------------------------------------------------------------ function/class map (objective 8)
def test_symbol_map_python_ast_exact_lines():
    from core.symbol_map import map_symbols
    src = "class A:\n    def m(self):\n        pass\n\nasync def top():\n    pass\n"
    s = map_symbols({"pkg/a.py": src})
    syms = {(x["kind"], x["owner"], x["name"], x["line"]) for x in s["files"][0]["symbols"]}
    assert ("class", "", "A", 1) in syms and ("method", "A", "m", 2) in syms and ("function", "", "top", 5) in syms
    assert s["files"][0]["method"] == "ast"


def test_symbol_map_regex_languages_labelled_and_negatives():
    from core.symbol_map import map_symbols
    s = map_symbols({"a.js": "export class Foo {}\nfunction bar() {}\nconst baz = (x) => x\n// function ghost() {}\n",
                     "B.java": "public class B {\n  public int run(int x) {\n  }\n}\n",
                     "n.py": "def broken(:\n", "notes.txt": "def nope():"})
    names = {x["name"] for f in s["files"] for x in f["symbols"]}
    assert {"Foo", "bar", "baz", "B", "run"} <= names and "ghost" not in names and "nope" not in names
    assert all(f["method"] == "regex" for f in s["files"])


def test_symbols_in_pipeline_report(tmp_path):
    r = analyze_archive(make_zip(tmp_path, {"a.py": "def f():\n    pass\n"}), language_analysis=False)
    assert r.symbols["totals"]["functions"] == 1
    assert "Functions and classes" in to_html(r) and json.loads(to_json(r))["symbols"]["totals"]["files"] == 1
