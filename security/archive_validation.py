"""Pure validation helpers for untrusted ZIP metadata.

Nothing here extracts or executes archive content.
"""
from __future__ import annotations

import re
import stat
import zipfile
from dataclasses import dataclass
from typing import Optional

_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ArchiveLimits:
    max_files: int = 20_000
    max_total_uncompressed: int = 1024 ** 3          # 1 GiB declared total
    max_member_size: int = 100 * 1024 ** 2           # 100 MiB per member
    max_text_file_size: int = 1024 ** 2              # 1 MiB read for scanning
    max_compression_ratio: float = 200.0
    ratio_min_size: int = 1024 * 1024                # ratio checked above this size
    max_name_length: int = 512
    max_scanned_files: int = 5_000


def safe_display(name: str, limit: int = 200) -> str:
    """Make an untrusted name safe to show in a UI/report."""
    cleaned = _CONTROL.sub("?", name)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def normalize_member_name(raw: str) -> str:
    return raw.replace("\\", "/")


def check_member_name(raw: str, limits: ArchiveLimits = ArchiveLimits()) -> Optional[str]:
    """Return a reason string if the member name is unsafe, else None."""
    if "\x00" in raw:
        return "name contains a NUL byte"
    if _CONTROL.search(raw):
        return "name contains control characters"
    if len(raw) > limits.max_name_length:
        return "name is too long"
    name = normalize_member_name(raw)
    if not name.strip("/"):
        return "empty name"
    if name.startswith("/"):
        return "absolute path (or UNC path)"
    if _DRIVE.match(name):
        return "drive-letter path"
    if ".." in name.split("/"):
        return "path traversal ('..')"
    return None


def check_zipinfo(info: zipfile.ZipInfo, limits: ArchiveLimits = ArchiveLimits()) -> Optional[str]:
    """Return a reason string if the member must not be read, else None."""
    reason = check_member_name(info.filename, limits)
    if reason:
        return reason
    if info.flag_bits & 0x1:
        return "encrypted member (cannot be inspected)"
    if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
        return "symbolic link (not followed)"
    if info.is_dir():
        return None
    if info.file_size > limits.max_member_size:
        return "member exceeds maximum allowed size"
    if info.file_size > limits.ratio_min_size:
        if info.compress_size <= 0:
            return "suspicious compression (zero compressed size)"
        if info.file_size / info.compress_size > limits.max_compression_ratio:
            return "suspicious compression ratio (possible zip bomb)"
    return None
