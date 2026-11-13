@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "REPO_ROOT=%cd%"
set "PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%"
set "PYTHONUTF8=1"

set "OPEN_BROWSER=1"
set "EXTRA_ARGS="

for %%A in (%*) do (
    if /I "%%~A"=="--no-browser" (
        set "OPEN_BROWSER=0"
    ) else (
        set "EXTRA_ARGS=!EXTRA_ARGS! %%A"
    )
)

echo ============================================================================
echo  WebUI Control AI - Starting Local Dashboard (http://127.0.0.1:8765)
echo ============================================================================

if "%OPEN_BROWSER%"=="1" (
    start "" cmd /c "ping -n 2 127.0.0.1 >nul && start http://127.0.0.1:8765"
)

python -m src.cli.main dashboard --port 8765 %EXTRA_ARGS%
exit /b %ERRORLEVEL%
