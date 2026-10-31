# Master prompt audit - Phases 1-4 (v0.4.0, merged)

Checked against the "MASTER CLAUDE CODE PROMPT" sections 3, 6 (Phases 1-3), 7 and 8. "Evidence" names the code and the
automated test that exercises it. Anything not verified is listed under **Gaps and unverified items**.

## Phase 1 - working ZIP analyzer
| Requirement | Status | Evidence |
|---|---|---|
| ZIP file selection | done | `ui/main_window.py select_zip`; `app.py --cli` |
| ZIP validation (traversal, absolute, bomb ratio, counts, sizes, symlink, encrypted) | done | `security/archive_validation.py`; `tests/security/test_archive_security.py` |
| Safe inventory, nothing extracted to disk | done | `core/archive_manager.py`; `test_nothing_is_extracted_to_disk` |
| File count / size reporting | done | `ArchiveSummary`; `tests/integration/test_sample_and_reports.py` |
| Language detection (extensions + markers) | done | `core/language_detector.py`; `tests/unit/test_detectors.py` |
| Framework / runtime identification | done | `core/framework_detector.py`; `test_detectors.py` |
| Database clue detection (evidence levels) | done | `core/database_detector.py`; `test_detectors.py` |
| Dependency manifest identification + parsing | done | `core/dependency_analyzer.py` |
| Python syntax check (ast, never executed) | done | `core/static_analysis.py`; `test_analysis_never_executes_project_code` |
| TODO/FIXME/HACK, heuristic security warnings | done | `core/static_analysis.py`; `tests/unit/test_static_analysis.py` |
| HTML + JSON reports, redaction, escaping | done | `core/report_generator.py`; `test_html_report_escapes_hostile_names`, `test_secrets_redacted_in_findings_and_reports` |
| Friendly UI status/errors, UI stays responsive | done | `tests/unit/test_ui_smoke.py`; manual run: 145 UI ticks processed while the sample analysis ran |
| Sample project + tests incl. malformed ZIP and traversal | done | `sample_project.zip`, `tests/unit/test_archive_inputs.py` |
Acceptance 1-8: all met in the Linux sandbox (see Verification below). Not run on Windows.

## Phase 2 - structure and dependencies
| Requirement | Status | Evidence |
|---|---|---|
| Directory and file tree | done | `core/project_inventory.py` |
| Entry-point identification | done | `core/structure_analysis.py`; `test_structure_phase2.py` |
| Manifest discovery | done | Phase 1 manifests + `config_discovery.py` |
| Imports, references, module relationships, cycles | done | `core/architecture_analyzer.py` |
| Dependency names + version constraints with file/line | done | `Dependency(name, spec, file, line)` |
| Configuration-file inventory | done | `core/config_discovery.py` |
| Database connection configuration discovery (redacted) | done | `core/config_discovery.py` |
| Environment-variable references (names only) | done | `core/config_discovery.py` |
| Confidence indicators: confirmed / strong / weak / unknown | done | `core/structure_analysis.py` |
| Missing / ambiguous configuration warnings | done | `core/structure_analysis.py` |
| Positive and negative tests | done | `tests/unit/test_structure_phase2.py` |
| Function / class mapping (objective 8) | **added in this merge** | `core/symbol_map.py` (Python exact via ast; other languages regex, labelled); `tests/unit/test_language_analysis_integration.py` |

## Phase 3 - language-specific static analysis
| Requirement | Status | Evidence |
|---|---|---|
| Python: AST, Ruff, optional Pyright | done | `plugins/static_analysis/python_adapter.py`; tests ran against real ruff 0.17 and pyright |
| JavaScript/TypeScript: ESLint, tsc | done | `js_adapter.py`; ran against real eslint 9 and tsc |
| C#/VB.NET: dotnet build, approval required, no package restore | done (parser tests only) | `other_adapters.py`; dotnet not installed here |
| Java: javac | done (parser tests only) | `other_adapters.py`; javac not installed here |
| SQL: sqlfluff, dialect option | done | `other_adapters.py`; ran against real sqlfluff 4.4 |
| Check tool availability first; never install | done | `unavailable` tool runs; `test_missing_tool_is_reported_not_faked` |
| Report analyzer name+version, command, exit code, stdout/stderr, file+line, kind | done | `ToolRun`, `Finding.kind`; HTML/JSON `language_analysis` |
| Heuristic never labelled compiler error | done | `test_convert_finding_never_upgrades_certainty` |
| Extensible adapters | done | `register_adapter`, `analyzers/*` |
| Wired into the main pipeline, reports and UI | done in this merge | `core/language_analysis.py`, orchestrator, `report_generator.py`, `ui/main_window.py` |

## Phase 4 - controlled build and test execution
| Requirement | Status | Evidence |
|---|---|---|
| Never run an uploaded project on the host | done | no host fallback: `sandbox.run_plan` -> `skipped`; `test_run_refused_without_matching_approval_and_skipped_without_runtime` |
| Explicit user approval before execution; user can review the command | done | `Approval.plan_hash` bound to command/env/image/limits/network/source; Build & Test tab shows the exact plan |
| Isolated disposable container | done, **isolation unverified** | `--rm`, scratch copy, `--read-only`, `--cap-drop ALL`, non-root; fake-runtime tests only |
| No host filesystem beyond approved copy; no credentials | done (command construction verified) | `container_command`, `prepare_workspace`, sanitized env |
| Network disabled by default | done (command construction verified) | `--network none`; network candidates flagged and shown in the plan |
| CPU / memory / process / time limits, output capture, exit codes | done (fake-runtime tests) | `Limits`, `ExecutionResult` |
| Timeout enforced, failure does not crash the app | done | `tests/unit/test_build_execution_plugin.py` |
| Python / Node / .NET / Java build and test commands | discovery done | `plugins/build_execution/discovery.py`; Maven/Gradle/.NET never run against real toolchains |
| Discover from manifests, never auto-run install hooks/scripts | done | lifecycle hooks listed as risks; `npm ci --ignore-scripts`; `.sh/.bat/Makefile` never offered |
| Unsafe execution refused or skipped safely | done | statuses `refused` / `skipped` |
| Static analysis still works when isolation is unavailable | done | pipeline reports isolation status and continues |

## Sections 7-8 (security, testing policy)
- Untrusted ZIP handling, no project code executed, secrets redacted (plugin tool output is now also redacted before it reaches reports): met.
- External tools: no shell, no stdin, timeout, cancellable, credential-like env vars stripped.
- `dotnet build` can run project build logic: never approved automatically; real isolation is Phase 4.

## Gaps and unverified items
- `javac` and `dotnet` paths verified only with parser unit tests on sample output; no real JDK or .NET SDK was available.
- Not run on Windows; GUI layout not inspected visually (it was run and populated under Xvfb).
- Regex symbol mapping for non-Python languages can miss or misreport declarations.
- No cross-tool de-duplication beyond the Python syntax error; Ruff config of the project is ignored by design.
- **Phase 4 isolation is not proven**: no Docker/Podman in the build sandbox. Run `python -m plugins.build_execution check`, then a harmless test run, and confirm `--network none` blocks access.
- Test-result parsing and baseline comparison are Phase 8; Phase 4 returns raw output.
- Phases 5-10 are placeholders by design.

## Verification (Linux sandbox, Python 3.12.3)
`xvfb-run -a python -m pytest -q` with ruff, sqlfluff, eslint, tsc and pyright installed: 206 passed, 1 skipped (javac not installed).
