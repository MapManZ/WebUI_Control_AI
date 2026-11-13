@echo off
setlocal
set "DSH_DIR="
if defined DSH_HOME if exist "%DSH_HOME%\DeepSeek Harness.exe" set "DSH_DIR=%DSH_HOME%"
if not defined DSH_DIR if exist "%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe" set "DSH_DIR=%LOCALAPPDATA%\Programs\DeepSeek Harness"
if not defined DSH_DIR if exist "%PROGRAMFILES%\DeepSeek Harness\DeepSeek Harness.exe" set "DSH_DIR=%PROGRAMFILES%\DeepSeek Harness"
if not defined DSH_DIR if exist "O:\DSHarness\DeepSeek Harness\DeepSeek Harness.exe" set "DSH_DIR=O:\DSHarness\DeepSeek Harness"
if not defined DSH_DIR (
    dsh %*
    exit /b %ERRORLEVEL%
)
set ELECTRON_RUN_AS_NODE=1
"%DSH_DIR%\DeepSeek Harness.exe" -e "const path=require('path'),url=require('url');process.argv=['node','dsh',...process.argv.slice(1)];process.pkg={};const binPath=path.join(path.dirname(process.execPath),'resources','app.asar','dsh','node_modules','@deepseek-ai','dsh','lib','bin.js');import(url.pathToFileURL(binPath).href).then(m=>m.runCli())" -- %*
exit /b %ERRORLEVEL%
