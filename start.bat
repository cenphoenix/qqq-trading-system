@echo off
setlocal
chcp 65001 >nul 2>&1
title QQQ Trading System v7

cd /d "%~dp0"

echo.
echo ============================================
echo   QQQ 0DTE Trading System v7
echo ============================================
echo.

if not exist ".venv-win\Scripts\python.exe" (
    echo [ERROR] .venv-win was not found.
    echo.
    echo Run these commands first:
    echo   C:\Users\Chris\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv-win
    echo   .\.venv-win\Scripts\activate
    echo   python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo [1/2] Using .venv-win
call ".venv-win\Scripts\activate.bat"

echo [2/2] Starting Web + trader
echo.
python run_web.py

endlocal
