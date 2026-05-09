@echo off
title QQQ Trading System
echo.
echo   QQQ 0DTE v6 Trading System
echo   Breakout + Reversal
echo ========================================
echo.

cd /d C:\Users\Admin\Desktop\QQQ_Live

if exist "dist\QQQ_Trading_v5.exe" (
    echo   EXE mode: launching QQQ_Trading_v5.exe
    start "" "dist\QQQ_Trading_v5.exe"
    goto :done
)

python -c "import main_app" >nul 2>&1
if not errorlevel 1 (
    echo   Script mode: launching main_app.py
    start "QQQ Trading" python main_app.py
    goto :done
)

echo   Fallback: Web + Trader
echo.
echo [1/2] Starting Web Dashboard...
start "QQQ Web" python trader_web.py
timeout /t 3 /nobreak >nul

echo [2/2] Starting Live Trader...
start "QQQ Trader" python live_trader.py
timeout /t 2 /nobreak >nul

:done
echo.
echo   System launched!
echo   Web: http://127.0.0.1:8080
echo.
echo   Press any key to open browser...
pause >nul
start http://127.0.0.1:8080
