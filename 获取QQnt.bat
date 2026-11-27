@echo off
rem ===========================================================================
rem  Launcher: fetch the QQ NT client from QSICK's own release server.
rem
rem  This used to call qqnt_get.py against GitHub. It now asks for the
rem  server source explicitly (`--server`), because the two routes differ:
rem
rem      server (this script)   a tar.gz prepared by the project, described by
rem                            descriptor.json with size + sha256, resumable
rem                            through HTTP Range. Fast and predictable.
rem      GitHub (default)      the official Tencent installer, unpacked by
rem                            qqnt_get.py in pure Python (PE + embedded 7z,
rem                            LZMA + BCJ2). Slower, and it downloads the
rem                            installer from a third-party mirror of GitHub
rem                            releases, so it is the fallback rather than the
rem                            first choice.
rem
rem  qqnt_get.py itself no longer uses gh-proxy mirrors at all: GitHub direct
rem  with a 5 s timeout, then this same server source.
rem
rem  Arguments are forwarded. Only --dir applies to the server route:
rem      --dir D:\out    unpack into another directory (default .\runtime)
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

rem `--server` first so the script never reaches for GitHub.
%PY% "%~dp0qqnt_get.py" --server %*
set "RC=%ERRORLEVEL%"

:halt
echo.
if not defined RC set "RC=1"
if not "%RC%"=="0" echo [NOTE] exit code %RC%
if not defined QSICK_NO_PAUSE pause
exit /b %RC%
