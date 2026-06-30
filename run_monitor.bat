@echo off
chcp 65001 >nul
REM ============================================================
REM   МОНИТОР ПОДАРКОВ Telegram — запуск в окне
REM   Закрыть окно = остановить. Видно все логи.
REM ============================================================

REM ====== ЗАПОЛНИТЕ СВОИ ДАННЫЕ (один раз) ======
set TG_API_ID=ВАШ_API_ID
set TG_API_HASH=ВАШ_API_HASH
set BOT_TOKEN=ВАШ_ТОКЕН_БОТА
set CHANNEL=@abcuzbek
set MAX_PRICE=500
set INTERVAL=60
REM =============================================

cd /d "%~dp0"

echo ============================================================
echo   Мониторинг: каждые %INTERVAL% сек, дешевле %MAX_PRICE% звезд
echo   Канал: %CHANNEL%
echo   Остановить: закройте это окно или нажмите Ctrl+C
echo ============================================================
echo.

python gifts_resale_parser.py --all --max-price-stars %MAX_PRICE% --interval %INTERVAL% --channel %CHANNEL%

echo.
echo Скрипт завершился. Нажмите любую клавишу...
pause >nul
