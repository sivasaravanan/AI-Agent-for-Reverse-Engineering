import time
import json
import os
import time

import pytest

from collab import (ApprovalError, AuthError, PERMISSIONS, TeamWorkspace, UserStore, sign_file, verify_file)
from collab.users import Session
from core import patch_manager
from core.patch_manager import PatchError

IT = 1000      # fast PBKDF2 for tests only


@pytest.fixture
def ws(tmp_path):
    w = TeamWorkspace(tmp_path / "team", iterations=IT)
    w.add_user(None, "root", "correct horse battery", "admin")
    admin = w.login("root", "correct horse battery")
    for n, r in (("alice", "analyst"), ("rev", "reviewer"), ("view", "viewer")):
        w.add_user(admin, n, "password-" + n * 2, r)
    return w


def S(name, role):
    return Session(name, role)


def test_bootstrap_and_validation(tmp_path):
    u = UserStore(tmp_path / "u.db", IT)
    with pytest.raises(AuthError, match="first user must be an admin"):
        u.create_user(None, "bob", "longenough123", "analyst")
    u.create_user(None, "root", "longenough123", "admin")
    with pytest.raises(AuthError, match="Sign in"):
        u.create_user(None, "bob", "longenough123", "analyst")
    admin = u.authenticate("root", "longenough123")
    for bad in (("b", "longenough123", "viewer"), ("bob", "short", "viewer"), ("bob", "longenough123", "god"), ("bad name", "longenough123", "viewer")):
        with pytest.raises(AuthError):
            u.create_user(admin, *bad)
    u.create_user(admin, "bob", "longenough123", "viewer")
    with pytest.raises(AuthError, match="exists"):
        u.create_user(admin, "bob", "longenough123", "viewer")


def test_password_not_stored_and_wrong_login_generic(ws):
    raw = (ws.root / "users.db").read_bytes()
    assert b"correct horse battery" not in raw
    with pytest.raises(AuthError) as a:
        ws.login("root", "nope-nope-nope")
    with pytest.raises(AuthError) as b:
        ws.login("ghost", "nope-nope-nope")
    assert str(a.value) == str(b.value)                          # no user enumeration


def test_lockout_after_repeated_failures(ws):
    for _ in range(5):
        with pytest.raises(AuthError):
            ws.users.authenticate("alice", "wrong-password")
    with pytest.raises(AuthError, match="locked"):
        ws.users.authenticate("alice", "password-alicealice")    # even the right password is refused while locked


def test_roles_are_enforced(ws):
    v = ws.users.authenticate("view", "password-viewview")
    with pytest.raises(AuthError):
        ws.users.list_users(v)
    with pytest.raises(AuthError):
        ws.add_user(v, "x1", "longenough123", "viewer")
    assert PERMISSIONS["viewer"] == {"view"} and "approve" not in PERMISSIONS["analyst"]
    analyst = ws.users.authenticate("alice", "password-alicealice")
    with pytest.raises(AuthError):
        ws.add_user(analyst, "x2", "longenough123", "viewer")


def test_last_admin_protected_and_disable_blocks_login(ws):
    admin = ws.users.authenticate("root", "correct horse battery")
    with pytest.raises(AuthError, match="last"):
        ws.set_role(admin, "root", "viewer")
    with pytest.raises(AuthError, match="last"):
        ws.set_disabled(admin, "root", True)
    ws.set_disabled(admin, "alice", True)
    with pytest.raises(AuthError, match="disabled"):
        ws.users.authenticate("alice", "password-alicealice")
    assert ws.users.session_for("alice") is None
    ws.set_role(admin, "rev", "viewer")
    assert ws.users.session_for("rev").role == "viewer"          # role change applies to remembered sessions


def test_approval_separation_of_duties_and_binding(ws):
    alice, rev = S("alice", "analyst"), S("rev", "reviewer")
    h = "a" * 64
    i = ws.request_approval(alice, "patch", "fix app.py", h)
    with pytest.raises(AuthError):
        ws.decide(alice, i, True)                                  # analysts cannot approve
    ws.users.create_user(ws.users.authenticate("root", "correct horse battery"), "rev2", "reviewer-pass1", "reviewer")
    j = ws.request_approval(rev, "patch", "x", h)
    with pytest.raises(ApprovalError, match="Separation"):
        ws.decide(rev, j, True)                                    # reviewer cannot approve own request
    with pytest.raises(ApprovalError, match="reason"):
        ws.decide(rev, i, False)
    ws.decide(rev, i, True, "looks right")
    assert ws.approvals.is_approved(i, h) and not ws.approvals.is_approved(i, "b" * 64)   # bound to the exact payload
    with pytest.raises(ApprovalError, match="Already"):
        ws.decide(rev, i, False, "late")
    with pytest.raises(ApprovalError):
        ws.request_approval(alice, "patch", "x", "")
    with pytest.raises(AuthError):
        ws.request_approval(S("view", "viewer"), "patch", "x", h)


def test_approval_expiry(tmp_path):
    w = TeamWorkspace(tmp_path / "t", iterations=IT, ttl_hours=0)
    a, r = S("a", "analyst"), S("r", "reviewer")
    i = w.approvals.request(a, "report", "s", "c" * 64)
    time.sleep(0.01)
    with pytest.raises(ApprovalError, match="expired"):
        w.approvals.decide(r, i, True)
    assert w.approvals.get(i)["status"] == "expired"


