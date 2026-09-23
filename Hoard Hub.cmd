@echo off
title Hoard Hub
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (where py >nul 2>nul && set "PYEXE=py -3")
if not defined PYEXE (where python >nul 2>nul && set "PYEXE=python")
if not defined PYEXE (
  echo   Python 3.11+ was not found. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)

%PYEXE% -c "import httpx, psutil" >nul 2>nul || (
  echo   Installing the two dependencies (httpx, psutil^)...
  %PYEXE% -m pip install --quiet httpx psutil
)

%PYEXE% -m hoard_link.hub %*
