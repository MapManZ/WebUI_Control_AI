@echo off
setlocal
if defined OPENCODE_CLI_PATH if exist "%OPENCODE_CLI_PATH%" (
    "%OPENCODE_CLI_PATH%" %*
    exit /b %ERRORLEVEL%
)
if exist "%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe" (
    "%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe" %*
    exit /b %ERRORLEVEL%
)
opencode-cli %*
exit /b %ERRORLEVEL%
