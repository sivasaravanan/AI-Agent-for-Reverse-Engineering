import json
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import database_analysis as dba
from database_analysis.sources import ArchiveLimits, ZipProvider, sanitize_member_name
from fixtures import SECRET, build_zip, multi_db_project


def test_invalid_zip_handled(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(dba.ArchiveError, match="not a valid ZIP"):
        dba.run(p)
    with pytest.raises(dba.ArchiveError, match="not found"):
        dba.run(tmp_path / "missing.zip")


def test_truncated_zip(tmp_path):
    good = build_zip(tmp_path / "g.zip", {"a.py": "import sqlite3\n" * 100})
    data = good.read_bytes()
    (tmp_path / "t.zip").write_bytes(data[: len(data) // 2])
    with pytest.raises(dba.ArchiveError):
        dba.run(tmp_path / "t.zip")


@pytest.mark.parametrize("name", ["../evil.py", "/etc/x.py", "C:/win/x.py", "a/../../b.py", "a\\..\\..\\b.py"])
def test_path_traversal_names_rejected(name):
    assert sanitize_member_name(name)[0] is None


def test_zip_with_traversal_entries_skipped_not_extracted(tmp_path):
    z = tmp_path / "t.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.py", "import sqlite3\n")
        zf.writestr("ok.py", "import sqlite3\n")
    res = dba.run(z)
    assert res.skipped.get("archive entry skipped: path traversal") == 1
    assert not (tmp_path.parent / "escape.py").exists()
    assert res.database("sqlite").evidence[0].file == "ok.py"


def test_too_many_files(tmp_path):
    z = build_zip(tmp_path / "m.zip", {f"f{i}.txt": "x" for i in range(30)})
    with pytest.raises(dba.ArchiveError, match="too many"):
        ZipProvider(z, ArchiveLimits(max_files=10))


def test_oversized_member_and_ratio_skipped(tmp_path):
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.sql", "A" * 5_000_000)
        zf.writestr("ok.py", "import sqlite3")
    with ZipProvider(z) as p:
        assert [f.path for f in p.list_files()] == ["ok.py"]
        assert ("bomb.sql", "suspicious compression ratio") in p.skipped


def test_project_code_is_never_executed(tmp_path):
    marker = tmp_path / "executed.txt"
    code = f"import sqlite3\nopen(r'{marker}', 'w').write('x')\n"
    z = build_zip(tmp_path / "p.zip", {"setup.py": code, "conftest.py": code, "app.py": code})
    dba.run(z)
    assert not marker.exists()


def test_no_network_or_database_connection(monkeypatch, tmp_path):
    def blocked(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    z = build_zip(tmp_path / "p.zip", multi_db_project())
    assert dba.run(z).databases


def test_directory_provider_skips_symlinks(tmp_path):
    (tmp_path / "a.py").write_text("import sqlite3")
    try:
        (tmp_path / "link.py").symlink_to(tmp_path / "a.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    res = dba.run(tmp_path)
    assert res.skipped.get("archive entry skipped: symbolic link") == 1


def test_cancel_and_progress(tmp_path):
    calls = []
    opts = dba.AnalysisOptions(progress=lambda d, t, p: calls.append((d, t)), cancel=lambda: len(calls) > 2)
    res = dba.run({f"f{i}.py": "import sqlite3" for i in range(10)}, opts)
    assert res.cancelled and calls


# ---------------------------------------------------------------- plug-in surface
def test_register_variants():
    d = {}
    dba.register(d)
    assert d["database_analysis"] is dba.run

    class Reg:
        def __init__(self): self.items = {}
        def register_analyzer(self, n, f): self.items[n] = f
    r = Reg()
    dba.register(r)
    assert "database_analysis" in r.items
    lst = []
    dba.register(lst)
    assert lst[0]["name"] == "database_analysis" and lst[0]["executes_project_code"] is False
    with pytest.raises(TypeError):
        dba.register(object())


def test_dict_adapter_json_serialisable(tmp_path):
    from database_analysis import plugin
    z = build_zip(tmp_path / "p.zip", multi_db_project())
    out = plugin.analyze({"zip_path": str(z)})
    json.dumps(out)
    assert out["findings_phase6"] and out["schema_version"] == "1.0"
    with pytest.raises(ValueError):
        plugin.analyze({})


def test_cli_roundtrip(tmp_path):
    z = build_zip(tmp_path / "p.zip", multi_db_project())
    root = Path(__file__).resolve().parents[2]
    cmd = [sys.executable, "-m", "database_analysis", str(z), "--json", str(tmp_path / "r.json"),
           "--html", str(tmp_path / "r.html"), "--md", str(tmp_path / "r.md")]
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    for f in ("r.json", "r.html", "r.md"):
        assert SECRET not in (tmp_path / f).read_text(encoding="utf-8")
    bad = tmp_path / "bad.zip"
    bad.write_text("nope")
    p = subprocess.run([sys.executable, "-m", "database_analysis", str(bad)], cwd=root, capture_output=True, text=True)
    assert p.returncode == 2 and "not a valid ZIP" in p.stderr


def test_html_escapes_hostile_content():
    res = dba.run({"a.py": "import sqlite3\nx = 'mysql://u:p@<script>alert(1)</script>/db'\n"})
    html = dba.render_html(res)
    assert "<script>alert(1)" not in html


def test_ui_panel_import_and_build():
    tk = pytest.importorskip("tkinter", reason="tkinter not installed in this environment")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    from tkinter import ttk
    from database_analysis.ui_panel import add_database_tab
    nb = ttk.Notebook(root)
    panel = add_database_tab(nb)
    panel.show(dba.run(multi_db_project()))
    root.destroy()
