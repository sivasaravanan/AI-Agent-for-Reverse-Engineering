"""Team workspace command line (Stage D). Passwords come from the ASE_PASSWORD environment variable or a hidden prompt, never argv.

  python scripts/team_cli.py WORKSPACE init-admin NAME
  python scripts/team_cli.py WORKSPACE --user NAME add-user NEW ROLE            (new user's password: ASE_NEW_PASSWORD or prompt)
  python scripts/team_cli.py WORKSPACE --user NAME users | approvals | audit
  python scripts/team_cli.py WORKSPACE --user NAME request KIND SUBJECT SHA256 | approve ID | reject ID REASON
  python scripts/team_cli.py WORKSPACE --user NAME sign FILE | verify FILE
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collab import ApprovalError, AuthError, SigningError, TeamWorkspace, WorkspaceError  # noqa: E402


def _pw(env: str, prompt: str) -> str:
    return os.environ.get(env) or getpass.getpass(prompt)


def main(argv=None, iterations=None) -> int:
    ap = argparse.ArgumentParser(description="Team workspace admin")
    ap.add_argument("workspace")
    ap.add_argument("--user")
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    try:
        ws = TeamWorkspace(a.workspace, **({"iterations": iterations} if iterations else {}))
        if a.cmd == "init-admin":
            if not a.args:
                raise ValueError("usage: init-admin NAME")
            ws.add_user(None, a.args[0], _pw("ASE_PASSWORD", "New admin password: "), "admin")
            print(f"Admin '{a.args[0]}' created.")
            return 0
        if a.cmd == "verify":                                     # verification needs no login
            v = ws.verify(a.args[0])
            print(("VALID: signed by {signer} ({role}) at {signed_at}" if v["valid"] else "INVALID: {reason}").format(**v))
            return 0 if v["valid"] else 1
        if not a.user:
            raise ValueError("--user is required")
        s = ws.login(a.user, _pw("ASE_PASSWORD", "Password: "))
        if a.cmd == "add-user":
            ws.add_user(s, a.args[0], _pw("ASE_NEW_PASSWORD", "New user's password: "), a.args[1])
            print("User created.")
        elif a.cmd == "users":
            for u in ws.users.list_users(s):
                print(f"{u['username']:20} {u['role']:9} {'disabled' if u['disabled'] else ''}")
        elif a.cmd == "approvals":
            for r in ws.approvals.list():
                print(f"#{r['id']} {r['status']:9} {r['kind']:9} {r['subject']} (by {r['requested_by']})")
        elif a.cmd == "request":
            print("Approval request #%d" % ws.request_approval(s, a.args[0], a.args[1], a.args[2]))
        elif a.cmd in ("approve", "reject"):
            ws.decide(s, int(a.args[0]), a.cmd == "approve", " ".join(a.args[1:]))
            print("Recorded.")
        elif a.cmd == "sign":
            print("Signature:", ws.sign(s, a.args[0]))
        elif a.cmd == "audit":
            s.require("view_audit")
            v = ws.audit.verify()
            print(("OK" if v["ok"] else "TAMPERED"), v["message"], f"({v['entries']} entries)")
            return 0 if v["ok"] else 1
        else:
            raise ValueError(f"unknown command {a.cmd}")
        return 0
    except (AuthError, ApprovalError, SigningError, WorkspaceError, ValueError, IndexError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
