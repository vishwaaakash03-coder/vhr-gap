@echo off
title VHR - what is the site serving?
cd /d "%~dp0"
python vhr_collector.py --status
echo.
pause
