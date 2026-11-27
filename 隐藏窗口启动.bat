@echo off
rem ============================================================================
rem  Hidden launcher for start.bat
rem
rem  Double-click this file. It starts start.bat in a window-less process, then
rem  its own console closes immediately. The file itself stays — it is meant to
rem  be double-clicked again next time.
rem
rem  Why PowerShell and not mshta/VBScript: the first version used
rem  `mshta vbscript:createobject("wscript.shell").run(...)`, and the nested
rem  quotes broke on a path containing non-ASCII characters -- the .bat exited
rem  without starting anything, silently. PowerShell takes the path as a
rem  single-quoted literal, which survives both spaces and non-ASCII names, and
rem  reports its errors instead of hiding them.
rem
rem  Comments are ASCII on purpose: a .bat is read with the OEM code page, and
rem  UTF-8 text in a `rem` line shows up as mojibake.
rem ============================================================================

setlocal EnableExtensions
cd /d "%~dp0"

rem Start start.bat detached, with windowStyle Hidden: no console of its own.
rem -PassThru is omitted deliberately -- the launcher does not wait, so
rem start.bat keeps running after this console closes.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~dp0start.bat' -WindowStyle Hidden"

rem Nothing more to do: `endlocal` and exit close this console. The file is
rem deliberately NOT deleted -- an earlier version deleted itself, which meant
rem it was gone after one use and had to be restored from git every time.
endlocal
exit /b 0
