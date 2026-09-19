@echo off
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-dev-background.ps1"
if errorlevel 1 (
  echo.
  echo MineContext failed to start. Check config\runtime\dev-background for logs.
  pause
)
