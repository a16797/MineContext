[CmdletBinding()]
param()

$ErrorActionPreference = 'SilentlyContinue'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = Join-Path $projectRoot 'config\runtime\dev-background'

Write-Host "Stopping MineContext..." -ForegroundColor Cyan

# 1. Kill frontend Electron & dev servers
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @('electron.exe', 'esbuild.exe') -and $_.ExecutablePath -like "*MineContext*") -or
    ($_.CommandLine -like "*MineContext*electron-vite*" -or $_.CommandLine -like "*MineContext*pnpm*")
} | ForEach-Object {
    Write-Host "Stopping frontend process $($_.ProcessId) ($($_.Name))" -ForegroundColor Gray
    Stop-Process -Id $_.ProcessId -Force
}

# 2. Kill backend python process
$backendPidFile = Join-Path $runtimeRoot 'backend.pid'
if (Test-Path $backendPidFile) {
    $backendPid = Get-Content $backendPidFile -Raw
    if ($backendPid -match '^\d+$') {
        $p = Get-Process -Id ([int]$backendPid) -ErrorAction SilentlyContinue
        if ($p -and $p.Path -like "*python*") {
            Write-Host "Stopping backend process $backendPid" -ForegroundColor Gray
            Stop-Process -Id ([int]$backendPid) -Force
        }
    }
}

# Fallback: kill opencontext backend if still running
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like "*opencontext.cli*start*"
} | ForEach-Object {
    Write-Host "Stopping opencontext backend process $($_.ProcessId)" -ForegroundColor Gray
    Stop-Process -Id $_.ProcessId -Force
}

# 3. Clean up PID files
Remove-Item (Join-Path $runtimeRoot '*.pid') -Force -ErrorAction SilentlyContinue

Write-Host "MineContext stopped successfully." -ForegroundColor Green
