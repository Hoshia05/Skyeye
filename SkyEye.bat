@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo First start: installing SkyEye. This takes a few minutes...
  call setup.bat auto
  if not exist ".venv\Scripts\pythonw.exe" (
    pause
    exit /b 1
  )
)
start "" ".venv\Scripts\pythonw.exe" app.py
