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
set /p DISC="Min discount below floor in %% (example 20): "
set /p INT="Interval in seconds (example 60): "

echo.
echo --- AUTO-BUY (spends real Stars!) ---
echo Leave 0 to disable buying. Read warnings before enabling.
set /p AUTOBUY="Enable auto-buy? (1=yes, 0=no): "
set BUYREAL=0
set BUYBUDGET=0
set BUYMAX=0
set BUYDISC=30
if "%AUTOBUY%"=="1" (
    set /p BUYDISC="Buy only if below floor by %% (example 30): "
    set /p BUYREAL="Real purchases? (1=real spends money, 0=test only): "
    set /p BUYBUDGET="Budget per run in stars (e.g. 2000; required for real): "
    set /p BUYMAX="Max price per gift in stars (0=no limit): "
)

(
echo api_id=%APIID%
echo api_hash=%APIHASH%
echo token=%TOKEN%
echo channel=%CH%
echo min_discount=%DISC%
echo interval=%INT%
echo all=1
echo auto_buy=%AUTOBUY%
echo buy_real=%BUYREAL%
echo buy_budget=%BUYBUDGET%
echo buy_max_price=%BUYMAX%
echo buy_min_discount=%BUYDISC%
) > config.txt

echo.
echo ============================================================
echo   Done. Settings saved to config.txt
echo   Now run: run_monitor.bat
echo ============================================================
echo.
pause
