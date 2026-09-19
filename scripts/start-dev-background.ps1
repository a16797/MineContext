[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$frontendRoot = Join-Path $projectRoot 'frontend'
$runtimeRoot = Join-Path $projectRoot 'config\runtime\dev-background'
$backendHealthUrl = 'http://127.0.0.1:1733/api/health'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

function Test-BackendHealth {
  try {
    $response = Invoke-RestMethod -Uri $backendHealthUrl -TimeoutSec 3
    return $response.code -eq 0 -and $response.data.status -eq 'healthy'
  } catch {
    return $false
  }
}

function Get-FrontendProcess {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
      $_.Name -eq 'electron.exe' -and
      $_.ExecutablePath -and
      $_.ExecutablePath.StartsWith($frontendRoot, [System.StringComparison]::OrdinalIgnoreCase)
    } |
    Select-Object -First 1
}

if (-not (Test-BackendHealth)) {
  $pythonPath = (Get-Command python -ErrorAction Stop).Source
  $backendProcess = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @('-m', 'opencontext.cli', 'start', '--port', '1733', '--config', 'config/config.yaml') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtimeRoot 'backend.stdout.log') `
    -RedirectStandardError (Join-Path $runtimeRoot 'backend.stderr.log') `
    -PassThru

  $backendProcess.Id | Set-Content -LiteralPath (Join-Path $runtimeRoot 'backend.pid')

  $backendReady = $false
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if (Test-BackendHealth) {
      $backendReady = $true
      break
    }
    if ($backendProcess.HasExited) {
      break
    }
  }

  if (-not $backendReady) {
    throw "MineContext backend failed to become healthy. See $runtimeRoot"
  }
}

$frontendProcess = Get-FrontendProcess
if (-not $frontendProcess) {
  $pnpmPath = (Get-Command pnpm.cmd -ErrorAction Stop).Source
  $frontendCommand = "/d /s /c `"`"$pnpmPath`" run dev`""
  $frontendLauncher = Start-Process `
    -FilePath $env:ComSpec `
    -ArgumentList $frontendCommand `
    -WorkingDirectory $frontendRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtimeRoot 'frontend.stdout.log') `
    -RedirectStandardError (Join-Path $runtimeRoot 'frontend.stderr.log') `
    -PassThru

  $frontendLauncher.Id | Set-Content -LiteralPath (Join-Path $runtimeRoot 'frontend-launcher.pid')

  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    $frontendProcess = Get-FrontendProcess
    if ($frontendProcess) {
      break
    }
    if ($frontendLauncher.HasExited) {
      break
    }
  }

  if (-not $frontendProcess) {
    throw "MineContext frontend failed to start. See $runtimeRoot"
  }
}

$frontendProcess = Get-FrontendProcess
Write-Output 'MineContext backend: healthy on 127.0.0.1:1733'
Write-Output "MineContext frontend: running (PID $($frontendProcess.ProcessId))"
Write-Output 'Closing the main window hides it to the system tray; use the tray Quit command to stop it.'
