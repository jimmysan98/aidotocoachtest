"""Превращение JSON от Game State Integration в компактный русский контекст.

Всё, что здесь происходит, — защитный разбор чужого JSON: Dota 2 присылает
разные наборы полей в зависимости от фазы игры и настроек .cfg.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .beginner import normalize_skill_level
from .heroes import HeroCatalog
from .map_timers import (
    CanonicalMapTimers,
    extract_match_id,
)
from .map_timers import (
    neutral_tier as _canonical_neutral_tier,
)
from .map_timers import (
    objectives_context as _canonical_objectives_context,
)
from .map_timers import (
    roshan_pit_location as _canonical_roshan_pit_location,
)
from .roles import normalize_position, position_label

log = logging.getLogger("aidota.context")

_STATE_LABELS = {
    "DOTA_GAMERULES_STATE_INIT": "инициализация",
    "DOTA_GAMERULES_STATE_WAIT_FOR_PLAYERS_TO_LOAD": "ожидание игроков",
    "DOTA_GAMERULES_STATE_HERO_SELECTION": "выбор героев",
    "DOTA_GAMERULES_STATE_STRATEGY_TIME": "время стратегии",
    "DOTA_GAMERULES_STATE_PRE_GAME": "подготовка к игре",
    "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS": "игра идёт",
    "DOTA_GAMERULES_STATE_POST_GAME": "игра окончена",
    "DOTA_GAMERULES_STATE_DISCONNECT": "нет соединения",
    "DOTA_GAMERULES_STATE_CUSTOM_GAME_SETUP": "настройка кастомной игры",
}


def _dig(data: Any, *keys: str, default: Any = None) -> Any:
    """Безопасно достаёт вложенное значение: _dig(gs, 'map', 'clock_time')."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _i(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _f(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Режим игры и тайминги
# --------------------------------------------------------------------------- #

GAME_MODE_LABELS: dict[int, str] = {
    0: "Unknown", 1: "All Pick", 2: "Captain's Mode", 3: "Random Draft",
    4: "Single Draft", 5: "All Random", 7: "Diretide", 8: "Reverse Captain's Mode",
    11: "Mid Only", 12: "Least Played", 13: "Limited Heroes", 15: "Custom",
    16: "Captain's Draft", 18: "Ability Draft", 20: "All Random Deathmatch",
    21: "1v1 Mid", 22: "All Draft", 23: "Turbo", 27: "Ranked All Pick",
}
GAME_MODE_TURBO = 23


def get_game_mode(payload: dict | None) -> int | None:
    """Достаёт game_mode из GSI; Turbo иногда пишут в customgamename."""
    if not isinstance(payload, dict):
        return None
    mode = _i(_dig(payload, "map", "game_mode"))
    if mode is not None:
        return mode
    name = _dig(payload, "map", "customgamename")
    if isinstance(name, str) and name:
        low = name.lower()
        if "turbo" in low:
            return GAME_MODE_TURBO
        if "all pick" in low or "allpick" in low:
            return 1
    return None


def is_turbo(game_mode: Any) -> bool:
    """Turbo (режим 23) — игра в ~1.6 раза быстрее по фарму и уровням."""
    return game_mode == GAME_MODE_TURBO or (
        isinstance(game_mode, str) and "turbo" in game_mode.lower()
    )


def turbo_factor(game_mode: Any) -> float:
    """Множитель для таймингов: 1.0 — обычный режим, ~1.6 — Turbo."""
    return 1.6 if is_turbo(game_mode) else 1.0


def game_mode_label(game_mode: Any) -> str:
    if game_mode is None:
        return "неизвестен"
    return GAME_MODE_LABELS.get(int(game_mode), str(game_mode))


