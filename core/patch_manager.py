"""Phase 8: human-approved code correction.

Workflow:  propose -> diff (shown to the user) -> approve -> backup -> apply -> verify -> (revert)

Hard rules enforced here (and covered by tests):
  * The original ZIP is never modified; patches are applied to a *workspace copy* extracted by
    ``Workspace.create_from_zip`` (safe extraction, nothing outside the workspace is ever written).
  * A patch is applied only when the caller passes ``approved=True`` AND the exact diff was generated for display.
  * A backup (+ manifest with hashes) is written before any file is touched; writes are atomic.
  * Project code is NEVER executed here. Test verification needs an *isolated* runner (Phase 4); without
    one the result says so and the fix is reported as NOT verified.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from core.archive_manager import ArchiveError, ArchiveReader
from core.patch_generators import FixSuggestion, suggest_fix, why_no_fix
from core.static_analysis import analyze_text_file
from models.finding import Finding
from models.patch import (
    ApplyOutcome, BackupInfo, NoFixAvailable, Patch, PatchError, RunSummary,
    StaticComparison, TestComparison, VerificationResult,
)
from security.archive_validation import ArchiveLimits

BACKUP_DIR = ".ase_backups"
MARKER = ".ase_workspace.json"
MAX_PATCH_FILE_BYTES = 1024 * 1024
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_WIN_BAD_CHARS = re.compile(r'[<>:"|?*]')


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =========================================================================== workspace
@dataclass
class WorkspaceInfo:
    root: str
    files_written: int
    skipped: List[Dict[str, str]]


def _windows_unsafe(name: str) -> Optional[str]:
    for part in name.split("/"):
        if _WIN_BAD_CHARS.search(part):
            return "name has characters that are invalid on Windows"
        if part and (part.endswith((" ", ".")) or _WIN_RESERVED.match(part)):
            return "name is reserved or invalid on Windows"
    return None


class Workspace:
    """A writable, on-disk copy of (part of) an analyzed archive."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise PatchError(f"Workspace folder not found: {root}")

    # ------------------------------------------------------------------ creation
    @classmethod
    def create_from_zip(cls, zip_path: str | Path, dest: str | Path, limits: Optional[ArchiveLimits] = None,
                        include_vendored: bool = False) -> Tuple["Workspace", WorkspaceInfo]:
        limits = limits or ArchiveLimits()
        src = Path(zip_path)
        if src.is_dir() or not src.name.lower().endswith(".zip"):      # folder / tar input: convert safely first
            from core.input_adapter import prepared_input
            try:
                with prepared_input(src, limits) as prep:
                    ws, info = cls.create_from_zip(prep.zip_path, dest, limits, include_vendored)
            except ArchiveError as exc:
                raise PatchError(str(exc))
            marker = Path(info.root) / MARKER
            data = json.loads(marker.read_text(encoding="utf-8"))
            data["source_archive"] = src.name
            marker.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return ws, info
        dest = Path(dest)
        if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            raise PatchError("Choose a new or empty folder for the workspace; it must not contain files.")
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        written, skipped, hashes = 0, [], {}
        try:
            with ArchiveReader(zip_path, limits) as reader:
                for e in reader.entries:
                    if e.is_dir:
                        continue
                    reason = e.skip_reason or (None if (include_vendored or not e.vendored) else "vendored folder (not extracted)")
                    reason = reason or _windows_unsafe(e.name)
                    if reason:
                        skipped.append({"name": e.name[:200], "reason": reason})
                        continue
                    target = (root / e.name).resolve()
                    if root not in target.parents:                 # defence in depth after name validation
                        skipped.append({"name": e.name[:200], "reason": "resolves outside the workspace"})
                        continue
                    try:
                        data = reader.read_bytes(e, limits.max_member_size)
                    except ArchiveError as exc:
                        skipped.append({"name": e.name[:200], "reason": str(exc)[:120]})
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    hashes[e.name] = _sha(data)
                    written += 1
        except ArchiveError as exc:
            raise PatchError(str(exc))
        (root / MARKER).write_text(json.dumps({
            "source_archive": Path(zip_path).name, "created_at": _now(), "files": hashes,
            "note": "Workspace copy created by AI Software Engineering Agent; the source ZIP is never modified."},
            indent=2), encoding="utf-8")
        return cls(root), WorkspaceInfo(str(root), written, skipped)

    # ------------------------------------------------------------------ access
    def resolve(self, rel: str) -> Path:
        """Map a relative archive path to a real path inside the workspace, or refuse."""
        norm = rel.replace("\\", "/")
        pure = PurePosixPath(norm)
        if not norm or pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", norm) or "\x00" in norm:
            raise PatchError(f"Unsafe file path refused: {rel!r}")
        if pure.parts[0] in (BACKUP_DIR,) or pure.name == MARKER:
            raise PatchError("Workspace metadata and backups cannot be patched.")
        cur = self.root
        for part in pure.parts:
            cur = cur / part
            if cur.is_symlink():
                raise PatchError(f"Symbolic links are not patched: {rel!r}")
        resolved = cur.resolve()
        if self.root not in resolved.parents:
            raise PatchError(f"Path escapes the workspace: {rel!r}")
        return resolved

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise PatchError(f"File not found in workspace: {rel}")
        if path.stat().st_size > MAX_PATCH_FILE_BYTES:
            raise PatchError("File is too large to patch (limit 1 MiB).")
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            raise PatchError("Binary files cannot be patched.")
        try:
            return data.decode("utf-8-sig") if data.startswith(b"\xef\xbb\xbf") else data.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError("File is not valid UTF-8 text; refusing to patch it.")


