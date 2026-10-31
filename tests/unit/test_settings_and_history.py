import json
import sqlite3

import pytest

from core.analysis_orchestrator import analyze_archive
from core.settings import Settings, load_settings, resolve_output_dir, save_settings, to_limits, validate
from database.db_manager import DbManager, HistoryError, file_sha256, from_dict
from models.analysis_result import AnalysisResult
from tests.fixtures.sample_project import build_sample_zip


# ------------------------------------------------------------------ settings
def test_defaults_file_matches_code_defaults():
    from pathlib import Path
    s, w = load_settings(Path(__file__).resolve().parents[2] / "config/settings.json")
    assert w == [] and s == Settings()


def test_validation_clamps_and_ignores_bad_values():
    s, w = validate({"max_history": 10**9, "history_enabled": "yes", "output_dir": "x\x00y",
                     "limits": {"max_files": -5, "max_member_mb": "big", "bogus": 1}, "unknown": 1})
    assert s.max_history == 5000 and s.history_enabled is True and s.output_dir == "reports"
    assert s.limits["max_files"] == 1 and s.limits["max_member_mb"] == 100
    assert len(w) == 5
    assert validate("junk")[1] and validate(None)[1] == []


def test_user_settings_override_and_roundtrip(tmp_path):
    user = tmp_path / "data" / "user_settings.json"
    s = Settings(output_dir="out", history_enabled=False, max_history=5)
    s.limits["max_files"] = 50
    save_settings(s, user)
    loaded, w = load_settings(None, user)
    assert loaded == s and w == []
    assert to_limits(loaded).max_files == 50 and to_limits(loaded).max_text_file_size == 1024 * 1024
    assert resolve_output_dir(loaded, tmp_path) == tmp_path / "out"
    assert resolve_output_dir(Settings(output_dir=str(tmp_path)), "/x") == tmp_path


def test_corrupt_settings_files_never_crash(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text("{not json")
    s, w = load_settings(bad, bad)
    assert s == Settings() and len(w) == 2
    bad.write_bytes(b"\xff\xfe\x00")
    assert load_settings(bad)[0] == Settings()


# ------------------------------------------------------------------ history database
@pytest.fixture
def two_results(tmp_path):
    z = build_sample_zip(tmp_path / "s.zip")
    r1 = analyze_archive(z)
    r2 = analyze_archive(z)
    r2.findings = [f for f in r2.findings if f.rule_id != "sec-debug-enabled"]
    return z, r1, r2


def test_save_and_load_roundtrip_is_exact(tmp_path, two_results):
    z, r1, _ = two_results
    db = DbManager(tmp_path / "h.db")
    hid = db.save_result(r1, {"html": "a.html", "json": "a.json"}, file_sha256(z))
    assert db.load_result(hid).to_dict() == r1.to_dict()
    row = db.list_history()[0]
    assert row.id == hid and row.finding_count == len(r1.findings) and row.stored and row.html_path == "a.html"
    assert row.archive_sha256 == file_sha256(z) and "Python" in row.languages


def test_old_phase1_style_result_still_loads():
    r = from_dict(AnalysisResult, {"archive_name": "x.zip", "findings": [{"rule_id": "r", "severity": "low", "category": "c", "title": "t"}]})
    assert r.structure is None and r.findings[0].title == "t"


def test_search_is_literal_not_sql_or_wildcard(tmp_path, two_results):
    _, r1, _ = two_results
    db = DbManager(tmp_path / "h.db")
    db.save_result(r1)
    assert db.list_history(search="s.zip") and not db.list_history(search="%x%") and not db.list_history(search="' OR 1=1 --")
    assert db.list_history(search="%") == [] and db.list_history(search="_.zip") == []


def test_trim_to_max_history_and_cascade(tmp_path, two_results):
    _, r1, _ = two_results
    db = DbManager(tmp_path / "h.db", max_history=3)
    ids = [db.save_result(r1) for _ in range(5)]
    assert [r.id for r in db.list_history()] == ids[:1:-1]
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(DISTINCT analysis_id) FROM findings").fetchone()[0] == 3     # cascade delete


def test_delete_clear_and_missing(tmp_path, two_results):
    _, r1, _ = two_results
    db = DbManager(tmp_path / "h.db")
    a, b = db.save_result(r1), db.save_result(r1)
    db.delete(a)
    assert [r.id for r in db.list_history()] == [b]
    with pytest.raises(HistoryError, match="no longer exists"):
        db.load(a)
    db.clear()
    assert db.list_history() == []


def test_compare_reports_resolved_and_new(tmp_path, two_results):
    _, r1, r2 = two_results
    db = DbManager(tmp_path / "h.db")
    a, b = db.save_result(r1), db.save_result(r2)
    d = db.compare(a, b)
    assert d["resolved_count"] == 1 and d["new_count"] == 0 and d["resolved"][0]["rule_id"] == "sec-debug-enabled"
    assert d["unchanged"] == len(r2.findings)
    assert db.compare(b, a)["new_count"] == 1


def test_damaged_blob_and_oversized_result(tmp_path, two_results, monkeypatch):
    _, r1, _ = two_results
    db = DbManager(tmp_path / "h.db")
    hid = db.save_result(r1)
    with sqlite3.connect(db.path) as con:
        con.execute("UPDATE analyses SET result_blob = ? WHERE id = ?", (b"garbage", hid))
    with pytest.raises(HistoryError, match="damaged"):
        db.load(hid)
    import database.db_manager as m
    monkeypatch.setattr(m, "MAX_BLOB_BYTES", 10)
    big = db.save_result(r1)
    assert not [r for r in db.list_history() if r.id == big][0].stored
    with pytest.raises(HistoryError, match="too large"):
        db.load(big)


def test_unusable_database_path_is_a_friendly_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(HistoryError):
        DbManager(blocker / "sub" / "h.db")


def test_secrets_are_not_stored_in_history(tmp_path, make_zip):
    r = analyze_archive(make_zip({"c.py": 'URL = "postgresql://u:topsecretpw@h/db"\nPASSWORD = "hunter2hunter2"\n'}))
    db = DbManager(tmp_path / "h.db")
    db.save_result(r)
    raw = (tmp_path / "h.db").read_bytes()
    assert b"topsecretpw" not in raw and b"hunter2hunter2" not in raw
    assert "topsecretpw" not in json.dumps(db.load(1)) and "hunter2hunter2" not in json.dumps(db.load(1))


def test_schema_file_is_real_and_versioned(tmp_path):
    from database.db_manager import _SCHEMA_FILE, SCHEMA_VERSION
    assert "PLACEHOLDER" not in _SCHEMA_FILE.read_text()
    db = DbManager(tmp_path / "h.db")
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
