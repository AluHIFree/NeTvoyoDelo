@echo off
chcp 65001 >nul
cd /d "%~dp0"
title NeTvoyoDelo Local
echo.
echo  NeTvoyoDelo - local service (SQLite)
echo  Database: %~dp0correspondences.db
echo  Browser:  http://127.0.0.1:8000
echo  Login:    admin / admin123  (or values from .env)
echo  Stop:     close this window or run STOP.bat
echo.
start "" "http://127.0.0.1:8000/api/auth/login"
"%~dp0python\python.exe" "%~dp0run_local.py"
echo.
echo Server stopped.
pause
