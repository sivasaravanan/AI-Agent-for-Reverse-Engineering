"""Stage A: folder/tar input, code map, HTML/CSS/PHP/Go analyzers, generated documentation."""
import io
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from analyzers.builtin_analyzers import CssAnalyzer, GoAnalyzer, HtmlAnalyzer, PhpAnalyzer, run_builtin_analyzers
from core.analysis_orchestrator import analyze_archive
from core.archive_manager import ArchiveError
from core.code_map import build_code_map, callees_of
from core.doc_generator import generate_documentation, write_documentation
from core.input_adapter import prepared_input
from tests.fixtures.sample_project import build_sample_zip


def _project(root: Path):
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("import os\n\ndef g(y):\n    return y\n\ndef f(x):\n    return g(x)\n")
    (root / "requirements.txt").write_text("flask\n")
    return root


# ------------------------------------------------------------------ inputs
def test_folder_input_analyzes_like_zip(tmp_path):
    r = analyze_archive(_project(tmp_path / "proj"))
    assert r.archive_name == "proj" and r.input_kind == "folder" and r.archive.file_count == 2
    assert any(l.language == "Python" for l in r.languages)


def test_folder_skips_symlinks_and_vendored(tmp_path):
    p = _project(tmp_path / "proj")
    (p / "node_modules").mkdir()
    (p / "node_modules" / "x.js").write_text("1")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET")
    try:
        os.symlink(outside, p / "link.txt")
    except OSError:
        pytest.skip("symlinks not permitted")
    r = analyze_archive(p)
    assert r.archive.file_count == 2 and any("symlink" in w for w in r.warnings)
    assert "TOP SECRET" not in str(r.to_dict())


def test_tar_input_blocks_traversal_and_links(tmp_path):
    t = tmp_path / "p.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        data = b"print(1)\n"
        ti = tarfile.TarInfo("proj/a.py"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        ev = tarfile.TarInfo("../evil.py"); ev.size = 1; tf.addfile(ev, io.BytesIO(b"x"))
        ab = tarfile.TarInfo("/abs.py"); ab.size = 1; tf.addfile(ab, io.BytesIO(b"x"))
        ln = tarfile.TarInfo("proj/link"); ln.type = tarfile.SYMTYPE; ln.linkname = "/etc/passwd"; tf.addfile(ln)
    r = analyze_archive(t)
    assert r.input_kind == "tar" and r.archive.file_count == 1
    joined = " ".join(r.warnings)
    assert "traversal" in joined and "absolute" in joined and "symlink" in joined
    assert not (tmp_path / "evil.py").exists()


def test_tar_bomb_is_bounded(tmp_path):
    from security.archive_validation import ArchiveLimits
    t = tmp_path / "b.tar"
    with tarfile.open(t, "w") as tf:
        for i in range(5):
            ti = tarfile.TarInfo(f"f{i}.txt"); ti.size = 10; tf.addfile(ti, io.BytesIO(b"x" * 10))
    with pytest.raises(ArchiveError):
        with prepared_input(t, ArchiveLimits(max_files=3)):
            pass


def test_unsupported_and_invalid_inputs(tmp_path):
    for name in ("x.7z", "x.rar"):
        f = tmp_path / name
        f.write_bytes(b"1234")
        with pytest.raises(ArchiveError, match="not supported"):
            analyze_archive(f)
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    with pytest.raises(ArchiveError):
        analyze_archive(bad)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ArchiveError):
        analyze_archive(empty)


def test_temp_files_are_cleaned_up(tmp_path):
    import tempfile
    before = set(Path(tempfile.gettempdir()).glob("ase_input_*"))
    analyze_archive(_project(tmp_path / "proj"))
    assert set(Path(tempfile.gettempdir()).glob("ase_input_*")) == before


def test_zip_still_works_and_marks_kind(tmp_path):
    r = analyze_archive(build_sample_zip(tmp_path / "s.zip"))
    assert r.input_kind == "zip"


# ------------------------------------------------------------------ code map
def test_python_code_map_and_calls():
    cm = build_code_map({"a.py": "class A:\n    def m(self, x, *a, **k):\n        return helper(x)\n\ndef helper(v):\n    '''doc'''\n    return v\n\nasync def go(): pass\n"})
    names = {s.qualname: s for s in cm.symbols}
    assert {"A", "A.m", "helper", "go"} <= set(names)
    assert names["A.m"].kind == "method" and names["A.m"].params == ["self", "x", "*a", "**k"] and names["helper"].doc
    assert names["A.m"].method == "ast"
    call = [c for c in cm.calls if c.callee == "helper"][0]
    assert call.caller == "A.m" and call.resolved == "a.py::helper"
    assert callees_of(cm, "a.py", "A.m")


