import os
import zipfile

import pytest

from core import patch_manager as pm
from core.patch_manager import Workspace, apply, diff, propose, revert
from models.patch import PatchError


@pytest.fixture
def ws(tmp_path, make_zip):
    z = make_zip({"app/main.py": "DEBUG = True\nx = 1\n", "app/crlf.py": "a = 1\r\nb = 2\r\n", "README.md": "hi\n",
                  "node_modules/dep/i.js": "1", "bin/tool.py": "x = 1\n"}, name="src.zip")
    w, info = Workspace.create_from_zip(z, tmp_path / "ws")
    w.info = info
    return w


# ------------------------------------------------------------------ workspace creation
def test_workspace_extracts_safe_files_only(ws):
    assert (ws.root / "app/main.py").read_text() == "DEBUG = True\nx = 1\n"
    assert not (ws.root / "node_modules").exists() and not (ws.root / "bin").exists()
    assert {s["reason"] for s in ws.info.skipped} == {"vendored folder (not extracted)"}
    assert (ws.root / ".ase_workspace.json").is_file()


def test_workspace_refuses_traversal_symlink_and_windows_names(tmp_path):
    z = tmp_path / "bad.zip"
    link = zipfile.ZipInfo("link.py")
    link.external_attr = (0o120777) << 16
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.py", "boom")
        zf.writestr("/abs.py", "boom")
        zf.writestr("C:/x.py", "boom")
        zf.writestr(link, "/etc/passwd")
        zf.writestr("ok/CON.txt", "x")
        zf.writestr("ok/a:b.txt", "x")
        zf.writestr("ok/good.py", "x = 1\n")
    w, info = Workspace.create_from_zip(z, tmp_path / "ws")
    assert info.files_written == 1 and (w.root / "ok/good.py").exists()
    assert not (tmp_path / "escape.py").exists() and not (tmp_path / "abs.py").exists()
    assert len(info.skipped) == 6


def test_workspace_requires_empty_destination(tmp_path, make_zip):
    d = tmp_path / "ws"
    d.mkdir()
    (d / "keep.txt").write_text("precious")
    with pytest.raises(PatchError, match="empty"):
        Workspace.create_from_zip(make_zip({"a.py": "x=1\n"}), d)
    assert (d / "keep.txt").read_text() == "precious"


def test_workspace_invalid_zip_is_friendly(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"junk" * 20)
    with pytest.raises(PatchError, match="not a valid ZIP"):
        Workspace.create_from_zip(bad, tmp_path / "ws")


def test_source_zip_never_modified(tmp_path, make_zip):
    z = make_zip({"a.py": "DEBUG = True\n"})
    before = z.read_bytes()
    w, _ = Workspace.create_from_zip(z, tmp_path / "ws")
    p = propose("a.py", "DEBUG = False\n", w)
    diff(p)
    apply(p, True, w)
    assert z.read_bytes() == before


