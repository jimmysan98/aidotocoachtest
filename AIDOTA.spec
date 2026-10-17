# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для AIDOTA.

Собирает onedir-версию: `dist\\AIDOTA\\AIDOTA.exe`.
Модель Vosk (~2.5 ГБ), data/ и hero_icons/ НЕ упаковываются в exe —
они копируются рядом с exe скриптом build_exe.py.
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = []

# Нативные библиотеки и данные пакетов.
for pkg in ("vosk", "sounddevice", "cv2", "win32com", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += collect_data_files("certifi")

# Windows COM для озвучки (SAPI).
hiddenimports += ["pythoncom", "win32api", "win32com.client", "win32timezone"]
# Лениво импортируемые модули проекта.
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
    excludes=["pytest", "tkinter.test", "unittest"],
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
    strip=False,
    upx=False,
    console=True,
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
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AIDOTA",
)
