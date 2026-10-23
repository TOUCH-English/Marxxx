@echo off
REM Windows one-click start (double-click)
cd /d "%~dp0.."
where py >nul 2>nul && (py -3 scripts\launcher.py start) || (python scripts\launcher.py start)
pause
