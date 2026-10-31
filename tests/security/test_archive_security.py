import stat
import zipfile

import pytest

from core.analysis_orchestrator import analyze_archive
from core.archive_manager import ArchiveError, ArchiveReader
from security.archive_validation import ArchiveLimits, check_member_name, check_zipinfo


@pytest.mark.parametrize("name,fragment", [
    ("../evil.py", "traversal"), ("a/../../evil.py", "traversal"), ("a\\..\\..\\evil.py", "traversal"),
    ("/etc/passwd", "absolute"), ("\\\\server\\share\\x", "absolute"), ("C:\\Windows\\x.dll", "drive"),
    ("C:/x", "drive"), ("a\x00b.py", "NUL"), ("a\nb.py", "control"),
])
def test_unsafe_member_names_rejected(name, fragment):
    assert fragment in check_member_name(name)


@pytest.mark.parametrize("name", ["src/app.py", "a/b/c.txt", "dir with space/f.py", "..hidden/file", "a..b/c"])
def test_safe_member_names_allowed(name):
    assert check_member_name(name) is None


def test_symlink_and_encrypted_members_flagged():
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert "symbolic" in check_zipinfo(link)
    enc = zipfile.ZipInfo("secret.txt")
    enc.flag_bits |= 0x1
    assert "encrypted" in check_zipinfo(enc)


def test_oversize_member_flagged():
    info = zipfile.ZipInfo("big.bin")
    info.file_size, info.compress_size = 10 * 1024 ** 2, 10 * 1024 ** 2
    assert "maximum" in check_zipinfo(info, ArchiveLimits(max_member_size=1024))


def test_traversal_entries_are_skipped_not_read(make_zip):
    z = make_zip({"../evil.py": "print(1)", "ok/app.py": "x = 1\n"})
    result = analyze_archive(z)
    assert any("traversal" in s["reason"] for s in result.archive.skipped_entries)
    assert any(f.rule_id == "archive-unsafe-entry" and f.severity == "high" for f in result.findings)
    assert result.archive.scanned_text_files == 1


def test_nothing_is_extracted_to_disk(make_zip, tmp_path):
    z = make_zip({"../escape.txt": "x", "/abs.txt": "x", "a.py": "x=1\n"})
    before = {p for p in tmp_path.parent.rglob("*")}
    analyze_archive(z)
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not (tmp_path / "a.py").exists()
    assert {p for p in tmp_path.parent.rglob("*")} == before


def test_zip_bomb_ratio_member_skipped(make_zip):
    z = make_zip({"bomb.txt": "0" * (5 * 1024 * 1024), "ok.py": "x=1\n"})
    result = analyze_archive(z, ArchiveLimits(max_compression_ratio=50))
    assert any("compression" in s["reason"] for s in result.archive.skipped_entries)
    assert result.archive.scanned_text_files == 1


def test_declared_total_size_limit_rejects_archive(make_zip):
    z = make_zip({"a.txt": "x" * 5000})
    with pytest.raises(ArchiveError, match="declared uncompressed size"):
        analyze_archive(z, ArchiveLimits(max_total_uncompressed=1000))


def test_file_count_limit_rejects_archive(make_zip):
    z = make_zip({f"f{i}.txt": "x" for i in range(20)})
    with pytest.raises(ArchiveError, match="exceeds the limit"):
        analyze_archive(z, ArchiveLimits(max_files=10))


def test_oversize_text_file_not_scanned_with_warning(make_zip):
    z = make_zip({"big.py": "x = 1\n" * 1000, "small.py": "y = 2\n"})
    result = analyze_archive(z, ArchiveLimits(max_text_file_size=500))
    assert any("larger than the scan limit" in w for w in result.warnings)
    assert result.archive.scanned_text_files == 1


def test_read_refuses_skipped_entry(make_zip):
    z = make_zip({"../x.txt": "x"})
    with ArchiveReader(z) as r:
        with pytest.raises(ArchiveError, match="Refusing"):
            r.read_bytes(r.entries[0])


def test_analysis_never_executes_project_code(make_zip, tmp_path):
    marker = tmp_path / "EXECUTED.txt"
    code = f"open(r'{marker}', 'w').write('boom')\nraise SystemExit(1)\n"
    z = make_zip({"evil/__init__.py": code, "evil/setup.py": code, "conftest.py": code,
                  "run.bat": "echo hacked > hacked.txt\n"})
    result = analyze_archive(z)
    assert not marker.exists()
    assert result.archive.file_count == 4


def test_secrets_redacted_in_findings_and_reports(make_zip, tmp_path):
    from core.report_generator import to_html, to_json
    z = make_zip({"cfg.py": 'DB = "postgresql://admin:Sup3rS3cret!@h/db"\nPASSWORD = "hunter2hunter2"\n'})
    result = analyze_archive(z)
    blob = to_json(result) + to_html(result)
    assert "Sup3rS3cret" not in blob and "hunter2hunter2" not in blob
    assert "REDACTED" in blob
