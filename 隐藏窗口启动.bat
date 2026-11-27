@echo off
rem ============================================================================
rem  Hidden launcher for start.bat
rem
rem  Double-click this file. It starts start.bat in a window-less process and
rem  deletes itself. Nothing else.
rem
rem  Why PowerShell and not mshta/VBScript: the previous attempt used
rem  `mshta vbscript:createobject("wscript.shell").run(...)`, whose quote nesting
rem  broke on a path containing non-ASCII characters -- the .bat exited without
rem  starting anything and without deleting itself. PowerShell takes the path as
rem  a single-quoted literal, which survives both spaces and non-ASCII names.
rem
rem  Comments are ASCII on purpose: a .bat is read with the OEM code page, and
rem  UTF-8 text in a `rem` line shows up as mojibake.
rem ============================================================================

setlocal EnableExtensions
cd /d "%~dp0"

rem Start start.bat detached, with windowStyle Hidden (no console of its own).
rem -PassThru is omitted deliberately: the launcher does not wait, and start.bat
rem keeps running after this file is gone.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~dp0start.bat' -WindowStyle Hidden"

rem Remove this launcher. `(goto)` ends the batch context first -- a .bat cannot
rem delete the file it is currently reading from -- then del runs against a
rem closed handle.
(goto) 2>nul & del "%~f0"
