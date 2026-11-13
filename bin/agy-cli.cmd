@echo off
setlocal
if defined AGY_CLI_PATH if exist "%AGY_CLI_PATH%" (
    "%AGY_CLI_PATH%" %*
    exit /b %ERRORLEVEL%
)
if exist "%LOCALAPPDATA%\agy\bin\agy.exe" (
    "%LOCALAPPDATA%\agy\bin\agy.exe" %*
    exit /b %ERRORLEVEL%
)
agy %*
exit /b %ERRORLEVEL%
