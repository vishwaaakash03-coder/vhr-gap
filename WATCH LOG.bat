@echo off
title VHR - live log
cd /d "%~dp0"
echo Watching vhr.log - close this window to stop. Nothing here changes anything.
echo.
powershell -NoProfile -Command "Get-Content -Path 'vhr.log' -Tail 30 -Wait"
