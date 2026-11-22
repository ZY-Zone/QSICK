@echo off
rem Compile every translation unit individually and report its COFF section sizes.
rem
rem Driving this from a batch file rather than `cmd /c "call vcvars && cl ..."` built by Python: the `&&`
rem chain loses the vcvars environment when the arguments contain spaces (the VS path has two), and the
rem failure is silent -- return code 1, no output, no .obj. Batch keeps the environment across lines.
setlocal
set "VSDIR="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VSDIR=%ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools"
if not defined VSDIR if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VSDIR=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools"
if not defined VSDIR echo [bench] no MSVC & exit /b 1
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 echo [bench] vcvars failed & exit /b 1

cd /d "%~dp0..\native\qsick-pipe"
if not exist "%~dp0..\temp\objbench" mkdir "%~dp0..\temp\objbench"

for %%F in (src\*.cpp) do (
    cl /nologo /c /MT /O2 /DNDEBUG /W3 "%%F" /Fo:"%~dp0..\temp\objbench\%%~nF.obj" >nul 2>&1
    if errorlevel 1 (
        echo [bench] %%F failed
    ) else (
        echo [bench] %%F ok
    )
)
exit /b 0
