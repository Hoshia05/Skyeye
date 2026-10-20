@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   SkyEye - Google Drive login (one time)
echo ============================================

if not exist "bin\rclone.exe" (
  echo Downloading rclone...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile 'rclone.zip'; Expand-Archive 'rclone.zip' -DestinationPath 'rclone_tmp' -Force; New-Item -ItemType Directory -Force 'bin' | Out-Null; Get-ChildItem 'rclone_tmp' -Recurse -Filter 'rclone.exe' | Select-Object -First 1 | Copy-Item -Destination 'bin\rclone.exe'; Remove-Item 'rclone.zip'; Remove-Item 'rclone_tmp' -Recurse -Force"
  if not exist "bin\rclone.exe" (
    echo rclone download failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

bin\rclone.exe --config rclone.conf lsd gdrive: --max-depth 1 >nul 2>nul
if %errorlevel%==0 goto ok

echo.
echo A browser window will open. Log in with the Google account that has
echo the recordings folder, then click "Allow". Come back here afterwards.
echo.
if exist rclone.conf (
  bin\rclone.exe --config rclone.conf config reconnect gdrive:
) else (
  bin\rclone.exe --config rclone.conf config create gdrive drive scope=drive
)

bin\rclone.exe --config rclone.conf lsd gdrive: --max-depth 1 >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo Login did not work. Delete rclone.conf and run setup_drive.bat again.
  pause
  exit /b 1
)

:ok
echo.
echo Google Drive is connected. Use drive.bat to analyze recordings from Drive.
echo (Keep rclone.conf private - it holds your Drive login.)
pause
