@echo off
rem PHANTOM NETWORK v1.2.1 launcher (Windows)
rem - installed mode: data in %APPDATA%\PhantomNetwork
rem - portable mode : data in <app>\data when portable.marker exists
rem Paths with spaces or Unicode usernames are safe: every use of the
rem variables is double-quoted and built from %~dp0 (no manual reassembly).
setlocal
set "HERE=%~dp0.."
if exist "%HERE%\portable.marker" (
  set "DATADIR=%HERE%\data"
) else (
  set "DATADIR=%APPDATA%\PhantomNetwork"
)
if not exist "%DATADIR%" mkdir "%DATADIR%"

rem Prefer the installer's application venv when present; fall back to the
rem system python (pure standard library is enough to run).
set "PY=python"
if exist "%HERE%\venv\Scripts\python.exe" set "PY=%HERE%\venv\Scripts\python.exe"

rem Start the companion FIRST, then open the browser once it answers - a
rem first launch must not show a dead error page.
cd /d "%HERE%"
start "Phantom Companion" /min cmd /c ""%PY%" -m companion --data-dir "%DATADIR%""
timeout /t 2 /nobreak >nul
start "" http://localhost:8415
echo Phantom Network companion starting (data: %DATADIR%).
echo The command center opens at http://localhost:8415 - close the minimized
echo "Phantom Companion" window to stop it.
endlocal
