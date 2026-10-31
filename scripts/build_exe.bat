@echo off
rem Build a standalone folder-style exe with PyInstaller (pip install pyinstaller first).
rem   scripts\build_exe.bat            -> console build (command-line options print their output)
rem   scripts\build_exe.bat windowed   -> no console window (normal desktop use)
cd /d "%~dp0\.."
set MODE=
if /i "%~1"=="windowed" set MODE=--windowed
python -m PyInstaller --noconfirm --onedir %MODE% --name AISoftwareEngineeringAgent --paths . ^
  --add-data "config;config" --add-data "database\schema.sql;database" ^
  --collect-submodules plugins --collect-submodules setup_doctor --collect-submodules institution --collect-submodules prerequisites app.py
if errorlevel 1 (echo Build failed. & exit /b 1)
echo Done: dist\AISoftwareEngineeringAgent\AISoftwareEngineeringAgent.exe
echo Keep the whole folder together and run it from a folder you can write to (reports and history are saved next to it).
