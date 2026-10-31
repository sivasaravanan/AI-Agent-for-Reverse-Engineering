# Architecture

Core logic is GUI-independent; Tkinter code lives in `ui/`.

| Layer | Modules |
|---|---|
| Phase 1-2 analysis | `core/archive_manager, language_detector, framework_detector, database_detector, dependency_analyzer, static_analysis, project_inventory, architecture_analyzer, config_discovery, structure_analysis, analysis_orchestrator`, `security/archive_validation, secret_scanner` |
| Phase 3 static analysis | `plugins/static_analysis` (adapters) <- `analyzers/*` bridge |
| Phase 4 execution | `plugins/build_execution` (discovery, plan, container sandbox) <- `services/{build_runner,test_runner,environment_service,isolated_test_runner}`, `security/execution_policy`, `ui/execution_dialog` |
| Phase 5 database | `database_analysis` <- `core/database_analyzer` |
| Phase 6 findings | `findings_plugin` <- `core/{finding_store,log_analyzer}` |
| Phase 7 AI | `providers/*` (Ollama, OpenAI-compatible, OpenRouter, router), `security/consent_policy`, `core/{ai_assistant,ai_settings,source_access}`, `ui/ai_panel` |
| Phase 8 fixes | `core/{patch_manager,patch_generators}`, `models/patch`, `ui/patch_dialog`, `scripts/fix_cli.py` |
| Phase 9 UI/history | `ui/{main_window,project_view,findings_view,report_view,settings_view,history_view}`, `core/{settings,report_preview}`, `database/db_manager` |
| Phase 10 reports | `report_delivery` <- `core/delivery_packager`, `core/report_generator` |

Data flow: ZIP -> `ArchiveReader` (in-memory, validated) -> detectors/structure -> optional phases 5/3/4(discovery) -> `AnalysisResult` ->
reports (HTML/JSON + Phase 10 set) -> history DB. Phases 7 and 8 act on the result on request: AI suggestions are consent-gated and
unverified; patches need explicit approval and are verified in the sandbox when available.

Rules enforced in code: no project code runs during analysis; execution needs approval + container; secrets are redacted in reports, logs,
AI payloads and packages; ambiguous evidence is labelled (confirmed/strong/weak); AI output is never marked verified.
Plugins stay self-contained (stdlib only) and are reached only through the bridge modules; `core/phase_registry.py` records file ownership.
