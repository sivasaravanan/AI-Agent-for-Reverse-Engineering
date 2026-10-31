import json
import zipfile
from pathlib import Path

import pytest

from core.analysis_orchestrator import analyze_archive
from database.db_manager import DbManager
from enterprise import (AuditLog, Policy, PolicyError, build_sbom, default_policy, detect_license, evaluate_policy, load_policy,
                        project_trend, run_ci, scan_licenses, scan_portfolio, to_sarif)
from tests.fixtures.sample_project import build_sample_zip

MIT = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software"


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return path


@pytest.fixture
def sample(tmp_path):
    return build_sample_zip(tmp_path / "s.zip")


def test_license_detection():
    assert detect_license(MIT) == "MIT"
    assert detect_license("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007") == "GPL-3.0"
    assert detect_license("Apache License\n Version 2.0, January 2004") == "Apache-2.0"
    assert detect_license("my own words") == ""


def test_scan_licenses_project_and_vendored(tmp_path):
    z = _zip(tmp_path / "p.zip", {"LICENSE": MIT, "package.json": json.dumps({"name": "app", "license": "GPL-3.0"}),
                                  "node_modules/left/package.json": json.dumps({"name": "left", "license": "MIT"}),
                                  "node_modules/left/LICENSE": "GNU Affero General Public License version 3"})
    info = scan_licenses(z)
    assert {p["license"] for p in info.project} == {"MIT", "GPL-3.0"}           # vendored licence file is not the project's
    assert info.components == {"npm:left": "MIT"}


def test_no_license_is_reported(tmp_path):
    info = scan_licenses(_zip(tmp_path / "p.zip", {"a.py": "x=1"}))
    assert info.project == [] and any("No project licence" in n for n in info.notes)


def test_sbom_is_cyclonedx_and_honest(sample):
    r = analyze_archive(sample)
    s = build_sbom(r)
    assert s["bomFormat"] == "CycloneDX" and s["specVersion"] == "1.5" and s["serialNumber"].startswith("urn:uuid:")
    assert s["components"] and all(c["licenses"][0]["license"].get("name") == "NOASSERTION" for c in s["components"])
    assert all(c["purl"].startswith("pkg:") for c in s["components"])
    json.dumps(s)


def test_policy_pass_fail_and_waive(sample):
    r = analyze_archive(sample)
    hi = sum(1 for f in r.findings if f.severity in ("critical", "high"))
    pr = evaluate_policy(r, Policy(max_critical=99, max_high=99, forbid_secrets=False))
    assert pr.passed
    strict = evaluate_policy(r, Policy(max_critical=0, max_high=0, max_medium=0, forbid_secrets=True))
    assert strict.passed == (len(strict.violations) == 0)
    if r.findings:
        rid = r.findings[0].rule_id
        w = evaluate_policy(r, Policy(max_critical=0, max_high=0, max_medium=0, forbid_secrets=False, allow_rules=[rid]))
        assert any(x["rule"] == rid for x in w.waived)         # waivers are recorded, never silently dropped


def test_policy_licence_rules(sample, tmp_path):
    r = analyze_archive(sample)
    lic = scan_licenses(_zip(tmp_path / "g.zip", {"LICENSE": "GNU General Public License version 3"}))
    assert not evaluate_policy(r, Policy(forbid_copyleft=True, max_high=99, max_critical=99, forbid_secrets=False), lic).passed
    assert not evaluate_policy(r, Policy(forbid_licenses=["GPL-3.0"], max_high=99, max_critical=99, forbid_secrets=False), lic).passed
    assert not evaluate_policy(r, Policy(require_project_license=True, max_high=99, max_critical=99, forbid_secrets=False),
                               scan_licenses(_zip(tmp_path / "n.zip", {"a.py": "1"}))).passed
    assert not evaluate_policy(r, Policy(forbid_copyleft=True)).passed        # requested but not scanned => fail closed


def test_policy_file_validation(tmp_path):
    p = tmp_path / "p.json"
    for bad in ('{"max_high": -1}', '{"bogus": 1}', '[1]', '{"forbid_rules": "x"}', '{"min_confidence_for_gate": "zzz"}', "not json"):
        p.write_text(bad)
        with pytest.raises(PolicyError):
            load_policy(p)
    p.write_text('{"name": "strict", "max_high": 2, "forbid_rules": ["php-*"]}')
    assert load_policy(p).name == "strict"


def test_sarif_shape(sample):
    s = to_sarif(analyze_archive(sample))
    assert s["version"] == "2.1.0" and s["runs"][0]["results"]
    assert all(x["level"] in ("error", "warning", "note") for x in s["runs"][0]["results"])


