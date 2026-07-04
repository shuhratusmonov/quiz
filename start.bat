@echo off
rem Запуск Portals Offers из правильной папки (там, где лежит этот файл).
rem %~dp0 = папка этого .bat, поэтому токен из token.txt всегда находится.
cd /d "%~dp0"
echo ============================================
echo   Portals Offers
echo   Открой в браузере: http://localhost:8080
echo   Остановить сервер: закрой это окно или Ctrl+C
echo ============================================
echo.
python server.py
echo.
echo Сервер остановлен.
pause
