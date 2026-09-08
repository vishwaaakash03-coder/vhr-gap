@echo off
title VHR - backfill past results from 49s
cd /d "%~dp0"
echo.
echo   Filling in past winning odds from 49s.co.uk.
echo   Days already finished are skipped, so this is safe to stop and re-run.
echo   About a minute and a half per day.
echo.
if "%~1"=="" (
  python vhr_49s.py 2026-08-01 2026-09-07
) else (
  python vhr_49s.py %*
)
echo.
pause
