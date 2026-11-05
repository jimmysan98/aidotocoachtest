"""CooldownTrackerSkill: голосовой трекер Рошана и ключевых ультимейтов врага.

Позволяет игроку голосом зафиксировать:
- «Астра, Рошан упал» / «Джарвис, Рошан упал»
- «Астра, Равага ушла», «купол ушел», «блэкхол ушел», «дум ушел» и др.

Автоматически считает время отката по часам GSI и предупреждает за 30 секунд
до респавна/отката способности.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("aidota.skills.cooldown_tracker")

ULTIMATE_COOLDOWNS: dict[str, dict[str, Any]] = {
    "ravage": {"name": "Равага Тайдхантера", "cd": 150},
    "chronosphere": {"name": "Хроносфера Войда", "cd": 160},
    "black_hole": {"name": "Блэкхол Энигмы", "cd": 180},
    "global_silence": {"name": "Глобал Сайленсера", "cd": 130},
    "doom": {"name": "Дум", "cd": 140},
    "echo_slam": {"name": "Эхо Слэм Шейкера", "cd": 110},
    "arena_of_blood": {"name": "Арена Марса", "cd": 90},
}


@dataclass
class UltEntry:
    key: str
    name: str
    start_sec: float
    duration: float
    alerted_30s: bool = False
    is_ready_alerted: bool = False


class CooldownTrackerSkill:
    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self.roshan_death_sec: float | None = None
        self._roshan_aegis_alerted = False
        self._roshan_min_alerted = False
        self._roshan_max_alerted = False
        self.tracked_ults: dict[str, UltEntry] = {}

    def reset(self) -> None:
        self.roshan_death_sec = None
        self._roshan_aegis_alerted = False
        self._roshan_min_alerted = False
        self._roshan_max_alerted = False
        self.tracked_ults.clear()

    # ------------------------------------------------------------------ #
    # Фиксация событий пользователем
    # ------------------------------------------------------------------ #
    def record_roshan(self, clock_seconds: Any = None) -> str:
        """Фиксирует убийство Рошана."""
        sec = self._parse_seconds(clock_seconds)
        self.roshan_death_sec = sec
        self._roshan_aegis_alerted = False
        self._roshan_min_alerted = False
        self._roshan_max_alerted = False
        log.info("Рошан записан на секунде %s", sec)
        return "Рошан убит! Таймер Рошана запущен: Аегис пропадёт через 5 минут, Респаун с 8 до 11 минут. Предупрежу заранее."

    def record_ult(self, ability_key: str | None, clock_seconds: Any = None) -> str:
        """Фиксирует применение ультимейта врагом."""
        sec = self._parse_seconds(clock_seconds)
        raw_key = (ability_key or "ravage").strip().lower()
        hero_to_ult = {
            "tidehunter": "ravage",
            "enigma": "black_hole",
            "faceless_void": "chronosphere",
            "void": "chronosphere",
            "silencer": "global_silence",
            "earthshaker": "echo_slam",
            "shaker": "echo_slam",
            "mars": "arena_of_blood",
        }
        key = hero_to_ult.get(raw_key, raw_key)
        info = ULTIMATE_COOLDOWNS.get(key, {"name": key.title(), "cd": 120})
        name = info["name"]
        cd = float(info["cd"])

        self.tracked_ults[key] = UltEntry(
            key=key,
            name=name,
            start_sec=sec,
            duration=cd,
            alerted_30s=False,
            is_ready_alerted=False,
        )
        log.info("Зафиксирован откат способности %s (%s) на %d сек", key, name, cd)
        return f"{name}: отсчёт запущен на {int(cd)} секунд. Предупрежу за 30 секунд до отката!"

    record_enemy_ult = record_ult

    # ------------------------------------------------------------------ #
    # Проверка таймеров на тиках GSI
    # ------------------------------------------------------------------ #
    def check(self, clock_seconds: Any = None) -> str | None:
        if not self.enabled:
            return None
        sec = self._parse_seconds(clock_seconds)

        # 1. Проверяем Рошана
        if self.roshan_death_sec is not None:
            elapsed = sec - self.roshan_death_sec
            if elapsed < 0:
                # Начался новый матч
                self.reset()
                return None

            # Предупреждение об окончании Аегиса (4:30 = 270 сек)
            if elapsed >= 270 and not self._roshan_aegis_alerted and elapsed < 310:
                self._roshan_aegis_alerted = True
                return "Внимание: через 30 секунд пропадёт Аегис!"

            # Предупреждение за 30 сек до минимального респавна (7:30 = 450 сек)
            if elapsed >= 450 and not self._roshan_min_alerted and elapsed < 490:
                self._roshan_min_alerted = True
                return "Внимание: через 30 секунд возможно появление Рошана!"

            # Предупреждение за 30 сек до гарантированного респавна (10:30 = 630 сек)
            if elapsed >= 630 and not self._roshan_max_alerted and elapsed < 670:
                self._roshan_max_alerted = True
                return "Внимание: через 30 секунд гарантированный респавн Рошана!"

        # 2. Проверяем ультимейты
        completed: list[str] = []
        for key, entry in list(self.tracked_ults.items()):
            elapsed = sec - entry.start_sec
            if elapsed < 0:
                completed.append(key)
                continue

            # Предупреждение за 30 секунд до отката
            if elapsed >= (entry.duration - 30) and not entry.alerted_30s and elapsed < entry.duration:
                entry.alerted_30s = True
                return f"Внимание, через 30 секунд у врага откатится {entry.name}!"

            if elapsed >= entry.duration:
                completed.append(key)

        for key in completed:
            self.tracked_ults.pop(key, None)

        return None

    check_alerts = check

    @staticmethod
    def _parse_seconds(clock_seconds: Any) -> float:
        try:
            if clock_seconds is not None:
                return float(clock_seconds)
        except (ValueError, TypeError):
            pass
        return time.time()
