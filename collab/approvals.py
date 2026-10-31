"""Approval workflow: an analyst requests, a different reviewer/admin decides. Approvals are bound to a payload hash
(e.g. the SHA-256 of a patch diff), expire, and can be used to gate applying a fix."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from collab.users import AuthError, Session

KINDS = ("patch", "report", "release", "exception")
DEFAULT_TTL_HOURS = 72


class ApprovalError(Exception):
    pass


class ApprovalStore:
    def __init__(self, path: str | Path, ttl_hours: int = DEFAULT_TTL_HOURS):
        self.path = Path(path)
        self.ttl = int(ttl_hours) * 3600
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as con:
            con.execute("CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, "
                        "payload_sha256 TEXT NOT NULL, note TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at REAL NOT NULL, "
                        "status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at REAL, decision_comment TEXT)")

    def _c(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def request(self, who: Session, kind: str, subject: str, payload_sha256: str, note: str = "") -> int:
        who.require("request_approval")
        if kind not in KINDS:
            raise ApprovalError(f"Unknown approval kind '{kind}'.")
        if not payload_sha256 or len(payload_sha256) < 16:
            raise ApprovalError("An approval must be bound to a payload hash.")
        with self._c() as con:
            return int(con.execute("INSERT INTO approvals(kind, subject, payload_sha256, note, requested_by, requested_at) VALUES (?,?,?,?,?,?)",
                                   (kind, subject[:200], payload_sha256, note[:500], who.username, time.time())).lastrowid)

    def decide(self, who: Session, approval_id: int, approve: bool, comment: str = "") -> None:
        who.require("approve")
        with self._c() as con:
            r = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise ApprovalError("No such approval request.")
            if r["status"] != "pending":
                raise ApprovalError(f"Already {r['status']}.")
            if r["requested_by"] == who.username:
                raise ApprovalError("Separation of duties: you cannot decide your own request.")
            if time.time() - r["requested_at"] > self.ttl:
                con.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
                raise ApprovalError("This request has expired; ask for a new one.")
            if not approve and not comment.strip():
                raise ApprovalError("Give a reason when rejecting.")
            con.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_comment=? WHERE id=?",
                        ("approved" if approve else "rejected", who.username, time.time(), comment[:500], approval_id))

    def get(self, approval_id: int) -> Optional[Dict[str, Any]]:
        with self._c() as con:
            r = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r) -> Dict[str, Any]:
        d = dict(r)
        if d["status"] == "pending" and time.time() - d["requested_at"] > self.ttl:
            d["status"] = "expired"
        for k in ("requested_at", "decided_at"):
            if d.get(k):
                d[k] = datetime.fromtimestamp(d[k], timezone.utc).isoformat(timespec="seconds")
        return d

    def list(self, status: str = "") -> List[Dict[str, Any]]:
        with self._c() as con:
            rows = [self._row(r) for r in con.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 500")]
        return [r for r in rows if not status or r["status"] == status]

    def is_approved(self, approval_id: int, payload_sha256: str) -> bool:
        """True only for an unexpired approval of exactly this payload."""
        with self._c() as con:
            r = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return bool(r and r["status"] == "approved" and r["payload_sha256"] == payload_sha256 and time.time() - r["decided_at"] <= self.ttl)
