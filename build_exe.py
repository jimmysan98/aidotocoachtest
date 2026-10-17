"""Сборка AIDOTA в standalone-папку с exe (PyInstaller onedir).

Запуск из обычной консоли Windows:

    .venv\\Scripts\\python.exe build_exe.py

Результат: dist\\AIDOTA\\AIDOTA.exe
Рядом с exe скрипт кладёт data/, hero_icons/, config.json и README.md.
Модель Vosk (~2.5 ГБ) НЕ копируется — положите её рядом с exe вручную
(папка vosk-model-ru-0.22) или укажите абсолютный путь в audio.model_path.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "AIDOTA"

# Что нужно положить рядом с exe (всё, кроме модели Vosk).
EXTRA_DIRS = ("data", "hero_icons")
EXTRA_FILES = ("config.json", "README.md")


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    print("Собираю AIDOTA через PyInstaller…")
    run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "AIDOTA.spec"])

    print(f"Копирую данные в {DIST} …")
    DIST.mkdir(parents=True, exist_ok=True)
    for name in EXTRA_DIRS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, DIST / name, dirs_exist_ok=True)
    for name in EXTRA_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, DIST / name)
    (DIST / ".cache").mkdir(parents=True, exist_ok=True)

    # Автоматически связываем модель Vosk через junction, если она есть в корне проекта
    vosk_src = ROOT / "vosk-model-ru-0.22"
    vosk_dst = DIST / "vosk-model-ru-0.22"
    if vosk_src.is_dir() and not vosk_dst.exists():
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(vosk_dst), str(vosk_src)], check=True)
            print(f"Модель Vosk привязана (junction): {vosk_dst}")
        except Exception as exc:
            print(f"Не удалось создать junction для модели Vosk: {exc}")

    print("\nГотово: " + str(DIST / "AIDOTA.exe"))
    print("Дальше:")
    print("  1. Положите модель Vosk в " + str(DIST / "vosk-model-ru-0.22"))
    print("     (или пропишите абсолютный путь в " + str(DIST / "config.json") + " -> audio.model_path)")
    print("  2. Запустите " + str(DIST / "AIDOTA.exe"))
    print("  3. Проверка окружения: AIDOTA.exe --doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
