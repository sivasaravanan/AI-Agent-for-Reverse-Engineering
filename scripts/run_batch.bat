@echo off
rem Usage: scripts\run_batch.bat D:\submissions [education|general|enterprise|government]
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: run_batch.bat FOLDER [PROFILE] & exit /b 2)
set PROFILE=%~2
if "%PROFILE%"=="" set PROFILE=general
python app.py --batch "%~1" --profile %PROFILE% --recursive
