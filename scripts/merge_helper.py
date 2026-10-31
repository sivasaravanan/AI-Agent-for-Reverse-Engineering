"""Merge a download from another session into this project WITHOUT overwriting working code.

Dry run (default):   python scripts/merge_helper.py --incoming downloaded_folder_or.zip
Apply safe changes:  python scripts/merge_helper.py --incoming ... --apply

Rules
  new               file does not exist here                          -> added
  identical         same content                                      -> skipped
  upgrade           ours is a PLACEHOLDER, theirs is real             -> replaced
  keep-ours         theirs is a placeholder, ours differs             -> left alone
  conflict          both real and different                           -> NOT touched; theirs is copied to
                                                                         merge_incoming/<path> for manual review
Existing implemented files are never overwritten. Compiled/cache/report/log files are ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.archive_manager import ArchiveReader  # noqa: E402
from core.phase_registry import is_placeholder_text  # noqa: E402
from security.archive_validation import ArchiveLimits  # noqa: E402

IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", "reports", "logs", "merge_incoming", ".vs", "node_modules"}
IGNORED_SUFFIX = (".pyc", ".pyo", ".log")
STAGING = "merge_incoming"
BIG = ArchiveLimits(max_member_size=20 * 1024 ** 2, max_text_file_size=20 * 1024 ** 2)


@dataclass
class Action:
    path: str
    kind: str          # new | identical | upgrade | keep-ours | conflict
    detail: str = ""


def _ignored(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p in IGNORED_PARTS for p in parts) or rel.endswith(IGNORED_SUFFIX)


def load_incoming(src: Path) -> Dict[str, bytes]:
    """Return {relative posix path: bytes} from a folder or a ZIP (validated, never extracted blindly)."""
    files: Dict[str, bytes] = {}
    if src.is_dir():
        for p in src.rglob("*"):
            if p.is_file() and not p.is_symlink():
                files[p.relative_to(src).as_posix()] = p.read_bytes()
    else:
        with ArchiveReader(src, BIG) as r:
            for e in r.entries:
                if not e.is_dir and not e.skip_reason:
                    files[e.name] = r.read_bytes(e, BIG.max_member_size)
    # strip a single common top-level folder (e.g. "ai_software_engineering_agent/")
    tops = {PurePosixPath(k).parts[0] for k in files if len(PurePosixPath(k).parts) > 1}
    if files and len(tops) == 1 and all(len(PurePosixPath(k).parts) > 1 for k in files):
        files = {"/".join(PurePosixPath(k).parts[1:]): v for k, v in files.items()}
    return {k: v for k, v in files.items() if not _ignored(k)}


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def plan(incoming: Dict[str, bytes], root: Path) -> List[Action]:
    actions: List[Action] = []
    for rel in sorted(incoming):
        data = incoming[rel]
        target = root / rel
        if not target.exists():
            actions.append(Action(rel, "new"))
            continue
        ours = target.read_bytes()
        if hashlib.sha256(ours).digest() == hashlib.sha256(data).digest():
            actions.append(Action(rel, "identical"))
            continue
        ours_ph, theirs_ph = is_placeholder_text(rel, _text(ours)), is_placeholder_text(rel, _text(data))
        if ours_ph and not theirs_ph:
            actions.append(Action(rel, "upgrade", "placeholder replaced by real implementation"))
        elif theirs_ph:
            actions.append(Action(rel, "keep-ours", "incoming file is only a placeholder"))
        else:
            actions.append(Action(rel, "conflict", f"review {STAGING}/{rel}"))
    return actions


def apply(actions: List[Action], incoming: Dict[str, bytes], root: Path) -> None:
    for a in actions:
        dest = (root / a.path).resolve()
        if root.resolve() not in dest.parents:
            continue  # never write outside the project
        if a.kind in ("new", "upgrade"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(incoming[a.path])
        elif a.kind == "conflict":
            stage = (root / STAGING / a.path).resolve()
            stage.parent.mkdir(parents=True, exist_ok=True)
            stage.write_bytes(incoming[a.path])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", required=True, help="folder or .zip downloaded from another session")
    ap.add_argument("--apply", action="store_true", help="write new/upgrade files and stage conflicts (default: dry run)")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    incoming = load_incoming(Path(args.incoming))
    actions = plan(incoming, root)
    for a in actions:
        if a.kind != "identical":
            print(f"{a.kind:<10} {a.path}  {a.detail}")
    counts = {k: sum(1 for a in actions if a.kind == k) for k in ("new", "upgrade", "identical", "keep-ours", "conflict")}
    print("\n" + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if args.apply:
        apply(actions, incoming, root)
        print("Applied. Conflicts (if any) were copied to", STAGING + "/ for manual review.")
    else:
        print("Dry run only. Re-run with --apply to write changes.")
    return 1 if counts["conflict"] else 0


if __name__ == "__main__":
    sys.exit(main())
