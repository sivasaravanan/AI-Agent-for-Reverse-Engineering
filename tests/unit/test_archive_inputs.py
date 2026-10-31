import zipfile

import pytest

from core.analysis_orchestrator import AnalysisCancelled, analyze_archive
from core.archive_manager import ArchiveError


def test_missing_file(tmp_path):
    with pytest.raises(ArchiveError, match="not found"):
        analyze_archive(tmp_path / "nope.zip")


def test_empty_file(tmp_path):
    p = tmp_path / "e.zip"
    p.write_bytes(b"")
    with pytest.raises(ArchiveError, match="empty"):
        analyze_archive(p)


def test_random_bytes_not_a_zip(tmp_path):
    p = tmp_path / "r.zip"
    p.write_bytes(b"this is definitely not a zip file" * 10)
    with pytest.raises(ArchiveError, match="not a valid ZIP"):
        analyze_archive(p)


def test_truncated_zip(make_zip, tmp_path):
    z = make_zip({"a.py": "x=1\n" * 100})
    data = z.read_bytes()
    bad = tmp_path / "trunc.zip"
    bad.write_bytes(data[: len(data) // 2])
    with pytest.raises(ArchiveError):
        analyze_archive(bad)


def test_empty_archive(tmp_path):
    p = tmp_path / "empty.zip"
    with zipfile.ZipFile(p, "w"):
        pass
    with pytest.raises(ArchiveError, match="empty"):
        analyze_archive(p)


def test_corrupt_member_data_is_a_warning_not_a_crash(make_zip):
    import zipfile as zf
    z = make_zip({"a.py": "x = 1\n" * 200, "b.py": "y = 2\n"}, compression=zf.ZIP_STORED)
    raw = bytearray(z.read_bytes())
    pos = raw.index(b"x = 1") + 10          # flip bytes inside a.py's stored data -> CRC mismatch
    raw[pos] ^= 0xFF
    z.write_bytes(bytes(raw))
    result = analyze_archive(z)
    assert any("a.py" in w for w in result.warnings)
    assert result.archive.scanned_text_files == 1


def test_cancel_before_start(make_zip):
    import threading
    ev = threading.Event()
    ev.set()
    with pytest.raises(AnalysisCancelled):
        analyze_archive(make_zip({"a.py": "x=1\n"}), cancel=ev)


def test_progress_callback_reaches_100(make_zip):
    seen = []
    analyze_archive(make_zip({"a.py": "x=1\n"}), progress=lambda f, m: seen.append(f))
    assert seen[-1] == 1.0 and seen == sorted(seen)
