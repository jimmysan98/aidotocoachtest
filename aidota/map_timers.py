"""Единый канонический источник истины по макро-таймерам карты Dota 2 (Phase 4 / Fork F2).

Модуль объединяет вычисления и edge-triggered оповещения:
- баунти-руны, водные и активные руны, цикл день/ночь и логово Рошана;
- лотосы, руны мудрости, катапульты, отводы (pull) и стаки (stack);
- Торментор, таймер Аегиса/Рошана и разряды нейтральных предметов (7/15/25/35/60 мин + Turbo);
- автоматический сброс состояния при смене ``matchid``;
- единую детекцию 6-го уровня вражеского мидера без ложного ``max(level)`` по сайдам.

Зависимости минимальны (только stdlib + ленивый доступ к ``meta_db`` для ролей героев),
чтобы исключить циклические импорты и обеспечивать Tier 0 задержку ``<= 10 ms``.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("aidota.map_timers")

# --------------------------------------------------------------------------- #
# Канонические периоды и тайминги (в секундах)
# --------------------------------------------------------------------------- #
BOUNTY_PERIOD_SEC = 180
WATER_RUNE_TIMES_SEC: tuple[int, ...] = (120, 240)
ACTIVE_RUNE_START_SEC = 360
ACTIVE_RUNE_PERIOD_SEC = 120
LOTUS_PERIOD_SEC = 180
WISDOM_PERIOD_SEC = 420
CATAPULT_PERIOD_SEC = 300
DAY_NIGHT_PERIOD_SEC = 300
TORMENTOR_SPAWN_SEC = 1200
TORMENTOR_RESPAWN_PERIOD_SEC = 600

ROSHAN_AEGIS_DURATION_SEC = 300
ROSHAN_MIN_RESPAWN_SEC = 480
ROSHAN_MAX_RESPAWN_SEC = 660

# Разряды нейтральных предметов (7/15/25/35/60 мин — обычный режим, 0.5x — Turbo).
NEUTRAL_TIERS_NORMAL: tuple[tuple[int, int, str], ...] = (
    (1, 420, "Седьмая минута — появились нейтральные предметы 1-го разряда! Зачищайте лесные лагеря за жетонами."),
    (2, 900, "Пятнадцатая минута — доступен 2-й разряд нейтральных предметов! Не забудьте обновить артефакт."),
    (3, 1500, "Двадцать пятая минута — доступен 3-й разряд нейтралок!"),
    (4, 2100, "Тридцать пятая минута — доступен 4-й разряд нейтральных предметов!"),
    (5, 3600, "Шестидесятая минута — появился 5-й разряд нейтральных предметов!"),
)

NEUTRAL_TIERS_TURBO: tuple[tuple[int, int, str], ...] = (
    (1, 210, "Четвёртая минута — в Турбо появились нейтральные предметы 1-го разряда! Зачищайте лагеря за жетонами."),
    (2, 450, "Восьмая минута — в Турбо доступен 2-й разряд нейтральных предметов! Обновите артефакт."),
    (3, 750, "Тринадцатая минута — в Турбо доступен 3-й разряд нейтралок!"),
    (4, 1050, "Восемнадцатая минута — в Турбо доступен 4-й разряд нейтральных предметов!"),
    (5, 1800, "Тридцатая минута — в Турбо появился 5-й разряд нейтральных предметов!"),
)

_MID_LANE_VALUES: frozenset[Any] = frozenset({2, "2", "mid", "middle", "midlane"})
_LANE_ALIAS_NUM = {"safe": 1, "bot": 1, "bottom": 1, "mid": 2, "middle": 2, "off": 3, "top": 3}


# --------------------------------------------------------------------------- #
# Чистые хелперы парсинга и форматирования
# --------------------------------------------------------------------------- #
def parse_clock_int(value: Any) -> int | None:
    """Безопасно извлекает целые секунды игрового таймера (отвергает bool)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def parse_clock_float(value: Any) -> float | None:
    """Безопасно извлекает float секунды таймера (отвергает bool)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def is_turbo_mode(game_mode: Any) -> bool:
    """Определяет режим Turbo по числовому коду 23 или строке ``'turbo'``."""
    if game_mode is None or isinstance(game_mode, bool):
        return False
    if isinstance(game_mode, int):
        return game_mode == 23
    text = str(game_mode).strip().lower()
    return text == "23" or "turbo" in text


def format_mmss(seconds: int) -> str:
    """Форматирует неотрицательные секунды как ``MM:SS``."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _format_minute(minutes: float) -> str:
    whole = int(minutes)
    secs = round((minutes - whole) * 60)
    if secs == 60:
        whole += 1
        secs = 0
    return f"{whole}:{secs:02d}"