def test_ambiguous_calls_are_not_resolved():
    cm = build_code_map({"a.py": "def f(): pass\n", "b.py": "def f(): pass\n", "c.py": "def u():\n    f()\n"})
    assert cm.calls[0].resolved == ""


def test_complexity_hotspot_and_syntax_error_file():
    body = "def big(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(15)) + "    return 0\n"
    cm = build_code_map({"a.py": body, "bad.py": "def (:\n"})
    assert cm.hotspots and cm.hotspots[0]["name"] == "big" and cm.hotspots[0]["complexity"] >= 12
    assert any(m["file"] == "bad.py" and "not parseable" in m["note"] for m in cm.modules)


def test_regex_languages_are_labelled_regex():
    cm = build_code_map({"W.java": "public class W {\n  public void run() {\n  }\n}\n", "x.go": "package x\ntype S struct{}\nfunc (s S) M() {}\nfunc F() {}\n",
                         "p.php": "<?php\nclass P {\n    public function q() {}\n}\n", "c.cs": "public class C {\n  public void Do() {\n  }\n}\n",
                         "m.ts": "export class K {}\nconst h = (a) => a\n"})
    by = {(s.language, s.name): s for s in cm.symbols}
    for key in [("Java", "W"), ("Java", "run"), ("Go", "S"), ("Go", "M"), ("Go", "F"), ("PHP", "P"), ("PHP", "q"), ("C#", "C"), ("C#", "Do"), ("TypeScript", "K"), ("TypeScript", "h")]:
        assert key in by and by[key].method == "regex", key
    assert cm.calls == []                                # no call graph is claimed for regex languages


def test_code_map_in_orchestrator_and_report(tmp_path):
    from core.report_generator import to_html
    r = analyze_archive(build_sample_zip(tmp_path / "s.zip"))
    assert r.code_map["summary"]["symbols"] > 0
    assert "Code map" in to_html(r)


# ------------------------------------------------------------------ analyzers
def test_html_checks():
    res = HtmlAnalyzer().analyze({"a.html": "<html><body><div><span></div><img src=x><a href=y target=_blank>z</a><p id=a><p id=a></body></html>", "t.html": "{{x}}{% y %}<div>"})
    rules = {f.rule_id for f in res.findings}
    assert {"html-unclosed-tag", "html-img-no-alt", "html-duplicate-id", "html-blank-no-noopener", "html-missing-lang", "html-missing-title"} <= rules
    assert all(f.verification == "unverified-heuristic" for f in res.findings)       # never claimed as a compiler error
    assert not any(f.file == "t.html" for f in res.findings)                          # server templates skipped


def test_html_clean_file_has_no_findings():
    ok = '<!doctype html><html lang="en"><head><title>t</title></head><body><p>a<p>b<img src=x alt=""></body></html>'
    assert HtmlAnalyzer().analyze({"ok.html": ok}).findings == []


def test_css_checks():
    res = CssAnalyzer().analyze({"a.css": "a { color: red;\n", "b.css": "}\n", "c.css": "/* open\na{}", "ok.css": "a{b:c}/* x */ .d{e:'}'}"})
    by = {f.file: f.rule_id for f in res.findings}
    assert by == {"a.css": "css-unclosed-brace", "b.css": "css-unmatched-brace", "c.css": "css-unterminated-comment"}


@pytest.mark.skipif(shutil.which("php") is None, reason="PHP not installed")
def test_php_syntax_and_heuristics():
    res = PhpAnalyzer().analyze({"bad.php": "<?php\nif (1 {\n", "x.php": "<?php\neval($_GET['a']);\necho $_GET['n'];\n", "ok.php": "<?php\necho 1;\n"})
    by = {(f.file, f.rule_id): f for f in res.findings}
    assert by[("bad.php", "php-syntax")].verification == "tool-reported" and by[("bad.php", "php-syntax")].line == 2
    assert ("x.php", "php-eval") in by and by[("x.php", "php-eval")].verification == "unverified-heuristic"
    assert ("x.php", "php-xss-echo") in by and not any(f.file == "ok.php" for f in res.findings)


