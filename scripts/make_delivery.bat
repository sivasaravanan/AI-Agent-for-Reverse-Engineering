@echo off
REM Usage: scripts\make_delivery.bat path\to\app_folder delivery.zip
python -m report_delivery package %1 --out %2 --run-tests --fail-on-secret
