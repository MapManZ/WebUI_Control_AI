@echo off
setlocal
if defined HERMES_CLI_PATH if exist "%HERMES_CLI_PATH%" (
    "%HERMES_CLI_PATH%" %*
    exit /b %ERRORLEVEL%
)
if exist "%LOCALAPPDATA%\hermes\bin\hermes.exe" (
    "%LOCALAPPDATA%\hermes\bin\hermes.exe" %*
    exit /b %ERRORLEVEL%
)
hermes %*
exit /b %ERRORLEVEL%
