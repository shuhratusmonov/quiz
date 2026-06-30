@echo off
cd /d "%~dp0"
REM ============================================================
REM   SETUP - run this once to save your settings to config.txt
REM   Enter ONLY the value, no quotes, no comments.
REM ============================================================

echo ============================================================
echo   Telegram Gifts Monitor - Setup
echo ============================================================
echo.

set /p APIID="API ID (number from my.telegram.org): "
set /p APIHASH="API Hash (string from my.telegram.org): "
set /p TOKEN="Bot token (from @BotFather): "
set /p CH="Channel (example @abcuzbek): "
set /p MP="Max price in stars (example 500): "
set /p INT="Interval in seconds (example 60): "

(
echo api_id=%APIID%
echo api_hash=%APIHASH%
echo token=%TOKEN%
echo channel=%CH%
echo max_price_stars=%MP%
echo interval=%INT%
echo all=1
) > config.txt

echo.
echo ============================================================
echo   Done. Settings saved to config.txt
echo   Now run: run_monitor.bat
echo ============================================================
echo.
pause
