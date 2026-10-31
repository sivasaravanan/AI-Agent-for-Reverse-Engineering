# Merge guide - combining downloads from several sessions

Every phase owns a fixed set of files (see `PHASES.md` and `core/phase_registry.py`). Files for phases that are not
built yet are **placeholders**: they import cleanly, expose the planned public names, and fail closed
(`NotImplementedError`, or `False` for execution/consent policies).

## Rules
1. A session that builds Phase N should replace only the files listed for Phase N and add new files.
2. Keep the public names listed in each placeholder's docstring so other modules keep working.
3. Shared files (`core/analysis_orchestrator.py`, `models/analysis_result.py`, `core/report_generator.py`,
   `ui/main_window.py`) change **additively only**: add new optional fields/sections; never remove or rename.
4. A real file starts with no `PLACEHOLDER = True` line. Non-Python placeholders carry `PLACEHOLDER (Phase N)`.

## Merging a download
```bat
python scripts\merge_helper.py --incoming C:\Downloads\other_session.zip          :: dry run
python scripts\merge_helper.py --incoming C:\Downloads\other_session.zip --apply
python scripts\phase_status.py
python -m pytest -q
```
| Result | Meaning |
|---|---|
| new | file did not exist - added |
| upgrade | our placeholder replaced by their real implementation |
| keep-ours | their file is only a placeholder - ignored |
| identical | nothing to do |
| conflict | both real and different - **not touched**; their copy is staged in `merge_incoming/` for manual review |

Existing implemented files are never overwritten automatically. Cache, report and log files are ignored.
