@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 "%SCRIPT_DIR%migrate_openclaw_agent.py" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python "%SCRIPT_DIR%migrate_openclaw_agent.py" %*
  exit /b %ERRORLEVEL%
)

echo Python 3 was not found. Install Python or use the Python launcher ^(py^).
exit /b 1
