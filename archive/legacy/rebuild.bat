@echo off
title QQQ Trading v5.0 - Rebuild EXE (Clean)
echo.
echo   QQQ Trading v5.0 - REBUILD (Clean)
echo ============================================
echo.

cd /d C:\Users\Admin\Desktop\QQQ_Live

echo [1/4] Killing old process...
taskkill /f /im QQQ_Trading_v5.exe 2>nul
timeout /t 2 /nobreak >nul

echo [2/4] Cleaning build cache...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist QQQ_Trading_v5.exe del /f QQQ_Trading_v5.exe
if exist QQQ_Trading_v5.spec del /f QQQ_Trading_v5.spec
echo       Build cache cleared.

echo [3/4] Rebuilding EXE (may take 3-5 minutes)...
pyinstaller qqq_trading.spec --clean --noconfirm --distpath . --workpath build 2>&1
if errorlevel 1 (
    echo.
    echo   BUILD FAILED! Check errors above.
    pause
    exit /b 1
)

echo.
echo [4/4] Done!
echo.
echo   EXE: QQQ_Trading_v5.exe
echo.
for %%A in (QQQ_Trading_v5.exe) do echo   Size: %%~zA bytes
echo.
pause
