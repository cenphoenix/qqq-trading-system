@echo off
title Stop Trading System

echo ============================================
echo   Stopping all Python processes...
echo ============================================

taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM python3.exe /T >nul 2>&1

timeout /t 2 /nobreak >nul
echo   All stopped.
echo.
pause
