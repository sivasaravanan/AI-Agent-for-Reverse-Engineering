@echo off
rem Run the tests, then build the delivery package with real test evidence.
cd /d "%~dp0\.."
python -m pytest -q || (echo Tests failed - package not built. & exit /b 1)
python app.py --deliver dist --run-tests
