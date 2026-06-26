@echo off
cd /d "%~dp0"
echo ==========================================
echo   Starting TikHub local proxy ...
echo   Then OPEN in your browser:  http://localhost:8787/
echo   KEEP THIS WINDOW OPEN.  Press Ctrl+C to stop.
echo ==========================================
echo.
python tikhub_proxy.py
if errorlevel 1 py tikhub_proxy.py
echo.
echo Proxy stopped. If you saw an error above, send a screenshot.
pause
