"""Мост к нативным оптимизациям на Rust (aidota_rust.dll).

Ускоряет критические алгоритмы (парсинг GSI, макро-тайминги рун/Рошана,
контр-предметы) с нулевыми накладными расходами и автоматическим
безопасным откатом на Python при отсутствии DLL.
"""

from __future__ import annotations

import ctypes
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("aidota.rust_bridge")

_LIB: ctypes.CDLL | None = None
_INIT_ATTEMPTED = False


def _get_lib() -> ctypes.CDLL | None:
    global _LIB, _INIT_ATTEMPTED
    if _INIT_ATTEMPTED:
        return _LIB

    _INIT_ATTEMPTED = True
    candidates = [
        Path(__file__).resolve().parent / "aidota_rust.dll",
        Path(__file__).resolve().parent.parent / "aidota-core" / "target" / "release" / "aidota_rust.dll",
        Path("aidota_rust.dll"),
    ]

    for path in candidates:
        if path.is_file():
            try:
                lib = ctypes.CDLL(str(path))
                # Настройка сигнатур
                lib.aidota_compute_timers_json.argtypes = [ctypes.c_int64, ctypes.c_int64]
                lib.aidota_compute_timers_json.restype = ctypes.c_void_p

                lib.aidota_build_advice_json.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_uint32,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                ]
                lib.aidota_build_advice_json.restype = ctypes.c_void_p

                lib.aidota_free_string.argtypes = [ctypes.c_void_p]
                lib.aidota_free_string.restype = None

                _LIB = lib
                log.info("Нативный Rust-ускоритель успешно загружен: %s", path)
                return _LIB
            except Exception as exc:
                log.warning("Не удалось загрузить Rust DLL (%s): %s", path, exc)

    log.debug("Rust-библиотека не найдена, используется чистый Python")
    return None


def is_rust_available() -> bool:
    """Возвращает True, если нативный Rust-движок активен."""
    return _get_lib() is not None


def compute_macro_timers_rust(clock_time: int, roshan_kill_time: int | None = None) -> dict[str, Any] | None:
    """Вычисляет тайминги на Rust в микросекунды."""
    lib = _get_lib()
    if lib is None:
        return None

    try:
        r_kill = int(roshan_kill_time) if roshan_kill_time is not None else -1
        ptr = lib.aidota_compute_timers_json(int(clock_time), r_kill)
        if not ptr:
            return None
        try:
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            if raw is None:
                return None
            return json.loads(raw.decode("utf-8"))
        finally:
            lib.aidota_free_string(ptr)
    except Exception as exc:
        log.warning("Ошибка вызова Rust compute_timers: %s", exc)
        return None


def build_advice_rust(
    hero_name: str,
    gold: int,
    owned_items: list[str],
    enemy_heroes: list[str],
) -> list[dict[str, Any]] | None:
    """Рассчитывает рекомендации по предметам на Rust."""
    lib = _get_lib()
    if lib is None:
        return None

    try:
        hero_b = hero_name.encode("utf-8")
        items_b = json.dumps(owned_items).encode("utf-8")
        enemies_b = json.dumps(enemy_heroes).encode("utf-8")

        ptr = lib.aidota_build_advice_json(hero_b, int(gold), items_b, enemies_b)
        if not ptr:
            return None
        try:
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            if raw is None:
                return None
            return json.loads(raw.decode("utf-8"))
        finally:
            lib.aidota_free_string(ptr)
    except Exception as exc:
        log.warning("Ошибка вызова Rust build_advice: %s", exc)
        return None
