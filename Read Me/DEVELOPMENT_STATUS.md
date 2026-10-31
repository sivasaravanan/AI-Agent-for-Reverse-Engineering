# Development Status

**Current state:** Phases 1-10 merged into one project and wired together.

## What is wired
- P1/P2: ZIP validation, inventory, detection, structure, imports, config/env discovery (`core/*`).
- P3: `plugins/static_analysis` via `analyzers/*`; findings source `phase3:<tool>`; "Static tools" tab; report section.
- P4: `plugins/build_execution` via `services/{build_runner,test_runner,environment_service,isolated_test_runner}.py`,
  `security/execution_policy.py`, `ui/execution_dialog.py` (approval dialog + Build & Test tab). Discovery only during analysis;
  every run needs approval and a container runtime. No host fallback.
- P5: `database_analysis` via `core/database_analyzer.py`; "Databases" tab; report section. No connections are made.
- P6: `findings_plugin` via `core/finding_store.py`, `core/log_analyzer.py`; "Findings Explorer" tab fed by analysis, run logs, AI hypotheses.
- P7: `providers/*`, `security/consent_policy.py`, `core/{ai_assistant,ai_settings,source_access}.py`, `ui/ai_panel.py`. AI off by default;
  keys never stored; external sends need per-payload consent; output always unverified; AI patches go through Phase 8.
- P8: patch workflow; verification uses the Phase 4 sandbox when a runtime exists, otherwise reports NOT VERIFIED.
- P9: dashboard, filterable findings, report preview, history DB, validated settings (`core/settings.py`, `database/*`, `ui/*_view.py`).
- P10: `report_delivery` via `core/delivery_packager.py`: section reports on every analysis, `--deliver`, GUI "Delivery package...".

## Merge decisions
- Base = phase 1 zip, overlaid by 2, 8, 9 (full-tree supersets); plugins 3-6 and 10 kept as packages behind bridge modules.
- `ui/main_window.py`: three-way merge (phase 8 base) of the phase 9 version and the phase 3-7 wiring.
- Shared `config/settings.json`: phase 9 keys + `ai` section (defaults). User AI changes go to `data/ai_settings.json`; phase 9 user settings stay in `data/user_settings.json`.
- Approval dialog: phase 9 fail-closed dialog extended with an optional Phase 4 `plan` (image, network, risks).
- Registry ownership moved: `config/settings.json` -> P7, `ui/execution_dialog.py` -> P4.

## Bugs found and fixed while merging
- Secret redaction missed prefixed names (`DB_PASSWORD = "..."`).
- Tk variables were read on the worker thread (GUI analysis crashed).
- Missing/empty settings file produced zero AI providers.
- Phase 9's tab-building code landed inside an unrelated method during the merge (caught by Phase 9's own test).

## Tests (Linux, Python 3.12.3 with Tk under xvfb)
- `xvfb-run -a python3.12 -m pytest -q`: 421 passed, 1 skipped, 0 failed (also recorded as evidence inside the delivery package).
- No Tk (Python 3.13): UI tests skip.

## Known limitations
See docs/KNOWN_ISSUES.md. Main ones: not run on Windows; container isolation and AI providers tested with fakes only;
many notebook tabs.

## Next
Run on Windows, verify Docker isolation once, try a real provider.
