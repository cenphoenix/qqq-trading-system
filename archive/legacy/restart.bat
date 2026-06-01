@echo off
title QQQ Trading System - Restart
echo.
echo   QQQ Trading System - Restart All
echo ============================================
echo.

cd /d C:\Users\Admin\Desktop\QQQ_Live

echo [1/5] Killing all Python processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM python3.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul
echo       Done.

echo.
echo [2/5] Checking data files...
python -c "import json; json.load(open('longbridge_orders.json'))" >nul 2>&1
if errorlevel 1 (
    echo       Fixing longbridge_orders.json ...
    echo {"orders":[],"total":0,"buy_count":0,"sell_count":0} > longbridge_orders.json
    echo       Fixed.
) else (
    echo       OK.
)

echo.
echo [3/5] Launching...
if exist "dist\QQQ_Trading_v5.exe" (
    echo       EXE mode: dist\QQQ_Trading_v5.exe
    start "" "dist\QQQ_Trading_v5.exe"
    goto :done
)

python -c "import main_app" >nul 2>&1
if not errorlevel 1 (
    echo       Script mode: python main_app.py
    start "QQQ Trading" python main_app.py
    goto :done
)

echo       Fallback mode: Web + Trader separately
echo.
echo [4/5] Starting Web Dashboard...
start "TraderWeb" cmd /c "cd /d C:\Users\Admin\Desktop\QQQ_Live && python trader_web.py"
timeout /t 3 /nobreak >nul
echo       Web started: http://127.0.0.1:8080

echo.
echo [5/5] Starting Live Trader...
start "LiveTrader" cmd /c "cd /d C:\Users\Admin\Desktop\QQQ_Live && python live_trader.py"
timeout /t 3 /nobreak >nul
echo       Trader started.

:done
echo.
echo ============================================
echo   All systems launched!
echo   Web: http://127.0.0.1:8080
echo   Press any key to close this window.
echo ============================================
pause >nul
