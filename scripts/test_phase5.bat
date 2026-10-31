@echo off
REM Run Phase 5 tests from the project root (folder containing database_analysis\)
python -m pip install pytest
python -m pytest tests\phase5 -v
