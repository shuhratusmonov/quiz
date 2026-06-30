@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM ============================================================
REM   ЗАПУСК мониторинга подарков (читает config.bat).
REM   Если config.bat ещё нет — сначала запустите setup.bat
REM ============================================================

if not exist config.bat (
    echo.
    echo [!] Сначала запустите setup.bat — он спросит ваши данные.
    echo.
    pause
    exit /b
)

call config.bat

echo ============================================================
echo   Мониторинг: каждые %INTERVAL% сек, дешевле %MAX_PRICE% звезд
echo   Канал: %CHANNEL%
echo   Остановить: закройте окно или нажмите Ctrl+C
echo ============================================================
echo.

python gifts_resale_parser.py --all --max-price-stars %MAX_PRICE% --interval %INTERVAL% --channel %CHANNEL%

echo.
echo Скрипт завершился. Нажмите любую клавишу...
pause >nul
