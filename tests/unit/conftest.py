"""Collection guard for two OBSOLETE test modules.

``test_execution_integration.py`` and ``test_language_analysis_integration.py`` were written for an earlier pipeline API
(``analyze_archive(language_analysis=...)``, ``core.language_analysis``, ``security.execution_policy.plan_is_approved``, ...).
The live project replaced that API, so both modules fail at import time - and a failing import aborts the WHOLE pytest run
("Interrupted: 2 errors during collection"). They are excluded here instead of being edited or deleted; the live behaviour is
covered by test_static_analysis_plugin.py, test_build_execution_plugin.py and tests/integration/test_all_phases_wired.py.
Remove this file after porting or deleting those two modules.
"""
collect_ignore = ["test_execution_integration.py", "test_language_analysis_integration.py"]
