@echo off
title Install Dependencies
cd /d "%~dp0"

echo ================================================
echo  Installing dependencies (wxauto4 needs Python 3.9 - 3.13)
echo  Tries Python 3.13 first, then falls back to default Python
echo ================================================
echo.

py -3.13 -c "print(1)" >nul 2>nul
if not errorlevel 1 (
    py -3.13 -m pip install requests wxauto4
) else (
    python -m pip install requests wxauto4
)

echo.
echo ================================================
echo  Done. If no error above, double-click start.bat.
echo  Note: wxauto4 only supports Python 3.9 - 3.13.
echo  If you only have Python 3.14, install 3.13 from:
echo  https://www.python.org/downloads/release/python-3130/
echo ================================================
pause
