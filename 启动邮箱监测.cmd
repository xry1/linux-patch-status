@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATCH_MONITOR_PYTHON=python"
where python >nul 2>&1
if errorlevel 1 if exist "%ProgramFiles%\Inkscape\bin\python.exe" set "PATCH_MONITOR_PYTHON=%ProgramFiles%\Inkscape\bin\python.exe"
"%PATCH_MONITOR_PYTHON%" -X utf8 "%~dp0mail_monitor.py" --watch
set "PATCH_MONITOR_EXIT=%ERRORLEVEL%"
pause
exit /b %PATCH_MONITOR_EXIT%
