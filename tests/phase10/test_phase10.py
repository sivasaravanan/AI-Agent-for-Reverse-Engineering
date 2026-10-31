import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import report_delivery as rd

SECRET = "SuperSecret123"


def sample_input():
    return {
        "project": {"name": "shop", "archive": "shop.zip"},
        "inventory": {"totals": {"files": 3, "bytes": 1200}, "files": [{"path": "a.py", "size": 100, "language": "Python"}]},
        "dependencies": [{"name": "psycopg2-binary", "version": "==2.9", "ecosystem": "pip", "file": "requirements.txt", "line": 1}],
        "static_analysis": {"results": [{"analyzer": "ruff", "version": "0.5.0", "command": "ruff check .", "exit_code": 1,
                                         "kind": "lint_warning", "stdout": f"a.py:1 password={SECRET}", "stderr": ""}]},
        "build_test": {"runs": [{"command": "pytest -q", "status": "failed", "exit_code": 1, "duration_s": 2.5,
                                 "stdout": "1 failed", "stderr": ""}]},
        "findings": [
            {"id": "F1", "severity": "high", "category": "security", "title": "Hard-coded credential", "file": "a.py", "line": 3,
             "evidence": f"DSN='postgres://u:{SECRET}@h/db'", "explanation": "x", "remediation": "rotate",
             "source": "heuristic", "confidence": "high", "verification_status": "static-detected"},
            {"id": "F2", "severity": "low", "title": "<script>alert(1)</script>", "file": "b.py", "line": 1},
        ],
        "database_analysis": {"databases": [{"db": "postgresql", "display": "PostgreSQL", "status": "confirmed", "rationale": "connection x1"}],
                              "connections": [{"display": "PostgreSQL", "level": "confirmed", "file": "a.py", "line": 3,
                                               "redacted": "postgres://u:****@h/db"}],
                              "findings_phase6": [{"id": "DBA-0001", "severity": "high", "title": "DB cred", "file": "a.py", "line": 3,
                                                   "rule": "DB-SEC-001", "verification_status": "static-detected"}]},
        "limitations": ["Static analysis only."],
    }


# ------------------------------------------------------------------ reports
def test_all_reports_created_and_secret_free(tmp_path):
    paths = rd.build_reports(sample_input(), tmp_path, probe_tools=False)
    assert set(paths) >= {"analysis_report.html", "analysis_report.json", "analysis_summary.md", "dependency_report.md",
                          "static_analysis_report.md", "security_findings.md", "build_test_results.md"}
    for p in paths.values():
        assert SECRET not in Path(p).read_text(encoding="utf-8"), p
    data = json.loads(Path(paths["analysis_report.json"]).read_text(encoding="utf-8"))
    assert data["summary"]["findings_total"] == 3          # 2 supplied + 1 merged from database_analysis
    assert data["summary"]["findings_by_severity"]["high"] == 2


def test_html_escapes_and_has_required_sections(tmp_path):
    html = Path(rd.build_reports(sample_input(), tmp_path, probe_tools=False)["analysis_report.html"]).read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html
    for h in ("Project inventory", "Dependency report", "Static analysis report", "Build and test results",
              "Security and quality findings", "Database analysis", "Environment and tool versions",
              "Known limitations", "Recommended next steps"):
        assert h in html
    assert "ruff check ." in html and "0.5.0" in html     # analyzer name/version/command preserved


def test_missing_sections_are_declared_not_fabricated(tmp_path):
    paths = rd.build_reports({"project": {"name": "x"}}, tmp_path, probe_tools=False)
    html = Path(paths["analysis_report.html"]).read_text(encoding="utf-8")
    assert "not provided" in html
    assert "does not prove the absence" in html
    data = json.loads(Path(paths["analysis_report.json"]).read_text(encoding="utf-8"))
    assert data["summary"]["sections"]["build_test"] == "not-provided"
    steps = " ".join(data["next_steps"])
    assert "No build/test results were provided" in steps
    assert "tests passed" not in html.lower()


