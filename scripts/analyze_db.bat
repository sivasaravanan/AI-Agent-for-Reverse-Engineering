@echo off
REM Usage: scripts\analyze_db.bat path\to\project.zip
python -m database_analysis %1 --json reports\db_report.json --html reports\db_report.html --md reports\db_report.md
