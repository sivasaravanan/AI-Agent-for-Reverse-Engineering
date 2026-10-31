"""Local users with roles. Passwords: PBKDF2-HMAC-SHA256 with a per-user salt; never stored or logged in clear.

Roles: admin (everything), reviewer (approve + sign), analyst (analyse, propose fixes, request approval), viewer (read only).
Bootstrap: the very first user must be an admin. Last-admin protection prevents locking everyone out.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROLES = ("admin", "reviewer", "analyst", "viewer")
PERMISSIONS: Dict[str, frozenset] = {
    "admin": frozenset({"view", "analyze", "grade", "propose_fix", "request_approval", "approve", "sign", "manage_users", "view_audit"}),
    "reviewer": frozenset({"view", "analyze", "grade", "propose_fix", "request_approval", "approve", "sign", "view_audit"}),
    "analyst": frozenset({"view", "analyze", "grade", "propose_fix", "request_approval"}),
    "viewer": frozenset({"view"}),
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
MIN_PASSWORD = 10
MAX_FAILS, LOCK_SECONDS = 5, 300
DEFAULT_ITERATIONS = 310_000


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    username: str
    role: str

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise AuthError(f"Your role ({self.role}) is not allowed to do this ({permission}).")


def _hash(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


class UserStore:
    def __init__(self, path: str | Path, iterations: int = DEFAULT_ITERATIONS):
        self.path = Path(path)
        self.iterations = max(1000, int(iterations))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as con:
            con.execute("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL, "
                        "hash TEXT NOT NULL, iterations INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, "
                        "fails INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")

    def _c(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _check_new(username: str, password: str, role: str) -> None:
        if not USERNAME_RE.match(username or ""):
            raise AuthError("Username must be 2-40 characters: letters, digits, dot, dash, underscore.")
        if role not in ROLES:
            raise AuthError(f"Unknown role '{role}'.")
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")

    def count(self) -> int:
        with self._c() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, actor: Optional[Session], username: str, password: str, role: str) -> None:
        self._check_new(username, password, role)
        first = self.count() == 0
        if first:
            if role != "admin":
                raise AuthError("The first user must be an admin.")
        else:
            if actor is None:
                raise AuthError("Sign in as an admin to add users.")
            actor.require("manage_users")
        salt = os.urandom(16)
        try:
            with self._c() as con:
                con.execute("INSERT INTO users(username, role, salt, hash, iterations, created_at) VALUES (?,?,?,?,?,?)",
                            (username, role, salt.hex(), _hash(password, salt, self.iterations), self.iterations,
                             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        except sqlite3.IntegrityError:
            raise AuthError("That username already exists.")

    def authenticate(self, username: str, password: str) -> Session:
        generic = AuthError("Wrong username or password.")          # same message for unknown user and wrong password
        with self._c() as con:
            r = con.execute("SELECT * FROM users WHERE username=?", (username or "",)).fetchone()
            if r is None:
                _hash(password or "", b"\0" * 16, self.iterations)  # keep timing similar
                raise generic
            now = time.time()
            if r["locked_until"] > now:
                raise AuthError(f"Account temporarily locked after repeated failures; try again in {int(r['locked_until'] - now) + 1}s.")
            if r["disabled"]:
                raise AuthError("This account is disabled.")
            ok = hmac.compare_digest(_hash(password or "", bytes.fromhex(r["salt"]), r["iterations"]), r["hash"])
            if not ok:
                fails = r["fails"] + 1
                con.execute("UPDATE users SET fails=?, locked_until=? WHERE username=?",
                            (0 if fails >= MAX_FAILS else fails, now + LOCK_SECONDS if fails >= MAX_FAILS else 0, username))
                con.commit()                                         # persist the counter BEFORE raising (the with-block rolls back on error)
                raise generic
            con.execute("UPDATE users SET fails=0, locked_until=0 WHERE username=?", (username,))
            return Session(r["username"], r["role"])

    def list_users(self, actor: Session) -> List[Dict]:
        actor.require("manage_users")
        with self._c() as con:
            return [{"username": r["username"], "role": r["role"], "disabled": bool(r["disabled"]), "created_at": r["created_at"]}
                    for r in con.execute("SELECT * FROM users ORDER BY username")]

    def _admins(self, con, excluding: str = "") -> int:
        return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND username<>?", (excluding,)).fetchone()[0]

    def set_role(self, actor: Session, username: str, role: str) -> None:
        actor.require("manage_users")
        if role not in ROLES:
            raise AuthError(f"Unknown role '{role}'.")
        with self._c() as con:
            if role != "admin" and self._admins(con, username) == 0:
                raise AuthError("Cannot remove the last active admin.")
            if con.execute("UPDATE users SET role=? WHERE username=?", (role, username)).rowcount == 0:
                raise AuthError("No such user.")

    def set_disabled(self, actor: Session, username: str, disabled: bool) -> None:
        actor.require("manage_users")
        with self._c() as con:
            if disabled and self._admins(con, username) == 0:
                raise AuthError("Cannot disable the last active admin.")
            if con.execute("UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, username)).rowcount == 0:
                raise AuthError("No such user.")

    def change_password(self, actor: Session, username: str, new_password: str) -> None:
        if actor.username != username:
            actor.require("manage_users")
        if len(new_password or "") < MIN_PASSWORD:
            raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
        salt = os.urandom(16)
        with self._c() as con:
            if con.execute("UPDATE users SET salt=?, hash=?, iterations=?, fails=0, locked_until=0 WHERE username=?",
                           (salt.hex(), _hash(new_password, salt, self.iterations), self.iterations, username)).rowcount == 0:
                raise AuthError("No such user.")

    def session_for(self, username: str) -> Optional[Session]:
        """Re-validate a remembered session against the store (role changes / disabling take effect immediately)."""
        with self._c() as con:
            r = con.execute("SELECT role, disabled FROM users WHERE username=?", (username,)).fetchone()
        return Session(username, r["role"]) if r and not r["disabled"] else None
