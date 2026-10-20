@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   SkyEye - one-time setup
echo ============================================

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% --version >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo Python is not installed.
  echo Install it with:   winget install Python.Python.3.12
  echo or from https://www.python.org/downloads/  ^(check "Add python.exe to PATH"^)
  echo Then start SkyEye again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)

echo Installing packages (first time takes a few minutes, ~1.5 GB for GPU libraries)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Install failed & pause & exit /b 1)

echo Creating a "SkyEye" shortcut on your desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\SkyEye.lnk'); $s.TargetPath='%~dp0SkyEye.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.IconLocation='%SystemRoot%\System32\shell32.dll,43'; $s.Save()" >nul 2>nul

echo.
echo Setup complete. Open SkyEye from the desktop shortcut.
if not "%~1"=="auto" pause
exit /b 0
