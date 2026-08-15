@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOMTRACE_ROOT=%~dp0"
set "ROOMTRACE_PYTHON=%ROOMTRACE_ROOT%processor\.venv\Scripts\python.exe"
set "ROOMTRACE_PYTHONW=%ROOMTRACE_ROOT%processor\.venv\Scripts\pythonw.exe"
set "ROOMTRACE_SETUP=%ROOMTRACE_ROOT%windows\Setup-RoomTrace.ps1"
set "ROOMTRACE_MARKER=%ROOMTRACE_ROOT%windows\.roomtrace-installed"

if not exist "%ROOMTRACE_PYTHON%" goto setup
if not exist "%ROOMTRACE_MARKER%" goto setup
"%ROOMTRACE_PYTHON%" -c "import roomtrace" >nul 2>&1
if errorlevel 1 goto setup
goto launch

:setup
echo.
echo RoomTrace first-time setup is running.
echo Python and the required packages will be installed for this folder.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOMTRACE_SETUP%"
if errorlevel 1 goto failed
if not exist "%ROOMTRACE_PYTHON%" goto failed

:launch
if not exist "%ROOMTRACE_PYTHONW%" set "ROOMTRACE_PYTHONW=%ROOMTRACE_PYTHON%"
if "%~1"=="" (
  start "" "%ROOMTRACE_PYTHONW%" -m roomtrace gui
) else (
  start "" "%ROOMTRACE_PYTHONW%" -m roomtrace gui "%~1"
)
exit /b 0

:failed
echo.
echo RoomTrace setup failed. Please read the message above and try again.
pause
exit /b 1
