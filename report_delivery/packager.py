"""Delivery package builder: secret-scanned, checksummed, reproducible-layout ZIP of the application itself.
Untrusted analysed projects, logs, history databases and credential files are never included."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from . import __version__
from .docs_gen import (RUN_BAT, TEST_BAT, architecture_markdown, changelog_stub, installation_markdown,
                       known_issues_markdown, user_guide_markdown)
from .redaction import find_secrets, redact_text


class PackagingError(Exception):
    """Raised when a package cannot be produced safely. Messages never contain secret values."""


DEFAULT_EXCLUDE_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
                        "env", "node_modules", "dist", "build", ".idea", ".vs", "logs", "reports", "uploads", "upload",
                        "workspace", "extracted", "sandbox", "analysis_runs", "history", "tmp", "temp"}
CREDENTIAL_PATTERNS = ["*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "id_rsa*", "id_ed25519*", "*.kdbx", ".env", ".env.local",
                       ".env.production", ".netrc", "credentials*.json", "secrets*.json", "*.token"]
DATA_PATTERNS = ["*.db", "*.sqlite", "*.sqlite3", "*.mdf", "*.ldf", "*.bak", "*.dump", "*.log", "*.pyc", "*.pyo", ".DS_Store", "Thumbs.db",
                 "*.zip", "*.rar", "*.7z", "*.tar", "*.tar.gz"]


@dataclass
class PackageOptions:
    version: str = "1.0.0"
    name: Optional[str] = None
    reports_dir: Optional[Union[str, Path]] = None          # output of build_reports(); included under reports/
    evidence: Optional[Dict[str, Any]] = None               # from collect_test_evidence()
    known_issues: Sequence[str] = ()
    changelog_text: Optional[str] = None
    exclude_dirs: Sequence[str] = ()
    allow_archives: Sequence[str] = ()                      # fnmatch patterns (rel paths) of .zip etc. to keep, e.g. sample_projects/*.zip
    secret_scan_allowlist: Sequence[str] = ()               # fnmatch patterns of files whose fake secrets are intentional (test fixtures)
    fail_on_secret: bool = False                            # True: raise instead of excluding the file
    generate_missing: bool = True
    max_total_bytes: int = 500_000_000
    max_file_bytes: int = 25_000_000
    python_hint: str = "3.11 or newer"


def _match(rel: str, base: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel, p) for p in patterns)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _scan_zip_bytes(data: bytes, max_members: int = 2000, max_member_bytes: int = 1_000_000):
    """Secret-scan the text members of a nested ZIP in memory (never extracted to disk)."""
    import io
    hits = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for i, info in enumerate(zf.infolist()):
                if i >= max_members:
                    break
                if info.is_dir() or info.file_size > max_member_bytes or info.flag_bits & 1:
                    continue
                blob = zf.read(info)
                if not _is_binary(blob):
                    hits += [(k, n) for k, n in find_secrets(blob.decode("utf-8", "replace"), info.filename)]
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return []
    return hits


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checklist(paths: List[str]) -> Dict[str, Dict[str, Any]]:
    low = [p.lower() for p in paths]

    def any_(pred) -> bool:
        return any(pred(p) for p in low)
    items = {
        "source_code": any_(lambda p: p.endswith(".py") and not p.startswith("tests/")),
        "requirements": any_(lambda p: p.endswith("requirements.txt")),
        "installation_instructions": any_(lambda p: "install" in p.rsplit("/", 1)[-1] and p.endswith(".md")) or any_(lambda p: p == "readme.md"),
        "run_scripts": any_(lambda p: p.startswith("scripts/run.")),
        "test_scripts": any_(lambda p: p.startswith("scripts/test.")),
        "sample_projects": any_(lambda p: p.startswith(("sample_projects/", "samples/", "tests/fixtures/"))),
        "tests": any_(lambda p: p.startswith("tests/") and p.endswith(".py")),
        "user_guide": any_(lambda p: "user_guide" in p or "user-guide" in p),
        "architecture_documentation": any_(lambda p: "architecture" in p and p.endswith(".md")),
        "changelog": any_(lambda p: p.rsplit("/", 1)[-1].startswith("changelog")),
        "known_issues": any_(lambda p: "known_issues" in p or "known-issues" in p),
        "build_and_test_evidence": any_(lambda p: p.startswith("evidence/")),
    }
    return {k: {"present": v} for k, v in items.items()}


def build_delivery_package(root: Union[str, Path], out_zip: Union[str, Path], options: Optional[PackageOptions] = None) -> Dict[str, Any]:
    o = options or PackageOptions()
    root = Path(root).resolve()
    if not root.is_dir():
        raise PackagingError(f"Project folder not found: {root.name}")
    out_zip = Path(out_zip).resolve()
    name = o.name or root.name
    prefix = f"{name}-{o.version}"
    skip_dirs = {d.lower() for d in DEFAULT_EXCLUDE_DIRS} | {d.lower() for d in o.exclude_dirs}
    files: Dict[str, bytes] = {}
    excluded: List[Dict[str, str]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        keep = []
        for d in sorted(dirnames):
            full = Path(dirpath) / d
            if full.is_symlink():
                excluded.append({"path": f"{rel_dir}/{d}".lstrip("./"), "reason": "symbolic link"})
            elif d.lower() in skip_dirs or d.lower().endswith(".egg-info"):
                excluded.append({"path": (f"{rel_dir}/{d}" if rel_dir != "." else d), "reason": "excluded directory (cache, logs, reports, uploads or build output)"})
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = (f"{rel_dir}/{fn}" if rel_dir != "." else fn)
            if full.resolve() == out_zip:
                continue
            if full.is_symlink():
                excluded.append({"path": rel, "reason": "symbolic link"})
                continue
            if _match(rel, fn, CREDENTIAL_PATTERNS) and not fn.lower().endswith((".example", ".sample", ".template")):
                excluded.append({"path": rel, "reason": "credential/secret file pattern"})
                continue
            if _match(rel, fn, DATA_PATTERNS) and not _match(rel, fn, o.allow_archives):
                excluded.append({"path": rel, "reason": "data, archive, log or compiled file (not source)"})
                continue
            try:
                size = full.stat().st_size
                if size > o.max_file_bytes:
                    excluded.append({"path": rel, "reason": "file too large"})
                    continue
                data = full.read_bytes()
            except OSError as exc:
                excluded.append({"path": rel, "reason": f"unreadable ({exc.__class__.__name__})"})
                continue
            if fn.lower().endswith(".zip") and not _match(rel, fn, o.secret_scan_allowlist):
                hits = _scan_zip_bytes(data)      # nested archives are inspected too (read-only, in memory)
            elif not _is_binary(data) and not _match(rel, fn, o.secret_scan_allowlist):
                hits = find_secrets(data.decode("utf-8", "replace"), rel)
            else:
                hits = []
            if hits:
                kinds = sorted({k for k, _ in hits})
                if o.fail_on_secret:
                    raise PackagingError(f"Possible secret(s) ({', '.join(kinds)}) found in {rel} line {hits[0][1]}; package not created.")
                excluded.append({"path": rel, "reason": f"possible secret detected ({', '.join(kinds)}) at line {hits[0][1]}; file left out"})
                continue
            total += len(data)
            if total > o.max_total_bytes:
                raise PackagingError("Package would exceed the size limit.")
            files[rel] = data

    # ---- reports + evidence supplied by the caller
    if o.reports_dir:
        rd = Path(o.reports_dir)
        if rd.is_dir():
            for p in sorted(rd.iterdir()):
                if p.is_file() and p.suffix.lower() in (".html", ".json", ".md", ".txt"):
                    text = p.read_text(encoding="utf-8", errors="replace")
                    files[f"reports/{p.name}"] = redact_text(text).encode("utf-8")
    if o.evidence:
        ev = dict(o.evidence)
        files["evidence/build_test_evidence.json"] = json.dumps(ev, indent=2, default=str).encode("utf-8")
        txt = (f"Command : {ev.get('command')}\nStatus  : {ev.get('status')}\nExit    : {ev.get('exit_code')}\n"
               f"Duration: {ev.get('duration_s')}s\nPython  : {ev.get('python')}\n\n--- stdout ---\n{ev.get('stdout', '')}\n--- stderr ---\n{ev.get('stderr', '')}\n")
        files["evidence/build_test_evidence.txt"] = redact_text(txt).encode("utf-8")

    # ---- generate only what is missing (never overwrite the project's own docs)
    generated: List[str] = []

    def gen(path: str, content: str) -> None:
        if path not in files and o.generate_missing:
            files[path] = content.encode("utf-8")
            generated.append(path)
    low = {p.lower(): p for p in files}
    if o.generate_missing:
        if not any("architecture" in p and p.endswith(".md") for p in low):
            gen("docs/ARCHITECTURE.md", architecture_markdown(files))
        reqs = files.get("requirements.txt", b"").decode("utf-8", "replace")
        if not any("install" in p.rsplit("/", 1)[-1] and p.endswith(".md") for p in low):
            gen("docs/INSTALLATION.md", installation_markdown(reqs, o.python_hint))
        if not any("user_guide" in p or "user-guide" in p for p in low):
            readme = files.get(low.get("readme.md", ""), b"").decode("utf-8", "replace")
            gen("docs/USER_GUIDE.md", user_guide_markdown(readme, "scripts/run.bat" in files))
        if not any(p.rsplit("/", 1)[-1].startswith("changelog") for p in low):
            gen("CHANGELOG.md", o.changelog_text or changelog_stub(o.version))
        if not any("known_issues" in p or "known-issues" in p for p in low):
            gen("KNOWN_ISSUES.md", known_issues_markdown(o.known_issues))
        gen("scripts/run.bat", RUN_BAT)
        gen("scripts/test.bat", TEST_BAT)
    if not files:
        raise PackagingError("Nothing to package after exclusions.")

    checklist = _checklist(sorted(files))
    missing = [k for k, v in checklist.items() if not v["present"]]
    manifest = {
        "package": prefix, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": f"report_delivery {__version__}", "checklist": checklist, "missing": missing,
        "generated_files": sorted(generated), "excluded": excluded,
        "files": [{"path": p, "bytes": len(d), "sha256": _sha(d)} for p, d in sorted(files.items())],
        "notes": ["Contains this application's own files only; analysed (untrusted) projects, logs, history databases and credential files are excluded.",
                  "Files marked in 'excluded' were left out deliberately; secret values are never recorded in this manifest.",
                  "Nothing here is certified safe or correct beyond the recorded build/test evidence "
                  + ("(included)." if o.evidence else "(none was supplied - tests were not recorded).")],
    }
    sums = "".join(f"{m['sha256']}  {m['path']}\n" for m in manifest["files"])
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_suffix(out_zip.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, d in sorted(files.items()):
            zf.writestr(zipfile.ZipInfo(f"{prefix}/{p}", date_time=time.gmtime(max(time.time(), 315532800))[:6]), d, zipfile.ZIP_DEFLATED)
        zf.writestr(f"{prefix}/DELIVERY_MANIFEST.json", json.dumps(manifest, indent=2))
        zf.writestr(f"{prefix}/SHA256SUMS.txt", sums)
    os.replace(tmp, out_zip)
    return {"zip": str(out_zip), "package": prefix, "file_count": len(files) + 2, "excluded": excluded,
            "generated": sorted(generated), "missing": missing, "checklist": checklist,
            "secrets_excluded": sum(1 for e in excluded if "secret detected" in e["reason"])}
