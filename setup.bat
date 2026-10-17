@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   AIDOTA - setup
echo ================================================

where python >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from python.org
    echo and enable "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv
        pause
        exit /b 1
    )
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check your internet connection.
    pause
    exit /b 1
)

if not exist "vosk-model-ru-0.22\am\final.mdl" (
    echo.
    echo WARNING: Vosk model not found.
    echo Download vosk-model-ru-0.22.zip from https://alphacephei.com/vosk/models
    echo and unpack it to vosk-model-ru-0.22\am\final.mdl
    echo Or change audio.model_path in config.json.
    echo.
)

if not exist "hero_icons\icons" (
    echo Downloading hero icons...
    ".venv\Scripts\python.exe" main.py --fetch-icons
)

echo.
echo Environment check:
".venv\Scripts\python.exe" main.py --doctor
echo.
echo Done. Next: install_gsi.bat, then run.bat
pause