def test_sign_and_verify_detects_changes(ws, tmp_path):
    rev = S("rev", "reviewer")
    f = tmp_path / "report.html"
    f.write_text("<p>ok</p>")
    sig = ws.sign(rev, f)
    v = ws.verify(f)
    assert v["valid"] and v["signer"] == "rev"
    f.write_text("<p>tampered</p>")
    assert not ws.verify(f)["valid"] and "CHANGED" in ws.verify(f)["reason"]
    f.write_text("<p>ok</p>")
    data = json.loads(sig.read_text()); data["signer"] = "root"; sig.write_text(json.dumps(data))
    assert not ws.verify(f)["valid"]                              # edited signature file
    sig.unlink()
    assert "no signature" in ws.verify(f)["reason"]
    with pytest.raises(AuthError):
        ws.sign(S("alice", "analyst"), f)                          # analysts cannot sign
    other = TeamWorkspace(tmp_path / "other", iterations=IT)
    ws.sign(rev, f)
    assert not verify_file(f, other.key)["valid"]                 # another workspace's key does not verify


def test_key_file_is_private(ws):
    ws.key
    mode = (ws.root / "signing.key").stat().st_mode & 0o777
    assert mode == 0o600 or os.name == "nt"


def test_shared_history_and_audit(ws):
    from core.analysis_orchestrator import analyze_archive
    from tests.fixtures.sample_project import build_sample_zip
    r = analyze_archive(build_sample_zip(ws.root / "s.zip"))
    with pytest.raises(AuthError):
        ws.save_analysis(S("view", "viewer"), r)
    ws.save_analysis(S("alice", "analyst"), r)
    w2 = TeamWorkspace(ws.root, iterations=IT)                    # a second user/session opening the same folder
    assert len(w2.history().list_history()) == 1
    assert ws.audit.verify()["ok"]
    acts = [e["action"] for e in ws.audit.entries()]
    assert {"user.create", "login", "analysis.save"} <= set(acts)
    assert "password" not in (ws.root / "audit.jsonl").read_text().lower().replace('"login.failed"', "")


def test_patch_apply_gate(ws, tmp_path):
    from core.patch_manager import Workspace, apply, diff, propose
    proj = tmp_path / "p"; proj.mkdir(); (proj / "a.py").write_text("x = 1\n")
    wsp = Workspace(proj)
    alice, rev = S("alice", "analyst"), S("rev", "reviewer")
    p = propose("a.py", "x = 2\n", wsp)
    diff(p)
    patch_manager.APPLY_GATE = ws.apply_gate(alice, None)
    try:
        with pytest.raises(PatchError, match="approval"):
            apply(p, True, wsp)                                    # no approval => refused, file untouched
        assert (proj / "a.py").read_text() == "x = 1\n"
        i = ws.request_approval(alice, "patch", "a.py", p.diff_hash)
        patch_manager.APPLY_GATE = ws.apply_gate(alice, i)
        with pytest.raises(PatchError):
            apply(p, True, wsp)                                    # pending is not approved
        ws.decide(rev, i, True, "ok")
        apply(p, True, wsp)
        assert (proj / "a.py").read_text() == "x = 2\n"
        assert [e["action"] for e in ws.audit.entries()].count("patch.apply") == 1
    finally:
        patch_manager.APPLY_GATE = None


def test_team_cli(tmp_path, monkeypatch, capsys):
    from scripts import team_cli
    w = str(tmp_path / "w")
    monkeypatch.setenv("ASE_PASSWORD", "admin-password-1")
    assert team_cli.main([w, "init-admin", "root"], IT) == 0
    monkeypatch.setenv("ASE_NEW_PASSWORD", "analyst-pass-1")
    assert team_cli.main([w, "--user", "root", "add-user", "ann", "analyst"], IT) == 0
    assert team_cli.main([w, "--user", "root", "users"], IT) == 0 and "ann" in capsys.readouterr().out
    monkeypatch.setenv("ASE_PASSWORD", "wrong")
    assert team_cli.main([w, "--user", "root", "users"], IT) == 2
    monkeypatch.setenv("ASE_PASSWORD", "admin-password-1")
    f = tmp_path / "r.txt"; f.write_text("hi")
    assert team_cli.main([w, "--user", "root", "sign", str(f)], IT) == 0
    assert team_cli.main([w, "verify", str(f)], IT) == 0
    f.write_text("changed")
    assert team_cli.main([w, "verify", str(f)], IT) == 1
    assert team_cli.main([w, "--user", "root", "audit"], IT) == 0


def test_ui_tabs_and_team_flow(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No display available")
    from ui import main_window as mw
    from tests.fixtures.sample_project import build_sample_zip
    try:
        win = mw.MainWindow(root, tmp_path / "reports")
        tabs = [win.nb.tab(i, "text") for i in range(win.nb.index("end"))]
        assert "Compliance" in tabs and "Team" in tabs
        tp = win.team_panel
        tp.folder_var.set(str(tmp_path / "team")); tp.user_var.set("root"); tp.pw_var.set("a-long-password-1")
        monkeypatch.setattr(mw.messagebox if hasattr(mw, "messagebox") else tk.messagebox, "showerror", lambda *a, **k: None, raising=False)
        tp.bootstrap(); tp.pw_var.set("a-long-password-1"); tp.login()
        assert tp.session and tp.session.role == "admin"
        win.zip_path = str(build_sample_zip(tmp_path / "s.zip"))
        win.start()
        deadline = time.time() + 60
        while win.worker.is_alive() or not win.q.empty():
            root.update(); time.sleep(0.01)
            assert time.time() < deadline
        for _ in range(5):
            root.update(); time.sleep(0.1)
        assert len(tp.ws.history().list_history()) == 1            # saved to the shared history
        win.compliance_panel.check()
        assert "Policy" in win.compliance_panel.text.get("1.0", "end")
        win.compliance_panel.verify_audit()
        assert "OK" in win.compliance_panel.text.get("1.0", "end")
        tp.logout()
        assert tp.session is None
    finally:
        root.destroy()
