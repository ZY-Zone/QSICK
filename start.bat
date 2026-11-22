@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title QSICK

rem start.bat          - WebUI with the synthetic QQ runtime
rem start.bat real     - attach to a running QQ.exe through the native payload
rem start.bat --check  - verify the local toolchain only

set "MODE="
if /i "%~1"=="real" set "MODE=--pipe"
if /i "%~1"=="mock" set "MODE=--mock"
if /i "%~1"=="pipe" set "MODE=--pipe"

where uv >nul 2>&1
if errorlevel 1 goto no_uv

if /i "%~1"=="--check" goto check

if not exist ".venv\Scripts\python.exe" goto first_run
goto launch

:check
echo [QSICK] uv:
uv --version
echo [QSICK] node:
where node >nul 2>&1
if errorlevel 1 goto check_no_node
node --version
goto check_deps

:check_no_node
echo   not installed - the headless client needs Node.js 18+
goto check_deps

:check_deps
echo [QSICK] python deps:
uv run python -c "import fastapi, uvicorn, jinja2; print('   ok')"
if not errorlevel 1 goto check_done
echo [QSICK] dependencies missing, running uv sync...
uv sync
:check_done
exit /b 0

:first_run
echo [QSICK] first run: installing Python dependencies...
uv sync
if errorlevel 1 goto sync_failed

:launch
if /i "%MODE%"=="--pipe" goto real_warning
goto port_check

:real_warning
echo [QSICK] real mode: start QQ first and make sure offsets.json has a verified entry.
echo [QSICK] an unverified build is rejected by the payload hook.

:port_check
powershell -NoProfile -Command "try { $r = Invoke-WebRequest 'http://127.0.0.1:5754/' -UseBasicParsing -TimeoutSec 2 } catch { exit 1 }; if ($r.Content -match 'QSICK') { exit 0 } else { exit 2 }"
if errorlevel 2 goto port_conflict
if errorlevel 1 goto headless_check
echo [QSICK] QSICK is already running on port 5754.
start "" "http://127.0.0.1:5754/"
pause
exit /b 0

:port_conflict
echo [QSICK] port 5754 is occupied by another application (SnowLuma?).
echo [QSICK] close it, or set a different WebUI port, for example:
echo   set QSICK_WEBUI_PORT=5098
echo   set QSICK_ONEBOT_PORT=3001
echo   start.bat
pause
exit /b 1

:headless_check
if exist "build\QQNT.dll" goto banner
echo [QSICK] note: build\QQNT.dll is missing; WebUI accounts -> QR login will fail.
echo [QSICK] core WebUI and OneBot still work.

:banner
echo.
echo [QSICK] starting...
echo [QSICK] WebUI:  http://127.0.0.1:5754/
echo [QSICK] OneBot: http://127.0.0.1:3000/
echo [QSICK] press Ctrl+C to stop.
echo.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:5754/'"
uv run qsick %MODE%
set "CODE=%ERRORLEVEL%"
echo.
echo [QSICK] exited with code %CODE%.
pause
exit /b %CODE%

:no_uv
echo [QSICK] uv was not found.
echo [QSICK] install it with:
echo   powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
pause
exit /b 1

:sync_failed
echo [QSICK] dependency install failed.
pause
exit /b 1
