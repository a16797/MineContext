@echo off
cd /d "%~dp0"
start "" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" "%~dp0scripts\start-dev-background.py"
if errorlevel 1 (
  echo.
  echo MineContext failed to start. Check config\runtime\dev-background for logs.
  pause
)
