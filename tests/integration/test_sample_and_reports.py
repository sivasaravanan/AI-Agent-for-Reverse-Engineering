import json
import re

from core.analysis_orchestrator import analyze_archive
from core.report_generator import to_html, write_reports
from tests.fixtures.sample_project import build_sample_zip


def test_sample_project_end_to_end(tmp_path):
    z = build_sample_zip(tmp_path / "sample_project.zip")
    r = analyze_archive(z)
    langs = {l.language for l in r.languages}
    assert {"Python", "JavaScript", "C#", "Java", "Go", "PHP", "SQL", "CSS"} <= langs
    fw = {d.name: d for d in r.frameworks}
    for name in ("Flask", "Express", "React", "ASP.NET Core", "Spring Boot", "Gin", "Laravel"):
        assert name in fw, name
    db = {d.name: d for d in r.databases}
    assert db["PostgreSQL"].confidence == "strong"       # DATABASE_URL
    assert db["SQL Server"].confidence == "strong"       # appsettings connection string
    assert db["SQLite"].confidence == "moderate"         # sqlite3 import
    assert db["MongoDB"].confidence == "weak"            # mongoose declared only
    assert db["MySQL/MariaDB"].confidence == "weak"      # README mention only
    ids = {f.rule_id for f in r.findings}
    assert {"py-syntax", "todo-marker", "sec-eval-exec", "sec-sql-format", "sec-hardcoded-secret",
            "sec-credentials-in-url", "sec-debug-enabled", "sec-innerhtml"} <= ids
    broken = [f for f in r.findings if f.rule_id == "py-syntax"]
    assert len(broken) == 1 and broken[0].file == "shop/app/broken.py" and broken[0].line == 1
    assert r.archive.vendored_file_count == 1
    assert not any(f.file and "node_modules" in f.file for f in r.findings)


def test_reports_created_and_valid(tmp_path):
    r = analyze_archive(build_sample_zip(tmp_path / "s.zip"))
    paths = write_reports(r, tmp_path / "out")
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0" and data["findings"] and data["limitations"]
    html = paths["html"].read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "Content-Security-Policy" in html
    assert "Sup3rS3cret" not in html and "Fake#Pass1" not in html
    assert "Fake#Pass1" not in paths["json"].read_text(encoding="utf-8")


def test_html_report_escapes_hostile_names(make_zip, tmp_path):
    z = make_zip({"<script>alert(1)</script>.py": "def x(:\n", "ok.py": "# TODO <img src=x onerror=alert(2)>\n"})
    html = to_html(analyze_archive(z))
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_report_filename_sanitized(make_zip, tmp_path):
    z = make_zip({"a.py": "x=1\n"}, name="my proj (v1)!.zip")
    paths = write_reports(analyze_archive(z), tmp_path / "o")
    assert re.fullmatch(r"my_proj_v1_\d{8}_\d{6}\.html", paths["html"].name)
