@echo off
cd /d "%~dp0"
REM ============================================================
REM   RUN MONITOR - reads settings from config.txt
REM   If config.txt is missing, run setup.bat first.
REM ============================================================

if not exist config.txt (
    echo.
    echo [!] config.txt not found. Run setup.bat first.
    echo.
    pause
    exit /b
)

python gifts_resale_parser.py --config config.txt

echo.
echo Script finished. Press any key...
pause >nul