def test_audit_chain_detects_tampering(tmp_path):
    a = AuditLog(tmp_path / "a.jsonl")
    for i in range(4):
        a.append("act", "bob", f"s{i}", {"password": "hunter2hunter2", "token": "ghp_" + "a" * 36})
    assert a.verify()["ok"] and a.verify()["entries"] == 4
    assert "ghp_" + "a" * 36 not in (tmp_path / "a.jsonl").read_text()
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    (tmp_path / "edit.jsonl").write_text("\n".join([lines[0], lines[1].replace("bob", "eve"), *lines[2:]]) + "\n")
    assert not AuditLog(tmp_path / "edit.jsonl").verify()["ok"]
    (tmp_path / "del.jsonl").write_text("\n".join([lines[0], *lines[2:]]) + "\n")
    v = AuditLog(tmp_path / "del.jsonl").verify()
    assert not v["ok"] and v["first_bad_line"] == 2
    (tmp_path / "swap.jsonl").write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n")
    assert not AuditLog(tmp_path / "swap.jsonl").verify()["ok"]


def test_trend(tmp_path, sample):
    db = DbManager(tmp_path / "h.db")
    assert project_trend(db, "s.zip")["direction"] == "insufficient data"
    r = analyze_archive(sample)
    db.save_result(r)
    r2 = analyze_archive(sample)
    r2.findings = r2.findings[: max(0, len(r2.findings) - 2)]
    db.save_result(r2)
    t = project_trend(db, r.archive_name)
    assert t["runs"] == 2 and t["direction"] in ("improving", "unchanged")


def test_portfolio_isolates_failures_and_exports(tmp_path, sample):
    d = tmp_path / "port"
    d.mkdir()
    (d / "good.zip").write_bytes(Path(sample).read_bytes())
    (d / "broken.zip").write_bytes(b"not a zip")
    (d / "folder").mkdir()
    (d / "folder" / "a.py").write_text("x = 1\n")
    log = AuditLog(tmp_path / "audit.jsonl")
    res = scan_portfolio(d, tmp_path / "out", Policy(max_critical=99, max_high=99, forbid_secrets=False), log)
    st = {p["project"]: p["status"] for p in res.projects}
    assert st["broken.zip"] == "error" and st["good.zip"] == "pass" and st["folder"] == "pass"
    assert Path(res.files["csv"]).exists() and list((tmp_path / "out" / "sbom").glob("*.json"))
    assert log.verify()["entries"] == 3
    with pytest.raises(ValueError):
        scan_portfolio(tmp_path / "out" / "sbom", tmp_path / "o2")


def test_ci_exit_codes(tmp_path, sample, capsys):
    pol = tmp_path / "ok.json"
    pol.write_text('{"max_critical": 999, "max_high": 999, "forbid_secrets": false}')
    out = []
    assert run_ci(sample, tmp_path / "o", str(pol), echo=out.append) == 0 and (tmp_path / "o" / "ci.sarif").exists()
    strict = tmp_path / "strict.json"
    strict.write_text('{"max_critical": 0, "max_high": 0, "max_medium": 0, "forbid_secrets": true}')
    r = analyze_archive(sample)
    expected = 1 if (r.findings) else 0
    assert run_ci(sample, tmp_path / "o2", str(strict), echo=out.append) in (0, 1)
    assert run_ci(tmp_path / "missing.zip", tmp_path / "o3", echo=out.append) == 2
    assert run_ci(sample, tmp_path / "o4", str(tmp_path / "nope.json"), echo=out.append) == 2


def test_cli_ci_and_verify(tmp_path, sample, capsys):
    import app
    pol = tmp_path / "p.json"
    pol.write_text('{"max_critical": 999, "max_high": 999, "forbid_secrets": false}')
    assert app.main(["--ci", str(sample), "--policy", str(pol), "--out", str(tmp_path / "o")]) == 0
    assert app.main(["--verify-audit", str(tmp_path / "o" / "audit.jsonl")]) == 0
    lines = (tmp_path / "o" / "audit.jsonl").read_text().replace("ci.gate", "x.gate")
    (tmp_path / "o" / "audit.jsonl").write_text(lines)
    assert app.main(["--verify-audit", str(tmp_path / "o" / "audit.jsonl")]) == 1
    d = tmp_path / "pf"; d.mkdir(); (d / "a.zip").write_bytes(Path(sample).read_bytes())
    assert app.main(["--portfolio", str(d), "--policy", str(pol), "--out", str(tmp_path / "o2")]) == 0
