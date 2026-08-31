@echo off
title VHR - single run
cd /d "%~dp0"
echo Fetching every race currently on the STBET card...
echo.
python vhr_collector.py --once
echo.
echo Workbook is in %~dp0days\
pause
