"""Превращение JSON от Game State Integration в компактный русский контекст.

Всё, что здесь происходит, — защитный разбор чужого JSON: Dota 2 присылает
разные наборы полей в зависимости от фазы игры и настроек .cfg.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .heroes import HeroCatalog
from .roles import position_label

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


def objectives_context(clock_seconds: Any, game_mode: Any = None) -> str:
    """Что из таймингов скоро наступит: руны, лотусы, Терзатель, Рошан."""
    minute = (_i(clock_seconds) or 0) / 60.0
    if minute < 0:
        return ""
    wisdom = _next_occurrence(minute, 7, 7)
    lotus = _next_occurrence(minute, 3, 3)
    tormentor = _next_occurrence(minute, 20, 10)
    parts = [
        f"руны мудрости {_fmt_minute(wisdom)}",
        f"лотусы {_fmt_minute(lotus)}",
        f"Терзатель {_fmt_minute(tormentor)}",
    ]
    if minute < 15:
        parts.append("Рошан: первый Аегис примерно с 15:00")
    else:
        parts.append("Рошан: возможно, жив (Аегис/Рефрешер)")
    return "Ближайшие цели — " + ", ".join(parts) + "."


def neutral_tier(minute: float, turbo: bool) -> int:
    if turbo:
        if minute < 4.5:
            return 1
        if minute < 9:
            return 2
        if minute < 14:
            return 3
        return 4
    if minute < 17:
        return 1
    if minute < 27:
        return 2
    if minute < 37:
        return 3
    return 4


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


def collect_draft(payload: dict | None, catalog: HeroCatalog | None) -> DraftInfo:
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


def _extract_allplayers(payload: dict) -> list[dict[str, Any]]:
    """Достаёт список игроков из разных вариантов GSI (если они вообще есть)."""
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
            npc = hero if isinstance(hero, str) else _dig(hero, "name")
            team = entry.get("team_name")
            if not isinstance(team, str):
                team_num = _i(entry.get("team"))
                team = {2: "radiant", 3: "dire"}.get(team_num) if team_num else None
            level = _i(entry.get("level")) or _i(_dig(hero, "level"))
            name = entry.get("name") or entry.get("player_name")
            result.append({"npc": npc, "team": team, "level": level, "name": name})
        if result:
            break
    return result


def current_hero_npc(payload: dict | None) -> str | None:
    npc = _dig(payload, "hero", "name")
    return npc if isinstance(npc, str) and npc else None


# --------------------------------------------------------------------------- #
# Извлечение предметов и детектор ошибок
# --------------------------------------------------------------------------- #


def _item_slots(raw_items: Any) -> list[str]:
    """Имена предметов в основных слотах (slot0..slot5), без мусора."""
    names: list[str] = []
    if not isinstance(raw_items, dict):
        return names
    for slot in sorted(raw_items):
        if not str(slot).startswith("slot"):
            continue
        value = raw_items[slot]
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and name and name != "item_empty":
            names.append(name)
    return names


def _has_tp(raw_items: Any) -> bool:
    """Есть ли TP-свиток или Travel Boots (в слотах или специальном слоте ТП)."""
    if not isinstance(raw_items, dict):
        return False
    for key, value in raw_items.items():
        if not (str(key).startswith("teleport") or str(key).startswith("slot")):
            continue
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and ("tpscroll" in name or "travel_boots" in name):
            return True
    return False


def _neutral_present(raw_items: Any) -> bool:
    if not isinstance(raw_items, dict):
        return False
    value = raw_items.get("neutral0")
    if not isinstance(value, dict):
        return False
    name = value.get("name")
    return isinstance(name, str) and bool(name) and name != "item_empty"


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
    raw_items = hero.get("items")

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

    # 3. Нет ТП.
    if minute >= (4 if turbo else 6) and not _has_tp(raw_items):
        mistakes.append(
            "Нет свитка телепортации (TP) — всегда держи TP для реакции на драки и дайвы."
        )

    # 4. Пустой нейтральный слот.
    if minute >= (4 if turbo else 8) and not _neutral_present(raw_items):
        tier = neutral_tier(minute, turbo)
        mistakes.append(f"Нейтральный слот пуст — надень нейтралку (сейчас тир {tier}).")

    # 5. Копление золота.
    if alive and gold is not None and (5 if turbo else 8) <= minute <= 35:
        hoard_limit = 3000 if turbo else 2200
        if gold > hoard_limit:
            mistakes.append(
                f"Держишь {gold} золота незатраченным — потрать на следующий слот или байбек."
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
        lines.append("Время суток: " + ("день" if daytime else "ночь") + ".")

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
        if hero.get("alive") is False:
            respawn = _i(hero.get("respawn_seconds"))
            hero_parts.append(f"мёртв, респавн {'через ' + str(respawn) + ' c' if respawn is not None else ''}".strip())
        hp = _f(hero.get("health_percent"))
        if hp is not None:
            hero_parts.append(f"HP {hp:.0f}%")
        mana = _f(hero.get("mana_percent"))
        if mana is not None:
            hero_parts.append(f"мана {mana:.0f}%")
        buyback_cost = _i(hero.get("buyback_cost"))
        buyback_cd = _i(hero.get("buyback_cooldown"))
        if buyback_cost:
            suffix = f", кд {buyback_cd} c" if buyback_cd else ""
            hero_parts.append(f"выкуп {buyback_cost}{suffix}")
        lines.append("Ваш герой: " + ", ".join(hero_parts) + ".")

        inventory = []
        raw_items = hero.get("items")
        if isinstance(raw_items, dict):
            for slot in sorted(raw_items):
                if not str(slot).startswith("slot"):
                    continue
                name = item_name(raw_items[slot], items)
                if name and name != "?" and not name.lower().startswith("empty"):
                    inventory.append(name)
        neutral = None
        if isinstance(raw_items, dict) and isinstance(raw_items.get("neutral0"), dict):
            raw_neutral = raw_items["neutral0"].get("name") if isinstance(raw_items["neutral0"], dict) else None
            if isinstance(raw_neutral, str) and raw_neutral and raw_neutral != "item_empty":
                neutral = item_name(raw_neutral, items)
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
