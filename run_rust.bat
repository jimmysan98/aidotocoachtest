@echo off
chcp 65001 >nul
cd /d "%~dp0aidota-core"
if exist "target\release\aidota-core.exe" (
    start "" "target\release\aidota-core.exe" %*
    exit /b 0
)
cargo run --release -- %*
