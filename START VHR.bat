@echo off
title VHR
cd /d "%~dp0"
echo Starting VHR...
schtasks /run /tn "VHR Collector" >nul 2>&1
if errorlevel 1 (
  start "" /min C:\Python314\pythonw.exe vhr_collector.py
  start "" /min C:\Python314\pythonw.exe vhr_results.py
  start "" /min C:\Python314\pythonw.exe vhr_dashboard.py --no-open
)
timeout /t 5 >nul
echo.
echo   card collector    waits for the 04:30 swap, writes days\*.xlsx
echo   results collector records which odds won, every 2 min
echo   gap dashboard     http://localhost:8770
echo.
echo Log: %~dp0vhr.log
timeout /t 6 >nul
