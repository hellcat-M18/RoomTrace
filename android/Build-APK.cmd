@echo off
setlocal EnableExtensions
chcp 65001 >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build-APK.ps1"
if errorlevel 1 (
  echo.
  echo Android APK build failed. Read the message above.
  pause
  exit /b 1
)

echo.
echo APK created. You can copy it to the phone and tap it to install.
pause
