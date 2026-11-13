@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "REPO_ROOT=%cd%"
set "PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%"
set "PYTHONUTF8=1"

set "AUTO_YES=0"
set "HAS_ARGS=0"
if not "%~1"=="" set "HAS_ARGS=1"

for %%A in (%*) do (
    if /I "%%~A"=="--yes" set "AUTO_YES=1"
    if /I "%%~A"=="-y" set "AUTO_YES=1"
)

if "%AUTO_YES%"=="1" (
    python -m src.cli.main setup %*
    exit /b %ERRORLEVEL%
)

echo ============================================================================
echo  WebUI Control AI - Semi-Automated Installer (ติดตั้งระบบกึ่งอัตโนมัติ)
echo ============================================================================
echo.
python -m src.cli.main setup %*
set "EXIT_CODE=%ERRORLEVEL%"

if "%HAS_ARGS%"=="0" (
    echo.
    pause
)

exit /b %EXIT_CODE%
