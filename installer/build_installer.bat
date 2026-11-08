@echo off
setlocal
cd /d "%~dp0"

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 7 not found. Install it from https://jrsoftware.org/isdl.php
    exit /b 1
)

if not exist "%~dp0..\SZAUCTRACKER.exe" (
    echo SZAUCTRACKER.exe not found. Build it first with PyInstaller.
    exit /b 1
)

"%ISCC%" "%~dp0SZAUCTRACKER.iss"
if errorlevel 1 exit /b 1
echo.
echo Installer is in dist\installer