# =========================================================================== propose / diff
def _match_eol(original: str, new: str) -> str:
    crlf, lf = original.count("\r\n"), original.count("\n") - original.count("\r\n")
    if crlf > lf and "\r\n" not in new:
        return new.replace("\n", "\r\n")
    if lf > crlf and "\r\n" in new and "\r\n" not in original:
        return new.replace("\r\n", "\n")
    return new


def propose(file: str, new_text: str, workspace: Optional[Workspace] = None, finding: Optional[Finding] = None,
            rationale: str = "", source: str = "manual", caution: str = "") -> Patch:
    """Create (but do not apply) a patch. ``file`` is relative to ``workspace`` (or absolute if no workspace)."""
    if workspace is None:
        p = Path(file)
        if not p.is_absolute():
            raise PatchError("Provide a workspace, or an absolute file path.")
        workspace, file = Workspace(p.parent), p.name
    if "\x00" in new_text:
        raise PatchError("Proposed text contains NUL bytes.")
    if source not in ("manual", "rule-based", "ai-unverified"):
        raise PatchError(f"Unknown patch source: {source}")
    original = workspace.read_text(file)
    new_text = _match_eol(original, new_text)
    if new_text == original:
        raise PatchError("The proposed text is identical to the current file; nothing to change.")
    return Patch(
        id=uuid.uuid4().hex[:12], file=file.replace("\\", "/"), workspace_root=str(workspace.root),
        original_text=original, new_text=new_text, original_sha256=_sha(original.encode("utf-8")),
        rationale=rationale, caution=caution, source=source, finding_id=finding.id if finding else "",
        rule_id=finding.rule_id if finding else "", created_at=_now())


def propose_from_finding(finding: Finding, workspace: Workspace) -> Patch:
    """Rule-based proposal for a finding, or NoFixAvailable explaining why not."""
    if not finding.file:
        raise NoFixAvailable("The finding has no file.")
    text = workspace.read_text(finding.file)
    suggestion: Optional[FixSuggestion] = suggest_fix(finding, text)
    if suggestion is None:
        raise NoFixAvailable(why_no_fix(finding, text))
    return propose(finding.file, suggestion.new_text, workspace, finding, suggestion.rationale,
                   "rule-based", suggestion.caution)


def diff(patch: Patch) -> str:
    """Unified diff for display. Generating it records the hash that ``apply`` later requires."""
    text = "".join(difflib.unified_diff(
        patch.original_text.splitlines(keepends=True), patch.new_text.splitlines(keepends=True),
        fromfile=f"a/{patch.file}", tofile=f"b/{patch.file}", n=3))
    patch.diff_hash = _sha((patch.original_sha256 + "\n" + patch.new_text).encode("utf-8"))
    return text


def _current_diff_hash(patch: Patch) -> str:
    return _sha((patch.original_sha256 + "\n" + patch.new_text).encode("utf-8"))


# =========================================================================== apply / revert
def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ase_tmp_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _encode_like(original_bytes: bytes, text: str) -> bytes:
    bom = b"\xef\xbb\xbf" if original_bytes.startswith(b"\xef\xbb\xbf") else b""
    return bom + text.encode("utf-8")


