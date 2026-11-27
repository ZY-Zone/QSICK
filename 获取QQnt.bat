@echo off
rem ===========================================================================
rem  Launcher: fetch the QQ NT client from QSICK's own release server.
rem
rem  Two shapes, decided by what sits next to this file -- the same script is
rem  shipped both in a source checkout and in the build output root:
rem
rem      QSICK.exe present     build output. The exe already contains a Python,
rem                            so `QSICK.exe --fetch-runtime --server` is used.
rem                            Requiring a system Python there would be absurd.
rem      only qqnt_get.py      source checkout. Runs it with the local Python.
rem
rem  Both end up in the same place: qqnt_get.py --server, which asks
rem  runtime_fetch for the tar.gz on our release server (descriptor.json with
rem  size + sha256, resumable through HTTP Range).
rem
rem  Why the server and not GitHub: the GitHub route downloads the official
rem  Tencent installer and unpacks its embedded 7z in pure Python. It is the
rem  fallback for when our server is unreachable, not the first choice. It is
rem  still reachable from the build with:
rem      QSICK.exe --fetch-runtime            (GitHub first, then the server)
rem
rem  Arguments are forwarded; only --dir applies to the server route:
rem      --dir D:\out    unpack into another directory (default .\runtime)
rem
rem  Set QSICK_NO_PAUSE=1 to skip the trailing "press any key" (for scripts).
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title QQ NT fetch

rem A build output has the frozen exe; use it rather than requiring Python.
if exist "%~dp0QSICK.exe" goto :built

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
    echo [ERROR] Python 3.9 or newer was not found in PATH,
    echo         and no QSICK.exe sits next to this script either.
    echo         Install it from https://www.python.org/downloads/windows/
    echo         and tick "Add python.exe to PATH" during setup.
    set "RC=1"
    goto :halt
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo Using Python %PYVER% via "%PY%"
echo.

rem QSICK_NO_FETCH_CONSOLE=1: this window already exists to show this download. Without it the
rem downloader would open a *second* window for the progress and leave this one blank.
set "QSICK_NO_FETCH_CONSOLE=1"
%PY% "%~dp0qqnt_get.py" --server %*
set "RC=%ERRORLEVEL%"
goto :halt

:built
echo Using "%~dp0QSICK.exe" (frozen build, no system Python needed)
set "QSICK_NO_FETCH_CONSOLE=1"
rem The exe is windowed, so progress goes to logs\console.log and the outcome is shown in a dialog.
"%~dp0QSICK.exe" --fetch-runtime --server %*
set "RC=%ERRORLEVEL%"

:halt
echo.
if not defined RC set "RC=1"
if not "%RC%"=="0" echo [NOTE] exit code %RC%
if not defined QSICK_NO_PAUSE pause
exit /b %RC%
