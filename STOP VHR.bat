@echo off
title Stop VHR
cd /d "%~dp0"
echo Stopping VHR...
schtasks /end /tn "VHR Collector" >nul 2>&1
rem Only kill this project's python processes - other python tools keep running.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -match 'vhr_(collector|results|dashboard)\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
if exist ".vhr.lock" del ".vhr.lock"
if exist ".vhr-results.lock" del ".vhr-results.lock"
echo Stopped.
timeout /t 4 >nul
