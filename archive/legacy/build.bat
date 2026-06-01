@echo off
title QQQ Trading v5.0 - Build EXE
echo.
echo   QQQ Trading v5.0 - Build EXE
echo ============================================
echo.

cd /d C:\Users\Admin\Desktop\QQQ_Live

echo [1/3] Checking dependencies...
python -c "import pystray, PIL, longbridge, numpy, tzdata; print('All OK')" 2>&1
if errorlevel 1 (
    echo       Installing missing packages...
    pip install pystray Pillow longbridge numpy tzdata
)
echo.

echo [2/3] Building EXE (may take 3-5 minutes)...
pyinstaller qqq_trading.spec --clean --noconfirm --distpath . --workpath build 2>&1
if errorlevel 1 (
    echo.
    echo   BUILD FAILED! Check errors above.
    pause
    exit /b 1
)

echo.
echo [3/3] Done!
echo.
echo   EXE: QQQ_Trading_v5.exe
echo.
for %%A in (QQQ_Trading_v5.exe) do echo   Size: %%~zA bytes
echo.
pause
