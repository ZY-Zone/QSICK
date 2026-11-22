@echo off
rem ===========================================================================
rem  Launcher: download the QQ NT installer and unpack it into .\runtime\
rem
rem  Everything (network, PE resources, 7z container, LZMA, BCJ2) is done by
rem  qqnt_get.py using only the Python standard library; this file just locates
rem  Python and runs it.
rem
rem  Extra arguments are forwarded, for example:
rem      --force          re-download even if the installer already exists
rem      --clean          wipe runtime\ before extracting
rem      --source X.exe   use a local installer, no network
rem
rem  Set QSICK_NO_PAUSE=1 to skip the trailing "press any key" (for scripts).
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title QQ NT fetch

if not exist "%~dp0qqnt_get.py" (
    echo [ERROR] qqnt_get.py is missing next to this script.
    set "RC=1"
    goto :halt
)

set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.9 or newer was not found in PATH.
    echo         Install it from https://www.python.org/downloads/windows/
    echo         and tick "Add python.exe to PATH" during setup.
    set "RC=1"
    goto :halt
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo Using Python %PYVER% via "%PY%"
echo.

%PY% "%~dp0qqnt_get.py" %*
set "RC=%ERRORLEVEL%"

:halt
echo.
if not defined RC set "RC=1"
if not "%RC%"=="0" echo [NOTE] exit code %RC%
if not defined QSICK_NO_PAUSE pause
exit /b %RC%