def _fmt_minute(minutes: float) -> str:
    total_seconds = int(round(minutes * 60))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _next_occurrence(minute: float, start: float, period: float) -> float:
    if minute <= start:
        return start
    return start + (int((minute - start) // period) + 1) * period


def roshan_pit_location(clock_seconds: Any = None, is_daytime: bool | None = None) -> str:
    """Определяет, в каком логове находится Рошан (делегирует в ``aidota.map_timers``)."""
    return _canonical_roshan_pit_location(clock_seconds, is_daytime=is_daytime)


def objectives_context(
    clock_seconds: Any,
    game_mode: Any = None,
    *,
    roshan_death_sec: float | int | None = None,
    window_sec: int = 45,
) -> str:
    """Что из таймингов скоро наступит (делегирует в ``aidota.map_timers``)."""
    return _canonical_objectives_context(
        clock_seconds,
        game_mode,
        roshan_death_sec=roshan_death_sec,
        window_sec=window_sec,
    )


def neutral_tier(minute: float, turbo: bool) -> int:
    """Номер доступного разряда нейтральных предметов по минуте игры (7/15/25/35/60)."""
    return _canonical_neutral_tier(minute, turbo=turbo)


def format_clock(seconds: Any) -> str:
    """Секунды игрового времени -> '12:34' (может быть отрицательным до игры)."""
    total = _i(seconds)
    if total is None:
        return "?"
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 60}:{total % 60:02d}"


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "нет данных"
    if seconds < 1.5:
        return "только что"
    if seconds < 60:
        return f"{int(seconds)} с назад"
    return f"{int(seconds // 60)} мин назад"


def item_name(gsi_item: Any, items: Mapping[str, str]) -> str:
    """'item_blink' -> 'Blink Dagger' (или аккуратное имя без словаря)."""
    if isinstance(gsi_item, dict):
        gsi_item = gsi_item.get("name")
    if not gsi_item:
        return "?"
    key = str(gsi_item)
    if key.startswith("item_"):
        key = key[5:]
    display = items.get(key)
    if display:
        return display
    return key.replace("_", " ").strip().title()


def hero_name_for(hint: Any, catalog: HeroCatalog | None) -> str:
    """Имя героя по id / npc-имени / словарю."""
    if hint is None:
        return "?"
    if catalog is not None:
        if isinstance(hint, int):
            hero = catalog.by_id(hint)
            if hero:
                return hero.get("localized_name") or hero.get("name") or "?"
        if isinstance(hint, str):
            hero = catalog.by_npc(hint)
            if hero:
                return hero.get("localized_name") or hero.get("name") or "?"
            if hint.isdigit():
                hero = catalog.by_id(int(hint))
                if hero:
                    return hero.get("localized_name") or hero.get("name") or "?"
            # Возможно, это уже отображаемое имя.
            return hint
    if isinstance(hint, str):
        if hint.startswith("npc_dota_hero_"):
            return hint.replace("npc_dota_hero_", "").replace("_", " ").title()
        return hint
    return str(hint)


@dataclass
class DraftInfo:
    my_team: str | None = None
    radiant: list[str] = field(default_factory=list)
    dire: list[str] = field(default_factory=list)
    bans: list[str] = field(default_factory=list)

    @property
    def my_picks(self) -> list[str]:
        return self.dire if self.my_team == "dire" else self.radiant

    @property
    def enemy_picks(self) -> list[str]:
        return self.radiant if self.my_team == "dire" else self.dire

    @property
    def empty(self) -> bool:
        return not (self.radiant or self.dire or self.bans)


def _pick_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id") or item.get("hero_id") or item.get("pick")
        if item is None:
            continue
        result.append(item)
    return result


def collect_draft(payload: dict | None, catalog: HeroCatalog | None = None) -> DraftInfo:
    if not isinstance(payload, dict):
        return DraftInfo()
    player_team = _dig(payload, "player", "team_name")
    if player_team not in {"radiant", "dire"}:
        player_team = None

    draft = payload.get("draft")
    info = DraftInfo(my_team=player_team if isinstance(player_team, str) else None)
    if not isinstance(draft, dict):
        return info

    for key, team in (("team2", "radiant"), ("team3", "dire")):
        team_data = draft.get(key)
        if not isinstance(team_data, dict):
            continue
        picks = _pick_list(team_data.get("pick_ids"))
        if not picks:
            picks = _pick_list(team_data.get("picks"))
        names = [hero_name_for(p, catalog) for p in picks]
        names = [n for n in names if n and n != "?"]
        if team == "radiant":
            info.radiant = names
        else:
            info.dire = names
        bans = _pick_list(team_data.get("bans"))
        info.bans.extend(hero_name_for(b, catalog) for b in bans if b)
    info.bans = [b for b in info.bans if b and b != "?"]
    return info


def _player_team(entry: Mapping[str, Any]) -> str | None:
    """Сторона игрока из allplayers: team_name / team (2/3) / player_slot."""
    team = entry.get("team_name")
    if isinstance(team, str):
        label = team.strip().lower()
        if label in ("radiant", "dire"):
            return label
    team_num = _i(entry.get("team"))
    if team_num in (2, 3):
        return {2: "radiant", 3: "dire"}[team_num]
    slot = _i(entry.get("player_slot"))
    if slot is not None:
        if 0 <= slot <= 4:
            return "radiant"
        if 128 <= slot <= 132:
            return "dire"
    return None


def _extract_allplayers(payload: dict) -> list[dict[str, Any]]:
    """Достаёт список игроков из разных вариантов GSI (если они вообще есть).

    Возвращает словари с устойчивыми ключами: ``npc``, ``team``, ``name``,
    ``level``, ``kills``, ``deaths``, ``assists``, ``alive``,
    ``respawn_seconds``, ``net_worth`` и ``items`` (сырые ``item_*``).
    Поля, которых нет в payload, остаются ``None`` / пустым списком.
    """
    candidates: list[Any] = []
    root = payload.get("allplayers")
    if root is not None:
        candidates.append(root)
    players = payload.get("players")
    if isinstance(players, dict):
        candidates.append(players.get("all_players"))
        candidates.append(players.get("allplayers"))
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        entries: Iterable[Any]
        if isinstance(candidate, dict):
            entries = candidate.values()
        elif isinstance(candidate, list):
            entries = candidate
        else:
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            hero = entry.get("hero")
            hero_dict = hero if isinstance(hero, dict) else {}
            npc = hero if isinstance(hero, str) else hero_dict.get("name")
            if not isinstance(npc, str) or not npc:
                fallback_npc = entry.get("hero_name") or entry.get("npc")
                npc = fallback_npc if isinstance(fallback_npc, str) and fallback_npc else None
            team = _player_team(entry) or _player_team(hero_dict)
            level = _i(entry.get("level"))
            if level is None:
                level = _i(hero_dict.get("level"))
            stats: dict[str, int | None] = {}
            for stat_key in ("kills", "deaths", "assists"):
                value = _i(entry.get(stat_key))
                if value is None:
                    value = _i(hero_dict.get(stat_key))
                stats[stat_key] = value
            alive = entry.get("alive")
            if not isinstance(alive, bool):
                hero_alive = hero_dict.get("alive")
                alive = hero_alive if isinstance(hero_alive, bool) else None
            respawn = _i(entry.get("respawn_seconds"))
            if respawn is None:
                respawn = _i(hero_dict.get("respawn_seconds"))
            net_worth = _i(entry.get("net_worth"))
            if net_worth is None:
                net_worth = _i(hero_dict.get("net_worth"))
            raw_items = entry.get("items")
            if not isinstance(raw_items, dict):
                raw_items = hero_dict.get("items")
            name = entry.get("name") or entry.get("player_name")
            lane = entry.get("lane") if entry.get("lane") is not None else hero_dict.get("lane")
            is_mid = entry.get("is_mid") if "is_mid" in entry else hero_dict.get("is_mid")
            pos_val = entry.get("position") if entry.get("position") is not None else hero_dict.get("position")
            result.append(
                {
                    "npc": npc,
                    "team": team,
                    "level": level,
                    "name": name if isinstance(name, str) and name else None,
                    "lane": lane,
                    "is_mid": is_mid,
                    "position": pos_val,
                    "kills": stats["kills"],
                    "deaths": stats["deaths"],
                    "assists": stats["assists"],
                    "alive": alive,
                    "respawn_seconds": respawn,
                    "net_worth": net_worth,
                    "items": _item_keys(raw_items),
                }
            )
        if result:
            break
    return result


def current_hero_npc(payload: dict | None) -> str | None:
    npc = _dig(payload, "hero", "name")
    return npc if isinstance(npc, str) and npc else None


# --------------------------------------------------------------------------- #
# Извлечение предметов и детектор ошибок
# --------------------------------------------------------------------------- #


# Пустые слоты GSI: "empty" (spectator), "item_empty" (playing), "" и None.
_EMPTY_ITEM_NAMES = frozenset(("", "empty", "item_empty"))


def is_empty_item_name(name: Any) -> bool:
    """Пустой ли слот предмета: ``None``, ``""``, ``"empty"`` или ``"item_empty"``.

    Dota 2 отдаёт пустые слоты по-разному в зависимости от режима: в обычной
    игре это ``item_empty``, в спектаторе — ``empty``. Оба варианта, а также
    пустая строка и ``None``, считаем пустотой.
    """
    if not isinstance(name, str):
        return True
    return name.strip().lower() in _EMPTY_ITEM_NAMES


def _item_payload_name(value: Any) -> str | None:
    """Имя предмета из GSI-значения (``{"name": ...}`` или строка) без пустышек."""
    name = value.get("name") if isinstance(value, dict) else value
    if is_empty_item_name(name) or not isinstance(name, str):
        return None
    return name.strip()


def _item_slots(raw_items: Any) -> list[str]:
    """Имена предметов в основных слотах (slot0..slot5), без мусора."""
    names: list[str] = []
    if not isinstance(raw_items, dict):
        return names
    for slot in sorted(raw_items, key=str):
        if not str(slot).startswith("slot"):
            continue
        name = _item_payload_name(raw_items[slot])
        if name:
            names.append(name)
    return names


def _has_tp(raw_items: Any) -> bool:
    """Есть ли TP-свиток или Travel Boots (в слотах или специальном слоте ТП)."""
    if not isinstance(raw_items, dict):
        return False
    for key, value in raw_items.items():
        if not (str(key).startswith("teleport") or str(key).startswith("slot")):
            continue
        name = _item_payload_name(value)
        if name and ("tpscroll" in name or "travel_boots" in name):
            return True
    return False


def _neutral_present(raw_items: Any) -> bool:
    if not isinstance(raw_items, dict):
        return False
    return bool(_item_payload_name(raw_items.get("neutral0")))


def _as_bool(value: Any, default: bool = False) -> bool:
    """Мягко приводит GSI-значение к bool (строки 'false' не должны давать True)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on", "да"):
            return True
        if low in ("false", "0", "no", "off", "нет", ""):
            return False
    return default


def _item_keys(raw_items: Any) -> list[str]:
    """Сырые имена ``item_*`` из слотов, ТП и нейтралки (без пустых слотов)."""
    names: list[str] = []
    if not isinstance(raw_items, dict):
        return names
    for slot in sorted(raw_items, key=str):
        key = str(slot)
        if not (key.startswith("slot") or key.startswith("teleport") or key == "neutral0"):
            continue
        name = _item_payload_name(raw_items[slot])
        if name:
            names.append(name)
    return names


def _item_display_list(raw_items: Any, items: Mapping[str, str] | None) -> list[str]:
    """Инвентарь в человекочитаемом виде: ['Power Treads', 'Battle Fury', ...]."""
    mapping = items or {}
    display: list[str] = []
    for raw in _item_keys(raw_items):
        name = item_name(raw, mapping)
        if name and name != "?":
            display.append(name)
    return display


def _neutral_item_display(raw_items: Any, items: Mapping[str, str] | None) -> str | None:
    """Название надетой нейтралки или None."""
    if not isinstance(raw_items, dict):
        return None
    name = _item_payload_name(raw_items.get("neutral0"))
    if not name:
        return None
    display = item_name(name, items or {})
    return display if display and display != "?" else None


# --------------------------------------------------------------------------- #
# Локальные предметы и способности (реальный GSI кладёт items/abilities
# на верхний уровень payload, а не внутрь hero)
# --------------------------------------------------------------------------- #

_ITEM_SECTION_PREFIXES = ("slot", "stash", "teleport", "neutral")
_ABILITY_SECTION_PREFIXES = ("ability",)
_TEAM_SECTION_KEYS = ("team2", "team3", "radiant", "dire")
_TEAM_SIDES = {"team2": "radiant", "radiant": "radiant", "team3": "dire", "dire": "dire"}
_IDENTITY_FIELDS = (
    "accountid", "account_id", "steamid", "steam_id",
    "player_slot", "playerid", "player_id",
)


def _section_has_payload(section: Any, prefixes: tuple[str, ...]) -> bool:
    if not isinstance(section, Mapping):
        return False
    for key in section:
        if str(key).strip().lower().startswith(prefixes):
            return True
    return False


def _clean_section(section: Mapping[str, Any], prefixes: tuple[str, ...]) -> dict[str, Any]:
    """Оставляет только слоты/способности, отбрасывая служебные поля игрока."""
    result: dict[str, Any] = {}
    for key, value in section.items():
        if str(key).strip().lower().startswith(prefixes):
            result[str(key)] = value
    return result


def _add_identity(identity: dict[str, str], raw_key: str, value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    text = str(value).strip()
    if not text:
        return
    low = raw_key.lower()
    if "account" in low:
        identity.setdefault("accountid", text)
    elif "steam" in low:
        identity.setdefault("steamid", text)
    elif "slot" in low:
        identity.setdefault("player_slot", text)
    elif "player" in low:
        identity.setdefault("playerid", text)


def _identity_fields(section: Any) -> dict[str, str]:
    """Идентификаторы игрока из произвольной секции GSI (player/hero/allplayers)."""
    identity: dict[str, str] = {}
    if not isinstance(section, Mapping):
        return identity
    for raw_key in _IDENTITY_FIELDS:
        _add_identity(identity, raw_key, section.get(raw_key))
    name = section.get("name")
    if isinstance(name, str) and name.strip():
        identity.setdefault("name", name.strip().casefold())
    return identity


def _payload_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    """Идентификаторы локального игрока из секций ``player``/``hero``."""
    identity: dict[str, str] = {}
    for key in ("player", "hero"):
        section = payload.get(key)
        if isinstance(section, Mapping):
            merged = _identity_fields(section)
            for field, value in merged.items():
                identity.setdefault(field, value)
            if "hero" == key:
                hero_name = section.get("name")
                if isinstance(hero_name, str) and hero_name.strip():
                    identity.setdefault("hero_npc", hero_name.strip())
    return identity


def _own_team_side(payload: Mapping[str, Any]) -> str | None:
    """Сторона локального игрока: ``radiant`` / ``dire`` (или None)."""
    player = payload.get("player")
    if isinstance(player, Mapping):
        team = player.get("team_name")
        if isinstance(team, str):
            low = team.strip().lower()
            if low in ("radiant", "dire"):
                return low
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _identity_matches(own: Mapping[str, str], other: Mapping[str, str]) -> bool:
    for key in ("accountid", "steamid", "player_slot", "playerid"):
        left = own.get(key)
        right = other.get(key)
        if not left or not right:
            continue
        if key == "player_slot":
            if _int_or_none(left) == _int_or_none(right):
                return True
        elif str(left).strip().casefold() == str(right).strip().casefold():
            return True
    return False


def _entry_hero_npc(entry: Mapping[str, Any]) -> str | None:
    hero = entry.get("hero")
    if isinstance(hero, str) and hero.strip():
        return hero.strip()
    if isinstance(hero, Mapping):
        name = hero.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    fallback = entry.get("hero_name") or entry.get("npc")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def _player_key_slot(player_key: Any, team_key: str | None) -> int | None:
    """Номер слота игрока по ключу ``playerN`` (0–4 Radiant, 128–132 Dire)."""
    match = re.fullmatch(r"player[\s_]*(\d+)", str(player_key or "").strip().lower())
    if not match:
        return None
    index = int(match.group(1))
    side = _TEAM_SIDES.get(str(team_key or "").strip().lower())
    if side == "radiant":
        return index if 0 <= index <= 4 else None
    if side == "dire":
        return 128 + index if 0 <= index <= 4 else None
    # Без явного team-ключа GSI нумерует игроков 0–4 Radiant, 5–9 Dire.
    if 0 <= index <= 4:
        return index
    if 5 <= index <= 9:
        return 128 + index - 5
    return None


def _allplayers_index(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Игроки по ключу ``playerN`` из ``allplayers``/``players`` (если есть)."""
    result: dict[str, Mapping[str, Any]] = {}
    groups: list[Any] = []
    root = payload.get("allplayers")
    if isinstance(root, (dict, list)):
        groups.append(root)
    players = payload.get("players")
    if isinstance(players, Mapping):
        groups.append(players.get("all_players"))
        groups.append(players.get("allplayers"))
    for group in groups:
        entries: Iterable[tuple[str, Any]]
        if isinstance(group, Mapping):
            entries = ((str(key), value) for key, value in group.items())
        elif isinstance(group, list):
            entries = ((f"player{index}", value) for index, value in enumerate(group))
        else:
            continue
        for key, entry in entries:
            if isinstance(entry, Mapping) and key not in result:
                result[key] = entry
    return result


def _spectator_local_section(
    section: Mapping[str, Any],
    payload: Mapping[str, Any],
    prefixes: tuple[str, ...],
    subkey: str,
) -> dict[str, Any] | None:
    """Находит секцию локального игрока в spectator-формате team2/team3 → playerN.

    Игрок сопоставляется по accountid/steamid/player_slot (в самой секции либо в
    ``allplayers``), по hero-имени для спектатора и по номеру ``playerN``.
    Если однозначного совпадения нет, но игрок в секции ровно один — берём его.
    """
    own = _payload_identity(payload)
    own_hero = own.get("hero_npc")
    own_side = _own_team_side(payload)
    allplayers = _allplayers_index(payload)
    candidates: list[tuple[bool, dict[str, Any]]] = []

    def consider(team_key: Any, player_key: Any, entry: Mapping[str, Any], data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        clean = _clean_section(data, prefixes)
        if not clean:
            return
        matched = False
        other = _identity_fields(entry)
        if isinstance(player_key, str):
            allplayer = allplayers.get(player_key) or allplayers.get(player_key.lower())
            if isinstance(allplayer, Mapping):
                merged = dict(other)
                for field, value in _identity_fields(allplayer).items():
                    merged.setdefault(field, value)
                other = merged
            slot = _player_key_slot(player_key, str(team_key) if team_key else None)
            own_slot = _int_or_none(own.get("player_slot"))
            if slot is not None and own_slot is not None and slot == own_slot:
                matched = True
        if not matched and _identity_matches(own, other):
            matched = True
        if not matched and own_hero:
            entry_hero = _entry_hero_npc(entry)
            if entry_hero and entry_hero == own_hero:
                matched = True
        if not matched and player_key is None and own_side:
            # Плоская team-секция (без playerN) — сверяем сторону игрока.
            if _TEAM_SIDES.get(str(team_key or "").strip().lower()) == own_side:
                matched = True
        candidates.append((matched, clean))

    for team_key, team in section.items():
        if not isinstance(team, Mapping):
            continue
        key_low = str(team_key).strip().lower()
        if key_low not in _TEAM_SECTION_KEYS:
            if key_low.startswith("player"):
                consider(None, str(team_key), team, team)
            continue
        if _section_has_payload(team, prefixes):
            # Секция сразу описывает наблюдаемого игрока (без вложенных playerN).
            consider(str(team_key), None, team, team)
            continue
        players = team.get("players") if isinstance(team.get("players"), (Mapping, list)) else team
        if isinstance(players, Mapping):
            entries: Iterable[tuple[Any, Any]] = players.items()
        elif isinstance(players, list):
            entries = ((f"player{index}", value) for index, value in enumerate(players))
        else:
            continue
        for player_key, entry in entries:
            if not isinstance(entry, Mapping):
                continue
            nested = entry.get(subkey)
            data = nested if isinstance(nested, Mapping) else entry
            consider(str(team_key), str(player_key), entry, data)

    for matched, data in candidates:
        if matched:
            return data
    non_empty = [data for _, data in candidates]
    if len(non_empty) == 1:
        return non_empty[0]
    return None


def extract_own_items(payload: dict | None) -> dict[str, Any]:
    """Сырые предметы локального игрока из реального GSI-пакета.

    Порядок: верхнеуровневая секция ``items`` (playing-формат со слотами
    ``slot*``/``stash*``/``teleport*``/``neutral*``) → spectator-формат
    ``team2/team3 → playerN`` → устаревший ``hero.items``. Возвращает ``{}``,
    если данных нет; никогда не падает на кривом payload.
    """
    if not isinstance(payload, dict):
        return {}
    section = payload.get("items")
    if isinstance(section, dict) and section:
        if _section_has_payload(section, _ITEM_SECTION_PREFIXES):
            return dict(section)
        spectator = _spectator_local_section(
            section, payload, _ITEM_SECTION_PREFIXES, "items"
        )
        if spectator:
            return spectator
    hero = payload.get("hero")
    if isinstance(hero, dict):
        nested = hero.get("items")
        if isinstance(nested, dict) and nested:
            return dict(nested)
    return {}


def extract_own_abilities(payload: dict | None) -> dict[str, Any]:
    """Способности локального игрока: верхнеуровневая ``abilities`` → ``hero.abilities``."""
    if not isinstance(payload, dict):
        return {}
    section = payload.get("abilities")
    if isinstance(section, dict) and section:
        if _section_has_payload(section, _ABILITY_SECTION_PREFIXES):
            return dict(section)
        if not any(
            str(key).strip().lower() in _TEAM_SECTION_KEYS for key in section
        ):
            return dict(section)
        spectator = _spectator_local_section(
            section, payload, _ABILITY_SECTION_PREFIXES, "abilities"
        )
        if spectator:
            return spectator
    hero = payload.get("hero")
    if isinstance(hero, dict):
        nested = hero.get("abilities")
        if isinstance(nested, dict) and nested:
            return dict(nested)
    return {}


# --------------------------------------------------------------------------- #
# Нетворс: GSI-значение или оценка по цене предметов
# --------------------------------------------------------------------------- #

_NET_WORTH_ITEM_PREFIXES = ("slot", "stash")


def _normalize_item_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("npc_dota_item_", "").replace("item_", "")
    text = text.replace("'", "").replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


def _item_cost_lookup(item_costs: Mapping[str, int] | None) -> dict[str, int]:
    lookup: dict[str, int] = {}
    if not isinstance(item_costs, Mapping):
        return lookup
    for name, cost in item_costs.items():
        try:
            value = int(cost)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        token = _normalize_item_token(name)
        if token:
            lookup.setdefault(token, value)
    return lookup


def lookup_item_cost(
    raw_name: Any,
    item_costs: Mapping[str, int] | None = None,
    *,
    items: Mapping[str, str] | None = None,
) -> int:
    """Цена предмета по сырому имени GSI и словарю display-имя → цена.

    Понимает ``item_blink``, ``blink`` и отображаемые имена: сначала ищет по
    точному нормализованному совпадению, затем — по префиксу (``blink`` ↔
    ``Blink Dagger``). Если цену найти не удалось — ``0``.
    """
    lookup = _item_cost_lookup(item_costs)
    if not isinstance(raw_name, str) or not raw_name.strip():
        return 0
    raw = raw_name.strip()
    candidates: list[str] = [raw]
    stripped = raw[5:] if raw.startswith("item_") else raw
    if stripped != raw:
        candidates.append(stripped)
    mapping = items or {}
    display = mapping.get(stripped) or mapping.get(raw)
    if isinstance(display, str) and display:
        candidates.append(display)
    candidates.append(stripped.replace("_", " "))
    candidates.append(stripped.replace("item_", "").replace("_", " ").title())
    for candidate in candidates:
        token = _normalize_item_token(candidate)
        if not token:
            continue
        if token in lookup:
            return lookup[token]
        for key, cost in lookup.items():
            if token == key:
                return cost
            if token.startswith(key + " ") or key.startswith(token + " "):
                return cost
    return 0


def estimate_net_worth(
    payload: dict | None,
    item_costs: Mapping[str, int] | None = None,
    *,
    items: Mapping[str, str] | None = None,
) -> tuple[int, bool]:
    """Нетворс игрока: ``(значение, это_оценка)``.

    Если реальный ``player.net_worth`` > 0 — возвращает его как есть и ``False``.
    Иначе оценивает как ``gold + сумма cost предметов в слотах/стэше`` (нейтралка
    и слот телепорта не учитываются) и возвращает ``True``.
    """
    if not isinstance(payload, dict):
        return 0, False
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    net_worth = _i(player.get("net_worth"))
    if net_worth is not None and net_worth > 0:
        return int(net_worth), False
    gold = _i(player.get("gold")) or 0
    total = max(0, int(gold))
    raw_items = extract_own_items(payload)
    if isinstance(raw_items, dict):
        for slot in sorted(raw_items, key=str):
            slot_key = str(slot).strip().lower()
            if not slot_key.startswith(_NET_WORTH_ITEM_PREFIXES):
                continue
            name = _item_payload_name(raw_items[slot])
            if name:
                total += lookup_item_cost(name, item_costs, items=items)
    return int(total), True


def analyze_player_mistakes(
    payload: dict | None, position: int | None, game_mode: Any = None
) -> list[str]:
    """Программно находит очевидные ошибки, которые стоит показать тренеру как факты.

    Возвращает список коротких русских фраз; используется и как отдельный
    раздел контекста, и как данные для LLM-разбора.
    """
    if not isinstance(payload, dict):
        return []
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
    clock = _i(_dig(payload, "map", "clock_time"))
    if clock is None or clock < 0 or position is None:
        return []
    pos = int(position)
    if pos not in (1, 2, 3, 4, 5):
        return []

    minute = clock / 60.0
    turbo = is_turbo(game_mode)
    factor = turbo_factor(game_mode)
    mode_note = "Turbo" if turbo else "All Pick"

    gpm = _i(player.get("gpm"))
    lh = _i(player.get("last_hits"))
    deaths = _i(player.get("deaths"))
    gold = _i(player.get("gold"))
    alive = hero.get("alive", True) is not False
    # Реальный GSI кладёт предметы в верхнеуровневую секцию items, а не в hero.
    raw_items = extract_own_items(payload)

    mistakes: list[str] = []

    # 1. Плохой фарм для кор-ролей (1–3).
    if pos in (1, 2, 3) and minute >= 4:
        base = {1: 520, 2: 470, 3: 420}[pos]
        if minute < 10:
            base *= 0.7
        target_gpm = base * factor
        if gpm is not None and gpm < target_gpm * 0.75:
            mistakes.append(
                f"Низкий GPM ({gpm}) для {position_label(pos)} на {_fmt_minute(minute)} — "
                f"ориентир ~{int(target_gpm)} ({mode_note})."
            )
        lh_target = {1: 4.5, 2: 4.0, 3: 3.5}[pos] * minute * factor
        if lh is not None and minute >= 6 and lh < lh_target * 0.6:
            mistakes.append(
                f"Мало добиваний ({lh}) для кор-роли к {_fmt_minute(minute)} — "
                f"ожидается ~{int(lh_target)}."
            )

    # 2. Фид / плохая позиционка.
    if deaths is not None:
        limit = 3 if minute < 10 else (6 if minute < 20 else 9)
        if deaths > limit:
            mistakes.append(
                f"Слишком много смертей ({deaths}) к {_fmt_minute(minute)} — "
                "играй от позиции, не лезь в драки без видения."
            )

    # 3. Нет ТП-свитка с 5-й минуты (одна проверка, без дублей с «нет ТП до 10-й»).
    if minute >= (4 if turbo else 5) and not _has_tp(raw_items):
        mistakes.append(
            "Нет ТП-свитка (свитка телепортации) после 5-й минуты — под угрозой ганга "
            "не сможешь быстро сменить линию."
        )

    # 4. Пустой нейтральный слот.
    if minute >= (4 if turbo else 8) and not _neutral_present(raw_items):
        tier = neutral_tier(minute, turbo)
        mistakes.append(f"Нейтральный слот пуст — надень нейтралку (сейчас тир {tier}).")

    # 5. Копление золота: ранний мид-гейм и отдельно поздняя стадия (байбек/слот).
    if alive and gold is not None:
        if (5 if turbo else 8) <= minute < 25:
            hoard_limit = 3000 if turbo else 2200
            if gold > hoard_limit:
                mistakes.append(
                    f"Держишь {gold} золота незатраченным — потрать на следующий слот или байбек."
                )
        elif minute >= 25:
            late_limit = int(3000 * factor)
            if gold > late_limit:
                mistakes.append(
                    f"Слишком много золота на руках ({gold}) в лейте — закупи следующий слот "
                    "или держи его под байбек, не копи впустую."
                )

    return mistakes


def build_gsi_context(
    payload: dict | None,
    catalog: HeroCatalog | None,
    items: Mapping[str, str] | None = None,
    *,
    age_seconds: float | None = None,
    stale_seconds: float = 45.0,
    position: int | None = None,
    mistakes: list[str] | None = None,
) -> str:
    """Собирает читаемый контекст для LLM и для команды «статус».

    Если передан ``position``, дописывает программно найденные ошибки
    (детектор) — их же можно передать готовыми через ``mistakes``.
    """
    items = items or {}
    if not isinstance(payload, dict) or not payload:
        return (
            "GSI не получает данных. Проверьте, что Dota 2 запущена с параметром "
            "-gamestateintegration, а файл gamestate_integration_aidota.cfg лежит "
            "в ...\\game\\dota\\cfg\\gamestate_integration\\."
        )

    map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}

    lines: list[str] = []
    if age_seconds is not None:
        age_text = format_age(age_seconds)
        if age_seconds > stale_seconds:
            lines.append(f"ВНИМАНИЕ: данные GSI устарели ({age_text}).")
        else:
            lines.append(f"Данные GSI: {age_text}.")

    state = map_data.get("game_state")
    state_label = _STATE_LABELS.get(str(state), str(state) if state else "неизвестно")
    clock = format_clock(map_data.get("clock_time"))
    score = ""
    radiant_score = _i(map_data.get("radiant_score"))
    dire_score = _i(map_data.get("dire_score"))
    if radiant_score is not None and dire_score is not None:
        score = f", счёт Radiant {radiant_score} : Dire {dire_score}"
    paused = " (пауза)" if map_data.get("paused") else ""
    lines.append(f"Режим: {state_label}, время {clock}{score}{paused}.")

    game_mode = get_game_mode(payload)
    turbo = is_turbo(game_mode)
    lines.append(
        f"Режим игры: {game_mode_label(game_mode)}"
        + (" (Turbo — фарм и уровни в ~1.6 раза быстрее)" if turbo else "")
        + "."
    )
    objectives = objectives_context(map_data.get("clock_time"), game_mode)
    if objectives:
        lines.append(objectives)

    daytime = map_data.get("daytime")
    if isinstance(daytime, bool):
        pit = roshan_pit_location(map_data.get("clock_time"), daytime)
        lines.append(f"Время суток: {'день' if daytime else 'ночь'} (Рошан в {pit}).")

    my_team = player.get("team_name")
    team_label = {"radiant": "Radiant", "dire": "Dire"}.get(str(my_team), "?")
    player_line = []
    if player:
        player_line.append(f"сторона {team_label}")
        for label, key in (("убийств", "kills"), ("смертей", "deaths"), ("помощи", "assists")):
            value = _i(player.get(key))
            if value is not None:
                player_line.append(f"{label} {value}")
        gold = _i(player.get("gold"))
        if gold is not None:
            player_line.append(f"золото {gold}")
        net_worth = _i(player.get("net_worth"))
        if net_worth is not None:
            player_line.append(f"net worth {net_worth}")
        for label, key in (("GPM", "gpm"), ("XPM", "xpm"), ("добиваний", "last_hits")):
            value = _i(player.get(key))
            if value is not None:
                player_line.append(f"{label} {value}")
    if player_line:
        lines.append("Вы: " + ", ".join(player_line) + ".")

    if hero:
        hero_name = hero_name_for(hero.get("name") or hero.get("id"), catalog)
        hero_parts = [hero_name]
        level = _i(hero.get("level"))
        if level is not None:
            hero_parts.append(f"ур. {level}")
        facet = hero.get("facet")
        if facet is not None and facet is not False:
            hero_parts.append(f"аспект {facet}")
        if hero.get("alive") is False:
            respawn = _i(hero.get("respawn_seconds"))
            hero_parts.append(f"мёртв, респавн {'через ' + str(respawn) + ' c' if respawn is not None else ''}".strip())
        hp = _f(hero.get("health_percent"))
        if hp is not None:
            hero_parts.append(f"HP {hp:.0f}%")
        mana = _f(hero.get("mana_percent"))
        if mana is not None:
            hero_parts.append(f"мана {mana:.0f}%")
        statuses = []
        if hero.get("stunned"):
            statuses.append("в стане")
        if hero.get("silenced"):
            statuses.append("в сайленсе")
        if hero.get("disarmed"):
            statuses.append("в дизарме")
        if hero.get("break"):
            statuses.append("истощён")
        if hero.get("hexed"):
            statuses.append("в хексе")
        if hero.get("magicimmune"):
            statuses.append("иммунитет к магии")
        if hero.get("rooted"):
            statuses.append("в оцепенении")
        if hero.get("muted"):
            statuses.append("предметы заглушены")
        if hero.get("taunted"):
            statuses.append("в таунте")
        if statuses:
            hero_parts.append(f"статус: {', '.join(statuses)}")
        buyback_cost = _i(hero.get("buyback_cost"))
        buyback_cd = _i(hero.get("buyback_cooldown"))
        if buyback_cost:
            suffix = f", кд {buyback_cd} c" if buyback_cd else ""
            hero_parts.append(f"выкуп {buyback_cost}{suffix}")
        lines.append("Ваш герой: " + ", ".join(hero_parts) + ".")

        inventory = []
        # Реальный GSI: предметы в верхнеуровневой секции payload["items"].
        raw_items = extract_own_items(payload)
        if isinstance(raw_items, dict):
            for slot in sorted(raw_items, key=str):
                if not str(slot).startswith("slot"):
                    continue
                raw_name = _item_payload_name(raw_items[slot])
                if not raw_name:
                    continue
                name = item_name(raw_name, items)
                if name and name != "?":
                    inventory.append(name)
        neutral = _neutral_item_display(raw_items, items)
        if inventory:
            lines.append("Инвентарь: " + ", ".join(inventory) + ".")
        if neutral and neutral != "?":
            lines.append(f"Нейтральный предмет: {neutral}.")

        tp_note = "есть" if _has_tp(raw_items) else "нет"
        lines.append(f"ТП-свиток: {tp_note}.")
        agh = "есть" if hero.get("aghanims_scepter") else "нет"
        shard = "есть" if hero.get("aghanims_shard") else "нет"
        lines.append(f"Аганим: {agh}, шард: {shard}.")

    draft = collect_draft(payload, catalog)
    if not draft.empty:
        if draft.my_picks:
            lines.append("Пики вашей команды: " + ", ".join(draft.my_picks) + ".")
        if draft.enemy_picks:
            lines.append("Пики противника: " + ", ".join(draft.enemy_picks) + ".")
        if draft.my_team is None:
            if draft.radiant:
                lines.append("Пики Radiant: " + ", ".join(draft.radiant) + ".")
            if draft.dire:
                lines.append("Пики Dire: " + ", ".join(draft.dire) + ".")
        if draft.bans:
            lines.append("Баны: " + ", ".join(draft.bans) + ".")

    allplayers = _extract_allplayers(payload)
    if allplayers:
        own = [p for p in allplayers if p.get("team") == draft.my_team or p.get("team") == my_team]
        enemy = [p for p in allplayers if p.get("team") and p not in own]
        for label, group in (("Союзники", own), ("Противники", enemy)):
            names = []
            for p in group:
                name = hero_name_for(p.get("npc"), catalog)
                level = p.get("level")
                names.append(f"{name} (ур. {level})" if level else name)
            if names:
                lines.append(f"{label}: " + ", ".join(names) + ".")

    if mistakes is None and position is not None:
        mistakes = analyze_player_mistakes(payload, position, game_mode)
    if mistakes:
        lines.append("Найденные ошибки (детектор):")
        lines.extend("• " + str(mistake) for mistake in mistakes)

    return "\n".join(lines)


def gsi_stale_hint(age: float | None, stale_seconds: float = 45.0) -> str | None:
    """Подсказка, если GSI молчит слишком долго."""
    if age is None:
        return (
            "GSI пока не получает данные: проверьте cfg-файл и параметр запуска "
            "-gamestateintegration, затем перезапустите Dota 2."
        )
    if age > stale_seconds:
        return (
            f"Последние данные GSI были {format_age(age)} — игра, похоже, закрыта "
            "или интеграция отключена."
        )
    return None


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Локальный профиль игрока (data/player_profile.json)
# --------------------------------------------------------------------------- #


def get_player_profile_path(data_dir: Path | str | None = None) -> Path:
    """Возвращает путь к записываемому ``player_profile.json``."""
    from .paths import bundled_data_dir, uses_isolated_runtime_root, writable_data_dir

    if not data_dir:
        return writable_data_dir() / "player_profile.json"
    base = Path(data_dir)
    if uses_isolated_runtime_root():
        try:
            if base.resolve() == bundled_data_dir().resolve():
                base = writable_data_dir()
        except OSError:
            pass
    return base / "player_profile.json"


def load_player_profile(data_dir: Path | str | None = None) -> dict[str, Any]:
    """Загружает профиль игрока из player_profile.json или создает начальный."""
    from .paths import read_json_safe

    path = get_player_profile_path(data_dir)
    data = read_json_safe(path)
    if isinstance(data, dict):
        return data

    return {
        "account_id": None,
        "preferred_position": 1,
        "signature_heroes": ["Juggernaut", "Slark", "Sven"],
        "common_mistakes": [],
        "matches_analyzed": 0,
        "last_updated": now_iso(),
    }


def save_player_profile(profile_data: dict[str, Any], data_dir: Path | str | None = None) -> Path:
    """Атомарно сохраняет профиль игрока в player_profile.json."""
    from .paths import atomic_write_json

    path = get_player_profile_path(data_dir)
    profile_data["last_updated"] = now_iso()
    atomic_write_json(path, profile_data, indent=2)
    return path


def update_player_profile_mistakes(
    data_dir: Path | str | None = None,
    mistakes: list[str] | None = None,
    hero_name: str | None = None,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Добавляет выявленные ошибки и сыгранного героя в профиль игрока."""
    profile = load_player_profile(data_dir)
    if account_id:
        profile["account_id"] = account_id
    if hero_name and hero_name != "?":
        sigs = profile.setdefault("signature_heroes", [])
        if hero_name not in sigs:
            sigs.append(hero_name)
            if len(sigs) > 10:
                profile["signature_heroes"] = sigs[-10:]
    if mistakes:
        cur_mistakes = profile.setdefault("common_mistakes", [])
        for m in mistakes:
            clean = str(m).strip()
            if clean and clean not in cur_mistakes:
                cur_mistakes.append(clean)
        profile["common_mistakes"] = cur_mistakes[-12:]
    profile["matches_analyzed"] = int(profile.get("matches_analyzed", 0)) + 1
    save_player_profile(profile, data_dir)
    return profile


def get_player_profile_summary(data_dir: Path | str | None = None) -> str:
    """Формирует компактную сводку профиля игрока для инъекции в системный промпт LLM."""
    profile = load_player_profile(data_dir)
    pos = profile.get("preferred_position", 1)
    sigs = profile.get("signature_heroes", [])
    mistakes = profile.get("common_mistakes", [])
    parts = [f"Основная роль: Pos {pos}"]
    if sigs:
        parts.append("сигнатурные герои: " + ", ".join(sigs[:5]))
    if mistakes:
        parts.append("частые ошибки из прошлых матчей: " + "; ".join(mistakes[:3]))
    return "Профиль игрока: " + ". ".join(parts) + "."


# --------------------------------------------------------------------------- #
# Радар угроз по вражескому драфту (B2)
# --------------------------------------------------------------------------- #

# Короткие знания «герой -> чем он опасен -> что брать». Ключ — короткий id
# NPC-героя (`npc_dota_hero_` отбрасывается). Список намеренно ручной: только
# проверенные, не выдуманные связки, чтобы тренер не галлюцинировал.
_ENEMY_ITEM_PRIORITY: tuple[str, ...] = (
    "item_black_king_bar",
    "item_blink",
    "item_sheepstick",
    "item_orchid",
    "item_silver_edge",
    "item_diffusal_blade",
    "item_aeon_disk",
)


def _hero_threat_key(value: Any) -> str:
    """Нормализует id / npc-имя / отображаемое имя героя через каноническую ``meta_db``."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if text.startswith("npc_dota_hero_"):
        text = text[len("npc_dota_hero_"):]
    try:
        from .meta_db import get_hero_meta_entry

        meta = get_hero_meta_entry(value)
        if meta and meta.get("id"):
            return str(meta["id"])
    except Exception:  # noqa: BLE001
        pass
    text = text.replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _hero_display(hint: Any, catalog: HeroCatalog | None) -> str | None:
    """Человекочитаемое имя героя (npc -> 'Phantom Assassin'), либо None."""
    name = hero_name_for(hint, catalog)
    if not name or name == "?":
        return None
    if name.startswith("npc_dota_hero_"):
        name = name[len("npc_dota_hero_"):].replace("_", " ").title()
    return name or None


def _resolve_my_team(payload: dict | None, catalog: HeroCatalog | None = None) -> str | None:
    """Сторона игрока: player.team_name или (если нет) драфт."""
    if not isinstance(payload, dict):
        return None
    team = _dig(payload, "player", "team_name")
    if isinstance(team, str):
        label = team.strip().lower()
        if label in ("radiant", "dire"):
            return label
    draft = collect_draft(payload, catalog)
    return draft.my_team


def _enemy_hero_candidates(
    payload: dict, catalog: HeroCatalog | None = None
) -> list[str]:
    """Вражеские герои: сначала из драфта, затем (если нужно) из allplayers."""
    candidates: list[str] = []
    draft = collect_draft(payload, catalog)
    candidates.extend(name for name in draft.enemy_picks if isinstance(name, str) and name and name != "?")
    my_team = _resolve_my_team(payload, catalog)
    if my_team:
        for player_entry in _extract_allplayers(payload):
            team = player_entry.get("team")
            if not team or team == my_team:
                continue
            npc = player_entry.get("npc")
            if npc:
                display = _hero_display(npc, catalog)
                if display:
                    candidates.append(display)
            elif player_entry.get("name"):
                candidates.append(str(player_entry["name"]))

    seen: set[str] = set()
    result: list[str] = []
    for name in candidates:
        key = _hero_threat_key(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _join_items_ru(items: list[str]) -> str:
    """'A или B' для двух предметов, 'A' для одного."""
    clean = [item for item in items if item]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    return f"{clean[0]} или {clean[1]}"


def analyze_enemy_threats(
    payload: dict | None,
    catalog: HeroCatalog | None = None,
    items: Mapping[str, str] | None = None,
) -> list[str]:
    """Короткие русские фразы о том, чем опасен вражеский драфт.

    Строятся из пиков врага (``draft`` / ``allplayers``) и, если GSI их отдаёт,
    из видимых предметов и уровней врагов. Если врагов нет — возвращает ``[]``.
    Не бросает исключений на мусорном payload.
    """
    if not isinstance(payload, dict) or not payload:
        return []
    items = items or {}
    candidates = _enemy_hero_candidates(payload, catalog)
    if not candidates:
        return []

    from .meta_db import get_hero_threat_summary

    phrases: list[str] = []
    covered: set[str] = set()
    for candidate in candidates:
        key = _hero_threat_key(candidate)
        if not key or key in covered:
            continue
        hint = get_hero_threat_summary(candidate)
        if hint is None:
            continue
        covered.add(key)
        display, threat, counter_items = hint
        phrase = f"У врага {display} — {threat}."
        counters = _join_items_ru(list(counter_items[:2]))
        if counters:
            phrase += f" Возьми {counters}."
        phrases.append(phrase)
        if len(phrases) >= 5:
            break

    # Видимые опасные предметы врага (если GSI их вообще отдаёт).
    if len(phrases) < 5:
        my_team = _resolve_my_team(payload, catalog)
        visible: set[str] = set()
        if my_team:
            for player_entry in _extract_allplayers(payload):
                team = player_entry.get("team")
                if team and team != my_team:
                    visible.update(player_entry.get("items") or [])
        dangerous = [key for key in _ENEMY_ITEM_PRIORITY if key in visible]
        if dangerous:
            names = [item_name(key, items) for key in dangerous[:2]]
            names = [name for name in names if name and name != "?"]
            if names:
                phrases.append(
                    f"У врага уже {' и '.join(names)} — не стой кучно и кайти инициацию."
                )

    # Самый прокачанный враг, если по нему ещё не было фразы.
    if len(phrases) < 5:
        my_team = _resolve_my_team(payload, catalog)
        best: tuple[int, str] | None = None
        if my_team:
            for player_entry in _extract_allplayers(payload):
                team = player_entry.get("team")
                if not team or team == my_team:
                    continue
                level = player_entry.get("level")
                if not isinstance(level, int) or level < 12:
                    continue
                npc = player_entry.get("npc")
                key = _hero_threat_key(npc) if npc else ""
                if key and key in covered:
                    continue
                display = _hero_display(npc, catalog) if npc else (player_entry.get("name") or "?")
                if display and display != "?" and (best is None or level > best[0]):
                    best = (level, str(display))
        if best is not None:
            phrases.append(
                f"Самый сильный враг — {best[1]} ({best[0]} ур.), фокусь его в драке."
            )

    return phrases[:5]


# --------------------------------------------------------------------------- #
# Динамический GSI-снимок для LLM
# --------------------------------------------------------------------------- #


def _display_items_from_keys(raw_keys: Iterable[Any], items: Mapping[str, str] | None) -> list[str]:
    """Список сырых ``item_*`` -> человекочитаемые названия."""
    mapping = items or {}
    display: list[str] = []
    for raw in raw_keys or []:
        if not isinstance(raw, str) or not raw:
            continue
        name = item_name(raw, mapping)
        if name and name != "?":
            display.append(name)
    return display


def _player_entry_snapshot(
    entry: Mapping[str, Any],
    catalog: HeroCatalog | None,
    items: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Один элемент ``my_team_players`` / ``enemy_players`` (B1)."""
    npc = entry.get("npc")
    hero_display: str | None = None
    if isinstance(npc, str) and npc:
        hero_display = _hero_display(npc, catalog)
    name = entry.get("name") or hero_display
    return {
        "name": name if isinstance(name, str) and name else None,
        "level": entry.get("level"),
        "kills": entry.get("kills"),
        "deaths": entry.get("deaths"),
        "assists": entry.get("assists"),
        "alive": entry.get("alive"),
        "respawn_seconds": entry.get("respawn_seconds"),
        "net_worth": entry.get("net_worth"),
        "items": _display_items_from_keys(entry.get("items") or [], items),
    }


def _self_player_snapshot(
    payload: dict,
    catalog: HeroCatalog | None,
    items: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    """Сам игрок из секций ``hero`` + ``player`` (если allplayers пуст)."""
    hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    if not hero and not player:
        return None
    hint = current_hero_npc(payload) or hero.get("id")
    # Без выбранного героя запись «сам игрок» бесполезна и создаёт мусор.
    if hint is None:
        return None
    hero_display: str | None = None
    if hint is not None:
        hero_display = _hero_display(hint, catalog)
    name = player.get("name") or hero_display
    alive = hero.get("alive")
    return {
        "name": name if isinstance(name, str) and name else None,
        "level": _i(hero.get("level")),
        "kills": _i(player.get("kills")),
        "deaths": _i(player.get("deaths")),
        "assists": _i(player.get("assists")),
        "alive": alive if isinstance(alive, bool) else None,
        "respawn_seconds": _i(hero.get("respawn_seconds")),
        "net_worth": _i(player.get("net_worth")),
        "items": _item_display_list(extract_own_items(payload), items),
    }


def _empty_snapshot_extras() -> dict[str, Any]:
    """Новые ключи B1 для offline-снимка (чтобы потребители не падали)."""
    return {
        "hero_items_display": [],
        "inventory_keys": [],
        "net_worth": 0,
        "net_worth_estimated": False,
        "neutral_item": None,
        "aghanims_scepter": False,
        "aghanims_shard": False,
        "buyback_cost": None,
        "buyback_available": False,
        "has_tp": False,
        "my_team_players": [],
        "enemy_players": [],
        "enemy_threats": [],
        "objectives": "",
    }


def get_live_gsi_snapshot(
    payload: dict | None,
    catalog: HeroCatalog | None = None,
    items: Mapping[str, str] | None = None,
    position: int | None = None,
    mmr_bracket: str | None = None,
    item_costs: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Извлекает структурированный срез текущего состояния матча для ИИ-тренера.

    Обратно совместим: старые ключи не меняются, новые ключи контракта B1
    (нейтралка, аганим/шард, байбек, ТП, составы команд, угрозы, цели) только
    добавляются. Контракт v1.2 добавляет ``inventory_keys``, ``net_worth``
    (GSI или оценка по цене предметов) и ``net_worth_estimated``; словарь цен
    ``item_costs`` (display-имя → цена) передаётся необязательным keyword.
    На кривом payload возвращает offline-снимок без исключений.
    """
    items = items or {}
    if not isinstance(payload, dict):
        offline: dict[str, Any] = {
            "available": False,
            "game_stage": "offline",
            "clock_seconds": None,
            "clock_formatted": "?",
            "position": position or 1,
        }
        offline.update(_empty_snapshot_extras())
        return offline

    map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}

    clock_sec = _i(map_data.get("clock_time"))
    if clock_sec is None:
        stage = "unknown"
    elif clock_sec < 0:
        stage = "pre_game"
    elif clock_sec < 600:
        stage = "laning"
    elif clock_sec < 1800:
        stage = "midgame"
    else:
        stage = "lategame"

    hero_name = hero_name_for(hero.get("name") or hero.get("id"), catalog) if hero else None
    # Реальный GSI: предметы в верхнеуровневой секции payload["items"].
    raw_items = extract_own_items(payload)
    # Старый ключ "inventory" сохраняет прежнюю семантику: только основные слоты.
    inv_names: list[str] = []
    inventory_keys: list[str] = []
    for slot_name in _item_slots(raw_items):
        display = item_name(slot_name, items)
        if display and display != "?":
            inv_names.append(display)
            inventory_keys.append(slot_name)
    net_worth, net_worth_estimated = estimate_net_worth(
        payload, item_costs, items=items
    )

    draft = collect_draft(payload, catalog)
    my_team = player.get("team_name")
    if isinstance(my_team, str):
        my_team = my_team.strip().lower()
    if my_team not in ("radiant", "dire"):
        my_team = draft.my_team or "radiant"

    # --- Составы команд: allplayers + собственная секция hero/player --------- #
    allplayers = _extract_allplayers(payload)
    my_team_players: list[dict[str, Any]] = []
    enemy_players: list[dict[str, Any]] = []
    own_npcs: set[str] = set()
    own_names: set[str] = set()
    for entry in allplayers:
        team = entry.get("team")
        if not team:
            continue
        summary = _player_entry_snapshot(entry, catalog, items)
        if team == my_team:
            my_team_players.append(summary)
            npc = entry.get("npc")
            if isinstance(npc, str) and npc:
                own_npcs.add(npc)
            name = entry.get("name")
            if isinstance(name, str) and name:
                own_names.add(name.strip().lower())
        else:
            enemy_players.append(summary)

    self_summary = _self_player_snapshot(payload, catalog, items)
    if self_summary is not None:
        self_npc = current_hero_npc(payload)
        self_name = player.get("name")
        self_name_key = self_name.strip().lower() if isinstance(self_name, str) and self_name else ""
        is_duplicate = (
            len(my_team_players) >= 5
            or (bool(self_npc) and self_npc in own_npcs)
            or (bool(self_name_key) and self_name_key in own_names)
        )
        if not is_duplicate:
            my_team_players.insert(0, self_summary)

    # --- Байбек / аганим / ТП / нейтралка ---------------------------------- #
    buyback_cost = _i(hero.get("buyback_cost"))
    buyback_cd = _i(hero.get("buyback_cooldown"))
    player_gold = _i(player.get("gold"))
    buyback_available = bool(
        buyback_cost
        and buyback_cost > 0
        and (buyback_cd is None or buyback_cd <= 0)
        and (player_gold is None or player_gold >= buyback_cost)
    )

    objectives = ""
    if clock_sec is not None and clock_sec >= 0:
        objectives = objectives_context(clock_sec, get_game_mode(payload))

    statuses = []
    if hero.get("stunned"):
        statuses.append("в стане")
    if hero.get("silenced"):
        statuses.append("в сайленсе")
    if hero.get("disarmed"):
        statuses.append("в дизарме")
    if hero.get("break"):
        statuses.append("истощён")
    if hero.get("hexed"):
        statuses.append("в хексе")
    if hero.get("magicimmune"):
        statuses.append("иммунитет к магии")
    if hero.get("rooted"):
        statuses.append("в оцепенении")
    if hero.get("muted"):
        statuses.append("предметы заглушены")
    if hero.get("taunted"):
        statuses.append("в таунте")

    return {
        "available": True,
        "clock_seconds": clock_sec,
        "clock_formatted": format_clock(clock_sec),
        "game_stage": stage,
        "radiant_score": _i(map_data.get("radiant_score"), 0),
        "dire_score": _i(map_data.get("dire_score"), 0),
        "my_team": my_team,
        "hero_name": hero_name,
        "hero_level": _i(hero.get("level")),
        "facet": hero.get("facet") if hero.get("facet") is not False else None,
        "statuses": statuses,
        "roshan_pit": roshan_pit_location(clock_sec, map_data.get("daytime")),
        "alive": hero.get("alive", True) is not False,
        "health_percent": _f(hero.get("health_percent")),
        "mana_percent": _f(hero.get("mana_percent")),
        "kills": _i(player.get("kills"), 0),
        "deaths": _i(player.get("deaths"), 0),
        "assists": _i(player.get("assists"), 0),
        "gold": _i(player.get("gold"), 0),
        "net_worth": net_worth,
        "net_worth_estimated": net_worth_estimated,
        "gpm": _i(player.get("gpm"), 0),
        "xpm": _i(player.get("xpm"), 0),
        "last_hits": _i(player.get("last_hits"), 0),
        "inventory": inv_names,
        "inventory_keys": inventory_keys,
        "allied_picks": draft.my_picks,
        "enemy_picks": draft.enemy_picks,
        "position": position or 1,
        # --- Новые ключи контракта B1 ------------------------------------- #
        "hero_items_display": list(inv_names),
        "neutral_item": _neutral_item_display(raw_items, items),
        "aghanims_scepter": _as_bool(hero.get("aghanims_scepter")),
        "aghanims_shard": _as_bool(hero.get("aghanims_shard")),
        "buyback_cost": buyback_cost,
        "buyback_available": buyback_available,
        "has_tp": _has_tp(raw_items),
        "my_team_players": my_team_players,
        "enemy_players": enemy_players,
        "enemy_threats": analyze_enemy_threats(payload, catalog, items),
        "objectives": objectives,
    }


def format_gsi_summary(snapshot: dict[str, Any]) -> str:
    """Форматирует снимок в строку для моментальной инъекции в системный промпт LLM."""
    if not snapshot.get("available"):
        return "[GSI: матч не запущен или нет соединения]"

    clock = snapshot.get("clock_formatted", "?")
    stage = snapshot.get("game_stage", "unknown")
    r_score = snapshot.get("radiant_score", 0)
    d_score = snapshot.get("dire_score", 0)
    team = snapshot.get("my_team", "radiant")
    hero = snapshot.get("hero_name") or "Герой не выбран"
    lvl = snapshot.get("hero_level")
    lvl_str = f" ур. {lvl}" if lvl else ""
    k = snapshot.get("kills", 0)
    d = snapshot.get("deaths", 0)
    a = snapshot.get("assists", 0)
    nw = snapshot.get("net_worth", 0)
    gold = snapshot.get("gold", 0)
    pos = snapshot.get("position", 1)
    inv = ", ".join(snapshot.get("inventory", [])) or "пусто"
    allies = ", ".join(snapshot.get("allied_picks", [])) or "нет"
    enemies = ", ".join(snapshot.get("enemy_picks", [])) or "нет"

    return (
        f"[GSI Снимок: {clock} ({stage}) | Счёт {r_score}:{d_score} (сторона {team}) | "
        f"Герой: {hero}{lvl_str}, KDA {k}/{d}/{a}, Золото {gold}, NW {nw} | "
        f"Роль: Pos {pos} | Инвентарь: {inv} | Союзники: {allies} | Враги: {enemies}]"
    )


def is_hero_in_combat(
    payload: Mapping[str, Any] | None,
    *,
    prev_hp_pct: float | None = None,
) -> bool:
    """Определяет, находится ли герой в активной драке/под контролем."""
    if not isinstance(payload, Mapping):
        return False
    hero = payload.get("hero")
    if not isinstance(hero, Mapping) or hero.get("alive") is False:
        return False
    for flag in ("stunned", "silenced", "disarmed", "hexed", "rooted", "taunted"):
        if hero.get(flag):
            return True
    cur_hp = _f(hero.get("health_percent"))
    if cur_hp is not None:
        if prev_hp_pct is not None and (prev_hp_pct - cur_hp) >= 12.0:
            return True
        if cur_hp < 35.0:
            return True
    return False


def compute_derived_signals(
    payload: Mapping[str, Any] | None,
    *,
    position: int = 1,
    next_item_name: str | None = None,
    next_item_cost: int | None = None,
    death_timestamps: list[int] | tuple[int, ...] | None = None,
    lh_idle_sec: int | None = None,
) -> list[str]:
    """Вычисляет компактные смысловые сигналы до передачи в LLM (меньше сырого шума).

    Покрывает: серию смертей за 3 мин, золото и ETA до целевого слота,
    нехватку маны на ульт/прокаст, просадку фарма/простой без крипов.
    """
    if not isinstance(payload, Mapping):
        return []
    map_data = payload.get("map") if isinstance(payload.get("map"), Mapping) else {}
    player = payload.get("player") if isinstance(payload.get("player"), Mapping) else {}
    hero = payload.get("hero") if isinstance(payload.get("hero"), Mapping) else {}
    abilities = payload.get("abilities") if isinstance(payload.get("abilities"), Mapping) else {}

    clock_sec = _i(map_data.get("clock_time"))
    pos = normalize_position(position)
    signals: list[str] = []

    # 1. Серия смертей за последние 3 минуты (180 сек)
    if death_timestamps and clock_sec is not None:
        recent_deaths = [t for t in death_timestamps if (clock_sec - int(t)) <= 180]
        if len(recent_deaths) >= 2:
            signals.append(
                f"серия смертей: {len(recent_deaths)} смерти за последние 3 мин — играй от безопасного фарма и вижна"
            )

    # 2. Остаток золота и примерное время (ETA) до следующего ключевого слота
    gold = _i(player.get("gold")) or 0
    gpm = _i(player.get("gpm")) or 0
    if next_item_name and next_item_cost and int(next_item_cost) > 0:
        missing_gold = max(0, int(next_item_cost) - gold)
        if missing_gold == 0:
            signals.append(f"хватает на {next_item_name} — доложи и привези курьером")
        elif missing_gold <= 1200:
            eff_gpm = max(250, gpm) if gpm > 0 else (450 if pos in (1, 2) else 320)
            eta_sec = max(15, int((missing_gold / eff_gpm) * 60))
            signals.append(
                f"до {next_item_name} осталось ~{missing_gold} золота (~{eta_sec}с) — избегай лишнего риска до покупки"
            )

    # 3. Нехватка маны на ультимейт или базовый прокаст
    hero_lvl = _i(hero.get("level")) or 0
    cur_mana = _f(hero.get("mana"))
    mana_pct = _f(hero.get("mana_percent"))
    if hero_lvl >= 1 and hero.get("alive") is not False:
        ult_cost = 0
        ult_cd = 0
        combo_cost = 0
        for ab in abilities.values():
            if not isinstance(ab, Mapping):
                continue
            cost = _i(ab.get("cost"))
            if cost is None:
                cost = _i(ab.get("mana_cost"))
            cost_val = max(0, cost or 0)
            cd_val = _i(ab.get("cooldown")) or 0
            if ab.get("ultimate") or ab.get("is_ultimate"):
                ult_cost = cost_val
                ult_cd = cd_val
            elif cost_val > 0 and _i(ab.get("level"), 0):
                combo_cost += cost_val

        if ult_cost > 0 and cur_mana is not None and ult_cd == 0 and cur_mana < ult_cost:
            signals.append(
                f"не хватает маны на ультимейт (есть {int(cur_mana)}, нужно {ult_cost}) — заряди ману перед дракой"
            )
        elif combo_cost > 0 and cur_mana is not None and cur_mana < min(combo_cost, 180):
            signals.append(
                f"мало маны на прокаст (есть {int(cur_mana)}) — не вступай в бой без стиков/регена"
            )
        elif hero_lvl >= 6 and mana_pct is not None and mana_pct < 20.0:
            signals.append("критически мало маны (<20%) — заряди ману до инициации")

    # 4. Темп фарма относительно роли и простой без добитых крипов
    lh = _i(player.get("last_hits"))
    if clock_sec is not None and clock_sec >= 240 and lh is not None and pos in (1, 2, 3):
        minutes = clock_sec / 60.0
        target_lh_per_min = 6.5 if pos == 1 else (5.8 if pos == 2 else 4.8)
        expected_lh = int(minutes * target_lh_per_min)
        if expected_lh >= 20 and lh < int(expected_lh * 0.65):
            signals.append(
                f"просадка темпа фарма ({lh} LH к {int(minutes)} мин при норме ~{expected_lh}) — добери линии и стаки"
            )
    if lh_idle_sec is not None and lh_idle_sec >= 65 and pos in (1, 2, 3) and clock_sec is not None and clock_sec >= 300:
        signals.append(f"простой без крипов {lh_idle_sec}с — вернись на фарм, не гуляй впустую")

    return signals[:4]


class MatchSignalTracker:
    """Хранит скользящее окно смертей (3 мин), HP и простоя LH для Контура 1/3."""

    def __init__(self) -> None:
        self._match_id: str | None = None
        self._prev_deaths: int = 0
        self._death_clocks: list[int] = []
        self._last_lh: int | None = None
        self._last_lh_clock: int | None = None
        self._prev_hp_pct: float | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._match_id = None
            self._prev_deaths = 0
            self._death_clocks.clear()
            self._last_lh = None
            self._last_lh_clock = None
            self._prev_hp_pct = None

    def observe(
        self,
        payload: Mapping[str, Any] | None,
        *,
        position: int = 1,
        next_item_name: str | None = None,
        next_item_cost: int | None = None,
    ) -> tuple[list[str], bool]:
        if not isinstance(payload, Mapping):
            return [], False
        map_data = payload.get("map") if isinstance(payload.get("map"), Mapping) else {}
        player = payload.get("player") if isinstance(payload.get("player"), Mapping) else {}
        hero = payload.get("hero") if isinstance(payload.get("hero"), Mapping) else {}

        match_id = extract_match_id(payload)
        clock_sec = _i(map_data.get("clock_time"))
        deaths = _i(player.get("deaths")) or 0
        lh = _i(player.get("last_hits"))
        hp_pct = _f(hero.get("health_percent"))

        with self._lock:
            if match_id and self._match_id != match_id:
                self._match_id = match_id
                self._prev_deaths = deaths
                self._death_clocks.clear()
                self._last_lh = lh
                self._last_lh_clock = clock_sec
                self._prev_hp_pct = hp_pct

            in_combat = is_hero_in_combat(payload, prev_hp_pct=self._prev_hp_pct)
            if hp_pct is not None:
                self._prev_hp_pct = hp_pct

            if deaths > self._prev_deaths:
                delta = min(5, deaths - self._prev_deaths)
                stamp = clock_sec if clock_sec is not None else 0
                for _ in range(delta):
                    self._death_clocks.append(stamp)
                self._prev_deaths = deaths
            elif deaths < self._prev_deaths:
                self._prev_deaths = deaths
                self._death_clocks.clear()

            if clock_sec is not None:
                self._death_clocks = [t for t in self._death_clocks if (clock_sec - t) <= 180]

            lh_idle_sec = 0
            if lh is not None and clock_sec is not None:
                if self._last_lh is None or lh > self._last_lh:
                    self._last_lh = lh
                    self._last_lh_clock = clock_sec
                elif self._last_lh_clock is not None and clock_sec > self._last_lh_clock:
                    lh_idle_sec = clock_sec - self._last_lh_clock

            clocks_snapshot = list(self._death_clocks)

        signals = compute_derived_signals(
            payload,
            position=position,
            next_item_name=next_item_name,
            next_item_cost=next_item_cost,
            death_timestamps=clocks_snapshot,
            lh_idle_sec=lh_idle_sec,
        )
        return signals, in_combat


# --------------------------------------------------------------------------- #
# Тайминги для мидера и макро-оповещения (MidTimersTracker)
# --------------------------------------------------------------------------- #


class MidTimersTracker:
    """Фасад совместимости над каноническим ``CanonicalMapTimers`` (Phase 4 / Fork F2)."""

    def __init__(
        self,
        enabled: bool = True,
        *,
        core: CanonicalMapTimers | None = None,
        proactive_enabled: bool = True,
    ):
        self._core = core if core is not None else CanonicalMapTimers(
            enabled=bool(enabled),
            proactive_enabled=bool(proactive_enabled),
        )
        if core is None:
            self._core.enabled = bool(enabled)
            self._core.proactive_enabled = bool(proactive_enabled)

    @property
    def core(self) -> CanonicalMapTimers:
        return self._core

    @property
    def enabled(self) -> bool:
        return self._core.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._core.enabled = bool(value)

    @property
    def proactive_enabled(self) -> bool:
        return self._core.proactive_enabled

    @proactive_enabled.setter
    def proactive_enabled(self, value: bool) -> None:
        self._core.proactive_enabled = bool(value)

    @property
    def _alerted_tormentor(self) -> bool:
        return self._core._alerted_tormentor

    @_alerted_tormentor.setter
    def _alerted_tormentor(self, value: bool) -> None:
        self._core._alerted_tormentor = bool(value)

    def reset(self) -> None:
        """Сбрасывает историю всех оповещений при старте нового матча."""
        self._core.reset()

    def sync_match_id(self, match_id: Any) -> bool:
        """Синхронизирует ``match_id`` и сбрасывает таймеры при смене матча."""
        return self._core.sync_match_id(match_id)

    def check_mid_runes(
        self,
        clock_seconds: Any,
        position: int = 1,
        *,
        match_id: Any = None,
    ) -> str | None:
        """Проверяет появление водных и активных рун на миде ровно за 20 секунд."""
        return self._core.check_mid_runes(clock_seconds, position=position, match_id=match_id)

    def check_mid_level_6(
        self,
        payload: dict | None,
        clock_seconds: Any = None,
        vision_level: int | None = None,
        clock_sec: Any = None,
        *,
        enemy_mid_level: int | None = None,
        match_id: Any = None,
        allow_predictive: bool = True,
    ) -> str | None:
        """Детектор 6-го уровня вражеского мидера (EnemyMidLevel6Alert)."""
        return self._core.check_mid_level_6(
            payload,
            clock_seconds=clock_seconds,
            vision_level=vision_level,
            clock_sec=clock_sec,
            enemy_mid_level=enemy_mid_level,
            match_id=match_id,
            allow_predictive=allow_predictive,
        )

    def check_macro_timers(
        self,
        clock_seconds: Any,
        position: int = 1,
        team_name: str | None = None,
        turbo: bool = False,
        *,
        match_id: Any = None,
    ) -> str | None:
        """Общие и специализированные тайминги через канонический ``CanonicalMapTimers``."""
        return self._core.check_macro_timers(
            clock_seconds,
            position=position,
            team_name=team_name,
            turbo=turbo,
            match_id=match_id,
        )


# --------------------------------------------------------------------------- #
# Автономный агент анализа игры (AutonomousCoachAgent)
# --------------------------------------------------------------------------- #


_KEY_POWER_SPIKE_ITEMS = {
    "item_blink": "Blink Dagger — отличный момент для неожиданной инициации и врыва в драку",
    "item_black_king_bar": "BKB готов — жмите драку пока неуязвимы к магии и контролю",
    "item_orchid": "Orchid собран — выходите на одиночные цели с прокастом через сайленс",
    "item_ultimate_scepter": "Aghanim's Scepter получен — используй усиление ультимейта по максимуму",
    "item_manta": "Manta Style собрана — используй иллюзии для пуша и сброса дебаффов",
    "item_sheepstick": "Hex (Scythe of Vyse) готов — фокусите ключевого вражеского кора моментально",
    "item_silver_edge": "Silver Edge собран — отключайте опасные пассивки врагов ударом из инвиза",
    "item_desolator": "Desolator готов — срезайте броню и пушьте башни после киллов",
    "item_diffusal_blade": "Diffusal Blade собран — сжигайте ману и замедляйте врагов на линии",
}

_DANGEROUS_ENEMY_ITEMS: dict[str, str] = {
    "item_blink": "держи позиционку и не стой кучно",
    "item_black_king_bar": "кайти его в драке",
    "item_orchid": "держитесь ближе к союзникам с диспелом",
    "item_silver_edge": "держите варды и просветку",
    "item_diffusal_blade": "он сжигает ману — не дерись на пустом манапуле",
    "item_sheepstick": "держитесь дальше от инициатора с Хексом",
    "item_aeon_disk": "не трать весь бурст в цель под диском",
    "item_bloodthorn": "сайленс и криты — нужен диспел",
}


def _russian_deaths(count: int) -> str:
    """Правильное слово для числа смертей: 2 смерти, 5 смертей."""
    try:
        number = abs(int(count))
    except (TypeError, ValueError):
        return "смертей"
    if number % 10 == 1 and number % 100 != 11:
        return "смерть"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return "смерти"
    return "смертей"


def _aegis_side(
    value: Any,
    payload: dict | None,
    catalog: HeroCatalog | None,
    my_team: str | None,
) -> str | None:
    """Чья сторона держит аегис: 'radiant' / 'dire' / None, если не понять."""
    if value == "self":
        return my_team
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        slot = int(value)
        if 2 <= slot <= 4:
            return "radiant"
        if 128 <= slot <= 132:
            return "dire"
        if 0 <= slot <= 1:
            return "radiant"
        if 5 <= slot <= 9:
            return "dire"
        return None
    if isinstance(value, dict):
        team_name = value.get("team_name")
        side = _aegis_side(team_name, payload, catalog, my_team)
        if side:
            return side
        team_num = value.get("team")
        if isinstance(team_num, (int, float)) and int(team_num) in (2, 3):
            return {2: "radiant", 3: "dire"}[int(team_num)]
        for key in ("player_slot", "slot", "player_id"):
            if key in value:
                side = _aegis_side(value.get(key), payload, catalog, my_team)
                if side:
                    return side
        for key in ("hero", "hero_name", "name", "player"):
            if key in value:
                side = _aegis_side(value.get(key), payload, catalog, my_team)
                if side:
                    return side
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("radiant", "dire"):
            return text
        if text.isdigit():
            return _aegis_side(int(text), payload, catalog, my_team)
        key = _hero_threat_key(value)
        if not key:
            return None
        draft = collect_draft(payload, catalog) if isinstance(payload, dict) else DraftInfo()
        if key in {_hero_threat_key(name) for name in draft.my_picks}:
            return my_team or "radiant"
        if key in {_hero_threat_key(name) for name in draft.enemy_picks}:
            return "dire" if (my_team or "radiant") == "radiant" else "radiant"
        if isinstance(payload, dict):
            for entry in _extract_allplayers(payload):
                team = entry.get("team")
                if not team:
                    continue
                candidates = {
                    _hero_threat_key(entry.get("npc")),
                    _hero_threat_key(entry.get("name")),
                }
                if key in candidates:
                    return team
        return None
    return None


class AutonomousCoachAgent:
    """Фоновый агент автономных подсказок (Power Spikes, макро-цели, анти-тилт).

    ``skill_level`` влияет на тон: новичку к подсказке добавляется короткое
    объяснение «почему это важно», опытный игрок получает прежний текст без
    разжёвывания.
    """

    def __init__(
        self,
        autonomy_level: str = "medium",
        cooldown_seconds: float = 45.0,
        on_advice: Callable[[str], None] | None = None,
        advice_ttl_seconds: float = 240.0,
        skill_level: str = "beginner",
        macro_timer: Any = None,
    ):
        self.autonomy_level = autonomy_level.strip().lower()
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.on_advice = on_advice
        self.skill_level = normalize_skill_level(skill_level)
        shared_core = getattr(macro_timer, "core", getattr(macro_timer, "_core", None))
        self.mid_timers = MidTimersTracker(core=shared_core)
        self._active_match_id: str | None = None

        self.last_advice_time: float = 0.0
        self._prev_player_items: set[str] = set()
        self._prev_enemy_items: dict[str, set[str]] = {}
        self._prev_deaths: int = 0
        self._death_streak: int = 0
        self._last_death_time: float = 0.0
        self._own_items_seeded = False
        self._enemy_items_seeded = False
        self._aegis_reported = False
        self._roshan_reported = False
        self._advice_ttl_seconds = max(0.0, float(advice_ttl_seconds))
        self._advice_cache: dict[str, float] = {}
        self._lock = threading.Lock()

    def reset(self, *, reset_timers: bool = True) -> None:
        """Сбрасывает состояние при начале нового матча."""
        with self._lock:
            if reset_timers:
                self.mid_timers.reset()
            self._active_match_id = None
            self.last_advice_time = 0.0
            self._prev_player_items.clear()
            self._prev_enemy_items.clear()
            self._prev_deaths = 0
            self._death_streak = 0
            self._last_death_time = 0.0
            self._own_items_seeded = False
            self._enemy_items_seeded = False
            self._aegis_reported = False
            self._roshan_reported = False
            self._advice_cache.clear()

    def is_autonomy_off(self) -> bool:
        """True, если автономные проактивные подсказки полностью отключены."""
        return self.autonomy_level in ("off", "none", "request_only", "только по запросу", "выкл")

    def set_autonomy_level(self, level: str) -> None:
        """Настраивает уровень автономности: 'high' / 'medium' / 'off'."""
        self.autonomy_level = level.strip().lower()

    def _sync_match_id(self, payload: Mapping[str, Any]) -> str | None:
        match_id = extract_match_id(payload)
        if not match_id:
            return None
        # CanonicalMapTimers сам сбрасывает таймеры ровно один раз на новый matchid
        # (даже если его уже переключил Coach.check_skills_alerts на том же тике).
        self.mid_timers.sync_match_id(match_id)
        if self._active_match_id is not None and match_id != self._active_match_id:
            self.reset(reset_timers=False)
            self._active_match_id = match_id
            return match_id
        if self._active_match_id is None:
            self._active_match_id = match_id
        return match_id

    def set_skill_level(self, level: str) -> str:
        """Нормализует и применяет уровень игрока (beginner/amateur/advanced/expert)."""
        self.skill_level = normalize_skill_level(level)
        return self.skill_level

    def _beginner_note(self, text: str, why: str) -> str:
        """Добавляет новичку короткое объяснение «почему это важно»."""
        if self.skill_level != "beginner" or not why:
            return text
        return f"{text} Почему это важно: {why}"

    def process_tick(
        self,
        payload: dict | None,
        catalog: HeroCatalog | None = None,
        items: Mapping[str, str] | None = None,
        position: int = 1,
    ) -> str | None:
        """Анализирует очередной GSI-тик и возвращает совет, если сработал триггер."""
        if not isinstance(payload, dict):
            return None

        match_id = self._sync_match_id(payload)

        map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        clock_sec = _i(map_data.get("clock_time"))
        if clock_sec is None or clock_sec < 0:
            return None

        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
        items = items or {}
        now = time.time()

        my_team = player.get("team_name")
        if isinstance(my_team, str):
            my_team = my_team.strip().lower()
        if my_team not in ("radiant", "dire"):
            my_team = None

        # Всегда фиксируем изменения смертей на каждом тике
        cur_deaths = _i(player.get("deaths"), 0)
        if cur_deaths > self._prev_deaths:
            if now - self._last_death_time < 240.0:
                self._death_streak += (cur_deaths - self._prev_deaths)
            else:
                self._death_streak = (cur_deaths - self._prev_deaths)
            self._prev_deaths = cur_deaths
            self._last_death_time = now

        # При выключенной автономности запрещены любые проактивные тик-алерты
        # (пользовательские ручные таймеры в MacroTimerSkill работают независимо).
        if self.is_autonomy_off():
            return None

        # 1. Приоритетные тайминги (руны на миде, 6-й уровень мидера, отводы, стаки, макро)
        # Если общий CanonicalMapTimers уже выдал алерт на этой же секунде (например, через MacroTimerSkill),
        # не дублируем второе срабатывание поверх него.
        if self.mid_timers.core.last_alert_clock_sec != clock_sec:
            mid_rune = self.mid_timers.check_mid_runes(clock_sec, position=position, match_id=match_id)
            if mid_rune:
                return mid_rune if self._emit(mid_rune, now) else None

            lvl6_alert = self.mid_timers.check_mid_level_6(payload, clock_sec, match_id=match_id)
            if lvl6_alert:
                return lvl6_alert if self._emit(lvl6_alert, now) else None

            game_mode = map_data.get("game_mode")
            turbo = is_turbo(game_mode)
            macro_alert = self.mid_timers.check_macro_timers(
                clock_sec,
                position=position,
                turbo=turbo,
                match_id=match_id,
            )
            if macro_alert:
                return macro_alert if self._emit(macro_alert, now) else None

        # 2. Аегис / Рошан — только если GSI реально отдаёт эти поля.
        aegis_alert = self._check_aegis_and_roshan(payload, catalog, my_team)
        if aegis_alert:
            return aegis_alert if self._emit(aegis_alert, now) else None

        # 3. Проверка кулдауна между свободными советами (не менее 45–60 сек)
        if now - self.last_advice_time < self.cooldown_seconds:
            return None

        # 4. Свои Power Spikes (детекция покупки ключевых артефактов)
        cur_items = set(_item_keys(extract_own_items(payload)))
        if not self._own_items_seeded:
            # Первый тик матча — запоминаем инвентарь без «советов на всё сразу».
            self._own_items_seeded = True
            self._prev_player_items = cur_items
        else:
            new_items = cur_items - self._prev_player_items
            self._prev_player_items = cur_items
            for item_key in sorted(new_items):
                if item_key in _KEY_POWER_SPIKE_ITEMS:
                    advice = f"Power Spike! {_KEY_POWER_SPIKE_ITEMS[item_key]}."
                    advice = self._beginner_note(
                        advice,
                        "паверспайк — короткое окно, когда ты сильнее врагов, и его выгодно "
                        "использовать для драки или объекта, пока оно не закрылось.",
                    )
                    if self._emit(advice, now):
                        self.last_advice_time = now
                        return advice

        # 5. Опасные предметы у врага (паверспайк по видимым предметам)
        enemy_advice = self._check_enemy_power_spikes(payload, catalog, items, my_team)
        if enemy_advice and self._emit(enemy_advice, now):
            self.last_advice_time = now
            return enemy_advice

        # 6. Анти-тилт / камбэк: серия смертей с учётом роли и героя
        if self._death_streak >= 2:
            streak = self._death_streak
            self._death_streak = 0
            advice = self._death_streak_advice(payload, catalog, position, streak)
            if self._emit(advice, now):
                self.last_advice_time = now
                return advice
            return None

        # 7. Макро-цели (только при высокой автономности)
        if self.autonomy_level in ("high", "высокая"):
            # Детекция Рошана: 15+ мин и 2+ мёртвых врага с таймером респавна
            if clock_sec >= 900:
                allplayers = _extract_allplayers(payload)
                dead_enemies = [
                    entry for entry in allplayers
                    if entry.get("team") and my_team and entry.get("team") != my_team
                    and (_i(entry.get("respawn_seconds")) or 0) > 15
                ]
                if len(dead_enemies) >= 2:
                    advice = "Двое ключевых врагов в таверне! Отличный тайминг забрать Рошана или снести Т2 вышку."
                    if self._emit(advice, now):
                        self.last_advice_time = now
                        return advice

            # Терзатель на 20:00+ минуте
            if 1200 <= clock_sec <= 1350 and not self.mid_timers._alerted_tormentor:
                self.mid_timers._alerted_tormentor = True
                advice = "Двадцатая минута — заберите Терзателя всей командой для бесплатного Шарда."
                if self._emit(advice, now):
                    self.last_advice_time = now
                    return advice

        # 8. Золото на руках (предотвращение потери при смерти)
        gold = _i(player.get("gold"), 0) or 0
        is_alive = _as_bool(hero.get("alive"), True)
        if is_alive and gold >= 3200 and clock_sec >= 600:
            advice = f"У тебя накопилось {gold} золота. Не держи лишнее — закажи ключевой артефакт курьером!"
            advice = self._beginner_note(
                advice,
                "золото в кармане не даёт силы: предмет в инвентаре начинает работать сразу, "
                "а после смерти часть золота уходит врагу.",
            )
            if self._emit(advice, now):
                self.last_advice_time = now
                return advice

        return None

    # ------------------------------------------------------------------ #
    # Выдача советов и защита от повторов
    # ------------------------------------------------------------------ #

    def _emit(self, text: str, now: float | None = None) -> bool:
        """Отправляет совет, если такой текст не звучал последние 3–5 минут."""
        if not isinstance(text, str) or not text.strip():
            return False
        text = text.strip()
        stamp = time.time() if now is None else now
        with self._lock:
            last = self._advice_cache.get(text)
            if last is not None and stamp - last < self._advice_ttl_seconds:
                return False
            self._advice_cache[text] = stamp
            if len(self._advice_cache) > 48:
                expired = [
                    key for key, moment in self._advice_cache.items()
                    if stamp - moment >= self._advice_ttl_seconds
                ]
                for key in expired:
                    self._advice_cache.pop(key, None)
        self._dispatch_advice(text)
        return True

    def _dispatch_advice(self, text: str) -> None:
        """Передает голосовой совет через коллбек."""
        if self.on_advice and text:
            try:
                self.on_advice(text)
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка в on_advice AutonomousCoachAgent: %s", exc)

    # ------------------------------------------------------------------ #
    # Серия смертей: совет под роль и героя
    # ------------------------------------------------------------------ #

    def _death_streak_advice(
        self,
        payload: dict,
        catalog: HeroCatalog | None,
        position: int,
        streak: int,
    ) -> str:
        """Совет после серии смертей: разный для кора и саппорта, с именем героя."""
        hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
        hero_display = None
        if hero:
            hero_display = _hero_display(hero.get("name") or hero.get("id"), catalog)
        count = max(2, int(streak))
        intro = f"Не тильтуй: {count} {_russian_deaths(count)} подряд"
        if hero_display:
            intro += f" на {hero_display}"

        try:
            pos = int(position)
        except (TypeError, ValueError):
            pos = None
        if pos == 1:
            tail = "смени линию, уйди в безопасный лес под вардами и добери ключевой слот."
        elif pos == 2:
            tail = "не ходи в реку без видения, фарми ближний лес и держи TP готовым."
        elif pos == 3:
            tail = "играй от вышки, проси ганги у саппортов и собери BKB или Pipe."
        elif pos in (4, 5):
            tail = "держи позицию за кором, ставь варды и не ходи в неизвестный лес один."
        else:
            tail = "смени линию, уйди в безопасный лес и собери Ghost Scepter или Force Staff."
        return self._beginner_note(
            f"{intro} — {tail}",
            "каждая смерть отдаёт врагу золото и опыт, а тебе — время на возврат в игру; "
            "лучше отойти, дождаться вижна и вернуться в фарм.",
        )

    # ------------------------------------------------------------------ #
    # Паверспайк врага по видимым предметам
    # ------------------------------------------------------------------ #

    def _enemy_item_sets(
        self, payload: dict, my_team: str | None
    ) -> dict[str, set[str]]:
        """Видимые предметы врагов: {ключ героя: набор item_*}."""
        result: dict[str, set[str]] = {}
        if not my_team:
            return result
        for entry in _extract_allplayers(payload):
            team = entry.get("team")
            if not team or team == my_team:
                continue
            hero_key = entry.get("npc") or entry.get("name")
            if not hero_key:
                continue
            bucket = result.setdefault(str(hero_key), set())
            bucket.update(item for item in (entry.get("items") or []) if isinstance(item, str))
        return result

    def _check_enemy_power_spikes(
        self,
        payload: dict,
        catalog: HeroCatalog | None,
        items: Mapping[str, str],
        my_team: str | None,
    ) -> str | None:
        """Замечает новый опасный предмет у врага (Blink, BKB, Hex, Orchid...)."""
        enemy_now = self._enemy_item_sets(payload, my_team)
        if not enemy_now:
            return None
        if not self._enemy_items_seeded:
            self._enemy_items_seeded = True
            self._prev_enemy_items = enemy_now
            return None

        advice: str | None = None
        for hero_key, current in enemy_now.items():
            previous = self._prev_enemy_items.get(hero_key)
            self._prev_enemy_items[hero_key] = set(current)
            if previous is None:
                # Враг впервые попал в поле зрения — не считаем его инвентарь «новым».
                continue
            new_items = current - previous
            for item_key in sorted(new_items):
                tail = _DANGEROUS_ENEMY_ITEMS.get(item_key)
                if not tail:
                    continue
                display_item = item_name(item_key, items)
                display_hero = _hero_display(hero_key, catalog)
                if display_hero and display_hero != display_item:
                    advice = f"Внимание! У врага {display_hero} появился {display_item}: {tail}."
                else:
                    advice = f"Внимание! У врага появился {display_item}: {tail}."
                break
            if advice:
                break
        return advice

    # ------------------------------------------------------------------ #
    # Аегис / Рошан
    # ------------------------------------------------------------------ #

    def _check_aegis_and_roshan(
        self,
        payload: dict,
        catalog: HeroCatalog | None,
        my_team: str | None,
    ) -> str | None:
        """Детект аегиса/Рошана, только если GSI отдаёт соответствующие поля."""
        map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        raw_aegis = None
        for key in ("aegis", "aegis_owner"):
            value = map_data.get(key)
            if value:
                raw_aegis = value
                break
        if raw_aegis is None:
            own_items = extract_own_items(payload)
            item_list = own_items.values() if isinstance(own_items, dict) else (own_items or ())
            for it in item_list:
                it_name = it.get("name") if isinstance(it, dict) else str(it)
                if it_name and "item_aegis" in it_name.lower():
                    raw_aegis = "self"
                    break
        if raw_aegis is not None:
            if self._aegis_reported:
                return None
            self._aegis_reported = True
            side = _aegis_side(raw_aegis, payload, catalog, my_team)
            if side is not None and my_team is not None:
                if side == my_team:
                    return self._beginner_note(
                        "У тебя аегис — можно давить на вышку и играть агрессивнее, пока он активен.",
                        "аегис даёт второй шанс в драке: одна смерть не отправит тебя на базу, "
                        "поэтому команда может играть смелее.",
                    )
                return self._beginner_note(
                    "У врага аегис — не дерись в лоб, кайти бой и не стой в одиночку.",
                    "пока у врага аегис, убийство не выключает его из игры, и он может "
                    "вернуться в драку — лучше кайтить и заставлять тратить аегис.",
                )
            return self._beginner_note(
                "Аегис забран — не дерись в лоб и держись рядом с командой.",
                "аегис даёт его владельцу второй шанс, поэтому драться нужно аккуратно и от вижна.",
            )
        if self._aegis_reported:
            # Аегис больше не в игре — следующий Рошан снова даст подсказку.
            self._aegis_reported = False

        timer = _i(map_data.get("roshan_respawn_timer"))
        if timer is None:
            timer = _i(_dig(payload, "roshan_respawn_timer"))
        if timer is None:
            return None
        if timer <= 0:
            self._roshan_reported = False
            return None
        if self._roshan_reported:
            return None
        self._roshan_reported = True
        respawn_in = _fmt_minute(timer / 60.0)
        return self._beginner_note(
            f"Рошан убит — аегис уже в игре, респавн примерно через {respawn_in}. "
            "Держись команды и не дерись в лоб.",
            "контроль Рошана даёт аегис и темп для пуша, поэтому команде важно знать, "
            "когда босс вернётся.",
        )

