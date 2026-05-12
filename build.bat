@echo off
REM Build script for Schwab Momentum Bot Windows executable
REM Requires: pip install pyinstaller

echo Building Schwab Momentum Bot...

pyinstaller ^
    --name "SchwabMomentumBot" ^
    --onefile ^
    --windowed ^
    --icon=NONE ^
    --add-data "config.yaml;." ^
    --hidden-import=schwab ^
    --hidden-import=PySide6.QtCore ^
    --hidden-import=PySide6.QtWidgets ^
    --hidden-import=PySide6.QtGui ^
    --hidden-import=sqlalchemy ^
    --hidden-import=apscheduler ^
    --hidden-import=apscheduler.schedulers.background ^
    --hidden-import=apscheduler.triggers.interval ^
    --hidden-import=apscheduler.triggers.cron ^
    --hidden-import=cryptography ^
    --hidden-import=httpx ^
    --hidden-import=yaml ^
    --hidden-import=numpy ^
    --hidden-import=pandas ^
    main.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build successful!
    echo Executable: dist\SchwabMomentumBot.exe
    echo.
    echo Copy these files alongside the .exe:
    echo   - config.yaml
    echo   - (Optional) universe_csv/ folder for CSV fallback
) else (
    echo.
    echo Build FAILED. Check errors above.
)

pause
