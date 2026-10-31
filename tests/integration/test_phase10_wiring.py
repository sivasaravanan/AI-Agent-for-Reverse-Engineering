"""Phase 10 wired into the application: report set, delivery package, CLI."""
import json
import zipfile
from pathlib import Path

import pytest

from core.analysis_orchestrator import analyze_archive
from core.delivery_packager import analysis_to_report_input, build_package, write_delivery_reports
from core.report_generator import write_reports
from tests.fixtures.sample_project import build_sample_zip

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return analyze_archive(build_sample_zip(tmp_path_factory.mktemp("s") / "sample.zip"))


def test_report_input_maps_every_phase(result):
    d = analysis_to_report_input(result)
    assert d["findings"] and d["dependencies"] and d["static_analysis"]["results"] and d["database_analysis"]["databases"]
    assert d["inventory"]["totals"]["files"] == result.archive.file_count
    assert "No approved run" in d["build_test"]["skipped_reason"]               # honest: nothing was executed
    assert all(f["verification_status"] for f in d["findings"])
    assert not any(f["source"].startswith("phase5") for f in d["findings"])      # DB findings arrive once, via database_analysis


def test_delivery_reports_written_and_secret_free(result, tmp_path):
    paths = write_delivery_reports(result, tmp_path)
    assert {"analysis_report.html", "analysis_summary.md", "security_findings.md", "build_test_results.md"} <= set(paths)
    blob = "".join(Path(p).read_text(encoding="utf-8") for p in paths.values())
    assert "Fake#Pass1" not in blob
    assert "skipped" in Path(paths["build_test_results.md"]).read_text().lower()


def test_write_reports_adds_phase10_set_without_breaking_old_keys(result, tmp_path):
    paths = write_reports(result, tmp_path)
    assert paths["html"].exists() and paths["json"].exists() and paths["summary_md"].exists()
    assert (paths["reports_dir"] / "dependency_report.md").exists()
    assert write_reports(result, tmp_path / "b", delivery=False).keys() == {"json", "html"}


def test_delivery_package_of_this_application(tmp_path):
    z = build_package(ROOT, tmp_path, version="test")
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        man = json.loads(zf.read(next(n for n in names if n.endswith("DELIVERY_MANIFEST.json"))))
        text = {n: zf.read(n) for n in names if n.endswith((".py", ".json", ".md", ".txt"))}
    assert any(n.endswith("app.py") for n in names) and any(n.endswith("SHA256SUMS.txt") for n in names)
    assert not any("/data/" in n or n.endswith((".db", ".env")) or "/reports/" in n or "__pycache__" in n for n in names)
    cl = man["checklist"]
    assert cl["source_code"]["present"] and cl["tests"]["present"] and cl["user_guide"]["present"]
    assert cl["build_and_test_evidence"]["present"] is False                       # not claimed when tests were not run
    # intentional fake keys live only in allow-listed test files; none may appear in application source
    assert not any(b"sk-test-SECRETKEY" in v for n, v in text.items() if "/tests/" not in n)


def test_package_excludes_secrets_and_history(tmp_path):
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "main.py").write_text("print(1)\n")
    (app / ".env").write_text("TOKEN=abc123456789\n")
    (app / "data" / "history.db").write_bytes(b"SQLite format 3\x00")
    (app / "cfg.py").write_text('API_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    z = build_package(app, tmp_path / "out", version="1")
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names)
    assert not any(n.endswith((".env", "history.db", "cfg.py")) for n in names)
    assert b"AKIAABCDEFGHIJKLMNOP" not in blob and b"abc123456789" not in blob


def test_cli_deliver(tmp_path, capsys):
    import app
    assert app.main(["--deliver", str(tmp_path)]) == 0
    assert "Delivery package:" in capsys.readouterr().out
    assert list(tmp_path.glob("*.zip"))
