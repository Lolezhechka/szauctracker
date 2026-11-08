@echo off
chcp 65001 >nul
title SZAUCTRACKER
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 app.py %*
    goto :end
)

where python >nul 2>nul
if not errorlevel 1 (
    python app.py %*
    goto :end
)

echo.
echo Python 3.10+ not found.
echo Download: https://www.python.org/downloads/
echo Or run SZAUCTRACKER.exe - it does not need Python.
echo.
pause
:end