APPLY_GATE = None   # optional extra gate (Stage D team workspace): callable(patch) that raises PatchError to refuse


def _preflight(patch: Patch, approved: bool, ws: Workspace) -> Tuple[Path, bytes]:
    """All checks that must pass before anything is written (or any test is run)."""
    if approved is not True:
        raise PatchError("Approval is required: the user must review the diff and explicitly approve it.")
    if patch.status != "proposed":
        raise PatchError(f"Patch is already '{patch.status}'; propose a new patch instead.")
    if not patch.diff_hash:
        raise PatchError("The diff has not been shown yet; call diff() and let the user review it before applying.")
    if patch.diff_hash != _current_diff_hash(patch):
        raise PatchError("The patch changed after its diff was shown; show the new diff and ask for approval again.")
    if APPLY_GATE is not None:
        APPLY_GATE(patch)
    target = ws.resolve(patch.file)
    if _sha(ws.read_text(patch.file).encode("utf-8")) != patch.original_sha256:
        raise PatchError("The file changed since this patch was proposed; propose it again.")
    return target, target.read_bytes()


def apply(patch: Patch, approved: bool = False, workspace: Optional[Workspace] = None,
          backup_dir: Optional[Path] = None) -> BackupInfo:
    """Apply an approved patch. Refuses unless every safety condition holds."""
    ws = workspace or Workspace(patch.workspace_root)
    target, current = _preflight(patch, approved, ws)
    new_bytes = _encode_like(current, patch.new_text)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = Path(backup_dir) if backup_dir else ws.root / BACKUP_DIR / f"{stamp}_{patch.id}"
    backup_file = bdir / Path(*PurePosixPath(patch.file).parts)
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_bytes(current)                                   # backup BEFORE touching the file
    info = BackupInfo(patch_id=patch.id, file=patch.file, target_path=str(target), backup_path=str(backup_file),
                      manifest_path=str(bdir / "manifest.json"), original_sha256=_sha(current),
                      new_sha256=_sha(new_bytes), applied_at=_now())
    Path(info.manifest_path).write_text(json.dumps({**info.__dict__, "source": patch.source, "rule_id": patch.rule_id,
                                                    "finding_id": patch.finding_id}, indent=2), encoding="utf-8")
    _atomic_write(target, new_bytes)                                   # original stays intact if this raises
    patch.status = "applied"
    return info


def revert(backup: BackupInfo, force: bool = False, patch: Optional[Patch] = None) -> Path:
    """Restore the backed-up original. Refuses if the file was edited after the patch unless ``force``."""
    target = Path(backup.target_path)
    if not Path(backup.backup_path).is_file():
        raise PatchError("The backup file is missing; cannot revert automatically.")
    if not target.is_file():
        raise PatchError("The patched file no longer exists.")
    if _sha(target.read_bytes()) != backup.new_sha256 and not force:
        raise PatchError("The file was modified after the patch was applied; reverting would discard those edits "
                         "(use force only if that is intended).")
    original = Path(backup.backup_path).read_bytes()
    if _sha(original) != backup.original_sha256:
        raise PatchError("The backup file does not match its recorded hash; refusing to restore it.")
    _atomic_write(target, original)
    if patch is not None:
        patch.status = "reverted"
    return target


# =========================================================================== verification
class IsolatedTestRunner(Protocol):
    """Implemented in Phase 4. ``isolated`` must be True or the runner is refused."""
    isolated: bool

    def run(self, workspace_root: Path) -> RunSummary: ...


@dataclass
class RollbackPolicy:
    auto_revert: bool = True
    block_severities: Tuple[str, ...] = ("critical", "high")
    revert_on_test_regression: bool = True


def _sig(findings: Sequence[Finding]) -> Counter:
    return Counter((f.rule_id, f.evidence) for f in findings)


def compare_static(patch: Patch, before_text: str, after_text: str) -> StaticComparison:
    before = analyze_text_file(patch.file, before_text)
    after = analyze_text_file(patch.file, after_text)
    b, a = _sig(before), _sig(after)
    by_sig = {(f.rule_id, f.evidence): f for f in before + after}

    def render(counter: Counter) -> List[Dict[str, Any]]:
        out = []
        for key, n in counter.items():
            f = by_sig[key]
            out += [{"rule_id": f.rule_id, "severity": f.severity, "title": f.title, "evidence": f.evidence}] * n
        return out

    resolved, new = b - a, a - b
    target = None
    if patch.rule_id:
        target = sum(1 for k in b.elements() if k[0] == patch.rule_id) > sum(1 for k in a.elements() if k[0] == patch.rule_id)
    return StaticComparison(target_resolved=target, resolved=render(resolved), new=render(new),
                            unchanged_count=sum((a & b).values()))


