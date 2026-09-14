@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not "%~1"=="" goto supplied
if not exist "%~dp0..\outputs\runyu-patch-status-cve.html" goto standalone
python build_pages.py --report "%~dp0..\outputs\runyu-patch-status-cve.html"
goto done
:supplied
python build_pages.py --archive "%~1"
goto done
:standalone
python build_pages.py
:done
if errorlevel 1 goto failed
echo Website generated in docs\index.html. Publishing is a separate step.
pause
exit /b 0
:failed
echo Build failed. See the message above.
pause
exit /b 1
