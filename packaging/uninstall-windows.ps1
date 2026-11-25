# PHANTOM NETWORK v1.2.1 - Windows uninstaller (normal PowerShell window)
#
# Removes ONLY Phantom Network's own program files, shortcuts, autostart
# entry and (with -RemoveData) its data directory. Transferred files inside
# directories the user shared from OTHER locations are never touched.
#
#   default       : remove app + shortcuts + autostart, KEEP runtime data
#   -RemoveData   : also delete %APPDATA%\PhantomNetwork (config/transfers)

param([switch]$RemoveData)
$ErrorActionPreference = "Continue"

$appDir = Join-Path $env:LOCALAPPDATA "PhantomNetwork"
$dataDir = Join-Path $env:APPDATA "PhantomNetwork"
$packagingDir = Join-Path $appDir "packaging"

# 1. autostart registration
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
  -Name "PhantomNetwork" -ErrorAction SilentlyContinue

# 2. shortcuts
$desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Phantom Network.lnk"
$startMenuLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Phantom Network.lnk"
Remove-Item $desktopLnk, $startMenuLnk -ErrorAction SilentlyContinue

# 3. stop a companion started by this installation, then remove the app dir
Get-Process python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like (Join-Path $appDir "*") } | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item $appDir -Recurse -Force -ErrorAction SilentlyContinue

# 4. runtime data: kept unless explicitly removed
if ($RemoveData) {
  Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "Runtime data removed ($dataDir)."
} else {
  Write-Host "Runtime data KEPT in $dataDir (config, transfer history, logs)."
  Write-Host "Delete it too with:  powershell -ExecutionPolicy Bypass -File <uninstaller> -RemoveData"
}

Write-Host "Phantom Network removed."
