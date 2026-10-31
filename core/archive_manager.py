"""Safe, read-only access to ZIP archives.

Members are never extracted to disk. They are read into memory with hard size
caps, and only after metadata validation.
"""
from __future__ import annotations

import posixpath
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from security.archive_validation import (
    ArchiveLimits, check_zipinfo, normalize_member_name,
)

VENDOR_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "venv", ".venv", "__pycache__",
    "site-packages", "bin", "obj", "dist", "build", "vendor", ".idea", ".vs",
    "packages", "target", ".gradle", ".tox", ".mypy_cache", ".pytest_cache",
}

TEXT_EXTENSIONS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".cs", ".vb",
    ".java", ".sql", ".html", ".htm", ".css", ".scss", ".php", ".go", ".rb",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".kt", ".sh", ".ps1", ".bat", ".cmd",
    ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".config", ".env", ".properties", ".txt", ".md", ".rst", ".csproj",
    ".vbproj", ".sln", ".gradle", ".kts", ".gitignore", ".example",
}
TEXT_NAMES = {"dockerfile", "makefile", "pipfile", "go.mod", "gemfile", "procfile"}
NO_CONTENT_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "poetry.lock",
    "pipfile.lock", "composer.lock",
}
ARCHIVE_EXTENSIONS = (".zip", ".jar", ".war", ".7z", ".rar", ".tar", ".tar.gz", ".tgz")


class ArchiveError(Exception):
    """Raised with a user-presentable message."""


@dataclass
class ArchiveEntry:
    name: str                      # normalized with '/' separators
    raw_name: str
    size: int
    compressed_size: int
    is_dir: bool
    skip_reason: Optional[str] = None
    vendored: bool = False
    info: Optional[zipfile.ZipInfo] = field(default=None, repr=False, compare=False)

    @property
    def extension(self) -> str:
        return posixpath.splitext(self.name)[1].lower()

    @property
    def basename(self) -> str:
        return posixpath.basename(self.name)


def is_vendored(name: str) -> bool:
    parts = [p.lower() for p in name.split("/")[:-1]]
    return any(p in VENDOR_DIRS for p in parts)


def is_text_candidate(name: str) -> bool:
    base = posixpath.basename(name).lower()
    if base in NO_CONTENT_NAMES or base.endswith(".lock"):
        return False
    if base in TEXT_NAMES or base.startswith(".env"):
        return True
    return posixpath.splitext(base)[1] in TEXT_EXTENSIONS


class ArchiveReader:
    """Context manager exposing validated entries and bounded reads."""

    def __init__(self, path: str | Path, limits: ArchiveLimits | None = None):
        self.path = Path(path)
        self.limits = limits or ArchiveLimits()
        self.entries: List[ArchiveEntry] = []
        self.warnings: List[str] = []
        self._zf: Optional[zipfile.ZipFile] = None

    def __enter__(self) -> "ArchiveReader":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._zf is not None:
            self._zf.close()
            self._zf = None

    def open(self) -> None:
        p = self.path
        if not p.is_file():
            raise ArchiveError(f"File not found: {p.name}")
        try:
            if p.stat().st_size == 0:
                raise ArchiveError("The selected file is empty (0 bytes).")
            self._zf = zipfile.ZipFile(p)
        except ArchiveError:
            raise
        except zipfile.BadZipFile:
            raise ArchiveError("This is not a valid ZIP archive (it may be corrupt or a different file type).")
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.LargeZipFile) as exc:
            raise ArchiveError(f"The archive could not be opened: {exc}")
        infos = self._zf.infolist()
        if not infos:
            raise ArchiveError("The ZIP archive is empty.")
        if len(infos) > self.limits.max_files:
            raise ArchiveError(
                f"Archive rejected: {len(infos):,} entries exceeds the limit of {self.limits.max_files:,}."
            )
        declared = sum(i.file_size for i in infos)
        if declared > self.limits.max_total_uncompressed:
            raise ArchiveError(
                f"Archive rejected: declared uncompressed size {declared:,} bytes exceeds the limit "
                f"of {self.limits.max_total_uncompressed:,} bytes."
            )
        seen = set()
        for info in infos:
            name = normalize_member_name(info.filename)
            is_dir = info.is_dir() or name.endswith("/")
            reason = check_zipinfo(info, self.limits)
            if name in seen and not is_dir:
                self.warnings.append("Duplicate entry names were found; only the first is analyzed.")
                reason = reason or "duplicate entry name"
            seen.add(name)
            self.entries.append(ArchiveEntry(
                name=name.rstrip("/") if is_dir else name,
                raw_name=info.filename, size=info.file_size,
                compressed_size=info.compress_size, is_dir=is_dir,
                skip_reason=reason, vendored=is_vendored(name), info=info,
            ))
        self.warnings = sorted(set(self.warnings))

    def read_bytes(self, entry: ArchiveEntry, max_bytes: Optional[int] = None) -> bytes:
        """Read a validated member into memory (never to disk)."""
        if self._zf is None:
            raise ArchiveError("Archive is not open.")
        if entry.skip_reason:
            raise ArchiveError(f"Refusing to read '{entry.name}': {entry.skip_reason}")
        cap = max_bytes if max_bytes is not None else self.limits.max_text_file_size
        try:
            with self._zf.open(entry.info) as fh:
                data = fh.read(cap + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError, OSError) as exc:
            raise ArchiveError(f"Could not read '{entry.name}': {exc}")
        if len(data) > cap:
            raise ArchiveError(f"'{entry.name}' is larger than the {cap:,}-byte read limit.")
        return data

    def read_text(self, entry: ArchiveEntry) -> Optional[str]:
        """Return decoded text, or None if the member looks binary."""
        data = self.read_bytes(entry)
        return decode_text(data)


def decode_text(data: bytes) -> Optional[str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:4096]:
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")
