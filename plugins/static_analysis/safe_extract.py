"""Hardened extraction of a ZIP into a scratch directory.

External analyzers need real files on disk. Only source/manifest file types are written;
nothing is ever executed. The whole archive is rejected on traversal / bomb indicators.
"""
from __future__ import annotations

import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Only these are written to disk (everything else - binaries, scripts, images - is ignored).
EXTRACT_SUFFIXES = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".vb", ".java", ".sql",
    ".json", ".csproj", ".vbproj", ".sln", ".props", ".targets",
}


class ArchiveRejected(Exception):
    """The archive is unsafe or unreadable; message is user-friendly."""


@dataclass
class ExtractLimits:
    max_members: int = 50_000
    max_total_bytes: int = 500 * 1024 * 1024
    max_file_bytes: int = 20 * 1024 * 1024
    max_ratio: float = 200.0      # only enforced for members > 1 MB


def safe_extract(zip_path, dest, limits: ExtractLimits | None = None) -> list:
    """Extract allowed members into dest. Returns a list of human-readable skip notes."""
    limits = limits or ExtractLimits()
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    notes: list = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveRejected(f"Not a valid ZIP archive: {exc}") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_members:
            raise ArchiveRejected(f"Archive has {len(infos)} entries (limit {limits.max_members}).")
        declared_total = 0
        for info in infos:
            norm = info.filename.replace("\\", "/")
            parts = PurePosixPath(norm).parts
            if "\x00" in info.filename or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in parts:
                raise ArchiveRejected(f"Unsafe path in archive (path traversal / absolute path): {info.filename!r}")
            declared_total += info.file_size
            if info.file_size > 1_048_576 and info.compress_size > 0 and \
                    info.file_size / info.compress_size > limits.max_ratio:
                raise ArchiveRejected(f"Suspicious compression ratio for {info.filename!r}.")
        if declared_total > limits.max_total_bytes:
            raise ArchiveRejected(f"Archive expands to {declared_total} bytes (limit {limits.max_total_bytes}).")

        written_total = 0
        for info in infos:
            if info.is_dir():
                continue
            norm = info.filename.replace("\\", "/")
            parts = PurePosixPath(norm).parts
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                notes.append(f"skipped symlink: {norm}")
                continue
            if info.flag_bits & 0x1:
                notes.append(f"skipped encrypted member: {norm}")
                continue
            if PurePosixPath(norm).suffix.lower() not in EXTRACT_SUFFIXES:
                continue
            if info.file_size > limits.max_file_bytes:
                notes.append(f"skipped oversized file ({info.file_size} bytes): {norm}")
                continue
            target = dest.joinpath(*parts)
            try:
                target.resolve().relative_to(dest)
            except ValueError:
                raise ArchiveRejected(f"Unsafe path in archive: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    written_total += len(chunk)
                    if written > limits.max_file_bytes or written_total > limits.max_total_bytes:
                        out.close()
                        raise ArchiveRejected(f"Member {norm!r} expands beyond declared size (zip bomb?).")
                    out.write(chunk)
    return notes
