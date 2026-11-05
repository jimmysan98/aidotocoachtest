# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для AIDOTA.

Собирает onedir-версию: `dist\\AIDOTA\\AIDOTA.exe`.
Упаковывает data/, hero_icons/, assets/ и иконку icon.ico.
Режим: --windowed / --noconsole (без чёрного окна консоли).
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = [
    ("data", "data"),
    ("hero_icons", "hero_icons"),
    ("assets", "assets"),
    ("default_preset.json", "."),
    ("config.example.json", "."),
    ("aidota/web", "aidota/web"),
]
binaries = []
hiddenimports = []

# Нативные библиотеки и данные пакетов
for pkg in (
    "vosk",
    "sounddevice",
    "cv2",
    "win32com",
    "certifi",
    "customtkinter",
    "pystray",
    "PIL",
    "rapidfuzz",
    "webview",
    "pyttsx3",
    "comtypes",
    "edge_tts",
    "mss",
    "soundfile",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

datas += collect_data_files("certifi")

# Windows COM и Win32 API
hiddenimports += [
    "pythoncom",
    "win32api",
    "win32gui",
    "win32con",
    "win32process",
    "win32com.client",
    "win32timezone",
    "mss",
    "requests",
    "urllib3",
]

# Все подмодули aidota
hiddenimports += collect_submodules("aidota")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "scipy",
        "pandas",
        "torch",
        "unittest",
        "IPython",
        "sqlite3",
        "pytest",
        "tkinter.test",
        # Оптимизация размера: тестовые/служебные пакеты не нужны в релизе
        "comtypes.test",
        "win32com.test",
        "PIL.ImageQt",
        "idlelib",
        "pydoc_data",
        "lib2to3",
        "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIDOTA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=False,
    icon="assets/icon.ico",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name="AIDOTA",
)
