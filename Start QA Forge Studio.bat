@echo off
rem ---------------------------------------------------------------------------
rem  QA Forge Studio - double-click to start.
rem  First start: creates a private Python environment in .venv, installs the
rem  framework and a browser (a few minutes, once). Later starts take seconds.
rem  Behind a corporate proxy / internal package mirror, IT can set PIP_INDEX_URL,
rem  HTTPS_PROXY and PLAYWRIGHT_DOWNLOAD_HOST before users run this file.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title QA Forge Studio
cd /d "%~dp0"
set "VPY=.venv\Scripts\python.exe"

echo.
echo   QA Forge Studio
echo   ---------------
echo   Keep this window open while you use Studio. Close it to stop Studio.
echo.

if exist "%VPY%" goto :deps

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo   Python 3.10 or newer is not installed on this computer.
  echo   Ask your IT team to install it, then double-click this file again.
  echo.
  pause
  exit /b 1
)
echo   First start: setting up QA Forge. This happens only once and takes a few minutes...
%PY% -m venv .venv || goto :failed

:deps
rem Install or update packages only when requirements.txt changed
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if not errorlevel 1 goto :browser
echo   Installing framework packages...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :failed
copy /y requirements.txt ".venv\requirements.installed" >nul

:browser
if exist ".venv\browser.ready" goto :start
if exist ".venv\use_edge" goto :start
echo   Installing the test browser...
"%VPY%" -m playwright install chromium
if errorlevel 1 (
  echo   Could not download the test browser. Studio will use Microsoft Edge from this computer instead.
  echo edge> ".venv\use_edge"
) else (
  echo ok> ".venv\browser.ready"
)

:start
if exist ".venv\use_edge" set "QAF__browser__channel=msedge"
echo   Starting Studio - your browser will open at http://127.0.0.1:8700
echo.
"%VPY%" -m qaforge studio --open
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo   Something went wrong (see the messages above).
echo   Send a screenshot of this window to your automation team.
echo.
pause
exit /b 1
