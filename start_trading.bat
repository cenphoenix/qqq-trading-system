@echo off
chcp 65001 >nul 2>&1
echo.
echo ================================
echo   QQQ 0DTE Trading System v4
echo   Hot-Blooded Youth's Exchange
echo ================================
echo.
echo Starting... Window will open shortly.
echo.
QQQ_Trading_v4.exe
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start. Check .env file.
    pause
)
