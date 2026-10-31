## 0.7.3 - Quick run, quick copy, automatic clear-up, storage options wired
- Quick run (Build & Test): one click runs the chosen command (or the best one found) with live output. Handled by the tool itself: a mistyped script name
  (`dupescopy.py` -> `dupescope.py`), a stopped Docker/Podman, a missing image (one question, or automatic), an oversized project, a timeout / out-of-memory
  (one retry with bigger limits), a missing Python package (re-run with requirements installed; only that network step asks for approval), a window program
  (straight to App Preview). New: `plugins/build_execution/quickrun.py`.
- Quick copy: the project is scanned first and copied once. A script run in a big project copies only the script, its imports and small data files
  (seconds instead of ~24 s and a refusal); a single chosen file runs too; "Trim oversized projects" (or Quick run) copies a trimmed project instead of refusing and lists
  what was left out. New: `plugins/build_execution/quickprep.py`; `sandbox.prepare_workspace` asks it first (the classic code is still the fallback).
- Clear-up: after every run all leftover copies (container runs AND analysis copies) and stopped `be-*` containers made by this tool are removed (setting, default on);
  "Clear project & copies" button (also forgets the selected project; your files are never touched); optional "forget the project after every run". New: `cleanup.py`.
- Storage options: new settings (largest data file, narrow-above size, trim, clear after run, forget after run, auto-pull) in Storage & clean-up; the scratch folder now also
  holds the analysis temp copies (`ase_src_`, `ase_input_`, `ase_an_`, `ase_php_`, `sa_plugin_`), not only container-run copies.
- Tests: `tests/unit/test_quick_run_and_cleanup.py` (27). Existing behaviour and tests unchanged.

## 0.7.2 - one source for everything
- Build & Test no longer has its own ZIP/folder picker: it uses the project chosen in the toolbar and discovers commands automatically when opened or when the project changes (read-only; nothing is run).

## 0.7.1 - compact icon UI
- New `ui/theme.py` (light/dark theme, compact ttk styles, tooltips, code-drawn window icon) and `ui/sidebar.py` (icon navigation in collapsible groups; replaces the 28-tab strip, tab names unchanged). Theme choice is remembered in `data/ui_prefs.json`.
- Slim icon toolbar and status bar with thin progress; tighter padding across all panels; icons on panel buttons. No behaviour changes.

## 0.7.0 - Stages B, C, D: institution features
- Stage B (education): batch grading of student ZIP/tar/folder submissions (or an LMS bulk ZIP), rubric scoring (static criteria + manual scores CSV), similarity review with starter-template exclusion, per-student reports, CSV/summary export. `--grade DIR`, "Batch grading" tab.
- Stage C (enterprise): policy gates (JSON), licence detection, CycloneDX 1.5 SBOM, SARIF 2.1.0 export, tamper-evident hash-chained audit log, trends from history, portfolio scan, CI gate (`--ci`, exit 0/1/2), `--portfolio`, `--verify-audit`, "Compliance" tab.
- Stage D (shared use): local shared-folder workspace with roles (admin/reviewer/analyst/viewer), PBKDF2 passwords with lockout, shared history, approval workflow (separation of duties, hash-bound, expiring) that can gate applying a fix, HMAC-signed reports, `scripts/team_cli.py`, "Team" tab.
- Fixes found while testing: audit log now redacts secret-looking values; lockout counter persisted correctly.

## 0.6.0 - Stage A: wider inputs and analysis
- Inputs: ZIP, .tar/.tar.gz/.tgz/.tar.bz2/.tar.xz and plain folders (via a safe temporary ZIP; links, traversal and vendored dirs skipped; temp files removed). .7z/.rar are rejected with a clear message.
- New analyzers: HTML, CSS, PHP (`php -l` + heuristics) and Go (`gofmt -e` + heuristics). Missing tools are reported as unavailable, never faked.
- Code map: functions/classes/methods and call graph (exact for Python via AST; other languages names only), complexity hotspots; new "Code map" tab and report section.
- Documentation generator (`--docs`, "Generate documentation" button): Markdown from analysis facts, unknowns marked TODO.
- 443 automated tests.

## 0.5.0 - phases 9 and 10 merged
- Dashboard, filterable findings, report preview, history database, validated settings (Phase 9).
- Phase 10 report set on every analysis; delivery packager with secret scan, SHA-256 manifest and test evidence (`--deliver`).
- Approval dialog now shows the Phase 4 plan.

## 0.4.0 - phases 3-7 merged, 8 wired
- Added/wired Phase 3 static analysis, Phase 4 sandboxed execution, Phase 5 database analysis, Phase 6 findings, Phase 7 AI providers.
- Report has sections for every phase; AI notes are labelled unverified.
- Fixed secret redaction for prefixed key names; fixed Tk access from worker thread.

# Changelog

## 0.3.0 - Phase 8: human-approved code correction
- Added: safe workspace extraction, patch proposal/diff/approve/backup/apply/revert, static before/after comparison,
  isolated-runner verification hook with baseline comparison and rollback policy, rule-based fix suggestions,
  review dialog (GUI), `scripts/fix_cli.py`, `models/patch.py`. Replaced the Phase 8 placeholder `core/patch_manager.py`
  (same public names: propose, diff, apply, revert).
- Changed (additive only): "Propose fix..." button and workspace handling in `ui/main_window.py`; version 0.3.0.
- 163 automated tests.

## 0.2.0 - Phase 2: project structure and dependencies
- Added: directory tree, entry points, config-file inventory, import/module graph (6 language families),
  circular-import detection, declared-vs-imported dependency checks, environment-variable discovery (names only),
  database connection-config discovery (redacted), confidence classes, configuration warnings, dependency summary.
- Added: GUI tabs (Structure, Entry points, Imports, Config & Env, Confidence, Config warnings), HTML report sections,
  `structure` key in JSON (schema_version unchanged; all Phase 1 keys and behavior unchanged).
- Added: placeholders for all files of phases 3-10, `core/phase_registry.py`, `scripts/phase_status.py`,
  `scripts/merge_helper.py`, `PHASES.md`, `docs/MERGE_GUIDE.md`.
- Changed (additive only): `Evidence.level` optional field; `AnalysisResult.structure` optional field; version 0.2.0.
- 110 automated tests.

## 0.1.0 - Phase 1: working ZIP analyzer
- ZIP selection, validation and safe in-memory inventory (nothing is extracted to disk).
- Limits: entry count, declared total size, per-member size, compression ratio; rejects traversal,
  absolute/drive/UNC paths, NUL/control characters, symlinks and encrypted members.
- Language detection (extensions + marker files), framework/runtime detection, database clue detection with
  strong / moderate / weak evidence levels, dependency manifest identification and light parsing.
- Python syntax check via `ast.parse` (never imports/executes), TODO/FIXME/HACK scan, heuristic security warnings.
- HTML (escaped, CSP-restricted) and JSON reports with secret redaction.
- Tkinter UI with worker thread, progress, cancel, findings grid and detail panel; `--cli` mode.
- 66 automated tests (unit, integration, security, UI smoke).
