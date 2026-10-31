"""Tests for plugins/static_analysis. External-tool tests skip when the tool is not installed."""
import json
import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.static_analysis import Adapter, ArchiveRejected, KINDS, Options, analyze, register_adapter  # noqa: E402
from plugins.static_analysis import runner  # noqa: E402
from plugins.static_analysis.base import run_command, sanitized_env  # noqa: E402
from plugins.static_analysis.other_adapters import parse_javac, parse_msbuild  # noqa: E402
from plugins.static_analysis.safe_extract import ExtractLimits, safe_extract  # noqa: E402

has = lambda tool: shutil.which(tool) is not None  # noqa: E731


def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def only(rep, analyzer):
    return [f for f in rep.findings if f.analyzer == analyzer]


# ---------------------------------------------------------------- archive safety
@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/abs/evil.py", "C:/evil.py", "..\\evil.py"])
def test_zip_traversal_rejected(tmp_path, name):
    z = make_zip(tmp_path / "t.zip", {name: "x=1"})
    with pytest.raises(ArchiveRejected):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_not_a_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(ArchiveRejected):
        analyze(bad)


def test_zip_bomb_ratio_rejected(tmp_path):
    z = make_zip(tmp_path / "b.zip", {"big.py": "0" * 5_000_000})
    with pytest.raises(ArchiveRejected):
        safe_extract(z, tmp_path / "out", ExtractLimits(max_ratio=50))


def test_total_size_limit(tmp_path):
    z = make_zip(tmp_path / "s.zip", {"a.py": "x" * 1000, "b.py": "y" * 1000})
    with pytest.raises(ArchiveRejected):
        safe_extract(z, tmp_path / "out", ExtractLimits(max_total_bytes=1500))


def test_only_source_files_written(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"ok.py": "x=1\n", "run.exe": "MZ", "setup.bat": "del *", "img.png": "x"})
    safe_extract(z, tmp_path / "out")
    names = {p.name for p in (tmp_path / "out").rglob("*") if p.is_file()}
    assert names == {"ok.py"}


def test_symlink_member_skipped(tmp_path):
    z = tmp_path / "l.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    notes = safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "out" / "link.py").exists()
    assert any("symlink" in n for n in notes)


# ---------------------------------------------------------------- python (no external tools needed)
def test_python_syntax_error_has_real_location(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"proj/bad.py": "x = 1\ndef f(:\n    pass\n"})
    rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    f = only(rep, "python-ast")[0]
    assert (f.kind, f.file, f.line) == ("parser_error", "bad.py", 2)  # single top folder is descended into
    assert f.column and f.verification == "tool-reported"


def test_clean_python_has_no_ast_findings(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"ok.py": "def f(a):\n    return a + 1\n"})
    rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    assert only(rep, "python-ast") == []
    assert [t.status for t in rep.tool_runs if t.analyzer == "python-ast"] == ["ok"]


def test_uploaded_code_is_never_executed(tmp_path):
    canary = tmp_path / "CANARY.txt"
    code = f"open({str(canary)!r}, 'w').write('executed')\nimport os\nos.system('echo hi')\n"
    z = make_zip(tmp_path / "e.zip", {"evil.py": code, "conftest.py": code, "setup.py": code})
    analyze(z, Options(languages={"python"}))
    assert not canary.exists()


def test_all_finding_kinds_are_valid(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"bad.py": "def f(:\n", "ok.py": "import os\n"})
    rep = analyze(z, Options(languages={"python"}))
    assert rep.findings and all(f.kind in KINDS for f in rep.findings)


# ---------------------------------------------------------------- orchestration behaviour
def test_unavailable_tool_is_reported_not_faked(tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.static_analysis.base.shutil.which", lambda name: None)
    z = make_zip(tmp_path / "p.zip", {"a.py": "x=1\n", "q.sql": "select 1;"})
    rep = analyze(z)
    status = {t.analyzer: t.status for t in rep.tool_runs}
    assert status["ruff"] == "unavailable" and status["sqlfluff"] == "unavailable"
    assert status["python-ast"] == "ok"


def test_dotnet_requires_approval(tmp_path):
    z = make_zip(tmp_path / "d.zip", {"App.csproj": "<Project/>", "P.cs": "class P{}"})
    rep = analyze(z, Options(languages={"csharp"}))
    run = [t for t in rep.tool_runs if t.analyzer == "dotnet-build"][0]
    assert run.status == "skipped" and run.command == [] and "approval" in run.note


def test_adapter_crash_is_isolated_and_recorded(tmp_path):
    class Boom(Adapter):
        name = "boom"
        languages = ("python",)

        def run(self, ctx):
            raise RuntimeError("kaboom")

    register_adapter(Boom)
    try:
        z = make_zip(tmp_path / "p.zip", {"bad.py": "def f(:\n"})
        rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    finally:
        runner.ADAPTERS.remove(Boom)
    assert any(t.analyzer == "boom" and t.status == "error" and "kaboom" in t.note for t in rep.tool_runs)
    assert only(rep, "python-ast")  # other adapters unaffected


def test_cancel_before_start_returns_partial(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"a.py": "x=1\n"})
    ev = threading.Event()
    ev.set()
    rep = analyze(z, Options(languages={"python"}), cancel=ev)
    assert any("Cancelled" in n for n in rep.notes) and rep.tool_runs == []


