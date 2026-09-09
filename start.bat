@echo off
rem ============================================================
rem  FunScriptCast-Nexus —— 启动器
rem  用字幕服务所在 venv 的 Python 运行宿主进程
rem  （pywebview + torch 都在那个环境里；可用环境变量 NEXUS_PY 覆盖）
rem ============================================================
setlocal

set "APP_DIR=%~dp0"
if not defined NEXUS_PY set "NEXUS_PY=E:\Development\Subtitle Server\.venv\Scripts\python.exe"
set "PY=%NEXUS_PY%"

if not exist "%PY%" (
  echo [错误] 找不到 Python 虚拟环境：
  echo        %PY%
  echo        请确认字幕服务的 .venv 存在，或设置环境变量 NEXUS_PY 指向可用的 python.exe。
  pause
  exit /b 1
)

cd /d "%APP_DIR%"
"%PY%" host_server.py %*
