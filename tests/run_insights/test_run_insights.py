"""Run insights: parsing, hints, trace building, safe source lookup, and the UI tab wiring."""
import time
import zipfile

import pytest

from plugins.build_execution.models import ExecutionResult
from run_insights import build_trace, hints_for, parse_output, render_html, render_text, source_context
from run_insights.parsers import normalize_path

PY_TB = '''Traceback (most recent call last):
  File "/work/tests/test_calc.py", line 5, in test_add
    assert add(1, 2) == 3
  File "/work/app/calc.py", line 2, in add
    return a + b + missing
NameError: name 'missing' is not defined
'''


def one(text, tool=None):
    ds = parse_output(text)
    ds = [d for d in ds if tool is None or d.tool == tool]
    assert ds, f"nothing parsed from: {text[:80]}"
    return ds[0]


def test_python_traceback_gives_location_and_call_trace():
    d = one(PY_TB, "python")
    assert (d.file, d.line, d.code) == ("app/calc.py", 2, "NameError")
    assert d.message == "name 'missing' is not defined"
    assert [f[:2] for f in d.frames] == [("tests/test_calc.py", 5), ("app/calc.py", 2)]         # oldest call first


def test_python_syntax_error_without_traceback_header():
    d = one('  File "/work/bad.py", line 3\n    def f(:\n          ^\nSyntaxError: invalid syntax\n', "python")
    assert (d.file, d.line, d.code) == ("bad.py", 3, "SyntaxError")


def test_pytest_and_unittest_failures():
    ds = parse_output("FAILED tests/test_a.py::test_x - assert 3 == 4\ntests/test_a.py:5: AssertionError\n"
                      "ERROR tests/test_b.py - ModuleNotFoundError: No module named 'pytest'\n")
    a = next(d for d in ds if d.file == "tests/test_a.py")
    assert a.test == "test_x" and a.line == 5 and "3 == 4" in a.message
    assert any("No module named 'pytest'" in d.message for d in ds)
    ut = one("FAIL: test_x (tests.test_calc.CalcTest.test_x)\n" + PY_TB, "python")
    assert ut.test == "tests.test_calc.CalcTest.test_x"


def test_node_stack_skips_internals_and_orders_oldest_first():
    d = one("TypeError: Cannot read properties of undefined (reading 'x')\n    at run (/work/src/app.js:10:15)\n"
            "    at Object.<anonymous> (/work/src/index.js:3:1)\n    at Module._compile (node:internal/modules/cjs/loader:1256:14)\n"
            "    at x (/work/node_modules/lib/i.js:1:1)\n", "node")
    assert (d.file, d.line) == ("src/app.js", 10)
    assert [f[0] for f in d.frames] == ["src/index.js", "src/app.js"]


def test_dotnet_errors_are_deduplicated_and_nuget_without_location_is_kept():
    out = ("/work/api/Program.cs(12,9): error CS1002: ; expected [/work/api/Api.csproj]\n" * 2
           + "error NU1301: Unable to load the service index for source https://api.nuget.org [/work/api/Api.csproj]\n")
    ds = parse_output(out)
    assert [d.code for d in ds] == ["CS1002", "NU1301"]
    assert (ds[0].file, ds[0].line, ds[0].col) == ("api/Program.cs", 12, 9)


def test_java_compile_error_and_exception_trace():
    ds = parse_output("Main.java:5: error: ';' expected\nException in thread \"main\" java.lang.NullPointerException: boom\n"
                      "\tat com.x.App.run(App.java:12)\n\tat com.x.App.main(App.java:5)\n")
    assert any(d.file == "Main.java" and d.line == 5 for d in ds)
    npe = next(d for d in ds if d.code.endswith("NullPointerException"))
    assert (npe.file, npe.line) == ("App.java", 12) and npe.frames[0][1] == 5


def test_typescript_and_generic_compilers():
    assert one("src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.", "typescript").code == "TS2322"
    g = one("main.c:10:5: error: expected ';' before 'return'", "generic")
    assert (g.file, g.line, g.col) == ("main.c", 10, 5)


def test_garbage_and_huge_input_do_not_break_or_hang():
    assert parse_output("", "") == []
    t0 = time.time()
    parse_output(("x" * 5000 + "\n") * 2000 + PY_TB * 50, "")
    assert time.time() - t0 < 5
    assert len(parse_output(("a.c:1:1: error: boom\n" * 5000))) <= 300


@pytest.mark.parametrize("raw,exp", [("/work/app/x.py", "app/x.py"), ("C:\\Users\\me\\proj\\x.py", "Users/me/proj/x.py"),
                                     ("./a/b.py", "a/b.py"), ("file:///work/a.js", "a.js"), ("", "")])
def test_normalize_path(raw, exp):
    assert normalize_path(raw) == exp


# --------------------------------------------------------------------------------------------- hints
def test_hints_cover_the_failures_users_actually_hit():
    h = hints_for("completed", 1, "ModuleNotFoundError: No module named 'pytest'")
    assert any("python:unittest" in x["text"] and "pip-pytest" in x["text"] for x in h)
    assert any("network" in x["text"] for x in hints_for("failed", 1, "error NU1301: Unable to load the service index"))
    assert any("time limit" in x["text"] for x in hints_for("timeout", None, ""))
    assert any("memory" in x["text"] for x in hints_for("failed", 137, ""))
    assert any("Set up Docker" in x["text"] for x in hints_for("failed", 125, "Unable to find image 'python:3.12-slim' locally"))
    assert any("No tests" in x["text"] for x in hints_for("failed", 5, "collected 0 items"))
    assert any("recognised" in x["text"] for x in hints_for("failed", 2, "something odd"))
    assert hints_for("completed", 0, "all good") == []