def compare_runs(baseline: RunSummary, after: RunSummary) -> TestComparison:
    return TestComparison(
        ran=True, baseline_passed=len(baseline.passed), baseline_failed=len(baseline.failed),
        after_passed=len(after.passed), after_failed=len(after.failed),
        regressions=sorted(baseline.passed & after.failed), fixed=sorted(baseline.failed & after.passed),
        timed_out=baseline.timed_out or after.timed_out)


def _safe_run(runner, root: Path, label: str, result: VerificationResult) -> Optional[RunSummary]:
    try:
        summary = runner.run(root)
    except Exception as exc:  # noqa: BLE001 - a runner failure must never crash the application
        result.skipped_reasons.append(f"Test run ({label}) failed to execute: {type(exc).__name__}: {str(exc)[:120]}")
        return None
    if not isinstance(summary, RunSummary):
        result.skipped_reasons.append(f"Test run ({label}) returned an unusable result.")
        return None
    return summary


def apply_and_verify(patch: Patch, approved: bool = False, workspace: Optional[Workspace] = None,
                     runner: Optional[IsolatedTestRunner] = None,
                     policy: Optional[RollbackPolicy] = None) -> ApplyOutcome:
    """Back up, apply, compare against the baseline, and roll back when a rollback condition is met."""
    policy = policy or RollbackPolicy()
    ws = workspace or Workspace(patch.workspace_root)
    _preflight(patch, approved, ws)                                    # refuse BEFORE running anything
    result = VerificationResult()
    baseline: Optional[RunSummary] = None
    runner_ok = False
    if runner is None:
        result.skipped_reasons.append("No isolated test runner is available (Phase 4); tests were not run.")
    elif getattr(runner, "isolated", False) is not True:
        result.skipped_reasons.append("The test runner is not isolated; project code is never executed on the host, "
                                      "so tests were not run.")
    else:
        runner_ok = True
        baseline = _safe_run(runner, ws.root, "baseline", result)
        runner_ok = baseline is not None
    backup = apply(patch, approved, ws)                                # raises PatchError when not allowed
    after_text = ws.read_text(patch.file)
    result.static = compare_static(patch, patch.original_text, after_text)
    result.level = "static-checked"
    for item in result.static.new:
        if item["severity"] in policy.block_severities:
            result.rollback_reasons.append(f"New {item['severity']} finding introduced: {item['title']}")
        else:
            result.warnings.append(f"New {item['severity']} finding introduced: {item['title']}")
    if result.static.target_resolved is False:
        result.warnings.append("The targeted finding still appears after the patch.")
    if runner_ok and baseline is not None:
        after = _safe_run(runner, ws.root, "after patch", result)
        if after is not None:
            result.tests = compare_runs(baseline, after)
            if result.tests.regressions and policy.revert_on_test_regression:
                result.rollback_reasons.append(f"{len(result.tests.regressions)} test(s) passed before and fail after the patch")
    reverted = False
    if result.rollback_reasons:
        result.level = "failed"
        if policy.auto_revert:
            revert(backup, patch=patch)
            patch.status = "rolled-back"
            reverted = True
    elif result.tests.ran and not result.tests.timed_out:
        result.level = "test-verified"
    # the claim never says more than what was actually established
    if result.level == "failed":
        result.claim = "FAILED: " + "; ".join(result.rollback_reasons) + (" The patch was rolled back." if reverted else " Rollback was not automatic.")
    elif result.level == "test-verified":
        result.claim = (f"No regressions in the isolated test run ({result.tests.after_passed} passed, "
                        f"{result.tests.after_failed} failed after; {result.tests.baseline_failed} failed before). "
                        "This shows the existing tests still behave the same; it does not prove the fix is correct.")
    else:
        result.claim = ("NOT VERIFIED: static re-check found no blocking new findings, but tests were not run, "
                        "so the fix is unproven. Review the change and run your tests.")
    msg = "Patch applied." if not reverted else "Patch was applied, failed verification, and was rolled back."
    return ApplyOutcome(patch=patch, backup=backup, verification=result, reverted=reverted, message=msg)
