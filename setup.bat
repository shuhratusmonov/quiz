@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM ============================================================
REM   НАСТРОЙКА мониторинга подарков.
REM   Запустите ОДИН раз — введите данные, они сохранятся.
REM   Ничего редактировать вручную не нужно.
REM ============================================================

echo ============================================================
echo   Настройка мониторинга подарков Telegram
echo ============================================================
echo.
echo Вводите ТОЛЬКО само значение, без кавычек и комментариев.
echo.

set /p APIID="API ID (число с my.telegram.org): "
set /p APIHASH="API Hash (строка с my.telegram.org): "
set /p TOKEN="Токен бота (от @BotFather): "
set /p CH="Канал (например @abcuzbek): "
set /p MP="Макс. цена в звёздах (например 500): "
set /p INT="Интервал в секундах (например 60): "

(
echo set TG_API_ID=%APIID%
echo set TG_API_HASH=%APIHASH%
echo set BOT_TOKEN=%TOKEN%
echo set CHANNEL=%CH%
echo set MAX_PRICE=%MP%
echo set INTERVAL=%INT%
) > config.bat

echo.
echo ============================================================
echo   Готово! Данные сохранены в config.bat
echo   Теперь запускайте run_monitor.bat
echo ============================================================
echo.
pause
