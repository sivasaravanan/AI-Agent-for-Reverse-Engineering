"""Accept more than ZIP files: folders and tar archives (.tar, .tar.gz/.tgz, .tar.bz2, .tar.xz).

Everything is converted into a private temporary ZIP so the single, hardened ZIP pipeline analyses it. Names are
validated with the same rules as ZIP members; symlinks, devices, absolute/traversal names and oversized members
are skipped and reported. Nothing is executed and nothing is extracted into the project's own location.
7z/RAR are not supported (they need third-party code); the error says so plainly.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from core.archive_manager import VENDOR_DIRS, ArchiveError, is_vendored
from security.archive_validation import ArchiveLimits, check_member_name, safe_display

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
UNSUPPORTED = (".7z", ".rar")


def _scratch_dir():
    """Temporary copies go to the scratch folder chosen in Storage & clean-up (None = the system temp folder)."""
    try:
        from plugins.build_execution.storage import temp_root
        return temp_root()
    except Exception:  # noqa: BLE001 - never a reason to fail
        return None

@dataclass
class PreparedInput:
    zip_path: Path
    display_name: str
    source_kind: str                       # zip | folder | tar
    notes: List[str] = field(default_factory=list)


def _is_tar(name: str) -> bool:
    return name.lower().endswith(TAR_SUFFIXES)


def _add(zf: zipfile.ZipFile, arcname: str, data: bytes, counters: dict, limits: ArchiveLimits, notes: List[str]) -> None:
    if len(data) > limits.max_member_size:
        notes.append(f"Skipped (too large): {safe_display(arcname)}")
        return
    counters["files"] += 1
    counters["bytes"] += len(data)
    if counters["files"] > limits.max_files:
        raise ArchiveError(f"Input rejected: more than {limits.max_files:,} files.")
    if counters["bytes"] > limits.max_total_uncompressed:
        raise ArchiveError("Input rejected: total size exceeds the configured limit.")
    zf.writestr(arcname, data)


def _from_folder(folder: Path, out: Path, limits: ArchiveLimits, notes: List[str]) -> None:
    counters = {"files": 0, "bytes": 0}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                           and d not in {".git", ".hg", ".svn"}]
            for fn in filenames:
                full = Path(dirpath) / fn
                rel = full.relative_to(folder).as_posix()
                if full.is_symlink() or not full.is_file():
                    notes.append(f"Skipped (symlink/special file): {safe_display(rel)}")
                    continue
                bad = check_member_name(rel, limits)
                if bad:
                    notes.append(f"Skipped ({bad}): {safe_display(rel)}")
                    continue
                if is_vendored(rel):
                    continue                       # same policy as archives: vendored folders are not read
                try:
                    if full.stat().st_size > limits.max_member_size:
                        notes.append(f"Skipped (too large): {safe_display(rel)}")
                        continue
                    _add(zf, rel, full.read_bytes(), counters, limits, notes)
                except OSError as exc:
                    notes.append(f"Skipped (unreadable: {type(exc).__name__}): {safe_display(rel)}")
        if counters["files"] == 0:
            raise ArchiveError("The folder contains no readable project files.")


def _from_tar(tar_path: Path, out: Path, limits: ArchiveLimits, notes: List[str]) -> None:
    counters = {"files": 0, "bytes": 0}
    try:
        tf = tarfile.open(tar_path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveError(f"This is not a valid tar archive: {type(exc).__name__}")
    with tf, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, m in enumerate(tf):
            if i > limits.max_files * 2:
                raise ArchiveError("Input rejected: too many entries.")
            if not m.isfile():
                if not m.isdir():
                    notes.append(f"Skipped ({'symlink' if m.issym() or m.islnk() else 'special file'}): {safe_display(m.name)}")
                continue
            bad = check_member_name(m.name, limits)
            if bad:
                notes.append(f"Skipped ({bad}): {safe_display(m.name)}")
                continue
            if m.size > limits.max_member_size:
                notes.append(f"Skipped (too large): {safe_display(m.name)}")
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            data = fh.read(limits.max_member_size + 1)          # bounded read: decompression bombs cannot exceed this
            _add(zf, m.name.replace("\\", "/").lstrip("./") or m.name, data, counters, limits, notes)
        if counters["files"] == 0:
            raise ArchiveError("The tar archive contains no regular files.")


@contextlib.contextmanager
def prepared_input(path, limits: Optional[ArchiveLimits] = None) -> Iterator[PreparedInput]:
    """Yield a ZIP path for any supported input. Temporary files are always removed."""
    limits = limits or ArchiveLimits()
    p = Path(path)
    name = p.name
    if p.is_dir():
        kind = "folder"
    elif p.is_file() and name.lower().endswith(UNSUPPORTED):
        raise ArchiveError(f"{p.suffix.upper()[1:]} archives are not supported (they need third-party code). "
                           "Please convert it to .zip or .tar.gz and try again.")
    elif p.is_file() and _is_tar(name):
        kind = "tar"
    else:
        yield PreparedInput(p, name, "zip")
        return
    tmp = Path(tempfile.mkdtemp(prefix="ase_input_", dir=_scratch_dir()))
    try:
        out = tmp / "input.zip"
        notes: List[str] = []
        (_from_folder if kind == "folder" else _from_tar)(p, out, limits, notes)
        yield PreparedInput(out, name or str(p), kind, notes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
