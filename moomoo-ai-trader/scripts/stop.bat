@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (py -3 scripts\launcher.py stop) || (python scripts\launcher.py stop)
pause