def test_directory_source_and_scratch_cleanup(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "bad.py").write_text("def f(:\n")
    before = {p.name for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("sa_plugin_*")} if os.name != "nt" else set()
    rep = analyze(tmp_path / "proj", Options(languages={"python"}, use_pyright=False))
    assert only(rep, "python-ast")
    after = {p.name for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("sa_plugin_*")} if os.name != "nt" else set()
    assert after <= before


def test_missing_source():
    with pytest.raises(FileNotFoundError):
        analyze("does-not-exist.zip")


# ---------------------------------------------------------------- subprocess runner
def test_timeout_enforced():
    r = run_command([sys.executable, "-c", "import time; time.sleep(30)"], ".", 1, threading.Event())
    assert r.outcome == "timeout" and r.duration_s < 10


def test_cancel_kills_process():
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    r = run_command([sys.executable, "-c", "import time; time.sleep(30)"], ".", 60, ev)
    assert r.outcome == "cancelled" and r.duration_s < 10


def test_missing_executable_is_handled():
    r = run_command(["definitely-not-a-real-tool-xyz"], ".", 5, threading.Event())
    assert r.outcome == "oserror"


def test_secrets_removed_from_child_env(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "s3cret")
    monkeypatch.setenv("DB_PASSWORD", "s3cret")
    monkeypatch.setenv("HARMLESS", "1")
    env = sanitized_env()
    assert "MY_API_KEY" not in env and "DB_PASSWORD" not in env and env["HARMLESS"] == "1"


# ---------------------------------------------------------------- output parsers (sample tool output)
def test_parse_javac():
    text = (
        "src/A.java:1: error: ';' expected\n"
        "public class A { void m() { int x = 1 } }\n"
        "                                     ^\n"
        "src/B.java:4: error: cannot find symbol\n"
        "    Foo f;\n"
        "    ^\n"
        "  symbol:   class Foo\n"
        "  location: class B\n"
        "src/B.java:9: warning: [serial] serializable class has no serialVersionUID\n"
        "class C implements java.io.Serializable {}\n"
        "^\n"
        "2 errors\n1 warning\n")
    f = parse_javac(text, Path("."), "javac 17")
    assert [(x.file, x.line, x.kind) for x in f] == [
        ("src/A.java", 1, "compiler_error"), ("src/B.java", 4, "compiler_error"), ("src/B.java", 9, "compiler_warning")]
    assert f[0].column == 38 and f[0].confidence == "high"
    assert f[1].confidence == "medium" and "Foo" in f[1].message  # unresolved symbol -> reduced confidence


def test_parse_msbuild():
    text = (
        "/w/P.cs(1,38): error CS1525: Invalid expression term ';' [/w/App.csproj]\n"
        "/w/P.cs(1,38): error CS1525: Invalid expression term ';' [/w/App.csproj]\n"
        "/w/P.cs(3,5): warning CS0168: The variable 'x' is declared but never used [/w/App.csproj]\n"
        "/w/App.csproj : error NU1101: Unable to find package Foo [/w/App.sln]\n"
        "Build FAILED.\n")
    f = parse_msbuild(text, Path("/w"), "8.0")
    assert len(f) == 3  # duplicate removed
    assert (f[0].kind, f[0].line, f[0].column, f[0].rule) == ("compiler_error", 1, 38, "CS1525")
    assert f[1].kind == "compiler_warning"
    assert f[2].line is None and f[2].confidence == "medium"


