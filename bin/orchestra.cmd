@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%"
python -m src.cli.main %*
exit /b %ERRORLEVEL%
