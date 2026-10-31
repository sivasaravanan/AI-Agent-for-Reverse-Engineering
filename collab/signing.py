"""Signed reports: HMAC-SHA256 over the file's SHA-256 + signer + time, stored in '<file>.sig.json'.

HMAC is symmetric: anyone holding the workspace key can also forge a signature, so keep the key file private (mode 600) and
share the workspace only with people you trust. A valid signature shows the file is unchanged since a workspace user signed it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from collab.users import Session
from core import __version__

CHUNK = 1 << 20


class SigningError(Exception):
    pass


def load_or_create_key(path: str | Path) -> bytes:
    p = Path(path)
    if p.exists():
        k = p.read_bytes()
        if len(k) < 32:
            raise SigningError("The signing key file is damaged (too short).")
        return k
    p.parent.mkdir(parents=True, exist_ok=True)
    k = os.urandom(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(k)
    return k


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _mac(key: bytes, body: Dict[str, Any]) -> str:
    return hmac.new(key, json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"), hashlib.sha256).hexdigest()


def sig_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".sig.json")


def sign_file(path: str | Path, key: bytes, who: Session) -> Path:
    who.require("sign")
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise SigningError("Only regular files can be signed.")
    body = {"file": p.name, "sha256": _sha(p), "signer": who.username, "role": who.role,
            "signed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool_version": __version__, "algorithm": "HMAC-SHA256"}
    out = sig_path(p)
    out.write_text(json.dumps({**body, "mac": _mac(key, body)}, indent=2), encoding="utf-8")
    return out


def verify_file(path: str | Path, key: bytes) -> Dict[str, Any]:
    p, s = Path(path), sig_path(path)
    if not p.is_file():
        return {"valid": False, "reason": "file not found"}
    if not s.is_file():
        return {"valid": False, "reason": "no signature file next to this file"}
    try:
        data = json.loads(s.read_text(encoding="utf-8"))
        mac = data.pop("mac")
    except (ValueError, KeyError, OSError):
        return {"valid": False, "reason": "signature file is unreadable"}
    if not hmac.compare_digest(_mac(key, data), str(mac)):
        return {"valid": False, "reason": "signature does not match (wrong workspace key or the signature file was edited)"}
    if data.get("file") != p.name:
        return {"valid": False, "reason": "signature belongs to a different file name"}
    if data.get("sha256") != _sha(p):
        return {"valid": False, "reason": "FILE CHANGED since it was signed"}
    return {"valid": True, "signer": data["signer"], "role": data["role"], "signed_at": data["signed_at"], "sha256": data["sha256"]}
