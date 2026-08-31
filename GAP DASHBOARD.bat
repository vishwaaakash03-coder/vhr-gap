@echo off
title VHR Gap Telemetry
cd /d "%~dp0"
rem Server already up? Just open the browser. Otherwise start it in this window.
powershell -NoProfile -Command "try{ (New-Object Net.Sockets.TcpClient('127.0.0.1',8770)).Close(); exit 0 }catch{ exit 1 }" >nul 2>&1
if not errorlevel 1 (
  start "" http://localhost:8770
  exit /b
)
echo Starting the gap dashboard...
python vhr_dashboard.py
