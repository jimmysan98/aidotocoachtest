"""Единый резолвер путей и атомарный JSON I/O (разделение read-only / writable состояния)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("aidota.paths")

APP_DIR_NAME = "AIDOTA"
BUNDLED_DATA_ALLOWLIST: tuple[str, ...] = ("heroes.json", "items.json")


def is_frozen() -> bool:
    """Возвращает True, если приложение запущено из PyInstaller-сборки."""
    return bool(getattr(sys, "frozen", False))


def app_install_root() -> Path:
    """Read-only корень установки (рядом с exe при заморозке, иначе корень проекта)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_data_dir(root: Path | None = None) -> Path:
    """Путь к read-only офлайн-снапшотам ``data/heroes.json`` и ``data/items.json``."""
    base = Path(root) if root is not None else app_install_root()
    return base / "data"


def uses_isolated_runtime_root() -> bool:
    """True в frozen-сборке или при явном переопределении ``AIDOTA_DATA_DIR``."""
    return is_frozen() or bool(os.environ.get("AIDOTA_DATA_DIR", "").strip())


def writable_root() -> Path:
    """Базовый каталог для пользовательских и кэш-данных (%LOCALAPPDATA%\\AIDOTA или проект)."""
    override = os.environ.get("AIDOTA_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return base / APP_DIR_NAME
    return app_install_root()


def default_config_path() -> Path:
    """Путь к пользовательскому ``config.json`` в записываемом каталоге."""
    return writable_root() / "config.json"


def writable_data_dir() -> Path:
    """Записываемый каталог для ``coach_memory.json`` и ``player_profile.json``."""
    return writable_root() / "data"


def writable_cache_dir() -> Path:
    """Записываемый каталог ``.cache``."""
    return writable_root() / ".cache"


def meta_cache_dir(cache_dir: Path | None = None) -> Path:
    """Подкаталог живого SWR-кэша OpenDota (``<cache>/meta``)."""
    base = Path(cache_dir) if cache_dir is not None else writable_cache_dir()
    return base / "meta"


def writable_logs_dir() -> Path:
    """Записываемый каталог для ``aidota.log`` и ``metrics.jsonl``."""
    return writable_root() / "logs"


def resolve_writable_dir(value: str | Path, *, fallback_base: Path | None = None) -> Path:
    """Разрешает путь к записываемому каталогу (учитывая %LOCALAPPDATA%\\AIDOTA в frozen)."""
    raw = Path(value)
    if raw.is_absolute():
        return raw
    base = fallback_base if fallback_base is not None else writable_root()
    return base / raw


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Атомарно записывает JSON (``tmp`` + ``flush`` + ``fsync`` + ``os.replace``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}_", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_json_safe(path: Path, default: Any = None) -> Any:
    """Читает JSON без выброса исключений при отсутствии файла или битом содержимом."""
    target = Path(path)
    if not target.is_file():
        return default
    try:
        raw = target.read_text(encoding="utf-8")
        if not raw.strip():
            return default
        return json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("Не удалось прочитать JSON %s: %s", target, exc)
        return default
