@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3.14 main.py %*
    if %errorlevel% equ 0 goto :eof
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
    if %errorlevel% equ 0 goto :eof
)

python main.py %*
if errorlevel 1 pause