def _next_occurrence(minute: float, start: float, period: float) -> float:
    if minute <= start:
        return start
    steps = int((minute - start) // period) + 1
    return start + steps * period


def neutral_schedule(turbo: bool = False) -> tuple[tuple[int, int, str], ...]:
    """Возвращает каноническое расписание разрядов нейтральных предметов."""
    return NEUTRAL_TIERS_TURBO if turbo else NEUTRAL_TIERS_NORMAL


def neutral_tier(minute: float, turbo: bool = False) -> int:
    """Номер доступного разряда нейтральных предметов по минуте игры (7/15/25/35/60; 0.5x в Turbo)."""
    if turbo:
        if minute < 7.5:
            return 1
        if minute < 12.5:
            return 2
        if minute < 17.5:
            return 3
        if minute < 30.0:
            return 4
        return 5
    if minute < 15.0:
        return 1
    if minute < 25.0:
        return 2
    if minute < 35.0:
        return 3
    if minute < 60.0:
        return 4
    return 5


def roshan_pit_location(clock_seconds: Any = None, is_daytime: bool | None = None) -> str:
    """Определяет логово Рошана по 300-секундному естественному циклу дня/ночи."""
    sec = parse_clock_int(clock_seconds)
    if sec is not None:
        if sec < 0:
            return "нижнем логове (Radiant, день)"
        cycle = (sec // DAY_NIGHT_PERIOD_SEC) % 2
        return "нижнем логове (Radiant, день)" if cycle == 0 else "верхнем логове (Dire, ночь)"
    if is_daytime is not None:
        return "нижнем логове (Radiant, день)" if is_daytime else "верхнем логове (Dire, ночь)"
    return "нижнем логове (Radiant, день)"


def describe_neutral_schedule(clock_seconds: Any = None, *, turbo: bool = False) -> str:
    """Человекочитаемый голосовой ответ о ближайшем разряде нейтральных предметов."""
    schedule = neutral_schedule(turbo=turbo)
    sec = parse_clock_int(clock_seconds)
    if sec is None or sec < 0:
        if turbo:
            return "В Турбо разряды нейтральных предметов открываются на 3:30, 7:30, 12:30, 17:30 и 30:00."
        return "Разряды нейтральных предметов открываются на 7-й, 15-й, 25-й, 35-й и 60-й минутах."

    mode_prefix = "в Турбо " if turbo else ""
    for tier, unlock_sec, _ in schedule:
        if sec < unlock_sec:
            rem = unlock_sec - sec
            if rem <= 90:
                return f"Нейтральные предметы {tier}-го разряда {mode_prefix}появятся на {_format_minute(unlock_sec / 60)}, через {rem} секунд."
            mins = max(1, (rem + 30) // 60)
            return f"Нейтральные предметы {tier}-го разряда {mode_prefix}появятся на {_format_minute(unlock_sec / 60)}, примерно через {mins} минут."
    return "Открыт максимальный 5-й разряд нейтральных предметов!"


def compute_roshan_state(
    roshan_death_sec: float | int | None,
    clock_seconds: Any = None,
) -> dict[str, Any]:
    """Рассчитывает состояние Аегиса и окна респавна Рошана по времени его убийства."""
    death = parse_clock_float(roshan_death_sec)
    if death is None:
        return {
            "recorded": False,
            "death_sec": None,
            "elapsed_sec": None,
            "aegis_remaining_sec": None,
            "respawn_min_sec": None,
            "respawn_max_sec": None,
            "status_text": "",
        }
    now_sec = parse_clock_float(clock_seconds)
    if now_sec is None:
        return {
            "recorded": True,
            "death_sec": int(death),
            "elapsed_sec": 0,
            "aegis_remaining_sec": ROSHAN_AEGIS_DURATION_SEC,
            "respawn_min_sec": ROSHAN_MIN_RESPAWN_SEC,
            "respawn_max_sec": ROSHAN_MAX_RESPAWN_SEC,
            "status_text": "Аегис 05:00 | Окно 08:00–11:00",
        }

    elapsed = int(now_sec - death)
    if elapsed < 0:
        return {
            "recorded": False,
            "death_sec": None,
            "elapsed_sec": None,
            "aegis_remaining_sec": None,
            "respawn_min_sec": None,
            "respawn_max_sec": None,
            "status_text": "",
        }

    aegis_rem = max(0, ROSHAN_AEGIS_DURATION_SEC - elapsed)
    resp_min = max(0, ROSHAN_MIN_RESPAWN_SEC - elapsed)
    resp_max = max(0, ROSHAN_MAX_RESPAWN_SEC - elapsed)

    if aegis_rem > 0:
        status_text = f"Аегис {format_mmss(aegis_rem)} | Окно {format_mmss(resp_min)}"
    elif resp_min > 0:
        status_text = f"Окно респавна через {format_mmss(resp_min)}"
    elif resp_max > 0:
        status_text = f"Окно респавна ОТКРЫТО (до {format_mmss(resp_max)})"
    else:
        status_text = "Рошан, вероятно, возродился"

    return {
        "recorded": True,
        "death_sec": int(death),
        "elapsed_sec": elapsed,
        "aegis_remaining_sec": aegis_rem,
        "respawn_min_sec": resp_min,
        "respawn_max_sec": resp_max,
        "status_text": status_text,
    }


def _periodic_remaining(sec: int, period: int) -> int:
    if sec < 0:
        return period
    rem = period - (sec % period)
    return rem


def upcoming_objectives(
    clock_seconds: Any,
    *,
    window_sec: int = 45,
    turbo: bool = False,
    roshan_death_sec: float | int | None = None,
) -> list[dict[str, Any]]:
    """Возвращает ключевые объективы карты, до которых осталось <= ``window_sec`` секунд."""
    sec = parse_clock_int(clock_seconds)
    if sec is None or sec < 0:
        return []
    win = max(1, int(window_sec))
    events: list[dict[str, Any]] = []

    def _add(key: str, label: str, in_sec: int | None) -> None:
        if in_sec is not None and 0 <= in_sec <= win:
            events.append({"key": key, "label": label, "in_sec": int(in_sec)})

    # Водные руны (2:00 и 4:00)
    for t_water in WATER_RUNE_TIMES_SEC:
        if sec <= t_water:
            _add("water", "водные руны на миду", t_water - sec)
            break

    # Активные руны (с 6:00 каждые 2 мин)
    if sec <= ACTIVE_RUNE_START_SEC:
        _add("active_rune", "первая руна силы", ACTIVE_RUNE_START_SEC - sec)
    else:
        rem_active = ACTIVE_RUNE_PERIOD_SEC - ((sec - ACTIVE_RUNE_START_SEC) % ACTIVE_RUNE_PERIOD_SEC)
        if rem_active == ACTIVE_RUNE_PERIOD_SEC:
            rem_active = 0
        _add("active_rune", "активная руна", rem_active)

    # Лотосы и баунти (каждые 3 мин)
    rem_3m = 0 if (sec > 0 and sec % LOTUS_PERIOD_SEC == 0) else _periodic_remaining(sec, LOTUS_PERIOD_SEC)
    _add("lotus", "лотосы в пруду", rem_3m)
    _add("bounty", "руны богатства", rem_3m)

    # Руны мудрости (каждые 7 мин)
    rem_wisdom = 0 if (sec > 0 and sec % WISDOM_PERIOD_SEC == 0) else _periodic_remaining(sec, WISDOM_PERIOD_SEC)
    _add("wisdom", "руны мудрости", rem_wisdom)

    # Катапульта и смена дня/ночи (каждые 5 мин)
    rem_5m = 0 if (sec > 0 and sec % DAY_NIGHT_PERIOD_SEC == 0) else _periodic_remaining(sec, DAY_NIGHT_PERIOD_SEC)
    _add("catapult", "волна катапульт", rem_5m)
    next_cycle = ((sec // DAY_NIGHT_PERIOD_SEC) + 1) if rem_5m > 0 else (sec // DAY_NIGHT_PERIOD_SEC)
    phase_target = "ночь" if (next_cycle % 2 == 1) else "день"
    _add("day_night", f"смена на {phase_target}", rem_5m)

    # Торментор (20:00)
    if sec <= TORMENTOR_SPAWN_SEC:
        _add("tormentor", "Терзатель", TORMENTOR_SPAWN_SEC - sec)

    # Разряды нейтральных предметов
    for tier, unlock_sec, _ in neutral_schedule(turbo=turbo):
        if sec <= unlock_sec:
            _add("neutral_tier", f"нейтралки Тир {tier}", unlock_sec - sec)
            break

    # Окно Рошана (если зафиксировано)
    roshan = compute_roshan_state(roshan_death_sec, sec)
    if roshan["recorded"]:
        aegis_rem = roshan["aegis_remaining_sec"]
        resp_min = roshan["respawn_min_sec"]
        if isinstance(aegis_rem, int) and 0 < aegis_rem <= win:
            _add("roshan_aegis", "истечение Аегиса", aegis_rem)
        if isinstance(resp_min, int) and 0 <= resp_min <= win:
            _add("roshan_respawn", "окно респавна Рошана", resp_min)

    events.sort(key=lambda item: (int(item["in_sec"]), str(item["key"])))
    return events


def compute_map_timers(
    clock_seconds: Any,
    *,
    turbo: bool = False,
    roshan_death_sec: float | int | None = None,
    is_daytime: bool | None = None,
    window_sec: int = 45,
) -> dict[str, Any]:
    """Вычисляет канонический срез всех таймеров карты для WebUI, контекста и промпта."""
    sec = parse_clock_int(clock_seconds)
    roshan = compute_roshan_state(roshan_death_sec, sec)
    pit = roshan_pit_location(sec, is_daytime=is_daytime)
    if sec is None:
        return {
            "available": False,
            "clock_seconds": None,
            "turbo": bool(turbo),
            "is_daytime": True if is_daytime is None else bool(is_daytime),
            "phase_label": "День",
            "day_night_remaining_sec": DAY_NIGHT_PERIOD_SEC,
            "bounty_remaining_sec": BOUNTY_PERIOD_SEC,
            "water_remaining_sec": WATER_RUNE_TIMES_SEC[0],
            "active_rune_remaining_sec": ACTIVE_RUNE_START_SEC,
            "active_rune_period_sec": ACTIVE_RUNE_START_SEC,
            "wisdom_remaining_sec": WISDOM_PERIOD_SEC,
            "wisdom_period_sec": WISDOM_PERIOD_SEC,
            "lotus_remaining_sec": LOTUS_PERIOD_SEC,
            "lotus_period_sec": LOTUS_PERIOD_SEC,
            "catapult_remaining_sec": CATAPULT_PERIOD_SEC,
            "stack_remaining_sec": 53,
            "pull_remaining_sec": 17,
            "tormentor_remaining_sec": TORMENTOR_SPAWN_SEC,
            "tormentor_available": False,
            "neutral_current_tier": 0,
            "neutral_next_tier": 1,
            "neutral_next_remaining_sec": 210 if turbo else 420,
            "neutral_period_sec": 210 if turbo else 420,
            "neutral_label": "03:30 (Тир 1)" if turbo else "07:00 (Тир 1)",
            "roshan_pit": pit,
            "roshan": roshan,
            "upcoming_30_45s": [],
        }

    if sec < 0:
        horn = abs(sec)
        first_tier_sec = neutral_schedule(turbo=turbo)[0][1]
        return {
            "available": True,
            "clock_seconds": sec,
            "turbo": bool(turbo),
            "is_daytime": True,
            "phase_label": "До горна",
            "day_night_remaining_sec": horn + DAY_NIGHT_PERIOD_SEC,
            "bounty_remaining_sec": horn,
            "water_remaining_sec": horn + WATER_RUNE_TIMES_SEC[0],
            "active_rune_remaining_sec": horn + ACTIVE_RUNE_START_SEC,
            "active_rune_period_sec": ACTIVE_RUNE_START_SEC,
            "wisdom_remaining_sec": horn + WISDOM_PERIOD_SEC,
            "wisdom_period_sec": WISDOM_PERIOD_SEC,
            "lotus_remaining_sec": horn + LOTUS_PERIOD_SEC,
            "lotus_period_sec": LOTUS_PERIOD_SEC,
            "catapult_remaining_sec": horn + CATAPULT_PERIOD_SEC,
            "stack_remaining_sec": horn + 53,
            "pull_remaining_sec": horn + 17,
            "tormentor_remaining_sec": horn + TORMENTOR_SPAWN_SEC,
            "tormentor_available": False,
            "neutral_current_tier": 0,
            "neutral_next_tier": 1,
            "neutral_next_remaining_sec": horn + first_tier_sec,
            "neutral_period_sec": first_tier_sec,
            "neutral_label": f"{format_mmss(first_tier_sec)} (Тир 1)",
            "roshan_pit": pit,
            "roshan": roshan,
            "upcoming_30_45s": [{"key": "bounty", "label": "начальные баунти-руны", "in_sec": horn}] if horn <= window_sec else [],
        }

    cycle_5m = sec // DAY_NIGHT_PERIOD_SEC
    natural_day = cycle_5m % 2 == 0
    day_night_rem = DAY_NIGHT_PERIOD_SEC - (sec % DAY_NIGHT_PERIOD_SEC)
    bounty_rem = _periodic_remaining(sec, BOUNTY_PERIOD_SEC)
    lotus_rem = _periodic_remaining(sec, LOTUS_PERIOD_SEC)
    wisdom_rem = _periodic_remaining(sec, WISDOM_PERIOD_SEC)
    catapult_rem = _periodic_remaining(sec, CATAPULT_PERIOD_SEC)

    water_rem: int | None = None
    for t_w in WATER_RUNE_TIMES_SEC:
        if sec < t_w:
            water_rem = t_w - sec
            break

    if sec < ACTIVE_RUNE_START_SEC:
        active_rem = ACTIVE_RUNE_START_SEC - sec
        active_period = ACTIVE_RUNE_START_SEC
    else:
        active_rem = ACTIVE_RUNE_PERIOD_SEC - ((sec - ACTIVE_RUNE_START_SEC) % ACTIVE_RUNE_PERIOD_SEC)
        active_period = ACTIVE_RUNE_PERIOD_SEC

    sec_in_min = sec % 60
    stack_rem = (53 - sec_in_min) if sec_in_min <= 53 else (60 - sec_in_min + 53)
    if sec_in_min <= 17:
        pull_rem = 17 - sec_in_min
    elif sec_in_min <= 47:
        pull_rem = 47 - sec_in_min
    else:
        pull_rem = 60 - sec_in_min + 17

    tormentor_rem = max(0, TORMENTOR_SPAWN_SEC - sec)
    tormentor_available = sec >= TORMENTOR_SPAWN_SEC

    schedule = neutral_schedule(turbo=turbo)
    curr_tier = 0
    prev_unlock = 0
    next_tier: int | None = None
    next_rem = 0
    neutral_period = schedule[0][1]
    for tier, unlock_sec, _ in schedule:
        if sec >= unlock_sec:
            curr_tier = tier
            prev_unlock = unlock_sec
        else:
            next_tier = tier
            next_rem = unlock_sec - sec
            neutral_period = max(1, unlock_sec - prev_unlock)
            break

    if next_tier is None:
        neutral_label = "Тир 5 (Макс)"
        neutral_period = 1
    else:
        neutral_label = f"{format_mmss(next_rem)} (Тир {next_tier})"

    return {
        "available": True,
        "clock_seconds": sec,
        "turbo": bool(turbo),
        "is_daytime": natural_day,
        "phase_label": "День" if natural_day else "Ночь",
        "day_night_remaining_sec": day_night_rem,
        "bounty_remaining_sec": bounty_rem,
        "water_remaining_sec": water_rem,
        "active_rune_remaining_sec": active_rem,
        "active_rune_period_sec": active_period,
        "wisdom_remaining_sec": wisdom_rem,
        "wisdom_period_sec": WISDOM_PERIOD_SEC,
        "lotus_remaining_sec": lotus_rem,
        "lotus_period_sec": LOTUS_PERIOD_SEC,
        "catapult_remaining_sec": catapult_rem,
        "stack_remaining_sec": stack_rem,
        "pull_remaining_sec": pull_rem,
        "tormentor_remaining_sec": tormentor_rem,
        "tormentor_available": tormentor_available,
        "neutral_current_tier": curr_tier,
        "neutral_next_tier": next_tier,
        "neutral_next_remaining_sec": next_rem,
        "neutral_period_sec": neutral_period,
        "neutral_label": neutral_label,
        "roshan_pit": pit,
        "roshan": roshan,
        "upcoming_30_45s": upcoming_objectives(
            sec, window_sec=window_sec, turbo=turbo, roshan_death_sec=roshan_death_sec
        ),
    }


def format_objectives_window(
    clock_seconds: Any,
    *,
    window_sec: int = 45,
    turbo: bool = False,
    roshan_death_sec: float | int | None = None,
    is_daytime: bool | None = None,
) -> str:
    """Компактная строка ближайших объективов на 30–45 сек + статус Рошана для промпта."""
    sec = parse_clock_int(clock_seconds)
    if sec is None or sec < 0:
        return ""
    timers = compute_map_timers(
        sec,
        turbo=turbo,
        roshan_death_sec=roshan_death_sec,
        is_daytime=is_daytime,
        window_sec=window_sec,
    )
    soon = timers.get("upcoming_30_45s") or []
    parts: list[str] = []
    for item in soon[:3]:
        in_sec = int(item.get("in_sec", 0))
        label = str(item.get("label") or "")
        if label:
            parts.append(f"{label} сейчас" if in_sec == 0 else f"{label} через {in_sec}с")
    roshan = timers.get("roshan") or {}
    if roshan.get("recorded") and roshan.get("status_text"):
        parts.append(f"Рошан ({timers['roshan_pit']}): {roshan['status_text']}")
    else:
        parts.append(f"Рошан в {timers['roshan_pit']}")
    return "; ".join(parts)


def objectives_context(
    clock_seconds: Any,
    game_mode: Any = None,
    *,
    roshan_death_sec: float | int | None = None,
    window_sec: int = 45,
) -> str:
    """Единый текст целей карты для GSI-контекста и тактического брифа."""
    sec = parse_clock_int(clock_seconds)
    if sec is None or sec < 0:
        return ""
    minute = sec / 60.0
    turbo = is_turbo_mode(game_mode)
    wisdom = _next_occurrence(minute, 7, 7)
    lotus = _next_occurrence(minute, 3, 3)
    tormentor = _next_occurrence(minute, 20, 10)
    pit = roshan_pit_location(sec)
    parts = [
        f"руны мудрости {_format_minute(wisdom)}",
        f"лотусы {_format_minute(lotus)}",
        f"Терзатель {_format_minute(tormentor)}",
    ]
    roshan = compute_roshan_state(roshan_death_sec, sec)
    if roshan["recorded"] and roshan["status_text"]:
        parts.append(f"Рошан (в {pit}): {roshan['status_text']}")
    elif minute < 15:
        parts.append(f"Рошан (в {pit}): первый Аегис примерно с 15:00")
    else:
        parts.append(f"Рошан (в {pit}): возможно, жив (Аегис/Рефрешер)")

    soon = upcoming_objectives(sec, window_sec=window_sec, turbo=turbo, roshan_death_sec=roshan_death_sec)
    soon_bits = [
        f"{item['label']} ({item['in_sec']}с)"
        for item in soon[:3]
        if item.get("label")
    ]
    text = "Ближайшие цели — " + ", ".join(parts) + "."
    if soon_bits:
        text += f" В окне {window_sec}с: {', '.join(soon_bits)}."
    return text


# --------------------------------------------------------------------------- #
# matchid и корректное определение 6-го уровня вражеского мидера
# --------------------------------------------------------------------------- #
def extract_match_id(payload: Any) -> str | None:
    """Извлекает стабильный идентификатор матча из GSI (``map.matchid``) или None."""
    if not isinstance(payload, Mapping):
        return None
    map_data = payload.get("map")
    if not isinstance(map_data, Mapping):
        return None
    raw = map_data.get("matchid")
    if raw is None or isinstance(raw, bool):
        return None
    clean = str(raw).strip()
    if not clean or clean in ("0", "none", "null"):
        return None
    return clean


def _team_from_player_entry(entry: Mapping[str, Any]) -> str | None:
    hero_raw = entry.get("hero")
    hero_map: Mapping[str, Any] = hero_raw if isinstance(hero_raw, Mapping) else {}
    for src in (entry, hero_map):
        t = src.get("team") or src.get("team_name")
        if isinstance(t, str):
            low = t.strip().lower()
            if low in ("radiant", "dire"):
                return low
        t_num = parse_clock_int(t)
        if t_num == 2:
            return "radiant"
        if t_num == 3:
            return "dire"
        slot = parse_clock_int(src.get("player_slot"))
        if slot is not None:
            if 0 <= slot <= 4:
                return "radiant"
            if 128 <= slot <= 132:
                return "dire"
    return None


def _is_explicit_mid_entry(entry: Mapping[str, Any]) -> tuple[bool, bool]:
    """Возвращает ``(has_lane_metadata, is_mid)`` для записи игрока."""
    hero_raw = entry.get("hero")
    hero_dict: Mapping[str, Any] = hero_raw if isinstance(hero_raw, Mapping) else {}
    has_meta = False

    for src in (entry, hero_dict):
        if "is_mid" in src and src.get("is_mid") is not None:
            has_meta = True
            if bool(src.get("is_mid")):
                return True, True
        lane = src.get("lane")
        if lane is not None:
            has_meta = True
            if isinstance(lane, bool):
                continue
            if lane in _MID_LANE_VALUES:
                return True, True
            if isinstance(lane, str) and lane.strip().lower() in _LANE_ALIAS_NUM:
                if _LANE_ALIAS_NUM[lane.strip().lower()] == 2:
                    return True, True
        pos = src.get("position")
        if pos is not None and not isinstance(pos, bool):
            has_meta = True
            if parse_clock_int(pos) == 2:
                return True, True
    return has_meta, False


def _hero_mid_score(hero_npc: str | None) -> int:
    """Ранг мид-пригодности героя: 2 = основной мидер, 1 = флекс, 0 = не мидер."""
    if not hero_npc:
        return 0
    key = re.sub(r"^npc_dota_hero_", "", str(hero_npc).strip().lower())
    if not key:
        return 0
    try:
        from . import meta_db

        meta = meta_db.META_HEROES_DB.get(key)
        if isinstance(meta, Mapping):
            roles = [int(r) for r in (meta.get("roles") or [])]
            if roles and roles[0] == 2:
                return 2
            if 2 in roles:
                return 1
            return 0
    except Exception:  # noqa: BLE001
        return 0
    return 0


def _collect_raw_players(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_groups: list[Any] = []
    if payload.get("allplayers") is not None:
        raw_groups.append(payload.get("allplayers"))
    players = payload.get("players")
    if isinstance(players, Mapping):
        raw_groups.append(players.get("all_players"))
        raw_groups.append(players.get("allplayers"))
    entries: list[Mapping[str, Any]] = []
    for group in raw_groups:
        if isinstance(group, Mapping):
            entries.extend(v for v in group.values() if isinstance(v, Mapping))
        elif isinstance(group, list):
            entries.extend(v for v in group if isinstance(v, Mapping))
        if entries:
            break
    return entries


def detect_enemy_mid_level(payload: Any, team_name: str | None = None) -> int | None:
    """Определяет уровень вражеского мидера без ложного ``max(level)`` по всем врагам.

    Правила:
    1. Если у врагов в GSI/снапшоте есть метаданные линии (``lane``, ``is_mid``,
       ``position``) — учитываем ТОЛЬКО игрока(ов) с ролью/линией мида; боковые
       лайнеры уровня 6+ никогда не дают ложного срабатывания.
    2. Если метаданных линии нет — выбираем вражеского героя с мидовой ролью
       по канонической базе ``meta_db`` (приоритет основному мидеру ``roles[0] == 2``,
       затем флексу ``2 in roles``), не беря ``max(level)`` у керри или саппортов.
    """
    if not isinstance(payload, Mapping):
        return None
    my_team = str(team_name or "").strip().lower()
    if my_team not in ("radiant", "dire"):
        player = payload.get("player")
        if isinstance(player, Mapping):
            p_team = str(player.get("team_name") or "").strip().lower()
            if p_team in ("radiant", "dire"):
                my_team = p_team
    if my_team not in ("radiant", "dire"):
        return None

    enemies: list[tuple[str | None, int, bool, bool]] = []
    any_lane_meta = False
    for entry in _collect_raw_players(payload):
        entry_team = _team_from_player_entry(entry)
        if not entry_team or entry_team == my_team:
            continue
        hero = entry.get("hero")
        hero_dict = hero if isinstance(hero, Mapping) else {}
        lvl = parse_clock_int(entry.get("level"))
        if lvl is None:
            lvl = parse_clock_int(hero_dict.get("level"))
        if lvl is None:
            continue
        npc_raw = hero if isinstance(hero, str) else hero_dict.get("name")
        npc = str(npc_raw or entry.get("hero_name") or entry.get("npc") or "").strip() or None
        has_meta, is_mid = _is_explicit_mid_entry(entry)
        if has_meta:
            any_lane_meta = True
        enemies.append((npc, lvl, has_meta, is_mid))

    if not enemies:
        return None

    if any_lane_meta:
        mid_levels = [lvl for _npc, lvl, _has_meta, is_mid in enemies if is_mid]
        return max(mid_levels) if mid_levels else None

    primary_mids = [lvl for npc, lvl, _hm, _im in enemies if _hero_mid_score(npc) == 2]
    if primary_mids:
        return max(primary_mids)
    flex_mids = [lvl for npc, lvl, _hm, _im in enemies if _hero_mid_score(npc) >= 1]
    if flex_mids:
        return max(flex_mids)
    return None


# --------------------------------------------------------------------------- #
# Единый stateful трекер таймеров карты
# --------------------------------------------------------------------------- #
@dataclass
class CanonicalMapTimers:
    """Потокобезопасный единый трекер алертов карты для ``MacroTimerSkill`` и ``MidTimersTracker``."""

    enabled: bool = True
    proactive_enabled: bool = True
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _match_id: str | None = field(default=None, init=False)
    _last_clock_sec: int | None = field(default=None, init=False)
    _last_alert_clock_sec: int | None = field(default=None, init=False)
    _alerted_water_runes: set[int] = field(default_factory=set, init=False)
    _alerted_active_runes: set[int] = field(default_factory=set, init=False)
    _alerted_wisdom: set[int] = field(default_factory=set, init=False)
    _alerted_lotus_bounty: set[int] = field(default_factory=set, init=False)
    _alerted_catapults: set[int] = field(default_factory=set, init=False)
    _alerted_stacks: set[int] = field(default_factory=set, init=False)
    _alerted_pulls: set[tuple[int, str]] = field(default_factory=set, init=False)
    _alerted_neutrals: set[int] = field(default_factory=set, init=False)
    _last_neutral_tier_seen: int = field(default=0, init=False)
    _alerted_day_night: set[int] = field(default_factory=set, init=False)
    _alerted_tormentor: bool = field(default=False, init=False)
    _alerted_mid_lvl6: bool = field(default=False, init=False)
    _custom_timers: list[tuple[int, str]] = field(default_factory=list, init=False)

    def reset(self, *, keep_match_id: bool = False) -> None:
        """Сбрасывает историю всех сработавших алертов (при смене матча или вручную)."""
        with self._lock:
            if not keep_match_id:
                self._match_id = None
            self._last_clock_sec = None
            self._last_alert_clock_sec = None
            self._alerted_water_runes.clear()
            self._alerted_active_runes.clear()
            self._alerted_wisdom.clear()
            self._alerted_lotus_bounty.clear()
            self._alerted_catapults.clear()
            self._alerted_stacks.clear()
            self._alerted_pulls.clear()
            self._alerted_neutrals.clear()
            self._last_neutral_tier_seen = 0
            self._alerted_day_night.clear()
            self._alerted_tormentor = False
            self._alerted_mid_lvl6 = False
            self._custom_timers.clear()

    def sync_match_id(self, match_id: Any) -> bool:
        """Синхронизирует ``match_id`` и автоматически сбрасывает состояние при новом матче."""
        if match_id is None or isinstance(match_id, bool):
            return False
        clean = str(match_id).strip()
        if not clean or clean in ("0", "none", "null"):
            return False
        with self._lock:
            if self._match_id is not None and clean != self._match_id:
                self.reset(keep_match_id=False)
                self._match_id = clean
                log.info("Смена matchid -> %s: состояние таймеров сброшено.", clean)
                return True
            if self._match_id is None:
                self._match_id = clean
            return False

    def add_custom_timer(self, target_seconds: int, label: str) -> None:
        """Добавляет пользовательский таймер по игровому времени."""
        with self._lock:
            self._custom_timers.append((int(target_seconds), str(label).strip()))

    def _has_any_alerts(self) -> bool:
        return bool(
            self._alerted_water_runes
            or self._alerted_active_runes
            or self._alerted_wisdom
            or self._alerted_lotus_bounty
            or self._alerted_catapults
            or self._alerted_stacks
            or self._alerted_pulls
            or self._alerted_neutrals
            or self._alerted_day_night
            or self._alerted_tormentor
            or self._alerted_mid_lvl6
        )

    @property
    def last_alert_clock_sec(self) -> int | None:
        return self._last_alert_clock_sec

    def _emit_alert_locked(self, sec: int, text: str) -> str:
        self._last_alert_clock_sec = sec
        return text

    def _check_custom_locked(self, sec: int) -> str | None:
        if not self._custom_timers:
            return None
        for idx, (target_sec, label) in enumerate(list(self._custom_timers)):
            if sec >= target_sec:
                self._custom_timers.pop(idx)
                return self._emit_alert_locked(sec, f"Напоминание: {label}!")
        return None

    def check_mid_runes(
        self,
        clock_seconds: Any,
        position: int = 1,
        *,
        match_id: Any = None,
    ) -> str | None:
        """Проверяет водные и активные руны (за 20 сек до спавна)."""
        if not self.enabled:
            return None
        if match_id is not None:
            self.sync_match_id(match_id)
        sec = parse_clock_int(clock_seconds)
        if sec is None:
            return None
        with self._lock:
            if sec < 0:
                if self._has_any_alerts():
                    self.reset(keep_match_id=True)
                return None

            # 1. Водные руны (2:00 = 120с, 4:00 = 240с -> за 20с: 100с, 220с)
            for target_sec in (100, 220):
                if target_sec - 2 <= sec <= target_sec + 4:
                    if target_sec not in self._alerted_water_runes:
                        self._alerted_water_runes.add(target_sec)
                        log.info("Сработал таймер: водные руны на миду (%d с, pos=%d)", target_sec, position)
                        if position == 4:
                            return self._emit_alert_locked(sec, "Через 20 секунд водные руны на миду — четвёрка, сместись и помоги мидеру законтролить руну.")
                        if position == 5:
                            return self._emit_alert_locked(sec, "Через 20 секунд водные руны на миду — проверь, нужна ли помощь мидеру.")
                        return self._emit_alert_locked(sec, "Через 20 секунд водные руны на миду, запушь пачку.")

            # 2. Активные руны (6:00 = 360с, каждые 2 мин -> за 20с: 340с, 460с...)
            if sec >= 338:
                cycle = (sec + 22) // ACTIVE_RUNE_PERIOD_SEC
                target_sec = cycle * ACTIVE_RUNE_PERIOD_SEC - 20
                if cycle >= 3 and target_sec - 2 <= sec <= target_sec + 4:
                    if cycle not in self._alerted_active_runes:
                        self._alerted_active_runes.add(cycle)
                        log.info("Сработал таймер: активная руна на миду (цикл %d, %d с, pos=%d)", cycle, sec, position)
                        if cycle == 3:
                            self._alerted_catapults.add(1)
                            if position in (4, 5):
                                return self._emit_alert_locked(sec, "Через 20 секунд активная руна на миду и ночная катапульта! Саппортам пора помочь на руне.")
                            return self._emit_alert_locked(sec, "Через 20 секунд активная руна на миду и ночная катапульта! Приготовься к пушу или гангу.")
                        if position in (4, 5):
                            return self._emit_alert_locked(sec, "Через 20 секунд активная руна на реке — помогите законтролить спавн.")
                        return self._emit_alert_locked(sec, "Через 20 секунд активная руна на миду, проконтролируй спавн.")
            return None

    def check_mid_level_6(
        self,
        payload: Mapping[str, Any] | None,
        clock_seconds: Any = None,
        vision_level: int | None = None,
        clock_sec: Any = None,
        *,
        enemy_mid_level: int | None = None,
        match_id: Any = None,
        allow_predictive: bool = True,
    ) -> str | None:
        """Детектор 6-го уровня вражеского мидера (GSI, Vision или тайминг 5:10–5:35)."""
        if not self.enabled:
            return None
        eff_match = match_id if match_id is not None else extract_match_id(payload)
        if eff_match is not None:
            self.sync_match_id(eff_match)

        with self._lock:
            if self._alerted_mid_lvl6:
                return None

            if vision_level and vision_level >= 6:
                self._alerted_mid_lvl6 = True
                log.info("Сработал детектор (Vision): вражеский мидер достиг %d уровня", vision_level)
                return "Внимание! Вражеский мидер достиг шестого уровня! Возможен ганг на боковых линиях."

            if enemy_mid_level is not None and enemy_mid_level >= 6:
                self._alerted_mid_lvl6 = True
                log.info("Сработал детектор: вражеский мидер достиг %d уровня", enemy_mid_level)
                return "Внимание! Вражеский мидер достиг шестого уровня! Возможен ганг на боковых линиях."

            if payload is None:
                sec_only = parse_clock_int(clock_seconds if clock_seconds is not None else clock_sec)
                if sec_only is not None and 329 <= sec_only <= 334:
                    self._alerted_mid_lvl6 = True
                    log.info("Сработал таймер: приближение 6-й минуты (мидер 6 lvl + руна)")
                    return "Внимание: шестая минута игры! Вражеский мидер получает шестой уровень и идёт за руной. Играйте осторожно!"
                return None

            if not isinstance(payload, Mapping):
                return None

            map_data = payload.get("map")
            sec = parse_clock_int(clock_seconds if clock_seconds is not None else clock_sec)
            if sec is None and isinstance(map_data, Mapping):
                sec = parse_clock_int(map_data.get("clock_time"))
            if sec is None or sec < 0 or sec > 600:
                return None

            detected_mid = detect_enemy_mid_level(payload)
            if detected_mid is not None:
                if detected_mid >= 6:
                    self._alerted_mid_lvl6 = True
                    log.info("Сработал детектор (GSI): вражеский мидер достиг %d уровня", detected_mid)
                    return self._emit_alert_locked(sec, "Внимание! Вражеский мидер достиг шестого уровня! Возможен ганг на боковых линиях.")
                # Вражеский мидер виден в GSI и его уровень < 6 — не шумим предиктивом.
                return None

            if not allow_predictive:
                return None

            hero_raw = payload.get("hero")
            my_hero: Mapping[str, Any] = hero_raw if isinstance(hero_raw, Mapping) else {}
            my_lvl = parse_clock_int(my_hero.get("level")) or 0
            exp_pct = parse_clock_float(my_hero.get("experience_percent")) or parse_clock_float(my_hero.get("experience")) or 0.0
            exp_ready = (exp_pct >= 65.0) if exp_pct > 1.0 else (exp_pct >= 0.65)

            is_mid_ally = my_lvl == 5 and exp_ready
            is_standard_mid_window = 310 <= sec <= 335
            player_raw = payload.get("player")
            player: Mapping[str, Any] = player_raw if isinstance(player_raw, Mapping) else {}
            kills = parse_clock_int(player.get("kills")) or 0
            deaths = parse_clock_int(player.get("deaths")) or 0
            is_fast_mid_window = (285 <= sec <= 300) and (kills + deaths >= 1)

            if is_mid_ally or is_standard_mid_window or is_fast_mid_window:
                self._alerted_mid_lvl6 = True
                log.info("Сработал предиктивный детектор 6 уровня мидера (sec=%d, lvl=%d)", sec, my_lvl)
                return self._emit_alert_locked(sec, "Осторожно! Вражеский мидер сейчас получает шестой уровень, берегись ульты!")
            return None

    def _edge_neutral_alert_locked(self, sec: int, turbo: bool) -> str | None:
        schedule = neutral_schedule(turbo=turbo)
        prev_sec = self._last_clock_sec
        self._last_clock_sec = sec

        highest_unlocked = 0
        triggered_text: str | None = None
        for tier, unlock_sec, text in schedule:
            if sec < unlock_sec:
                continue
            highest_unlocked = tier
            if tier in self._alerted_neutrals:
                continue
            if prev_sec is None:
                # Холодный старт в окне разблокировки тира (напр., 420 / 901 / Turbo 211):
                # если мы сильно позже тира (середина игры), отмечаем без ложного позднего алерта.
                if sec <= unlock_sec + 15:
                    triggered_text = text
            elif sec <= unlock_sec + 6 or (prev_sec < unlock_sec <= sec and (sec - prev_sec) <= 15):
                # Срабатывает ровно раз на пороге разблокировки (в т.ч. при кратковременном лаге GSI <= 15с),
                # но большие скачки времени далеко за окном не спамят задним числом.
                triggered_text = text

        if highest_unlocked > self._last_neutral_tier_seen:
            for tier, _, _ in schedule:
                if tier <= highest_unlocked:
                    self._alerted_neutrals.add(tier)
            self._last_neutral_tier_seen = highest_unlocked

        if triggered_text:
            log.info("Сработал edge-таймер нейтральных предметов: Тир %d (sec=%d, turbo=%s)", highest_unlocked, sec, turbo)
        return triggered_text

    def _check_tormentor_locked(self, sec: int) -> str | None:
        if 1178 <= sec <= 1184 and not self._alerted_tormentor:
            self._alerted_tormentor = True
            log.info("Сработал макро-таймер: появление Терзателя")
            return self._emit_alert_locked(sec, "Через 20 секунд появится Терзатель на двадцатой минуте! Соберитесь за бесплатным Шардом.")
        return None

    def check_macro_timers(
        self,
        clock_seconds: Any,
        position: int = 1,
        team_name: str | None = None,
        turbo: bool = False,
        *,
        match_id: Any = None,
    ) -> str | None:
        """Проверяет Торментора, мудрость, лотосы+баунти, пуллы, стаки, нейтралки, катапульты и день/ночь."""
        if not self.enabled:
            return None
        if match_id is not None:
            self.sync_match_id(match_id)
        sec = parse_clock_int(clock_seconds)
        if sec is None or sec < 0:
            return None

        with self._lock:
            # 1. Торментор: 20:00 (1200с), предупреждение за 20с (1180с)
            tormentor_msg = self._check_tormentor_locked(sec)
            if tormentor_msg:
                return tormentor_msg

            # 2. Руны мудрости: каждые 7 минут (420с), предупреждение за 20–25с
            if sec >= 390:
                cycle = (sec + 30) // WISDOM_PERIOD_SEC
                target_sec = cycle * WISDOM_PERIOD_SEC - 20
                if cycle >= 1 and target_sec - 10 <= sec <= target_sec + 4:
                    if cycle not in self._alerted_wisdom:
                        self._alerted_wisdom.add(cycle)
                        min_num = cycle * 7
                        log.info("Сработал макро-таймер: руны мудрости (цикл %d, sec=%d, pos=%d)", cycle, sec, position)
                        if position in (4, 5):
                            return self._emit_alert_locked(sec, f"Через 20 секунд руны мудрости на {min_num}-й минуте! Саппорты 4 и 5, смещайтесь за руной, можно украсть вражескую.")
                        return self._emit_alert_locked(sec, f"Через 20 секунд руны мудрости на {min_num}-й минуте! Не забудьте забрать.")

            # 3. Лотосы и руны богатства (баунти): каждые 3 минуты (180с), предупреждение за 20с
            if sec >= 155:
                cycle = (sec + 22) // LOTUS_PERIOD_SEC
                target_sec = cycle * LOTUS_PERIOD_SEC - 20
                if cycle >= 1 and target_sec - 3 <= sec <= target_sec + 4:
                    if cycle not in self._alerted_lotus_bounty:
                        self._alerted_lotus_bounty.add(cycle)
                        log.info("Сработал макро-таймер: лотосы + баунти (цикл %d, sec=%d, pos=%d)", cycle, sec, position)
                        if position in (4, 5):
                            return self._emit_alert_locked(sec, "Через 20 секунд появятся лотосы в пруду и руны богатства — саппорт, забери бесплатный лотус и баунти!")
                        return self._emit_alert_locked(sec, "Через 20 секунд появятся лотосы в пруду и руны богатства.")

            # 4. Отводы крипов для позиций 4 и 5 во время стадии линии (1:00 — 8:00)
            if position in (4, 5) and 60 <= sec <= 480:
                minute = sec // 60
                sec_in_min = sec % 60
                team_clean = (team_name or "").strip().lower()
                team_label = "Свет" if "radiant" in team_clean else ("Тьма" if "dire" in team_clean else "")

                if 11 <= sec_in_min <= 15:
                    pull_key = (minute, "pull_17")
                    if pull_key not in self._alerted_pulls:
                        self._alerted_pulls.add(pull_key)
                        log.info("Сработал таймер: отвод на 17-й секунде (мин %d, pos %d)", minute, position)
                        if position == 5:
                            prefix = f"{team_label}, пятёрка" if team_label else "Пятёрка"
                            return self._emit_alert_locked(sec, f"{prefix}: 15-я секунда — если линия пушится, готовь отвод на малый лагерь на 17-й секунде!")
                        prefix = f"{team_label}, четвёрка" if team_label else "Четвёрка"
                        return self._emit_alert_locked(sec, f"{prefix}: 15-я секунда — готовь отвод большого лагеря на 17-й секунде, чтобы держать линию у вышки.")

                if 36 <= sec_in_min <= 45:
                    pull_key = (minute, "pull_47")
                    if pull_key not in self._alerted_pulls:
                        self._alerted_pulls.add(pull_key)
                        log.info("Сработал таймер: отвод на 47-й секунде (мин %d, pos %d)", minute, position)
                        if position == 5:
                            prefix = f"{team_label}, пятёрка" if team_label else "Пятёрка"
                            return self._emit_alert_locked(sec, f"{prefix}: готовься к отводу на малый лагерь на 47-й секунде!")
                        prefix = f"{team_label}, четвёрка" if team_label else "Четвёрка"
                        return self._emit_alert_locked(sec, f"{prefix}: готовься к отводу большого лагеря на 47-й секунде!")

            # 5. Стаки нейтралов: саппортам (:48–:53), остальным ролям (:52–:53)
            if sec >= 48:
                minute_idx = sec // 60
                sec_in_min = sec % 60
                if position in (4, 5) and 48 <= sec_in_min <= 53:
                    if minute_idx not in self._alerted_stacks:
                        self._alerted_stacks.add(minute_idx)
                        log.info("Сработал макро-таймер: стак нейтралов для саппорта (мин %d, sec=%d)", minute_idx, sec)
                        return self._emit_alert_locked(sec, "50-я секунда — время стакать нейтралов! Ударь крипов на 53–55-й секунде и уводи из лагеря.")
                if 50 <= sec <= 1500 and sec_in_min in (52, 53):
                    if minute_idx not in self._alerted_stacks:
                        self._alerted_stacks.add(minute_idx)
                        log.info("Сработал макро-таймер: стак нейтралов (мин %d)", minute_idx)
                        return self._emit_alert_locked(sec, "53-я секунда — время стакать нейтралов.")

            # 6. Edge-triggered нейтральные предметы (7/15/25/35/60; Turbo 0.5x)
            neutral_msg = self._edge_neutral_alert_locked(sec, turbo)
            if neutral_msg:
                return self._emit_alert_locked(sec, neutral_msg)

            # 7. Смена дня и ночи (каждые 300с) и волна катапульт (за 20с на :280)
            if sec >= 295 and (sec % DAY_NIGHT_PERIOD_SEC) in (298, 299, 0, 1):
                cycle_idx = (sec + 2) // DAY_NIGHT_PERIOD_SEC
                if cycle_idx > 0 and cycle_idx not in self._alerted_day_night:
                    self._alerted_day_night.add(cycle_idx)
                    if cycle_idx % 2 == 1:
                        log.info("Сработал таймер дня и ночи: наступила ночь (цикл %d)", cycle_idx)
                        return self._emit_alert_locked(sec, "Наступила ночь. Обзор снижен до 800. Опасайтесь внезапных смок-атак врагов!")
                    log.info("Сработал таймер дня и ночи: наступил день (цикл %d)", cycle_idx)
                    return self._emit_alert_locked(sec, "Наступил день — обзор восстановлен, Рошан перешёл в нижнее логово.")

            if sec >= 278:
                cat_cycle = (sec + 22) // CATAPULT_PERIOD_SEC
                target_cat = cat_cycle * CATAPULT_PERIOD_SEC - 20
                if cat_cycle >= 1 and target_cat - 2 <= sec <= target_cat + 2:
                    if cat_cycle not in self._alerted_catapults:
                        self._alerted_catapults.add(cat_cycle)
                        log.info("Сработал таймер: катапульта (цикл %d, sec=%d)", cat_cycle, sec)
                        return self._emit_alert_locked(sec, "Через 20 секунд волна катапульт — можно давить вышку!")

            return None

    def check_all(
        self,
        clock_seconds: Any,
        *,
        position: int = 1,
        team_name: str | None = None,
        enemy_mid_level: int | None = None,
        payload: Mapping[str, Any] | None = None,
        turbo: bool = False,
        match_id: Any = None,
        proactive: bool | None = None,
    ) -> str | None:
        """Единый вызов проверки пользовательских и всех макро-таймеров карты за один тик."""
        if not self.enabled:
            return None
        eff_match = match_id if match_id is not None else extract_match_id(payload)
        if eff_match is not None:
            self.sync_match_id(eff_match)

        sec = parse_clock_int(clock_seconds)
        if sec is None:
            return None

        with self._lock:
            if sec < 0:
                if self._has_any_alerts():
                    self.reset(keep_match_id=True)
                return None

            custom = self._check_custom_locked(sec)
            if custom:
                return custom

            allow_proactive = self.proactive_enabled if proactive is None else bool(proactive)
            if not allow_proactive:
                return None

            lvl6 = self.check_mid_level_6(
                payload,
                sec,
                enemy_mid_level=enemy_mid_level,
                match_id=eff_match,
                allow_predictive=(payload is not None),
            )
            if lvl6:
                return lvl6

            tormentor_msg = self._check_tormentor_locked(sec)
            if tormentor_msg:
                return tormentor_msg

            mid_rune = self.check_mid_runes(sec, position=position, match_id=eff_match)
            if mid_rune:
                return mid_rune

            return self.check_macro_timers(
                sec,
                position=position,
                team_name=team_name,
                turbo=turbo,
                match_id=eff_match,
            )


__all__ = [
    "ACTIVE_RUNE_PERIOD_SEC",
    "ACTIVE_RUNE_START_SEC",
    "BOUNTY_PERIOD_SEC",
    "CATAPULT_PERIOD_SEC",
    "CanonicalMapTimers",
    "DAY_NIGHT_PERIOD_SEC",
    "LOTUS_PERIOD_SEC",
    "NEUTRAL_TIERS_NORMAL",
    "NEUTRAL_TIERS_TURBO",
    "ROSHAN_AEGIS_DURATION_SEC",
    "ROSHAN_MAX_RESPAWN_SEC",
    "ROSHAN_MIN_RESPAWN_SEC",
    "TORMENTOR_RESPAWN_PERIOD_SEC",
    "TORMENTOR_SPAWN_SEC",
    "WATER_RUNE_TIMES_SEC",
    "WISDOM_PERIOD_SEC",
    "compute_map_timers",
    "compute_roshan_state",
    "describe_neutral_schedule",
    "detect_enemy_mid_level",
    "extract_match_id",
    "format_mmss",
    "format_objectives_window",
    "is_turbo_mode",
    "neutral_schedule",
    "neutral_tier",
    "objectives_context",
    "parse_clock_float",
    "parse_clock_int",
    "roshan_pit_location",
    "upcoming_objectives",
]
