"""A shared team folder: users, approvals, history database, audit log and signing key live together."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from collab.approvals import ApprovalStore
from collab.signing import load_or_create_key, sign_file, verify_file
from collab.users import AuthError, Session, UserStore, DEFAULT_ITERATIONS
from database.db_manager import DbManager
from enterprise.audit import AuditLog


class WorkspaceError(Exception):
    pass


class TeamWorkspace:
    def __init__(self, folder: str | Path, iterations: int = DEFAULT_ITERATIONS, ttl_hours: int = 72):
        self.root = Path(folder)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"Workspace folder is not usable ({exc}).")
        self.users = UserStore(self.root / "users.db", iterations)
        self.approvals = ApprovalStore(self.root / "approvals.db", ttl_hours)
        self.audit = AuditLog(self.root / "audit.jsonl")
        self._key: Optional[bytes] = None

    # history is shared: one DB for the whole team, rows carry the analysing user in the audit log
    def history(self) -> DbManager:
        return DbManager(self.root / "history.db")

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = load_or_create_key(self.root / "signing.key")
        return self._key

    # each action below checks the role, then audits
    def login(self, username: str, password: str) -> Session:
        try:
            s = self.users.authenticate(username, password)
        except AuthError as exc:
            self.audit.append("login.failed", username or "?", "", {"reason": str(exc)[:80]})
            raise
        self.audit.append("login", s.username, "", {"role": s.role})
        return s

    def add_user(self, actor: Optional[Session], username: str, password: str, role: str) -> None:
        self.users.create_user(actor, username, password, role)
        self.audit.append("user.create", actor.username if actor else "bootstrap", username, {"role": role})

    def set_role(self, actor: Session, username: str, role: str) -> None:
        self.users.set_role(actor, username, role)
        self.audit.append("user.role", actor.username, username, {"role": role})

    def set_disabled(self, actor: Session, username: str, disabled: bool) -> None:
        self.users.set_disabled(actor, username, disabled)
        self.audit.append("user.disable" if disabled else "user.enable", actor.username, username)

    def request_approval(self, who: Session, kind: str, subject: str, payload_sha256: str, note: str = "") -> int:
        i = self.approvals.request(who, kind, subject, payload_sha256, note)
        self.audit.append("approval.request", who.username, subject, {"id": i, "kind": kind, "payload": payload_sha256[:16]})
        return i

    def decide(self, who: Session, approval_id: int, approve: bool, comment: str = "") -> None:
        self.approvals.decide(who, approval_id, approve, comment)
        self.audit.append("approval.approve" if approve else "approval.reject", who.username, str(approval_id), {"comment": comment})

    def sign(self, who: Session, path: str | Path) -> Path:
        out = sign_file(path, self.key, who)
        self.audit.append("report.sign", who.username, Path(path).name)
        return out

    def verify(self, path: str | Path) -> Dict[str, Any]:
        return verify_file(path, self.key)

    def save_analysis(self, who: Session, result, report_paths=None, archive_sha256: str = "") -> int:
        who.require("analyze")
        aid = self.history().save_result(result, report_paths, archive_sha256)
        self.audit.append("analysis.save", who.username, result.archive_name, {"history_id": aid, "findings": len(result.findings)})
        return aid

    def apply_gate(self, who: Session, approval_id: Optional[int]):
        """Return a callable for core.patch_manager.APPLY_GATE: a patch may be applied only with an approved, matching request."""
        from core.patch_manager import PatchError

        done = set()

        def gate(patch) -> None:
            who.require("propose_fix")
            if approval_id is None or not self.approvals.is_approved(approval_id, patch.diff_hash):
                raise PatchError("Team workspace: this diff needs an approved, unexpired approval request for exactly this change.")
            if (patch.id, patch.diff_hash) not in done:          # apply_and_verify checks twice; audit once
                done.add((patch.id, patch.diff_hash))
                self.audit.append("patch.apply", who.username, patch.file, {"approval": approval_id, "diff": patch.diff_hash[:16]})
        return gate
