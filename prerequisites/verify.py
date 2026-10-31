"""Installer verification: SHA-256 (from a trusted checksum source) and Windows Authenticode publisher check."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_shasums(text: str) -> Dict[str, str]:
    """Parse ``<hash>  <name>`` / ``<hash> *<name>`` lines (sha256sum format) -> {name: hash}."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2).strip().split("/")[-1]] = m.group(1).lower()
    return out


@dataclass
class VerifyResult:
    ok: bool
    method: str = "none"        # sha256 | authenticode | sha256+authenticode | none
    detail: str = ""
    sha256: str = ""
    publisher: str = ""


Runner = Callable[[Sequence[str]], tuple]


def _default_runner(cmd: Sequence[str]) -> tuple:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = subprocess.run(list(cmd), capture_output=True, timeout=60, stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


def authenticode(path: Path, runner: Optional[Runner] = None) -> Dict[str, str]:
    """{'status','publisher'} from PowerShell's Get-AuthenticodeSignature; status 'Unavailable' off Windows or on error."""
    run = runner or _default_runner
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "[pscustomobject]@{Status=[string]$s.Status; Subject=[string]$s.SignerCertificate.Subject} | ConvertTo-Json -Compress")
    rc, out, err = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"& {{ {script} }}", str(path)])
    if rc != 0 or not out.strip():
        return {"status": "Unavailable", "publisher": "", "detail": (err or out).strip()[:200]}
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except ValueError:
        return {"status": "Unavailable", "publisher": "", "detail": out.strip()[:200]}
    return {"status": str(data.get("Status", "")), "publisher": str(data.get("Subject", "")), "detail": ""}


def verify_installer(path: Path, *, expected_sha256: str = "", publishers: Iterable[str] = (), require: bool = True,
                     windows: bool = True, runner: Optional[Runner] = None) -> VerifyResult:
    """ok when the hash matches, or (no hash given) the signature is Valid and the subject contains an expected publisher.
    A hash that is given but does not match always fails. With ``require`` and no evidence at all, fails closed."""
    path = Path(path)
    if not path.is_file():
        return VerifyResult(False, "none", "File not found.")
    sha = sha256_file(path)
    methods = []
    if expected_sha256:
        if sha.lower() != expected_sha256.lower():
            return VerifyResult(False, "sha256", f"SHA-256 mismatch: expected {expected_sha256[:16]}..., got {sha[:16]}...", sha)
        methods.append("sha256")
    pubs = [p.lower() for p in publishers if p]
    publisher = ""
    if windows and pubs:
        sig = authenticode(path, runner)
        publisher = sig.get("publisher", "")
        if sig["status"] == "Valid" and any(p in publisher.lower() for p in pubs):
            methods.append("authenticode")
        elif not methods:
            why = f"signature status '{sig['status']}'" + (f", signer '{publisher[:80]}'" if publisher else "")
            if require:
                return VerifyResult(False, "none", f"No trusted checksum and the installer is not validly signed by the expected publisher ({why}).", sha, publisher)
    if methods:
        return VerifyResult(True, "+".join(methods), "Verified.", sha, publisher)
    if require:
        return VerifyResult(False, "none", "No checksum or signature could be used to verify this installer.", sha)
    return VerifyResult(True, "none", "UNVERIFIED (verification not required by policy).", sha)