# ---------------------------------------------------------------- real external tools (skipped if absent)
@pytest.mark.skipif(not has("ruff"), reason="ruff not installed")
def test_ruff_real(tmp_path):
    z = make_zip(tmp_path / "r.zip", {"a.py": "import os\n\ndef f():\n    return nope\n"})
    rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    rules = {f.rule for f in only(rep, "ruff")}
    assert {"F401", "F821"} <= rules
    assert all(f.kind == "lint_warning" for f in only(rep, "ruff"))


@pytest.mark.skipif(not has("ruff"), reason="ruff not installed")
def test_project_ruff_config_is_ignored(tmp_path):
    z = make_zip(tmp_path / "r.zip", {"a.py": "import os\n", "ruff.toml": "", "pyproject.toml": "[tool.ruff]\nselect=[]\n"})
    rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    assert any(f.rule == "F401" for f in only(rep, "ruff"))


@pytest.mark.skipif(not has("pyright"), reason="pyright not installed")
def test_pyright_real(tmp_path):
    z = make_zip(tmp_path / "r.zip", {"a.py": "def f() -> int:\n    return 'x'\n"})
    rep = analyze(z, Options(languages={"python"}))
    pf = only(rep, "pyright")
    assert pf and pf[0].kind == "type_error" and pf[0].line == 2


@pytest.mark.skipif(not has("tsc"), reason="tsc not installed")
def test_tsc_real_type_error(tmp_path):
    z = make_zip(tmp_path / "t.zip", {"a.ts": 'const x: number = "s";\n'})
    rep = analyze(z, Options(languages={"typescript"}))
    by = {(f.file, f.rule): f for f in only(rep, "tsc")}
    assert by[("a.ts", "TS2322")].kind == "compiler_error" and by[("a.ts", "TS2322")].line == 1


@pytest.mark.skipif(not has("tsc"), reason="tsc not installed")
def test_tsc_real_syntax_error(tmp_path):
    z = make_zip(tmp_path / "t.zip", {"b.ts": "function g( { }\n"})
    rep = analyze(z, Options(languages={"typescript"}))
    assert any(f.file == "b.ts" and f.kind == "parser_error" for f in only(rep, "tsc"))
    assert [t.status for t in rep.tool_runs if t.analyzer == "tsc"] == ["findings"]


@pytest.mark.skipif(not has("tsc"), reason="tsc not installed")
def test_tsc_unresolved_import_is_reduced_confidence(tmp_path):
    z = make_zip(tmp_path / "t.zip", {"a.ts": "import x from 'not-installed-pkg';\nexport default x;\n"})
    rep = analyze(z, Options(languages={"typescript"}))
    f = [f for f in only(rep, "tsc") if f.rule == "TS2307"][0]
    assert f.confidence == "medium"


@pytest.mark.skipif(not has("eslint"), reason="eslint not installed")
def test_eslint_real(tmp_path):
    z = make_zip(tmp_path / "j.zip", {"a.js": "const a = {k:1, k:2};\n", "b.js": "let = = 3;\n"})
    rep = analyze(z, Options(languages={"javascript"}))
    ef = only(rep, "eslint")
    assert any(f.rule == "no-dupe-keys" and f.kind == "lint_warning" for f in ef)
    assert any(f.file == "b.js" and f.kind == "parser_error" for f in ef)


@pytest.mark.skipif(not has("sqlfluff"), reason="sqlfluff not installed")
def test_sqlfluff_real(tmp_path):
    z = make_zip(tmp_path / "s.zip", {"q.sql": "SELEC * FROM t WHERE;\n"})
    rep = analyze(z, Options(languages={"sql"}))
    assert any(f.rule == "PRS" and f.kind == "parser_error" and f.confidence == "medium" for f in only(rep, "sqlfluff"))


@pytest.mark.skipif(not has("javac"), reason="javac not installed")
def test_javac_real(tmp_path):
    z = make_zip(tmp_path / "j.zip", {"A.java": "public class A { void m() { int x = 1 } }\n"})
    rep = analyze(z, Options(languages={"java"}))
    assert any(f.kind == "compiler_error" and f.line == 1 for f in only(rep, "javac"))


# ---------------------------------------------------------------- reports
def test_report_files_and_html_escaping(tmp_path):
    z = make_zip(tmp_path / "p.zip", {"bad.py": "def f(:\n"})
    rep = analyze(z, Options(languages={"python"}, use_pyright=False))
    rep.findings[0].message = "<script>alert(1)</script>"
    j = rep.write_json(tmp_path / "out" / "r.json")
    h = rep.write_html(tmp_path / "out" / "r.html")
    data = json.loads(j.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == len(rep.findings) and data["limitations"]
    html_text = h.read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html_text and "&lt;script&gt;" in html_text
