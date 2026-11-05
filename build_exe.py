"""Сборка AIDOTA в standalone-папку с exe (PyInstaller onedir) + установщик Inno Setup.

Запуск из обычной консоли Windows:

    .venv\\Scripts\\python.exe build_exe.py

Результат:
    dist\\AIDOTA\\AIDOTA.exe   — автономное приложение;
    AIDOTA_Setup.exe          — единый установщик (если установлен Inno Setup 6).

Модель Vosk small (~87 МБ) копируется в дистрибутив автоматически (если лежит в
корне проекта), поэтому установщик работает «из коробки». Большая модель
vosk-model-ru-0.22 (~2.5 ГБ) НЕ копируется — её можно скачать из интерфейса.

Пользовательский config.json разработчика в дистрибутив НЕ попадает: вместо него
кладётся config.example.json, а приложение создаёт config.json при первом запуске.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "AIDOTA"

# Что нужно положить рядом с exe (всё, кроме модели Vosk).
EXTRA_DIRS = ("data", "hero_icons", "assets")
EXTRA_FILES = (
    "default_preset.json",
    "config.example.json",
    "LICENSE",
    "CHANGELOG.md",
    "README.md",
    "installer_notes.txt",
    "THIRD_PARTY_NOTICES.md",
)

# Файлы, которые ни при каких условиях не должны попасть в дистрибутив.
FORBIDDEN_IN_DIST = ("config.json", "installer.iss", "AIDOTA.spec", "build_exe.py")

ISCC_CANDIDATES = (
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
    Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
)


def app_version() -> str:
    """Версия приложения из aidota/__init__.py (единый источник истины)."""
    sys.path.insert(0, str(ROOT))
    try:
        from aidota import __version__  # noqa: PLC0415 — локальный импорт после sys.path

        return str(__version__).strip()
    finally:
        if str(ROOT) in sys.path:
            sys.path.remove(str(ROOT))


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def find_iscc() -> Path | None:
    """Ищет командный компилятор Inno Setup 6."""
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return Path(found)
    return None


def copy_dist_extras(version: str) -> bool:
    """Копирует данные рядом с exe. Возвращает False, если нет модели Vosk small."""
    DIST.mkdir(parents=True, exist_ok=True)
    for name in EXTRA_DIRS:
        src = ROOT / name
        if src.is_dir():
            print(f"  + {name}/")
            shutil.copytree(src, DIST / name, dirs_exist_ok=True)
    for name in EXTRA_FILES:
        src = ROOT / name
        if src.is_file():
            print(f"  + {name}")
            shutil.copy2(src, DIST / name)
    (DIST / ".cache").mkdir(parents=True, exist_ok=True)
    (DIST / "logs").mkdir(parents=True, exist_ok=True)

    # Защита от случайной упаковки пользовательского конфига и dev-мусора.
    for name in FORBIDDEN_IN_DIST:
        stale = DIST / name
        if stale.is_file():
            stale.unlink()
    for stale_dir in (DIST / "logs", DIST / ".cache"):
        if stale_dir.is_dir():
            for leftover in stale_dir.glob("*"):
                if leftover.is_file():
                    leftover.unlink()

    model_ok = True
    vosk_small_src = ROOT / "vosk-model-small-ru-0.22"
    vosk_small_dst = DIST / "vosk-model-small-ru-0.22"
    if vosk_small_src.is_dir():
        print(f"  + vosk-model-small-ru-0.22/ (компактная модель)")
        shutil.copytree(vosk_small_src, vosk_small_dst, dirs_exist_ok=True)
    else:
        model_ok = False
        print(
            "  [!] ВНИМАНИЕ: vosk-model-small-ru-0.22 не найдена в корне проекта.\n"
            "      Установщик соберётся, но распознавание речи не будет работать\n"
            "      без скачивания модели. Скачайте её: https://alphacephei.com/vosk/models"
        )
    return model_ok


def build_installer(version: str) -> bool:
    """Собирает AIDOTA_Setup.exe через Inno Setup. Возвращает True при успехе."""
    iscc = find_iscc()
    if iscc is None:
        print(
            "\n[!] Inno Setup (ISCC.exe) не найден — установщик не собран.\n"
            "    Установите Inno Setup 6: winget install --id JRSoftware.InnoSetup\n"
            "    (или скачайте с https://jrsoftware.org/isinfo.php) и повторите сборку."
        )
        return False

    print(f"\nСобираю установщик через Inno Setup: {iscc} …")
    setup_exe = ROOT / "AIDOTA_Setup.exe"
    if setup_exe.is_file():
        setup_exe.unlink()
    run([str(iscc), f"/DMyAppVersion={version}", "installer.iss"])
    if not setup_exe.is_file():
        print("[!] Ошибка: установщик не появился после работы ISCC.")
        return False
    size_mb = setup_exe.stat().st_size / (1024 * 1024)
    print("\n========================================================")
    print(f"УСПЕХ! Установщик готов: {setup_exe} ({size_mb:.1f} МБ)")
    print(f"Версия: {version}")
    print("Можно отдавать пользователям: установка «из коробки», без настройки Python.")
    print("========================================================\n")
    return True


def main() -> int:
    version = app_version()
    print(f"Собираю AIDOTA {version} через PyInstaller…")
    run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "AIDOTA.spec"])

    print(f"\nКопирую данные в {DIST} …")
    model_ok = copy_dist_extras(version)
    (DIST / "version.txt").write_text(f"{version}\n", encoding="utf-8")

    ok = build_installer(version)
    dist_exe = DIST / "AIDOTA.exe"
    print(f"Приложение: {dist_exe} ({dist_exe.stat().st_size / (1024 * 1024):.1f} МБ)" if dist_exe.is_file() else "[!] exe не найден")
    if not model_ok:
        print("[!] Соберите/скачайте vosk-model-small-ru-0.22 и пересоберите установщик.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
