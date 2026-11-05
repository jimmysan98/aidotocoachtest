@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" main.py %*
    exit /b 0
)
start "" python main.py %*
