@echo off
setlocal EnableExtensions

rem This script is kept ASCII-only to avoid batch file encoding issues.

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul || (
    echo Failed to enter the script directory.
    exit /b 1
)

if not exist "bm_decoder.c" (
    echo bm_decoder.c was not found in %CD%.
    popd >nul
    exit /b 1
)

set "VSDEVCMD="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"

if exist "%VSWHERE%" (
    for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find Common7\Tools\VsDevCmd.bat`) do (
        set "VSDEVCMD=%%I"
    )
)

if not defined VSDEVCMD (
    for %%I in (
        "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2019\Community\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2019\Professional\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles%\Microsoft Visual Studio\2019\Enterprise\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Community\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Professional\Common7\Tools\VsDevCmd.bat"
        "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Enterprise\Common7\Tools\VsDevCmd.bat"
    ) do (
        if not defined VSDEVCMD if exist %%~I set "VSDEVCMD=%%~I"
    )
)

if not defined VSDEVCMD (
    echo Visual Studio C++ build tools were not found.
    echo Install the Desktop development with C++ workload,
    echo or edit VSDEVCMD in this script.
    popd >nul
    exit /b 1
)

call "%VSDEVCMD%" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 (
    echo Failed to initialize the MSVC build environment.
    popd >nul
    exit /b 1
)

echo Building bm_decoder.dll with MSVC...
cl /nologo /utf-8 /LD /O2 /MT /arch:AVX2 bm_decoder.c /Febm_decoder.dll
set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" (
    echo Build failed with exit code %BUILD_EXIT%.
    popd >nul
    exit /b %BUILD_EXIT%
)

del /q bm_decoder.obj bm_decoder.exp bm_decoder.lib 2>nul

echo Build succeeded: %CD%\bm_decoder.dll
popd >nul
exit /b 0