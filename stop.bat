@echo off
setlocal
title Stop QQQ Trading System

echo ============================================
echo   Stopping QQQ Trading System...
echo ============================================

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path '%~dp0').Path.TrimEnd('\');" ^
  "Get-CimInstance Win32_Process | Where-Object { ($_.Name -in @('python.exe','python3.exe')) -and ($_.CommandLine -like '*qqq-trading-system*' -or $_.CommandLine -like '*run_web.py*' -or $_.CommandLine -like '*live_trader.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

timeout /t 2 /nobreak >nul
echo   Done.
echo.
pause
endlocal