def test_next_steps_traceable():
    n = rd.normalize_input(sample_input())
    steps = rd.recommend_next_steps(n)
    assert any("high severity" in s for s in steps)
    assert any("did not succeed" in s for s in steps)
    assert any("Rotate" in s for s in steps)


def test_skipped_build_is_reported_as_skipped():
    n = rd.normalize_input({"build_test": {"skipped_reason": "no isolation environment available"}})
    assert rd.build_summary(n)["sections"]["build_test"] == "skipped"
    assert any("skipped" in s for s in rd.recommend_next_steps(n))


def test_environment_reports_missing_tools_honestly(monkeypatch):
    import report_delivery.env_info as ei
    monkeypatch.setattr(ei.shutil, "which", lambda t: None)
    env = rd.collect_environment(tools=["dotnet", "git"])
    assert env["tools"]["dotnet"] == {"installed": False, "version": None}
    assert env["python"]


def test_redaction_and_secret_finder():
    assert SECRET not in rd.redact_text(f"password={SECRET}")
    assert "ghp_" not in rd.redact_text("t=ghp_" + "a" * 36)
    kinds = [k for k, _ in rd.find_secrets("x=1\nAKIA" + "A" * 16 + "\n")]
    assert "AWS access key id" in kinds
    assert rd.find_secrets("password=${DB_PASS}\npassword=\n") == []


