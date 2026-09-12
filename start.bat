@echo off
rem ============================================================
rem  FunScriptCast-Nexus -- launcher
rem
rem  Runs host_server.py with a venv Python.
rem  The script may sit either in the project root or in a packaged
rem  folder (dist-app), so it locates host_server.py itself instead of
rem  assuming "the script's folder is the project".
rem
rem  Override the interpreter with: set NEXUS_PY=<path to python.exe>
rem ============================================================
setlocal

set "SCRIPT_DIR=%~dp0"

rem ---- locate host_server.py: prefer this folder, then its parent ----
set "APP_DIR="
if exist "%SCRIPT_DIR%host_server.py" (
  set "APP_DIR=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%..\host_server.py" (
  set "APP_DIR=%SCRIPT_DIR%..\"
)

if not defined APP_DIR (
  echo [ERROR] host_server.py not found. Looked in:
  echo         %SCRIPT_DIR%
  echo         %SCRIPT_DIR%..\
  echo.
  echo         This folder holds only the packaged .exe; run that instead,
  echo         or place this script in the project root.
  pause
  exit /b 1
)

rem ---- locate the interpreter: NEXUS_PY, then .venv here, then parent ----
if not defined NEXUS_PY (
  if exist "%APP_DIR%.venv\Scripts\python.exe" (
    set "NEXUS_PY=%APP_DIR%.venv\Scripts\python.exe"
  ) else if exist "%APP_DIR%..\.venv\Scripts\python.exe" (
    set "NEXUS_PY=%APP_DIR%..\.venv\Scripts\python.exe"
    echo [INFO] No .venv next to host_server.py; using the parent one:
    echo        %APP_DIR%..\.venv
  )
)

if not defined NEXUS_PY (
  echo [ERROR] Python venv not found. Tried:
  echo         %APP_DIR%.venv\Scripts\python.exe
  echo         %APP_DIR%..\.venv\Scripts\python.exe
  echo         Set NEXUS_PY to a python.exe that has pywebview installed.
  pause
  exit /b 1
)

if not exist "%NEXUS_PY%" (
  echo [ERROR] Python not found at NEXUS_PY:
  echo         %NEXUS_PY%
  pause
  exit /b 1
)

cd /d "%APP_DIR%"
"%NEXUS_PY%" host_server.py %*
