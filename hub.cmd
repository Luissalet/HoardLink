@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PYTHONUTF8=1"
if exist "%~dp0.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if not errorlevel 1 goto launcher
python -P -m hoard_link.hub.cli %*
exit /b %errorlevel%

:venv
"%~dp0.venv\Scripts\python.exe" -P -m hoard_link.hub.cli %*
exit /b %errorlevel%

:launcher
py -3 -P -m hoard_link.hub.cli %*
exit /b %errorlevel%
