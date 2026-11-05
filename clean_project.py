"""Скрипт глубокой очистки проекта и оптимизации ассетов AIDOTA.

1. Удаляет временные файлы сборки, артефакты тестов, __pycache__, .pytest_cache, .cache, build, dist.
2. Сжимает иконки героев (hero_icons/*.png) через Pillow (квантование палитры до 256 цветов / 8-bit PNG).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent


def remove_path(path: Path) -> bool:
    """Безопасно удаляет файл или каталог."""
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
            return True
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
    except Exception as exc:
        print(f"  [!] Ошибка удаления {path}: {exc}")
    return False


def clean_caches(root: Path) -> int:
    """Удаляет все __pycache__, *.pyc, *.pyo, .pytest_cache, .cache, пропуская виртуальное окружение."""
    count = 0
    print("[1/3] Поиск и удаление кэшей и временных файлов...")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # Строго пропускаем виртуальное окружение и git-репозиторий
        if ".venv" in dirnames:
            dirnames.remove(".venv")
        if ".git" in dirnames:
            dirnames.remove(".git")

        for dirname in list(dirnames):
            if dirname in ("__pycache__", ".pytest_cache", ".cache", "test-tmp"):
                target = Path(dirpath) / dirname
                if remove_path(target):
                    count += 1
                    dirnames.remove(dirname)

        for filename in filenames:
            if filename.endswith((".pyc", ".pyo")):
                target = Path(dirpath) / filename
                if remove_path(target):
                    count += 1
    return count


def clean_build_artifacts(root: Path, keep_dist: bool = True) -> None:
    """Удаляет папки сборки build, *.egg-info (сохраняя готовый dist/AIDOTA)."""
    print("[2/3] Очистка временных каталогов сборки (build)...")
    for name in ("build", ".pytest_cache"):
        target = root / name
        if target.is_dir():
            print(f"  Удаляю {target}...")
            remove_path(target)

    if not keep_dist:
        dist_dir = root / "dist"
        if dist_dir.is_dir():
            print(f"  Удаляю {dist_dir}...")
            remove_path(dist_dir)

    # Очистка логов (сохраняем саму папку logs)
    logs_dir = root / "logs"
    if logs_dir.is_dir():
        for log_file in logs_dir.glob("*.log"):
            remove_path(log_file)


def optimize_hero_icons(icons_dir: Path) -> tuple[int, int, int]:
    """Сжимает все PNG-иконки героев методом квантования палитры (8-bit palette)."""
    print("[3/3] Оптимизация и сжатие иконок героев (hero_icons)...")
    if not icons_dir.is_dir():
        print(f"  Каталог {icons_dir} не найден, пропускаю.")
        return 0, 0, 0

    count = 0
    orig_total_size = 0
    new_total_size = 0

    for file in icons_dir.glob("*.png"):
        try:
            sz_before = file.stat().st_size
            orig_total_size += sz_before

            with Image.open(file) as img:
                if img.mode != "P":
                    quantized = img.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
                    quantized.save(file, format="PNG", optimize=True)
                else:
                    img.save(file, format="PNG", optimize=True)

            sz_after = file.stat().st_size
            new_total_size += sz_after
            count += 1
        except Exception as exc:
            print(f"  [!] Ошибка сжатия {file.name}: {exc}")

    return count, orig_total_size, new_total_size


def main() -> int:
    print("=" * 60)
    print(" AIDOTA: Глубокая очистка и оптимизация проекта")
    print("=" * 60)

    caches_removed = clean_caches(PROJECT_ROOT)
    print(f"  Удалено кэш-объектов: {caches_removed}")

    clean_build_artifacts(PROJECT_ROOT)

    icons_dir = PROJECT_ROOT / "hero_icons"
    count, before_sz, after_sz = optimize_hero_icons(icons_dir)
    if count > 0:
        saved_kb = (before_sz - after_sz) / 1024
        print(f"  Сжато иконок: {count} шт.")
        print(f"  Исходный размер: {before_sz / 1024:.1f} KB -> Сжатый: {after_sz / 1024:.1f} KB (сэкономлено {saved_kb:.1f} KB)")

    print("=" * 60)
    print(" Очистка и оптимизация успешно завершены!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
