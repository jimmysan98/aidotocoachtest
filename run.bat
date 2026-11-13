@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM 1. Оконный запуск GUI без лишнего окна консоли (при двойном клике)
if "%~1"=="" (
    if exist ".venv\Scripts\pythonw.exe" (
        start "" ".venv\Scripts\pythonw.exe" main.py
        exit /b 0
    )
    if exist ".venv\Scripts\python.exe" (
        start "" ".venv\Scripts\python.exe" main.py
        exit /b 0
    )
    start "" pythonw main.py 2>nul || start "" python main.py
    exit /b 0
)

REM 2. Консольный запуск с аргументами (run.bat --doctor, run.bat --text)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
    exit /b %ERRORLEVEL%
)

python main.py %*
if errorlevel 1 pause
