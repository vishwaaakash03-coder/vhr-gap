@echo off
title VHR - send today's racecard
cd /d "%~dp0"
echo.
python vhr_whatsapp.py --send %*
echo.
pause
