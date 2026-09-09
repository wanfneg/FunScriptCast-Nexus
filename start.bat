@echo off
rem ============================================================
rem  FunScriptCast-Nexus -- launcher
rem  Runs the host process with the bundled venv Python
rem  (vendor/dlna + vendor/subtitle + models are self-contained).
rem  Override with: set NEXUS_PY=<path to python.exe>
rem ============================================================
setlocal

set "APP_DIR=%~dp0"
if not defined NEXUS_PY set "NEXUS_PY=%APP_DIR%.venv\Scripts\python.exe"

if not exist "%NEXUS_PY%" (
  echo [ERROR] Python venv not found:
  echo         %NEXUS_PY%
  echo         Set NEXUS_PY to a python.exe that has pywebview installed.
  pause
  exit /b 1
)

cd /d "%APP_DIR%"
"%NEXUS_PY%" host_server.py %*