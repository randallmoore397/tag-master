@echo off
title BluePeak TAG Master
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 bluepeak_tag_master.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python bluepeak_tag_master.py
    goto done
)

echo.
echo  Python 3 was not found on this computer.
echo  Install it from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" during setup), then run this file again.
echo.

:done
pause