# ------------------------------------------------------------------ evidence
def test_test_evidence_records_real_run(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_a():\n    assert 1 == 1\n")
    ev = rd.collect_test_evidence(tmp_path, timeout=120)
    assert ev["status"] == "passed" and ev["exit_code"] == 0 and "1 passed" in ev["stdout"]
    (tmp_path / "test_bad.py").write_text("def test_b():\n    assert False\n")
    ev2 = rd.collect_test_evidence(tmp_path, timeout=120)
    assert ev2["status"] == "failed" and ev2["exit_code"] != 0


def test_evidence_timeout_enforced(tmp_path):
    ev = rd.collect_test_evidence(tmp_path, command=[sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert ev["status"] == "timeout"


# ------------------------------------------------------------------ packaging
def make_project(root: Path):
    (root / "app.py").write_text('"""Main entry."""\nprint("hi")\n')
    (root / "requirements.txt").write_text("requests>=2\n")
    (root / "README.md").write_text("# Demo\nRun it.\n")
    (root / "core").mkdir()
    (root / "core" / "engine.py").write_text('"""Engine module."""\nclass Engine:\n    pass\ndef run():\n    pass\n')
    (root / "tests").mkdir()
    (root / "tests" / "test_engine.py").write_text("def test_x():\n    assert True\n")
    (root / "sample_projects").mkdir()
    (root / "sample_projects" / "demo.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    # things that must NOT ship
    (root / ".env").write_text(f"API_KEY={SECRET}\n")
    (root / "config").mkdir()
    (root / "config" / "settings.json").write_text('{"api_key": "%s"}' % SECRET)
    (root / "history.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    (root / "uploads").mkdir()
    (root / "uploads" / "customer_project.py").write_text("print('untrusted')")
    (root / "logs").mkdir()
    (root / "logs" / "app.log").write_text("log")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    (root / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n")


def test_package_contents_and_exclusions(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    make_project(proj)
    res = rd.build_delivery_package(proj, tmp_path / "out" / "d.zip", rd.PackageOptions(version="2.0.0", allow_archives=["sample_projects/*.zip"]))
    with zipfile.ZipFile(res["zip"]) as zf:
        names = zf.namelist()
        text = "\n".join(zf.read(n).decode("utf-8", "replace") for n in names)
    pre = "proj-2.0.0/"
    for must in ("app.py", "core/engine.py", "requirements.txt", "tests/test_engine.py", "sample_projects/demo.zip",
                 "DELIVERY_MANIFEST.json", "SHA256SUMS.txt", "docs/ARCHITECTURE.md", "docs/INSTALLATION.md",
                 "docs/USER_GUIDE.md", "CHANGELOG.md", "KNOWN_ISSUES.md", "scripts/run.bat", "scripts/test.bat"):
        assert pre + must in names, must
    for bad in (".env", "history.db", "uploads/customer_project.py", "logs/app.log", "key.pem", "config/settings.json"):
        assert pre + bad not in names, bad
    assert SECRET not in text
    reasons = {e["path"]: e["reason"] for e in res["excluded"]}
    assert "credential" in reasons[".env"] and "secret detected" in reasons["config/settings.json"]
    assert res["secrets_excluded"] == 1
    assert "build_and_test_evidence" in res["missing"]      # honest: no evidence supplied


def test_manifest_checksums_match(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    make_project(proj)
    res = rd.build_delivery_package(proj, tmp_path / "d.zip")
    import hashlib
    with zipfile.ZipFile(res["zip"]) as zf:
        man = json.loads(zf.read("p-1.0.0/DELIVERY_MANIFEST.json"))
        for f in man["files"]:
            assert hashlib.sha256(zf.read("p-1.0.0/" + f["path"])).hexdigest() == f["sha256"]
        assert "p-1.0.0/SHA256SUMS.txt" in zf.namelist()
        assert any("untrusted" in n.lower() for n in man["notes"])


def test_existing_docs_not_overwritten_and_evidence_included(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    make_project(proj)
    (proj / "CHANGELOG.md").write_text("# real history\n")
    (proj / "docs").mkdir()
    (proj / "docs" / "ARCHITECTURE.md").write_text("hand written")
    ev = {"command": "pytest", "status": "passed", "exit_code": 0, "duration_s": 1, "python": "3.12", "stdout": "3 passed", "stderr": ""}
    res = rd.build_delivery_package(proj, tmp_path / "d.zip", rd.PackageOptions(evidence=ev, known_issues=["Tk untested"]))
    with zipfile.ZipFile(res["zip"]) as zf:
        assert zf.read("p-1.0.0/CHANGELOG.md") == b"# real history\n"
        assert zf.read("p-1.0.0/docs/ARCHITECTURE.md") == b"hand written"
        assert "3 passed" in zf.read("p-1.0.0/evidence/build_test_evidence.txt").decode()
        assert "Tk untested" in zf.read("p-1.0.0/KNOWN_ISSUES.md").decode()
    assert "build_and_test_evidence" not in res["missing"]
    assert "docs/ARCHITECTURE.md" not in res["generated"] and "CHANGELOG.md" not in res["generated"]


def test_fail_on_secret_raises_without_leaking(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text(f"password = '{SECRET}'\n")
    with pytest.raises(rd.PackagingError) as ei:
        rd.build_delivery_package(proj, tmp_path / "d.zip", rd.PackageOptions(fail_on_secret=True))
    assert SECRET not in str(ei.value)
    assert not (tmp_path / "d.zip").exists()


def test_allowlist_keeps_intentional_fixture_secrets(tmp_path):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "app.py").write_text("x=1\n")
    (proj / "tests" / "fixtures.py").write_text(f"SECRET='{SECRET}'\npassword = '{SECRET}'\n")
    res = rd.build_delivery_package(proj, tmp_path / "d.zip", rd.PackageOptions(secret_scan_allowlist=["tests/fixtures.py"]))
    assert res["secrets_excluded"] == 0


def test_symlinks_and_bad_root(tmp_path):
    with pytest.raises(rd.PackagingError):
        rd.build_delivery_package(tmp_path / "nope", tmp_path / "d.zip")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("x=1")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    try:
        (proj / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    res = rd.build_delivery_package(proj, tmp_path / "d.zip")
    assert any(e["reason"] == "symbolic link" for e in res["excluded"])
    with zipfile.ZipFile(res["zip"]) as zf:
        assert not any("link.txt" in n for n in zf.namelist())


def test_empty_project_refused(tmp_path):
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / ".env").write_text("A=b")
    res = None
    with pytest.raises(rd.PackagingError):
        rd.build_delivery_package(tmp_path / "e", tmp_path / "d.zip", rd.PackageOptions(generate_missing=False))


def test_architecture_doc_handles_syntax_errors(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "ok.py").write_text('"""Fine."""\nclass A: pass\n')
    (proj / "bad.py").write_text("def (:\n")
    res = rd.build_delivery_package(proj, tmp_path / "d.zip")
    with zipfile.ZipFile(res["zip"]) as zf:
        arch = zf.read("p-1.0.0/docs/ARCHITECTURE.md").decode()
    assert "Fine." in arch and "syntax error" in arch


# ------------------------------------------------------------------ plug-in surface
def test_run_all_and_register(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    make_project(proj)
    out = rd.run({"action": "all", "input": sample_input(), "out_dir": str(tmp_path / "rep"), "probe_tools": False,
                  "root": str(proj), "out_zip": str(tmp_path / "d.zip"), "run_tests": True, "test_timeout": 120})
    assert out["test_evidence"]["status"] == "passed"
    with zipfile.ZipFile(out["package"]["zip"]) as zf:
        assert "p-1.0.0/reports/analysis_report.html" in zf.namelist()
        assert "p-1.0.0/evidence/build_test_evidence.txt" in zf.namelist()
        assert SECRET not in "".join(zf.read(n).decode("utf-8", "replace") for n in zf.namelist())
    d = {}
    rd.register(d)
    assert d["report_delivery"] is rd.run
    with pytest.raises(ValueError):
        rd.run({"action": "bogus"})
    with pytest.raises(TypeError):
        rd.register(object())


def test_cli(tmp_path):
    root = Path(__file__).resolve().parents[2]
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps(sample_input()))
    p = subprocess.run([sys.executable, "-m", "report_delivery", "reports", str(inp), "--out", str(tmp_path / "r"), "--no-probe"],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "r" / "analysis_report.html").exists()
    p = subprocess.run([sys.executable, "-m", "report_delivery", "package", str(tmp_path / "missing"), "--out", str(tmp_path / "x.zip")],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 2 and "not found" in p.stderr


def test_secret_scanner_ignores_code_expressions_but_catches_literals():
    code = 'x = connect(password=p["password"], pwd=cfg.pwd)\npassword: Optional[str] = None\nself.password = password\n'
    assert rd.find_secrets(code, "a.py") == []
    assert rd.find_secrets(f"password = '{SECRET}'\n", "a.py")
    assert rd.find_secrets(f"DB_PASSWORD={SECRET}\n", ".env.production")
    assert rd.find_secrets(f"db_password={SECRET}\n", "settings.ini")
    assert rd.find_secrets(f"password={SECRET}\n", "script.py") == []      # bare value in code = expression, not literal
    assert rd.find_secrets("postgres://u:****@h/db\npassword=****\n", "report.md") == []


def test_nested_archive_is_secret_scanned(tmp_path):
    proj = tmp_path / "p"
    (proj / "sample_projects").mkdir(parents=True)
    (proj / "app.py").write_text("x=1\n")
    with zipfile.ZipFile(proj / "sample_projects" / "s.zip", "w") as zf:
        zf.writestr("conf/.env", f"DB_PASSWORD={SECRET}\n")
    res = rd.build_delivery_package(proj, tmp_path / "d.zip", rd.PackageOptions(allow_archives=["sample_projects/*.zip"]))
    assert res["secrets_excluded"] == 1
    res2 = rd.build_delivery_package(proj, tmp_path / "d2.zip", rd.PackageOptions(allow_archives=["sample_projects/*.zip"],
                                                                                 secret_scan_allowlist=["sample_projects/*.zip"]))
    assert res2["secrets_excluded"] == 0
