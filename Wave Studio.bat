@echo off
rem Wave Studio - double-click to open the Wave Script / Weave Script window
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw wave_studio.py
    exit /b
)
python wave_studio.py
if errorlevel 1 pause
