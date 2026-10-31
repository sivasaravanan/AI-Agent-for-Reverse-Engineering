@echo off
rem Builds the local image used by the App Preview panel for GUI apps (Xvfb + Tk + xdotool + ImageMagick).
rem   scripts\build_preview_image.bat            -> uses docker (or podman if docker is missing)
rem   scripts\build_preview_image.bat nerdctl    -> use another docker-compatible program
cd /d "%~dp0\.."
set RT=%~1
if "%RT%"=="" (
  where docker >nul 2>nul && set RT=docker
)
if "%RT%"=="" (
  where podman >nul 2>nul && set RT=podman
)
if "%RT%"=="" (echo No docker/podman found. Pass the program name as the first argument. & exit /b 1)
echo Building ase-preview-gui:latest with %RT% (needs internet once)...
%RT% build -t ase-preview-gui:latest -f app_preview\images\Dockerfile.gui app_preview\images
if errorlevel 1 (echo Build failed. & exit /b 1)
echo Done. The App Preview panel will use this image for GUI apps.
