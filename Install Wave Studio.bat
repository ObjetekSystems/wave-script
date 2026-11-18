@echo off
rem One-time setup for Wave Studio, Wave Script and Weave Script. Double-click to run.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo.
    echo 1. Install Python 3.10 or newer from https://www.python.org/downloads/
    echo 2. In the installer, tick "Add python.exe to PATH" at the bottom of the first screen.
    echo 3. Then double-click this file again.
    pause
    exit /b
)
python --version
echo.
echo Installing the libraries Wave Script needs (a few minutes)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Something went wrong. Copy the last lines above into an issue on the GitHub page.
    pause
    exit /b
)
python -c "import tkinter" 2>nul
if errorlevel 1 (
    echo.
    echo Python is missing its window toolkit (tcl/tk^), which Wave Studio needs.
    echo Run the Python installer again, choose Modify, tick "tcl/tk and IDLE", and finish.
    pause
    exit /b
)
echo.
echo All set. Double-click "Wave Studio.bat" to start.
pause
