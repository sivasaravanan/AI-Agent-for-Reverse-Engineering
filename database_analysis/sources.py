"""Safe file providers. Archive members are READ into memory, never extracted to disk,
and no project code is ever executed."""
from __future__ import annotations

import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union


class ArchiveError(Exception):
    """Raised for unusable / unsafe archives. The message is safe to show to users."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_files: int = 20_000
    max_total_bytes: int = 1_000_000_000
    max_member_bytes: int = 200_000_000
    max_ratio: float = 200.0           # per-member uncompressed/compressed
    max_archive_ratio: float = 1000.0  # whole archive (only enforced above 100 MB)
    max_path_len: int = 400


@dataclass
class FileInfo:
    path: str
    size: int


class FileProvider:
    """Interface the analyzer needs. Implement it to plug in the host app's own archive manager."""

    def __init__(self) -> None:
        self.skipped: List[Tuple[str, str]] = []

    def list_files(self) -> List[FileInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, path: str, max_bytes: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def sanitize_member_name(name: str, max_len: int = 400) -> Tuple[Optional[str], str]:
    """Return (normalised_name, "") or (None, reason). Never touches the filesystem."""
    if "\x00" in name:
        return None, "NUL byte in name"
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None, "absolute path"
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None, "path traversal"
    clean = "/".join(parts)
    if not clean:
        return None, "empty name"
    if len(clean) > max_len:
        return None, "path too long"
    return clean, ""


class ZipProvider(FileProvider):
    def __init__(self, zip_path: Union[str, os.PathLike], limits: ArchiveLimits = ArchiveLimits()):
        super().__init__()
        self.limits = limits
        path = Path(zip_path)
        if not path.is_file():
            raise ArchiveError(f"File not found: {path.name}")
        try:
            self._zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise ArchiveError("The selected file is not a valid ZIP archive (corrupt or wrong format).")
        except (OSError, NotImplementedError) as exc:
            raise ArchiveError(f"The archive could not be opened: {exc.__class__.__name__}")
        try:
            infos = self._zf.infolist()
            if len(infos) > limits.max_files:
                raise ArchiveError(f"Archive has too many entries ({len(infos)} > {limits.max_files}).")
            total = sum(i.file_size for i in infos)
            if total > limits.max_total_bytes:
                raise ArchiveError("Archive uncompressed size exceeds the safety limit.")
            arch_size = max(path.stat().st_size, 1)
            if total > 100_000_000 and total / arch_size > limits.max_archive_ratio:
                raise ArchiveError("Archive has a suspicious compression ratio (possible zip bomb).")
        except ArchiveError:
            self._zf.close()
            raise
        self._members: Dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.is_dir():
                continue
            clean, reason = sanitize_member_name(info.filename, limits.max_path_len)
            if clean is None:
                self.skipped.append((info.filename[:120], reason))
                continue
            if clean in self._members:
                self.skipped.append((clean, "duplicate entry"))
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                self.skipped.append((clean, "symbolic link"))
                continue
            if info.flag_bits & 0x1:
                self.skipped.append((clean, "encrypted member"))
                continue
            if info.file_size > limits.max_member_bytes:
                self.skipped.append((clean, "member too large"))
                continue
            if info.file_size > 1_000_000 and info.compress_size > 0 and \
                    info.file_size / info.compress_size > limits.max_ratio:
                self.skipped.append((clean, "suspicious compression ratio"))
                continue
            self._members[clean] = info

    def list_files(self) -> List[FileInfo]:
        return [FileInfo(p, i.file_size) for p, i in self._members.items()]

    def read(self, path: str, max_bytes: int) -> bytes:
        info = self._members.get(path)
        if info is None:
            return b""
        try:
            with self._zf.open(info) as fh:
                return fh.read(max_bytes)
        except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError, EOFError) as exc:
            self.skipped.append((path, f"unreadable member ({exc.__class__.__name__})"))
            return b""

    def close(self) -> None:
        try:
            self._zf.close()
        except Exception:  # noqa: BLE001 - closing must never raise into the UI
            pass


class DirectoryProvider(FileProvider):
    """For an already-extracted project folder. Symlinks are never followed."""

    def __init__(self, root: Union[str, os.PathLike], max_files: int = 20_000):
        super().__init__()
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ArchiveError(f"Folder not found: {self.root.name}")
        self._max_files = max_files

    def list_files(self) -> List[FileInfo]:
        out: List[FileInfo] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if os.path.islink(full):
                    self.skipped.append((fn, "symbolic link"))
                    continue
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                try:
                    out.append(FileInfo(rel, os.path.getsize(full)))
                except OSError:
                    continue
                if len(out) > self._max_files:
                    raise ArchiveError("Folder has too many files for analysis.")
        return out

    def read(self, path: str, max_bytes: int) -> bytes:
        full = (self.root / path).resolve()
        if self.root not in full.parents and full != self.root:
            self.skipped.append((path, "outside project root"))
            return b""
        try:
            with open(full, "rb") as fh:
                return fh.read(max_bytes)
        except OSError as exc:
            self.skipped.append((path, f"unreadable ({exc.__class__.__name__})"))
            return b""


class MemoryProvider(FileProvider):
    """In-memory files, e.g. when the host application already holds the inventory."""

    def __init__(self, files: Mapping[str, Union[str, bytes]]):
        super().__init__()
        self._files: Dict[str, bytes] = {}
        for name, content in files.items():
            clean, reason = sanitize_member_name(name)
            if clean is None:
                self.skipped.append((name[:120], reason))
                continue
            self._files[clean] = content.encode("utf-8") if isinstance(content, str) else bytes(content)

    def list_files(self) -> List[FileInfo]:
        return [FileInfo(p, len(b)) for p, b in self._files.items()]

    def read(self, path: str, max_bytes: int) -> bytes:
        return self._files.get(path, b"")[:max_bytes]
