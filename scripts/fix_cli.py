"""Command-line fix workflow (Phase 8).

  python scripts/fix_cli.py project.zip --workspace C:\\work\\fixes --list
  python scripts/fix_cli.py project.zip --workspace C:\\work\\fixes --finding F-0003
  python scripts/fix_cli.py --revert C:\\work\\fixes\\.ase_backups\\<stamp>_<id>\\manifest.json

The ZIP is never modified. Patches go to a workspace copy, only after you read the diff and answer 'y'.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.analysis_orchestrator import analyze_archive  # noqa: E402
from core.archive_manager import ArchiveError  # noqa: E402
from core.patch_generators import can_suggest  # noqa: E402
from core.patch_manager import (  # noqa: E402
    Workspace, apply_and_verify, diff, propose_from_finding, revert,
)
from models.patch import BackupInfo, NoFixAvailable, PatchError  # noqa: E402


def run(zip_path, workspace_dir, finding_id=None, list_only=False, input_fn=input, out=print) -> int:
    try:
        result = analyze_archive(zip_path)
    except ArchiveError as exc:
        out(f"ERROR: {exc}")
        return 2
    fixable = [f for f in result.findings if can_suggest(f)]
    if list_only or not finding_id:
        out(f"{len(fixable)} finding(s) have an automatic fix proposal:")
        for f in fixable:
            out(f"  {f.id}  {f.severity:<6} {f.rule_id:<22} {f.file}:{f.line}")
        return 0
    finding = next((f for f in result.findings if f.id == finding_id), None)
    if finding is None:
        out(f"ERROR: no finding {finding_id}")
        return 2
    try:
        dest = Path(workspace_dir)
        if dest.exists() and (dest / ".ase_workspace.json").exists():
            ws = Workspace(dest)
        else:
            ws, info = Workspace.create_from_zip(zip_path, dest)
            out(f"Workspace created: {info.root} ({info.files_written} files; {len(info.skipped)} skipped)")
        patch = propose_from_finding(finding, ws)
    except NoFixAvailable as exc:
        out(f"No automatic fix: {exc}")
        return 1
    except PatchError as exc:
        out(f"ERROR: {exc}")
        return 2
    out(diff(patch))
    out(f"Rationale: {patch.rationale}")
    if patch.caution:
        out(f"Caution:   {patch.caution}")
    if input_fn("Apply this patch to the workspace copy? [y/N] ").strip().lower() != "y":
        out("Not applied.")
        return 0
    try:
        outcome = apply_and_verify(patch, approved=True, workspace=ws)
    except PatchError as exc:
        out(f"Refused: {exc}")
        return 2
    out(outcome.message)
    out(outcome.verification.claim)
    for line in outcome.verification.skipped_reasons + outcome.verification.warnings:
        out(f"  - {line}")
    if outcome.backup:
        out(f"Backup manifest (use --revert to undo): {outcome.backup.manifest_path}")
    return 0


def run_revert(manifest_path, out=print, force=False) -> int:
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        fields = BackupInfo.__dataclass_fields__
        revert(BackupInfo(**{k: v for k, v in data.items() if k in fields}), force=force)
    except (OSError, ValueError, TypeError, PatchError) as exc:
        out(f"Revert refused: {exc}")
        return 2
    out("Reverted: original file content restored.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", nargs="?")
    ap.add_argument("--workspace")
    ap.add_argument("--finding")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--revert", metavar="MANIFEST")
    ap.add_argument("--force", action="store_true", help="with --revert: discard edits made after the patch")
    a = ap.parse_args(argv)
    if a.revert:
        return run_revert(a.revert, force=a.force)
    if not a.zip or (not a.workspace and not a.list):
        ap.error("zip and --workspace are required (or use --list / --revert)")
    return run(a.zip, a.workspace, a.finding, a.list)


if __name__ == "__main__":
    sys.exit(main())
