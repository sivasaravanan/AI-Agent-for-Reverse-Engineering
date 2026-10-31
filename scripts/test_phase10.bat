@echo off
REM Run Phase 10 tests from the project root (folder containing report_delivery\)
python -m pip install pytest
python -m pytest tests\phase10 -v
