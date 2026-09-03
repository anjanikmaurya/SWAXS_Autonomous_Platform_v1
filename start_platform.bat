@echo off
REM ===========================================================================
REM  start_platform.bat - start the SWAXS Platform Hub on Windows.
REM
REM  Use this from the Anaconda Prompt / Command Prompt, or just double-click it
REM  in File Explorer. PowerShell users can use start_platform.ps1 instead
REM  (nicer output), but this file works everywhere and needs no execution
REM  policy change.
REM
REM      start_platform.bat
REM      start_platform.bat D:\data\Auto_Run     (pre-select a project folder)
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- UTF-8, or the log's emoji and units crash the Windows console ---------
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM --- find Python: active env first, then .\venv, then PATH -----------------
set PY=
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set PY=%VIRTUAL_ENV%\Scripts\python.exe
if not defined PY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set PY=%CONDA_PREFIX%\python.exe
if not defined PY if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY where py     >nul 2>&1 && set PY=py

if not defined PY (
  echo.
  echo   x No Python found on this machine.
  echo.
  echo   Install Python 3.11+ from https://www.python.org/downloads/windows/
  echo   and TICK "Add python.exe to PATH" in the installer.
  echo   Then follow QUICKSTART.md.
  echo.
  pause
  exit /b 1
)

REM --- version gate ---------------------------------------------------------
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo.
  echo   x Your Python is older than 3.10 - too old for this platform.
  echo     Windows has no prebuilt numpy/scipy wheels for 3.9 and older, so
  echo     pip would try to compile them and fail.
  echo.
  "%PY%" -c "import sys; print('     you have: Python %%d.%%d' %% sys.version_info[:2])"
  echo.
  pause
  exit /b 1
)

REM --- are dependencies installed? -----------------------------------------
"%PY%" -c "import flask, numpy, scipy, matplotlib, pandas, yaml, fabio, pyFAI" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   x Dependencies are not installed yet.
  echo.
  echo     Run this once, then start again:
  echo       "%PY%" -m pip install -r requirements-core.txt
  echo.
  pause
  exit /b 1
)

if not "%~1"=="" (
  set SWAXS_PROJECT=%~1
  echo   Project: %SWAXS_PROJECT%
) else (
  echo   Project: pick it in the hub UI ^(top-right^)
)

if not defined SWAXS_HUB_PORT set SWAXS_HUB_PORT=5000

echo.
echo   +======================================================+
echo   ^|   SWAXS Platform Hub                                 ^|
echo   ^|   -^> http://localhost:%SWAXS_HUB_PORT%                            ^|
echo   ^|                                                      ^|
echo   ^|   Apps are started from the hub web page:             ^|
echo   ^|     5009 Calibration  5001 Reduction  5002 Vis & Avg ^|
echo   ^|     5003 Background   5006 Quality    5004 Analysis  ^|
echo   ^|     5008 Analyzer     5007 Reactor    5005 Tassone   ^|
echo   ^|                                                      ^|
echo   ^|   Press Ctrl-C to stop the hub AND its apps           ^|
echo   +======================================================+
echo.

"%PY%" hub\app.py
if errorlevel 1 pause
