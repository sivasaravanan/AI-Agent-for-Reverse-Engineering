# Phase map

Status values come from `python scripts/phase_status.py`.

| Phase | Title | Status | Files (owner) |
|---|---|---|---|
| 1 | Working ZIP analyzer | done | see `core/phase_registry.py` |
| 2 | Project structure and dependencies | done (this release) | `core/project_inventory.py`, `core/architecture_analyzer.py`, `core/config_discovery.py`, `core/structure_analysis.py`, `models/project_info.py` |
| 3 | Language-specific static analysis | placeholder | `analyzers/*` |
| 4 | Controlled build and test execution | placeholder | `services/{environment_service,build_runner,test_runner}.py`, `security/execution_policy.py` |
| 5 | Database analysis | placeholder | `core/database_analyzer.py` |
| 6 | Error explanation and debugging | placeholder | `core/finding_store.py`, `core/log_analyzer.py` |
| 7 | AI provider integration | placeholder | `providers/*`, `security/consent_policy.py` |
| 8 | Human-approved code correction | done (built ahead of 3-7; see limits) | `core/patch_manager.py`, `core/patch_generators.py`, `models/patch.py`, `ui/patch_dialog.py`, `scripts/fix_cli.py` |
| 9 | Desktop UI and history | placeholder | `ui/{project,findings,report,settings}_view.py`, `ui/execution_dialog.py`, `database/*`, `config/settings.json` |
| 10 | Reports and delivery | placeholder | `core/delivery_packager.py`, `scripts/build.bat`, `docs/{USER_GUIDE,ARCHITECTURE,KNOWN_ISSUES}.md` |

See `docs/MERGE_GUIDE.md` for how to combine downloads from different sessions.