@pytest.mark.parametrize("bad", ["../x.py", "/etc/passwd", "C:/x.py", "a/../../x.py", ".ase_backups/x", ".ase_workspace.json"])
def test_resolve_rejects_unsafe_paths(ws, bad):
    with pytest.raises(PatchError):
        ws.resolve(bad)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_resolve_rejects_symlink_escape(ws, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    (ws.root / "evil.py").symlink_to(outside)
    with pytest.raises(PatchError, match="Symbolic"):
        ws.resolve("evil.py")
    (ws.root / "dir_link").symlink_to(tmp_path)
    with pytest.raises(PatchError):
        ws.resolve("dir_link/outside.py")


# ------------------------------------------------------------------ propose
def test_propose_errors(ws):
    with pytest.raises(PatchError, match="identical"):
        propose("app/main.py", "DEBUG = True\nx = 1\n", ws)
    with pytest.raises(PatchError, match="NUL"):
        propose("app/main.py", "a\x00b", ws)
    with pytest.raises(PatchError, match="not found"):
        propose("nope.py", "x", ws)
    with pytest.raises(PatchError, match="Unknown patch source"):
        propose("app/main.py", "y = 1\n", ws, source="magic")
    with pytest.raises(PatchError, match="workspace"):
        propose("relative.py", "x")


def test_propose_refuses_binary_and_non_utf8_and_huge(ws):
    (ws.root / "b.bin").write_bytes(b"\x00\x01\x02")
    (ws.root / "l1.py").write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    (ws.root / "big.py").write_text("x = 1\n" * 300000)
    for name, msg in (("b.bin", "Binary"), ("l1.py", "UTF-8"), ("big.py", "too large")):
        with pytest.raises(PatchError, match=msg):
            propose(name, "y\n", ws)


def test_propose_preserves_crlf(ws):
    p = propose("app/crlf.py", "a = 1\nb = 3\n", ws)
    assert p.new_text == "a = 1\r\nb = 3\r\n"


def test_absolute_path_without_workspace(ws):
    p = propose(str(ws.root / "app/main.py"), "DEBUG = False\n")
    assert p.file == "main.py"


# ------------------------------------------------------------------ approval gates
def test_apply_requires_explicit_approval(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    for flag in (False, None, 1, "yes"):
        with pytest.raises(PatchError, match="Approval"):
            apply(p, flag, ws)
    assert (ws.root / "app/main.py").read_text() == "DEBUG = True\nx = 1\n"
    assert not (ws.root / ".ase_backups").exists()


def test_apply_requires_diff_to_have_been_shown(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    with pytest.raises(PatchError, match="diff has not been shown"):
        apply(p, True, ws)


def test_apply_refuses_if_patch_changed_after_diff(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    p.new_text = "import os; os.system('x')\n"
    with pytest.raises(PatchError, match="changed after its diff"):
        apply(p, True, ws)
    assert "DEBUG = True" in (ws.root / "app/main.py").read_text()


def test_apply_refuses_if_file_drifted(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    (ws.root / "app/main.py").write_text("DEBUG = True\nx = 2  # edited meanwhile\n")
    with pytest.raises(PatchError, match="changed since"):
        apply(p, True, ws)
    assert "edited meanwhile" in (ws.root / "app/main.py").read_text()


def test_apply_success_creates_backup_and_manifest(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    d = diff(p)
    assert "-DEBUG = True" in d and "+DEBUG = False" in d
    info = apply(p, True, ws)
    assert (ws.root / "app/main.py").read_text() == "DEBUG = False\nx = 1\n"
    assert open(info.backup_path).read() == "DEBUG = True\nx = 1\n"
    import json
    m = json.load(open(info.manifest_path))
    assert m["patch_id"] == p.id and m["original_sha256"] != m["new_sha256"]
    assert p.status == "applied"
    with pytest.raises(PatchError, match="already"):
        apply(p, True, ws)                               # cannot be applied twice


def test_failed_write_leaves_original_intact(ws, monkeypatch):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    monkeypatch.setattr(pm.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        apply(p, True, ws)
    assert (ws.root / "app/main.py").read_text() == "DEBUG = True\nx = 1\n"
    assert p.status == "proposed"
    assert not [f for f in (ws.root / "app").iterdir() if f.name.startswith(".ase_tmp_")]


def test_bom_preserved(ws):
    (ws.root / "bom.py").write_bytes(b"\xef\xbb\xbfDEBUG = True\n")
    p = propose("bom.py", "DEBUG = False\n", ws)
    diff(p)
    apply(p, True, ws)
    assert (ws.root / "bom.py").read_bytes() == b"\xef\xbb\xbfDEBUG = False\n"


# ------------------------------------------------------------------ revert
def test_revert_restores_and_refuses_later_edits(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    info = apply(p, True, ws)
    (ws.root / "app/main.py").write_text("DEBUG = False\nx = 99  # user edit\n")
    with pytest.raises(PatchError, match="modified after"):
        revert(info)
    assert "user edit" in (ws.root / "app/main.py").read_text()
    revert(info, force=True, patch=p)
    assert (ws.root / "app/main.py").read_text() == "DEBUG = True\nx = 1\n" and p.status == "reverted"


def test_revert_plain_and_tampered_backup(ws):
    p = propose("app/main.py", "DEBUG = False\nx = 1\n", ws)
    diff(p)
    info = apply(p, True, ws)
    open(info.backup_path, "w").write("malicious\n")
    with pytest.raises(PatchError, match="hash"):
        revert(info)
    open(info.backup_path, "w").write("DEBUG = True\nx = 1\n")
    revert(info)
    assert (ws.root / "app/main.py").read_text() == "DEBUG = True\nx = 1\n"
    os.remove(info.backup_path)
    with pytest.raises(PatchError):
        revert(info)
