# Phase 10 - Reports and Delivery plug-in

Self-contained package `report_delivery/` (standard library only, Python 3.11+). Independent of other phases; it accepts
their output as plain dicts, so it works with whatever has been built so far.

## Install into the main app
1. Copy `report_delivery/` next to `app.py`; optionally copy `tests/phase10/` into `tests/`.
2. Reports: `rd.build_reports(data, "reports/")`
3. Package: `rd.run({"action": "package", "root": ".", "out_zip": "delivery.zip", "run_tests": True})`
CLI: `python -m report_delivery reports input.json --out reports/` and `python -m report_delivery package <root> --out delivery.zip --run-tests --fail-on-secret`.

## Reports written (`build_reports`)
`analysis_report.html` (full), `analysis_report.json` (machine readable), `analysis_summary.md`, `dependency_report.md`,
`static_analysis_report.md`, `security_findings.md`, `build_test_results.md`, `project_inventory.md`.
Contents: inventory, dependencies, static-analysis (analyzer, version, command, exit code, stdout/stderr, result type), build/test runs,
findings (severity, evidence, remediation, source, confidence, verification status), database analysis (Phase 5 dict), environment and
tool versions (probed from PATH; missing tools shown as "not installed"), known limitations, recommended next steps.

**Honesty rules:** a section that was not supplied is shown as *not provided*/*skipped*, never filled in; an empty findings list
states it does not prove absence of issues; next steps are derived by rules from the supplied data; all text is redacted then HTML-escaped.

## Delivery package (`build_delivery_package`)
Zip `<name>-<version>/` containing the application's source, requirements, scripts, sample projects, tests, docs, reports and
evidence, plus `DELIVERY_MANIFEST.json` (checklist, SHA-256 per file, exclusions with reasons, generated files) and `SHA256SUMS.txt`.
* **Excluded:** `.env`, keys/certs, logs, history/SQLite/DB files, caches, uploads/workspace/extracted (untrusted analysed projects),
  archives (unless allow-listed), symlinks, files over 25 MB.
* **Secret scan:** text files and members of allow-listed nested ZIPs are scanned; files with secrets are left out (or `fail_on_secret=True`
  aborts). Findings name the file/line/kind, never the value. Intentional fake secrets (test fixtures) need `secret_scan_allowlist`.
* **Generated only if missing** (never overwrites yours): docs/ARCHITECTURE.md (AST scan), docs/INSTALLATION.md, docs/USER_GUIDE.md starter,
  CHANGELOG.md stub, KNOWN_ISSUES.md, scripts/run.bat, scripts/test.bat.
* **Evidence:** `run_tests=True` runs the *delivered application's own* test suite and records the real command, exit code and output
  (status passed/failed/timeout/unavailable). Without it the manifest says no evidence was recorded. Uploaded projects are never executed here.
* The checklist lists anything absent (for example `requirements`, `build_and_test_evidence`); nothing is claimed that is not in the zip.

## Known limitations
* Reports are only as complete as the data passed in. Secret scanning is pattern-based (can miss exotic secrets, can flag harmless strings).
* Architecture doc is a structural listing, not design rationale. User guide is a starter.
* No installer/EXE building (that belongs to a build step; not claimed). Packaging does not sign the zip.
* Tkinter/UI is not part of this plug-in.
