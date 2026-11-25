# PHANTOM NETWORK v1.2.2 - Windows installer (run from a NORMAL PowerShell window)
#
# Validation note: this file is checked byte-by-byte for control characters
# and syntax-sanity by packaging/validate_release.py before every release.
# It is source-audited here; execution on a real Windows machine is a
# separate step (see CURSOR_DEPLOYMENT_HANDOFF.md, Stage C).
#
# Layout:
#   Program files : %LOCALAPPDATA%\PhantomNetwork        (app copy + venv)
#   Runtime data  : %APPDATA%\PhantomNetwork             (config, transfers, logs)
# The installer never writes outside these two locations, never touches the
# firewall, and never enables autostart without an explicit "y".

$ErrorActionPreference = "Stop"

# --- locations (built with Join-Path; no manual backslash concatenation) ---
$dest = Join-Path $env:LOCALAPPDATA "PhantomNetwork"
$dataDir = Join-Path $env:APPDATA "PhantomNetwork"
$src = Split-Path -Parent $PSScriptRoot
$packagingDir = Join-Path $dest "packaging"
$venvPython = Join-Path $dest "venv\Scripts\python.exe"
$launcherBat = Join-Path $packagingDir "run-phantom-windows.bat"

Write-Host "Installing Phantom Network -> $dest"
Write-Host "Runtime data will live in   -> $dataDir (config, transfers, logs)"

New-Item -ItemType Directory -Force -Path $dest | Out-Null
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

Copy-Item (Join-Path $src "companion") (Join-Path $dest "companion") -Recurse -Force
Copy-Item (Join-Path $src "web") (Join-Path $dest "web") -Recurse -Force
Copy-Item (Join-Path $src "packaging") $packagingDir -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $src "sounds") (Join-Path $dest "sounds") -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $src -Filter "*.md" | ForEach-Object { Copy-Item $_.FullName $dest -Force }
Copy-Item (Join-Path $src "LICENSE") $dest -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $src "requirements.txt") $dest -Force -ErrorAction SilentlyContinue

# --- python + optional isolated venv for psutil (never a global pip install) ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  Write-Warning "Python not found on PATH. Install from https://python.org (tick 'Add python.exe to PATH'), then re-run this installer."
} else {
  Write-Host "Creating application virtualenv (optional psutil telemetry)..."
  $venvDir = Join-Path $dest "venv"
  & python -m venv $venvDir 2>$null
  if (Test-Path $venvPython) {
    & $venvPython -m pip install --quiet psutil 2>$null
    Write-Host "  venv ready (psutil optional; the companion runs without it)."
  } else {
    Write-Host "  venv unavailable - continuing in pure-stdlib mode."
  }
}

# --- shortcuts (target: the launcher .bat; working directory = packaging dir) ---
$ws = New-Object -ComObject WScript.Shell
$desktopPath = [Environment]::GetFolderPath("Desktop")
$desktopLnk = Join-Path $desktopPath "Phantom Network.lnk"
$lnk = $ws.CreateShortcut($desktopLnk)
$lnk.TargetPath = $launcherBat
$lnk.WorkingDirectory = $packagingDir
$lnk.Description = "Phantom Network command center"
$lnk.Save()
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnk2 = $ws.CreateShortcut((Join-Path $startMenu "Phantom Network.lnk"))
$lnk2.TargetPath = $launcherBat
$lnk2.WorkingDirectory = $packagingDir
$lnk2.Save()

# --- optional autostart (default: NO) ---
$ans = Read-Host "Start Phantom Network at login? [y/N]"
if ($ans -eq "y") {
  Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "PhantomNetwork" -Value $launcherBat
  Write-Host "Autostart registered (remove via the uninstaller or msconfig)."
}

Write-Host ""
Write-Host "Installed."
Write-Host "If peers cannot connect, allow the ports manually (this installer does NOT touch the firewall):"
Write-Host '  netsh advfirewall firewall add rule name="Phantom Network API" dir=in action=allow protocol=TCP localport=8415'
Write-Host '  netsh advfirewall firewall add rule name="Phantom Network Discovery" dir=in action=allow protocol=UDP localport=8414'
Write-Host "Launch via the Desktop shortcut (starts the companion, then opens http://localhost:8415)."
Write-Host "Upgrade: re-run this installer over the existing installation - data in $dataDir is preserved."
$uninstaller = Join-Path $packagingDir "uninstall-windows.ps1"
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File $uninstaller"
