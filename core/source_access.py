"""Read-only access to selected files of an archive (for Phase 7 AI requests). Nothing is extracted to disk."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from core.archive_manager import ArchiveError, ArchiveReader, is_text_candidate
from security.archive_validation import ArchiveLimits


def _scratch_dir():
    """Temporary copies go to the scratch folder chosen in Storage & clean-up (None = the system temp folder)."""
    try:
        from plugins.build_execution.storage import temp_root
        return temp_root()
    except Exception:  # noqa: BLE001 - never a reason to fail
        return None
class ZipSourceReader:
    def __init__(self, zip_path, limits: Optional[ArchiveLimits] = None):
        self.limits = limits or ArchiveLimits()
        self._index: Optional[Dict[str, int]] = None
        self._tmp = None
        src = Path(zip_path)
        if src.is_dir() or not src.name.lower().endswith(".zip"):      # folder / tar: one safe temp ZIP, cleaned at exit
            import atexit, contextlib, shutil
            from core.input_adapter import prepared_input
            cm = prepared_input(src, self.limits)
            try:
                prep = cm.__enter__()
                import tempfile
                self._tmp = Path(tempfile.mkdtemp(prefix="ase_src_", dir=_scratch_dir()))
                shutil.copyfile(prep.zip_path, self._tmp / "src.zip")
                cm.__exit__(None, None, None)
                atexit.register(shutil.rmtree, self._tmp, ignore_errors=True)
                src = self._tmp / "src.zip"
            except ArchiveError:
                cm.__exit__(None, None, None)
                src = Path("missing.zip")
        self.zip_path = str(src)

    def list_text_files(self) -> List[str]:
        with ArchiveReader(self.zip_path, self.limits) as r:
            return sorted(e.name for e in r.entries if not e.is_dir and not e.skip_reason and not e.vendored
                          and is_text_candidate(e.name) and e.size <= self.limits.max_text_file_size)

    def read(self, name: str) -> Optional[str]:
        """Text of one safe archive member, or None (unsafe, binary, too large or missing)."""
        try:
            with ArchiveReader(self.zip_path, self.limits) as r:
                for e in r.entries:
                    if e.name == name and not e.is_dir and not e.skip_reason and e.size <= self.limits.max_text_file_size:
                        return r.read_text(e)
        except ArchiveError:
            return None
        return None
