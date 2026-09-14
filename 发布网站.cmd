@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not "%~1"=="" goto supplied
if not exist "%~dp0..\outputs\runyu-patch-status-cve.html" goto standalone
python publish_pages.py --report "%~dp0..\outputs\runyu-patch-status-cve.html"
goto done
:supplied
python publish_pages.py --archive "%~1"
goto done
:standalone
python publish_pages.py
:done
if errorlevel 1 goto failed
echo Pushed successfully. GitHub Pages may need a short time to deploy.
pause
exit /b 0
:failed
echo Publish failed. Local report is preserved. See the message above.
pause
exit /b 1