def test_php_unavailable_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = PhpAnalyzer().analyze({"a.php": "<?php eval($_GET['x']);"})
    assert res.status == "unavailable" and "not installed" in res.note
    assert not any(f.rule_id == "php-syntax" for f in res.findings)


@pytest.mark.skipif(shutil.which("gofmt") is None, reason="Go not installed")
def test_go_syntax_and_heuristics():
    res = GoAnalyzer().analyze({"bad.go": "package x\nfunc f( {\n", "ok.go": "package x\nfunc F() {}\n",
                                "tls.go": "package x\nvar c = Config{InsecureSkipVerify: true}\n"})
    rules = {(f.file, f.rule_id) for f in res.findings}
    assert ("bad.go", "go-syntax") in rules and ("tls.go", "go-tls-skip-verify") in rules and not any(f[0] == "ok.go" for f in rules)
    assert len([f for f in res.findings if f.file == "bad.go"]) == 1                    # no cascade of duplicates


def test_analyzer_failure_does_not_break_others(monkeypatch):
    monkeypatch.setattr(HtmlAnalyzer, "analyze", lambda self, files: (_ for _ in ()).throw(RuntimeError("boom")))
    f, runs = run_builtin_analyzers({"a.html": "<p>", "a.css": "a{"})
    assert any(r["analyzer"] == "html" and r["status"] == "error" for r in runs) and any(x.rule_id == "css-unclosed-brace" for x in f)


# ------------------------------------------------------------------ documentation
def test_generated_documentation_is_faithful(tmp_path):
    r = analyze_archive(build_sample_zip(tmp_path / "s.zip"))
    md = generate_documentation(r, "Shop")
    for h in ("# Shop - technical documentation", "## 2. How to run", "## 3. Dependencies", "## 6. Code structure", "## 8. Limitations"):
        assert h in md
    assert "TODO" in md and "flask" in md and "Fake#Pass1" not in md
    assert "def get_db" not in md                                                       # symbols, never source text
    p = write_documentation(r, tmp_path)
    assert p.name == "s_documentation.md" and p.read_text(encoding="utf-8") == generate_documentation(r)


def test_cli_docs_flag_and_folder(tmp_path, capsys):
    import app
    assert app.main(["--cli", str(_project(tmp_path / "proj")), "--out", str(tmp_path / "o"), "--docs"]) == 0
    assert list((tmp_path / "o").glob("*_documentation.md")) and "Documentation:" in capsys.readouterr().out


# ------------------------------------------------------------------ UI
def test_ui_folder_input_code_map_tab_and_docs(tmp_path, monkeypatch):
    import time
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from ui import main_window as mw
    try:
        win = mw.MainWindow(root, tmp_path / "reports")
        monkeypatch.setattr(mw.filedialog, "askdirectory", lambda **k: str(_project(tmp_path / "proj")))
        win.select_folder()
        assert win.zip_path.endswith("proj") and str(win.analyze_btn["state"]) == "normal"
        win.start()
        deadline = time.time() + 60
        while win.worker.is_alive() or not win.q.empty():
            root.update(); time.sleep(0.01)
            assert time.time() < deadline
        for _ in range(5):
            root.update(); time.sleep(0.1)
        assert any("f" == win.cmap.item(i, "values")[1] for i in win.cmap.get_children())
        win.generate_docs()
        assert list((tmp_path / "reports").glob("*_documentation.md"))
    finally:
        root.destroy()


def test_build_tab_follows_main_source_without_own_picker(tmp_path):
    import time
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from tkinter import ttk
    from ui import main_window as mw
    try:
        win = mw.MainWindow(root, tmp_path / "reports")
        tab = win.exec_tab
        labels = [w.cget("text") for w in tab.winfo_children()[0].winfo_children() if isinstance(w, ttk.Button)]
        assert "ZIP..." not in labels and "Folder..." not in labels          # no second picker
        proj = _project(tmp_path / "proj")
        (proj / "requirements.txt").write_text("pytest\n"); (proj / "tests").mkdir(); (proj / "tests" / "test_a.py").write_text("def test_x():\n    assert True\n")
        win._set_source(str(proj))
        win._select_tab("Build & Test")
        for _ in range(10):
            root.update(); time.sleep(0.05)
        assert tab.src.get() == str(proj) and tab._source() == str(proj)
        assert tab._cands, "commands should be discovered automatically for the selected project"
        win._set_source(str(tmp_path))                                         # changing the project updates the tab
        for _ in range(10):
            root.update(); time.sleep(0.05)
        assert tab._source() == str(tmp_path)
    finally:
        root.destroy()
