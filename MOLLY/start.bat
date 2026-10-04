@echo off
setlocal EnableExtensions
chcp 65001 >nul
title MOLLY - AI-pomoshchnik PTO
cd /d "%~dp0"

echo.
echo ============================================================
echo   МОЛЛИ - AI-помощник ПТО
echo ============================================================
echo.

rem ---------- 1. Python ----------
call :findpy
if defined PY goto :havepy

echo Python 3.10 или новее не найден. Пробую установить автоматически...
where winget >nul 2>nul
if errorlevel 1 goto :nopython
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto :havepy

:nopython
echo.
echo Не удалось найти или установить Python.
echo Установите Python 3.12 с https://www.python.org/downloads/
echo На первом экране установщика отметьте "Add python.exe to PATH".
echo Затем запустите start.bat ещё раз.
echo.
pause
exit /b 1

:havepy
rem ---------- 2. Окружение и компоненты (только при первом запуске) ----------
if exist ".venv\Scripts\python.exe" goto :venvok
echo Создаю окружение, это делается один раз...
%PY% -m venv .venv
if errorlevel 1 goto :fail
:venvok

fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if not errorlevel 1 goto :depsok
echo Устанавливаю компоненты. Нужен интернет, это делается один раз...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
copy /y requirements.txt ".venv\requirements.installed" >nul
:depsok

rem ---------- 3. Запуск ----------
set MOLLY_OPEN_BROWSER=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd backend
"..\.venv\Scripts\python.exe" main.py
set RC=%errorlevel%
cd ..
if "%RC%"=="0" exit /b 0

echo.
echo МОЛЛИ завершилась с ошибкой, код %RC%.
echo Подробности: data\logs\molly.log
echo.
pause
exit /b %RC%

:fail
echo.
echo Подготовка не удалась. Проверьте подключение к интернету и запустите start.bat ещё раз.
echo Если ошибка повторяется - удалите папку .venv и запустите снова.
echo.
pause
exit /b 1

:findpy
set "PY="
for %%C in ("py -3" "python") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~C"
  )
)
if defined PY exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
exit /b 0
