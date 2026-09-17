@echo off
title VHR - link WhatsApp
cd /d "%~dp0"
echo.
echo   A Chrome window will open with the WhatsApp QR code.
echo   On the phone: WhatsApp ^> Linked devices ^> Link a device, and scan it.
echo   This is needed once. After that the sheet is sent every morning by itself.
echo.
python vhr_whatsapp.py --login
if errorlevel 1 (
  echo.
  echo   Not linked. Run this again when the phone is ready.
  pause
  exit /b 1
)
echo.
choice /c YN /m "  Send today's racecard to Akash now, as a test"
if errorlevel 2 goto done
python vhr_whatsapp.py --send
:done
echo.
pause
