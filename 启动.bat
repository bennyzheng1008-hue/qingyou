@echo off
title 轻友
cd /d "%~dp0"

echo ================================================
echo  WeChat Auto Reply - starting...
echo  (Keep this window open. Close it to stop the app)
echo ================================================
echo.

rem Prefer Python 3.13: wxauto4 only ships for Python 3.9 - 3.13
py -3.13 -c "print(1)" >nul 2>nul
if not errorlevel 1 (
    py -3.13 main.py
) else (
    python main.py
)

if errorlevel 1 (
    echo.
    echo The program exited with an error. Screenshot the message above.
    pause
)