# --------------------------------------------------------------------------------------------- trace
def make_result(**kw):
    base = dict(status="failed", candidate_id="python:unittest", argv=["python", "-m", "unittest"], image="python:3.12-slim",
                backend="docker", exit_code=1, stdout="", stderr=PY_TB, duration_s=1.5, network_enabled=False,
                limits={"timeout_s": 300}, notes=["Copied 3 file(s) into a disposable workspace."], started="2026-10-15T10:00:00Z")
    base.update(kw)
    return ExecutionResult(**base)


def test_build_trace_from_result_and_dict_and_renderers():
    t = build_trace(make_result())
    assert t.status == "failed" and t.stats["errors"] == 1 and "NameError" in t.summary or "1 error" in t.summary
    labels = [e["label"] for e in t.events]
    assert labels[0] == "Plan" and "First error" in labels and labels[-1] == "Finished"
    assert "network off" in next(e["detail"] for e in t.events if e["label"] == "Sandbox")
    d = build_trace(make_result().to_dict())
    assert d.stats == t.stats
    txt = render_text(t)
    assert "app/calc.py:2" in txt and "call trace for NameError" in txt
    page = render_html(build_trace(make_result(
        stderr='Traceback (most recent call last):\n  File "/work/a.py", line 1, in <module>\n'
               'ValueError: <img src=x onerror=alert(1)>\n')))
    assert "<img src=x" not in page and "&lt;img" in page and "Content-Security-Policy" in page
    assert build_trace(make_result(status="completed", exit_code=0, stderr="", stdout="OK")).hints == []


# --------------------------------------------------------------------------------------------- source lookup
def make_zip(tmp_path, files, name="p.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return str(p)


CALC = "def add(a, b):\n    return a + b + missing\n\n\nprint(add(1, 2))\n"


def test_source_context_from_zip_matches_by_suffix_and_marks_target(tmp_path):
    z = make_zip(tmp_path, {"myproj/app/calc.py": CALC, "other/calc.py": "x = 1\n"})
    c = source_context(z, "app/calc.py", 2, radius=1)
    assert c["path"] == "myproj/app/calc.py"
    assert [(x["n"], x["target"]) for x in c["lines"]] == [(1, False), (2, True), (3, False)]
    assert "missing" in c["lines"][1]["text"]
    assert source_context(z, "nope.py", 1)["note"].endswith("is not in the project.")
    assert "fewer than" in source_context(z, "app/calc.py", 99)["note"]
    assert source_context(z, "app/calc.py", 0)["lines"] == [] and source_context("", "a", 1)["lines"] == []


def test_source_context_from_folder_cannot_escape_the_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "app" / "calc.py").write_text(CALC)
    (tmp_path / "secret.txt").write_text("TOP SECRET\nline2\n")
    assert source_context(str(proj), "app/calc.py", 2)["lines"][1]["target"] is True
    for evil in ("../secret.txt", "../../secret.txt", "/etc/passwd", "..\\secret.txt"):
        c = source_context(str(proj), evil, 1)
        assert not any("SECRET" in x["text"] for x in c["lines"])


def test_binary_and_unreadable_sources_give_a_note_not_an_error(tmp_path):
    z = make_zip(tmp_path, {"a.bin": b"\x00\x01\x02" * 100})
    assert source_context(z, "a.bin", 1)["note"] == "Binary file."
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert source_context(str(bad), "a.py", 1)["note"].startswith("Could not read")
    assert source_context(str(tmp_path / "missing"), "a.py", 1)["note"] == "Source not found."


# --------------------------------------------------------------------------------------------- UI
tk = pytest.importorskip("tkinter")


def _root():
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("No display available (run under xvfb-run or on a desktop)")


def test_view_shows_diagnostic_source_and_call_trace(tmp_path):
    from ui.run_insights_view import RunInsightsView
    z = make_zip(tmp_path, {"app/calc.py": CALC, "tests/test_calc.py": "from app.calc import add\nassert add(1, 2) == 3\n"})
    root = _root()
    try:
        v = RunInsightsView(root, get_source=lambda: z)
        v.pack(fill="both", expand=True)
        v.show_result(make_result())
        root.update()
        assert len(v.diag.get_children()) == 1 and "FAILED" in v.summary.get()
        shown = v.src.get("1.0", "end")
        assert "app/calc.py" in shown and "missing" in shown
        assert len(v.frames.get_children()) == 2
        v.frames.selection_set("f0")
        root.update()
        assert "tests/test_calc.py" in v.src.get("1.0", "end")
        assert "HINTS" in v.notes.get("1.0", "end") and "TIMELINE" in v.notes.get("1.0", "end")
        v.show_result(make_result(status="completed", exit_code=0, stderr="", stdout="OK"))
        assert v.diag.get_children() == ()
    finally:
        root.destroy()


def test_main_window_feeds_runs_to_both_the_old_handler_and_the_new_tab(tmp_path):
    from core.settings import Settings
    from ui.main_window import MainWindow
    root = _root()
    try:
        mw = MainWindow(root, tmp_path / "reports", settings=Settings(history_enabled=False), base_dir=tmp_path)
        assert "Run Insights" in [mw.nb.tab(t, "text") for t in mw.nb.tabs()]
        mw.exec_tab.on_result(make_result())
        root.update()
        assert len(mw.insights_view.diag.get_children()) == 1                     # new tab got the run
        assert "Result hook failed" not in mw.exec_tab.status.get()                # and the existing handler did not break
    finally:
        root.destroy()
