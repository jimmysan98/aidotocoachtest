"""Тактический «мозг» ИИ-тренера AIDOTA.

Слой между сырым состоянием игры (GSI + драфт + мета-база) и LLM:

- :func:`detect_query_kind` — понимает, о чём спросил игрок (билд, контрпик,
  прокачка, лейн, тайминг…), чтобы подмешать именно нужные знания;
- :func:`build_brief` — собирает компактный «тактический бриф» из GSI: стадия,
  роль, герой, золото, инвентарь, драфт, угрозы, ошибки, цели и фокус;
- :func:`retrieve_knowledge` — достаёт из ``meta_db`` ровно те знания, которые
  нужны для ответа (билд, контр-предметы, порядок прокачки, контрпики, лейн);
- :func:`render_system_prompt` — собирает системный промпт с правилами,
  брифом и знаниями, защищая модель от галлюцинаций;
- :func:`sanitize_answer` — чистит ответ для озвучки: убирает markdown, списки,
  эмодзи, «воду», обрезает до 1–2 предложений.

Модуль намеренно не импортирует ``aidota.coach`` (чтобы не было цикла):
Coach передаёт свои legacy-подсказки параметрами.
"""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Iterable, Mapping

from .beginner import level_label, normalize_skill_level
from .heroes import HeroCatalog, normalize
from .roles import position_label

log = logging.getLogger("aidota.coach_brain")

# --------------------------------------------------------------------------- #
# 1. Понимание вопроса
# --------------------------------------------------------------------------- #

_QUERY_KIND_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pick",
        (
            "кого пикнуть", "кого взять", "кого брать", "что пикнуть", "контрпик",
            "кого банить", "пикни", "какого героя", "кого поставить", "кого лучше взять",
        ),
    ),
    (
        "counter",
        (
            "против", "контрит", "контр-", "чем контрить", "как играть против",
            "что делать с", "кого боится", "чем убить",
        ),
    ),
    (
        "build",
        (
            "что купить", "что собрать", "какой билд", "какой закуп", "закуп",
            "сборка", "сборку", "предмет", "артефакт", "что брать на", "билд",
            "стартов", "закупк",
        ),
    ),
    (
        "skill",
        (
            "качать", "прокачка", "прокачк", "скилл", "скиллы", "способност",
            "абилка", "абилк", "максить", "что брать первым", "порядок прокачки",
        ),
    ),
    (
        "lane",
        (
            "линия", "линии", "лейн", "стою на", "харрасс", "харас", "ластхит",
            "под вишкой", "у вышки", "отвод", "стандартная линия",
        ),
    ),
    (
        "timing",
        (
            "когда", "тайминг", "тайминги", "минута", "минуте", "рошан", "руна",
            "терзатель", "лотос", "аегис", "нейтралк", "стак",
        ),
    ),
    (
        "analysis",
        (
            "разбери", "разбор", "разбор матча", "как я сыграл", "мои ошибки",
            "что не так", "анализ",
        ),
    ),
    (
        "status",
        ("статус", "что по игре", "счёт", "счет", "ситуация", "как дела", "что по матчу"),
    ),
    (
        "explain",
        (
            "что такое", "что значит", "что означает", "объясн", "как работает",
            "как работают", "что это такое", "объясни простыми словами",
        ),
    ),
    (
        "why",
        ("почему", "зачем", "в чём смысл", "в чем смысл"),
    ),
    (
        "matchup",
        ("матчап", "матчапм", "кто сильнее", "кто кого", "сравни героев", "сильный ли"),
    ),
)

_KIND_PRIORITY = {kind: index for index, (kind, _) in enumerate(_QUERY_KIND_PATTERNS)}


def _prepare_text(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def detect_query_kind(text: str) -> str:
    """Определяет тип вопроса игрока.

    Возвращает один из тегов: ``pick``, ``counter``, ``build``, ``skill``,
    ``lane``, ``timing``, ``analysis``, ``status``, ``explain``, ``why``,
    ``matchup`` или ``chat``.
    """
    prepared = _prepare_text(text)
    if not prepared:
        return "chat"
    best: tuple[float, str] | None = None
    for kind, patterns in _QUERY_KIND_PATTERNS:
        for pattern in patterns:
            if pattern in prepared:
                score = 100.0 - _KIND_PRIORITY[kind]
                best = (score, kind)
                break
        if best is not None:
            break
    # «что купить против пуджа» должно уйти в counter, а не в build:
    # counter имеет более высокий приоритет, чем build, и стоит раньше в списке.
    return best[1] if best else "chat"


# --------------------------------------------------------------------------- #
# 2. Тактический бриф из GSI
# --------------------------------------------------------------------------- #

_STAGE_RU = {
    "pre_game": "до начала игры",
    "laning": "линии (0–10 мин)",
    "midgame": "мидгейм (10–25 мин)",
    "lategame": "лейтгейм (25+ мин)",
    "unknown": "стадия неизвестна",
    "offline": "матч не запущен",
}

_ROLE_FOCUS = {
    1: {
        "laning": "фарми безопасно, добивай ластхиты и не разменивайся без выгоды",
        "midgame": "не теряй темп фарма, собирай ключевой слот и дерись только за объектив",
        "lategame": "играй от байбека и позиционки, не дерись без вижна",
    },
    2: {
        "laning": "контролируй руну и опыт, после шестого уровня смотри ротации",
        "midgame": "реализуй тайминги и ротируй по рунам мудрости",
        "lategame": "фокусируй ключевую цель, не лови контроль без BKB",
    },
    3: {
        "laning": "держи линию, ищи размены и не отдавай опыт зря",
        "midgame": "тайминг инициации — зови команду на объектив",
        "lategame": "танкуй и инициируй только с байбеком и сейвом",
    },
    4: {
        "laning": "делай смоки, отводы и стаки, сейвь кора",
        "midgame": "контролируй руны и вижн, помогай миду",
        "lategame": "держи сейв-предметы для кора и вижн на объективах",
    },
    5: {
        "laning": "ставь вижн, сейвь кора, забирай лотусы и руны мудрости",
        "midgame": "вижн на объективах, руны мудрости, сейв в драке",
        "lategame": "держи сейвы для кора, не отдавай вижн",
    },
}

# Угрозы из базы знаний: тег → конкретные контр-предметы (fallback без meta_db).
_TAG_COUNTERS: dict[str, str] = {
    "evasion": "MKB / Bloodthorn",
    "heal_regen": "Spirit Vessel / Eye of Skadi",
    "passives": "Silver Edge",
    "saves_buffs": "Nullifier",
    "magic_burst_cc": "BKB / Pipe of Insight",
    "single_target_cc": "Linken's Sphere / Lotus Orb",
    "illusions_swarms": "Mjollnir / Shiva's Guard",
    "right_clickers": "Heaven's Halberd / Ghost Scepter",
    "high_mobility": "Orchid Malevolence / Scythe of Vyse",
    "invis": "Dust of Appearance / Sentry Ward",
    "summons": "Crimson Guard / Mjollnir",
    "silence": "BKB / Manta Style / Eul's Scepter",
    "global_presence": "вижн и позиционка на линиях",
    "split_push": "защита Т2 и контроль карты",
    "tanky": "Spirit Vessel / Eye of Skadi",
    "burst": "BKB / Aeon Disk",
}

# Свои ключевые предметы: что делать сразу после покупки (паверспайк).
_OWN_POWER_SPIKES: dict[str, str] = {
    "Blink Dagger": "готов врываться и инициировать — ищи изоляцию цели",
    "Black King Bar": "можно драться сквозь магию — зови команду на объектив",
    "Aghanim's Scepter": "твой ультимейт усилен — играй через него",
    "Aghanim's Shard": "используй усиленную способность в каждой драке",
    "Manta Style": "сбрасывай дебаффы и пушь иллюзиями",
    "Scythe of Vyse": "лови ключевую цель хексом до её ультимейта",
    "Silver Edge": "отключай пассивки врага ударом из инвиза",
    "Desolator": "срезай броню и сноси вышки после киллов",
    "Diffusal Blade": "сжигай ману и замедляй цель",
    "Orchid Malevolence": "выходи на одиночную цель через сайленс",
    "Force Staff": "вытаскивай себя и союзников из зоны контроля",
    "Pipe of Insight": "закрывай магический урон команды в драке",
    "Crimson Guard": "блокируй физический урон команды в замесе",
    "Guardian Greaves": "держи команду на регене в затяжной драке",
    "Eye of Skadi": "срезай реген и замедляй цель",
}


# --------------------------------------------------------------------------- #
# 2a. Живые данные матча: top-level items/abilities + советы по закупкам
# --------------------------------------------------------------------------- #

_LIVE_ADVICE_MODULE: Any | None = None
_LIVE_ADVICE_IMPORT_TRIED = False


def _live_advice_module() -> Any:
    """Лениво подтягивает ``aidota.live_advice`` (бриф работает и без него)."""
    global _LIVE_ADVICE_MODULE, _LIVE_ADVICE_IMPORT_TRIED
    if not _LIVE_ADVICE_IMPORT_TRIED:
        _LIVE_ADVICE_IMPORT_TRIED = True
        try:
            from . import live_advice

            _LIVE_ADVICE_MODULE = live_advice
        except Exception:  # noqa: BLE001 — без модуля советов бриф остаётся рабочим
            log.debug("coach_brain: модуль live_advice недоступен")
            _LIVE_ADVICE_MODULE = None
    return _LIVE_ADVICE_MODULE


def _accepts_kwarg(func: Any, name: str) -> bool:
    """Принимает ли функция keyword-параметр ``name`` (для новых полей контракта v1.2)."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _item_key(value: Any) -> str:
    """'item_black_king_bar' / 'Black King Bar' -> 'blackkingbar' (для сопоставления)."""
    text = str(value or "").strip().lower()
    if text.startswith("item_"):
        text = text[5:]
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def _display_items_map(items: Mapping[str, Any] | None) -> dict[str, str]:
    """Словарь предметов -> {slug: display}; терпит оба формата API/снапшота."""
    result: dict[str, str] = {}
    if not isinstance(items, Mapping):
        return result
    for key, value in items.items():
        if isinstance(value, Mapping):
            display = str(value.get("dname") or value.get("display") or key)
        else:
            display = str(value or key)
        if str(key):
            result[str(key)] = display
    return result


def _slot_name(value: Any) -> Any:
    """Имя предмета из GSI-слота (dict ``{"name": "item_..."}`` или строка)."""
    if isinstance(value, Mapping):
        return value.get("name")
    return value


def _is_empty_item(name: Any, ctx: Any | None = None) -> bool:
    """Пустой слот: общий helper ``context.is_empty_item_name`` либо локальная проверка."""
    helper = getattr(ctx, "is_empty_item_name", None) if ctx is not None else None
    if callable(helper):
        try:
            return bool(helper(name))
        except Exception:  # noqa: BLE001 — helper не должен ронять бриф
            pass
    text = str(name or "").strip().lower()
    return not text or text in {"?", "empty", "item_empty"} or text.startswith("empty")


def _own_items(payload: dict, ctx: Any) -> dict[str, Any]:
    """Сырые предметы локального игрока: top-level ``payload["items"]`` в приоритете."""
    extracted = _safe_call(ctx, "extract_own_items", payload)
    if isinstance(extracted, dict):
        return extracted
    top = payload.get("items")
    if isinstance(top, dict):
        slot_keys = [
            key for key in top
            if str(key).startswith(("slot", "stash", "teleport", "neutral"))
        ]
        if slot_keys:
            return dict(top)
    hero = payload.get("hero")
    if isinstance(hero, dict) and isinstance(hero.get("items"), dict):
        return dict(hero["items"])
    return {}


def _own_abilities(payload: dict, ctx: Any) -> dict[str, Any]:
    """Способности локального игрока: top-level ``payload["abilities"]`` в приоритете."""
    extracted = _safe_call(ctx, "extract_own_abilities", payload)
    if isinstance(extracted, dict):
        return extracted
    top = payload.get("abilities")
    if isinstance(top, dict) and top:
        return dict(top)
    hero = payload.get("hero")
    if isinstance(hero, dict) and isinstance(hero.get("abilities"), dict):
        return dict(hero["abilities"])
    return {}


def _display_item(raw: Any, display_map: Mapping[str, str], ctx: Any) -> str:
    """Сырое ``item_*`` -> отображаемое имя (или аккуратное имя без словаря)."""
    name = _safe_call(ctx, "item_name", raw, display_map)
    if isinstance(name, str) and name.strip() and name != "?":
        return name.strip()
    text = str(raw or "").strip()
    if text.startswith("item_"):
        text = text[5:]
    return text.replace("_", " ").title()


def _inventory_from_items(
    raw_items: Mapping[str, Any], display_map: Mapping[str, str], ctx: Any
) -> tuple[list[str], list[str]]:
    """Инвентарь (основные слоты) + сырые ``item_*`` в том же порядке."""
    display: list[str] = []
    keys: list[str] = []
    if not isinstance(raw_items, Mapping):
        return display, keys
    for slot in sorted(raw_items, key=str):
        if not str(slot).startswith("slot"):
            continue
        raw = _slot_name(raw_items[slot])
        if _is_empty_item(raw, ctx):
            continue
        name = _display_item(raw, display_map, ctx)
        if name:
            display.append(name)
            keys.append(str(raw))
    return display, keys


def _neutral_from_items(
    raw_items: Mapping[str, Any], display_map: Mapping[str, str], ctx: Any
) -> str | None:
    """Название надетой нейтралки (``neutral0``) или ``None``."""
    value = raw_items.get("neutral0") if isinstance(raw_items, Mapping) else None
    raw = _slot_name(value)
    if raw is None or _is_empty_item(raw, ctx):
        return None
    return _display_item(raw, display_map, ctx) or None


def _estimate_net_worth(
    ctx: Any,
    payload: dict,
    raw_items: Mapping[str, Any],
    gold: Any,
    item_costs: Mapping[str, int] | None,
) -> tuple[int | None, bool]:
    """Fallback-нетворс: ``context.estimate_net_worth`` либо gold + цена слотов/стэша."""
    helper = getattr(ctx, "estimate_net_worth", None)
    if callable(helper):
        result = _safe_call(ctx, "estimate_net_worth", payload, item_costs)
        if isinstance(result, (tuple, list)) and len(result) == 2:
            value = _as_int(result[0])
            if value is not None:
                return value, bool(result[1])
    if not isinstance(raw_items, Mapping):
        return None, False
    cost_index: dict[str, int] = {}
    if isinstance(item_costs, Mapping):
        for name, value in item_costs.items():
            key = _item_key(name)
            cost = _as_int(value)
            if key and cost is not None:
                cost_index[key] = cost
    total = _as_int(gold) or 0
    for slot, value in raw_items.items():
        key = str(slot)
        if not (key.startswith("slot") or key.startswith("stash")):
            continue
        raw = _slot_name(value)
        if raw is None or _is_empty_item(raw, ctx):
            continue
        cost = cost_index.get(_item_key(raw))
        if cost:
            total += cost
    return total, True


def _ability_display(name: Any, hero_key: str) -> str:
    """'rubick_spell_steal' + 'rubick' -> 'Spell Steal'."""
    text = str(name or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    prefix = f"{hero_key.lower()}_" if hero_key else ""
    if prefix and lowered.startswith(prefix):
        text = text[len(prefix):]
    return text.replace("_", " ").strip().title()


def _find_ultimate(abilities: Mapping[str, Any]) -> dict[str, Any] | None:
    """Ищет запись ультимейта в секции способностей GSI."""
    if not isinstance(abilities, Mapping):
        return None
    entries = [value for value in abilities.values() if isinstance(value, Mapping)]
    for entry in entries:
        if entry.get("ultimate") is True or entry.get("is_ultimate") is True:
            return dict(entry)
    return None


def _live_snapshot(
    ctx: Any,
    payload: dict,
    catalog: HeroCatalog | None,
    items: Mapping[str, Any] | None,
    position: int,
    item_costs: Mapping[str, int] | None,
) -> dict[str, Any]:
    """Снимок GSI из ``context.get_live_gsi_snapshot`` (с ценами предметов v1.2)."""
    func = getattr(ctx, "get_live_gsi_snapshot", None)
    if not callable(func):
        return {}
    kwargs: dict[str, Any] = {"position": position}
    if item_costs and _accepts_kwarg(func, "item_costs"):
        kwargs["item_costs"] = item_costs
    snapshot = _safe_call(ctx, "get_live_gsi_snapshot", payload, catalog, items, **kwargs)
    return snapshot if isinstance(snapshot, dict) else {}


def _build_item_advice(
    snapshot: dict[str, Any],
    hero_name: str,
    position: int,
    enemy_heroes: Iterable[str],
    item_costs: Mapping[str, int] | None,
) -> list[dict[str, Any]]:
    """Живые советы по закупкам из ``live_advice.build_item_advice`` (без падений)."""
    module = _live_advice_module()
    func = getattr(module, "build_item_advice", None) if module is not None else None
    if not callable(func):
        return []
    try:
        advice = func(
            snapshot,
            hero_name=hero_name or "",
            position=position,
            enemy_heroes=[str(enemy) for enemy in enemy_heroes if str(enemy).strip()],
            item_costs=item_costs,
        )
    except Exception as exc:  # noqa: BLE001 — совет не должен ронять бриф
        log.debug("coach_brain: build_item_advice упал: %s", exc)
        return []
    if not isinstance(advice, (list, tuple)):
        return []
    return [
        dict(entry)
        for entry in advice
        if isinstance(entry, Mapping) and str(entry.get("item") or "").strip()
    ][:3]


def _format_advice(advice: Iterable[Mapping[str, Any]], *, limit: int = 2) -> str:
    """Форматирует советы для промпта: «предмет (цена) — причина; ...»."""
    entries = [
        entry for entry in advice
        if isinstance(entry, Mapping) and str(entry.get("item") or "").strip()
    ]
    if not entries:
        return ""
    module = _live_advice_module()
    formatter = getattr(module, "format_item_advice", None) if module is not None else None
    if callable(formatter):
        try:
            text = formatter(entries, limit=limit)
            if isinstance(text, str) and text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001 — резервное форматирование ниже
            pass
    parts: list[str] = []
    for entry in entries[: max(1, int(limit or 1))]:
        item = str(entry.get("item") or "").strip()
        cost = _as_int(entry.get("cost"))
        gold_needed = _as_int(entry.get("gold_needed"))
        chunk = item
        if cost:
            chunk += f" ({cost}"
            if gold_needed and gold_needed > 0:
                chunk += f" — не хватает {gold_needed}"
            chunk += ")"
        reason = str(entry.get("reason") or "").strip()
        if reason:
            chunk += f" — {reason}"
        parts.append(chunk)
    return "; ".join(parts)


def _safe_call(module: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    func = getattr(module, name, None)
    if not callable(func):
        return None
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.debug("coach_brain: %s.%s упал: %s", getattr(module, "__name__", "?"), name, exc)
        return None


def _context_module() -> Any:
    from . import context  # локальный импорт: избегаем циклов и гонок при импорте

    return context


def _hero_key(hero: Mapping[str, Any] | None) -> str:
    if not isinstance(hero, Mapping):
        return ""
    raw = str(hero.get("name", "") or "")
    if raw.startswith("npc_dota_hero_"):
        return raw.replace("npc_dota_hero_", "").lower()
    return normalize(str(hero.get("localized_name", "") or raw))


def resolve_mentioned_hero(text: str, catalog: HeroCatalog | None) -> dict | None:
    """Находит героя, упомянутого в свободной фразе игрока.

    «что собрать против фантомки» → Phantom Assassin, «что качать на пудже» → Pudge.
    Возвращает ``None``, если герой не назван. Не роняет ответ на мусоре.
    """
    if not text or catalog is None or not getattr(catalog, "available", False):
        return None
    try:
        match = catalog.find(text) or catalog.find_in_tokens(text)
    except Exception:  # noqa: BLE001
        return None
    return match.hero if match is not None else None


def _stage_for_clock(clock_sec: int | None) -> str:
    if clock_sec is None:
        return "unknown"
    if clock_sec < 0:
        return "pre_game"
    if clock_sec < 600:
        return "laning"
    if clock_sec < 1800:
        return "midgame"
    return "lategame"


def _focus_for(position: int, stage: str, hero_name: str | None) -> str:
    role_focus = _ROLE_FOCUS.get(int(position or 1), _ROLE_FOCUS[5])
    focus = role_focus.get(stage)
    if not focus:
        return ""
    if hero_name:
        return f"{hero_name}: {focus}"
    return focus


def _threats_fallback(enemy_picks: Iterable[str]) -> list[str]:
    """Угрозы из мета-базы, если ``context.analyze_enemy_threats`` ещё нет."""
    threats: list[str] = []
    names = [str(x) for x in enemy_picks if str(x).strip()]
    if not names:
        return threats
    try:
        from . import meta_db
    except Exception:  # noqa: BLE001
        return threats
    items = _safe_call(meta_db, "get_hero_threats", names)
    if not isinstance(items, list):
        return threats
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("ru_name") or item.get("hero_id") or "?"
        tags = [str(t) for t in (item.get("tags") or [])]
        counter_items = [str(i) for i in (item.get("counter_items") or [])]
        if not counter_items:
            for tag in tags[:2]:
                hint = _TAG_COUNTERS.get(tag)
                if hint:
                    counter_items.append(hint)
        if counter_items:
            threats.append(f"{name} — опасен ({item.get('threat') or ', '.join(tags[:2])}); бери {counter_items[0]}")
    return threats[:5]


def build_brief(
    payload: dict | None,
    catalog: HeroCatalog | None = None,
    items: Mapping[str, Any] | None = None,
    *,
    position: int = 1,
    profile: Any | None = None,
    hero: Mapping[str, Any] | None = None,
    item_costs: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Собирает структурированный тактический бриф текущего матча.

    Предметы и способности берутся из **верхнеуровневых** секций GSI
    (``payload["items"]`` / ``payload["abilities"]``) через хелперы
    ``aidota.context``, нетворс — из ``get_live_gsi_snapshot`` (включая
    fallback-оценку), а ``item_advice``/``next_item`` — из
    ``aidota.live_advice.build_item_advice`` с учётом золота, инвентаря,
    роли, вражеского драфта и цен предметов (``item_costs``).
    """
    items = items or {}
    item_costs = item_costs or None
    position = int(position or 1)
    brief: dict[str, Any] = {
        "available": False,
        "position": position,
        "role": position_label(position),
        "stage": "offline",
        "stage_ru": _STAGE_RU["offline"],
        "clock_seconds": None,
        "clock": "?",
        "minute": None,
        "score": "",
        "my_team": None,
        "hero_name": None,
        "hero_level": None,
        "alive": None,
        "respawn_seconds": None,
        "hp_percent": None,
        "mana_percent": None,
        "kda": "",
        "gold": None,
        "net_worth": None,
        "net_worth_estimated": False,
        "gpm": None,
        "xpm": None,
        "last_hits": None,
        "inventory": [],
        "inventory_keys": [],
        "neutral_item": None,
        "has_tp": None,
        "buyback_available": None,
        "aghanims_scepter": None,
        "aghanims_shard": None,
        "ult_name": None,
        "ult_cooldown": None,
        "ult_ready": None,
        "my_picks": [],
        "enemy_picks": [],
        "bans": [],
        "threats": [],
        "mistakes": [],
        "objectives": "",
        "power_spikes": [],
        "item_advice": [],
        "next_item": None,
        "focus": _focus_for(position, "laning", None),
    }
    if not isinstance(payload, dict) or not payload:
        return brief

    ctx = _context_module()
    map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    hero_data = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
    snapshot = _live_snapshot(ctx, payload, catalog, items, position, item_costs)

    clock_sec = _safe_call(ctx, "_i", map_data.get("clock_time"))
    if not isinstance(clock_sec, int):
        try:
            clock_sec = int(clock_sec) if clock_sec is not None else None
        except (TypeError, ValueError):
            clock_sec = None
    stage = _stage_for_clock(clock_sec)
    game_mode = _safe_call(ctx, "get_game_mode", payload)
    clock_text = _safe_call(ctx, "format_clock", clock_sec) or "?"

    brief.update(
        {
            "available": True,
            "stage": stage,
            "stage_ru": _STAGE_RU.get(stage, stage),
            "clock_seconds": clock_sec,
            "clock": str(clock_text),
            "minute": (max(0, clock_sec) // 60) if isinstance(clock_sec, int) else None,
            "game_mode": _safe_call(ctx, "game_mode_label", game_mode) or "",
            "turbo": bool(_safe_call(ctx, "is_turbo", game_mode)),
        }
    )

    radiant = _safe_call(ctx, "_i", map_data.get("radiant_score"))
    dire = _safe_call(ctx, "_i", map_data.get("dire_score"))
    if isinstance(radiant, int) and isinstance(dire, int):
        brief["score"] = f"{radiant}:{dire}"

    draft = _safe_call(ctx, "collect_draft", payload, catalog)
    my_picks = list(getattr(draft, "my_picks", []) or [])
    enemy_picks = list(getattr(draft, "enemy_picks", []) or [])
    bans = list(getattr(draft, "bans", []) or [])
    brief["my_picks"] = my_picks
    brief["enemy_picks"] = enemy_picks
    brief["bans"] = bans[:10]

    # Профиль героя игрока
    if hero is None:
        hero = _safe_hero(payload, catalog)
    hero_name = None
    if isinstance(hero, Mapping):
        hero_name = str(hero.get("localized_name") or hero.get("name") or "") or None
    elif isinstance(hero_data, dict) and hero_data.get("name"):
        resolved = catalog.by_npc(str(hero_data.get("name"))) if catalog is not None else None
        hero_name = (resolved or {}).get("localized_name")
    brief["hero_name"] = hero_name

    brief["hero_level"] = _safe_call(ctx, "_i", hero_data.get("level"))
    brief["alive"] = hero_data.get("alive")
    brief["respawn_seconds"] = _safe_call(ctx, "_i", hero_data.get("respawn_seconds"))
    brief["hp_percent"] = _safe_call(ctx, "_f", hero_data.get("health_percent"))
    brief["mana_percent"] = _safe_call(ctx, "_f", hero_data.get("mana_percent"))
    kills = _safe_call(ctx, "_i", player.get("kills"))
    deaths = _safe_call(ctx, "_i", player.get("deaths"))
    assists = _safe_call(ctx, "_i", player.get("assists"))
    if all(isinstance(v, int) for v in (kills, deaths, assists)):
        brief["kda"] = f"{kills}/{deaths}/{assists}"
    brief["gold"] = _safe_call(ctx, "_i", player.get("gold"))

    # Живые предметы: в реальном GSI они лежат в верхнеуровневой секции payload["items"].
    display_items = _display_items_map(items)
    own_items = _own_items(payload, ctx)
    inventory, inventory_keys = _inventory_from_items(own_items, display_items, ctx)
    snapshot_inventory = snapshot.get("inventory")
    if not inventory and isinstance(snapshot_inventory, list):
        inventory = [str(name).strip() for name in snapshot_inventory if str(name).strip()]
        snapshot_keys = snapshot.get("inventory_keys")
        if isinstance(snapshot_keys, list):
            inventory_keys = [str(key) for key in snapshot_keys if str(key).strip()]
    brief["inventory"] = inventory
    brief["inventory_keys"] = list(inventory_keys)
    neutral = _neutral_from_items(own_items, display_items, ctx) or snapshot.get("neutral_item")
    brief["neutral_item"] = neutral if isinstance(neutral, str) and neutral.strip() else None
    brief["has_tp"] = _has_tp(own_items)
    if brief["has_tp"] is None and isinstance(snapshot.get("has_tp"), bool):
        brief["has_tp"] = snapshot["has_tp"]

    # Нетворс: приоритет у живого snapshot (в т.ч. оценка), затем player.net_worth,
    # затем fallback-оценка «золото + стоимость предметов» с учётом item_costs.
    net_worth = _as_int(snapshot.get("net_worth"))
    estimated = bool(snapshot.get("net_worth_estimated"))
    player_net_worth = _as_int(player.get("net_worth"))
    if not net_worth or net_worth <= 0:
        net_worth = player_net_worth if player_net_worth and player_net_worth > 0 else None
        estimated = False
    if not net_worth:
        estimated_net_worth, was_estimated = _estimate_net_worth(
            ctx, payload, own_items, brief["gold"], item_costs
        )
        if estimated_net_worth and estimated_net_worth > 0:
            net_worth = estimated_net_worth
            estimated = bool(was_estimated)
    brief["net_worth"] = net_worth
    brief["net_worth_estimated"] = bool(estimated) if net_worth else False

    brief["gpm"] = _safe_call(ctx, "_i", player.get("gpm"))
    brief["xpm"] = _safe_call(ctx, "_i", player.get("xpm"))
    brief["last_hits"] = _safe_call(ctx, "_i", player.get("last_hits"))
    brief["my_team"] = player.get("team_name") or getattr(draft, "my_team", None)

    cost = hero_data.get("buyback_cost")
    cooldown = hero_data.get("buyback_cooldown")
    try:
        brief["buyback_available"] = bool(cost) and not bool(cooldown)
    except Exception:  # noqa: BLE001
        brief["buyback_available"] = None
    brief["aghanims_scepter"] = bool(hero_data.get("aghanims_scepter"))
    brief["aghanims_shard"] = bool(hero_data.get("aghanims_shard"))

    # Способности: в реальном GSI — верхнеуровневая секция payload["abilities"];
    # для промпта достаточно ультимейта и его готовности.
    own_abilities = _own_abilities(payload, ctx)
    ult = _find_ultimate(own_abilities)
    if ult is not None:
        npc = _safe_call(ctx, "current_hero_npc", payload)
        hero_key = str(npc or "").replace("npc_dota_hero_", "") if isinstance(npc, str) else ""
        if not hero_key and hero_name:
            hero_key = _item_key(hero_name)
        ult_cooldown = _as_int(ult.get("cooldown"))
        can_cast = ult.get("can_cast")
        ult_ready: bool | None = None
        if isinstance(can_cast, bool):
            ult_ready = can_cast and (ult_cooldown or 0) <= 0
        elif isinstance(ult_cooldown, int):
            ult_ready = ult_cooldown <= 0
        ult_name = _ability_display(ult.get("name") or ult.get("ability_name"), hero_key)
        brief["ult_name"] = ult_name or None
        brief["ult_cooldown"] = ult_cooldown
        brief["ult_ready"] = ult_ready

    # Угрозы: сначала radar из context, потом fallback по мета-базе
    threats = _safe_call(ctx, "analyze_enemy_threats", payload, catalog, items)
    if not isinstance(threats, list) or not threats:
        threats = _threats_fallback(enemy_picks)
    brief["threats"] = [str(t) for t in (threats or []) if str(t).strip()][:5]

    # Ошибки детектора
    mistakes = _safe_call(ctx, "analyze_player_mistakes", payload, position, game_mode)
    if isinstance(mistakes, list):
        brief["mistakes"] = [str(m) for m in mistakes][:4]

    # Ближайшая макро-цель
    objectives = _safe_call(ctx, "objectives_context", clock_sec, game_mode)
    if isinstance(objectives, str):
        brief["objectives"] = objectives

    # Паверспайки по уже собранным предметам
    spikes = [f"{item}: {_OWN_POWER_SPIKES[item]}" for item in inventory if item in _OWN_POWER_SPIKES]
    brief["power_spikes"] = spikes[:3]

    # Живые советы по закупкам: привязаны к золоту, инвентарю, роли и драфту.
    advice_snapshot = {
        "available": True,
        "hero_name": hero_name or "",
        "position": position,
        "gold": brief.get("gold") or 0,
        "net_worth": brief.get("net_worth") or 0,
        "net_worth_estimated": brief.get("net_worth_estimated", False),
        "inventory": list(inventory),
        "inventory_keys": list(inventory_keys),
        "neutral_item": brief.get("neutral_item"),
        "enemy_picks": list(enemy_picks),
        "clock_seconds": clock_sec,
        "game_stage": stage,
    }
    item_advice = _build_item_advice(
        advice_snapshot, hero_name or "", position, enemy_picks, item_costs
    )
    brief["item_advice"] = item_advice
    brief["next_item"] = item_advice[0] if item_advice else None

    brief["focus"] = _focus_for(position, stage, hero_name)

    # Подсказка о роли из профиля игрока (пул героев не раскрываем в брифе)
    if profile is not None:
        brief["mmr_bracket"] = getattr(profile, "mmr_bracket", "") or ""
    return brief


def _safe_hero(payload: dict, catalog: HeroCatalog | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    npc = _safe_call(_context_module(), "current_hero_npc", payload)
    if npc and catalog is not None:
        found = catalog.by_npc(str(npc))
        if found is not None:
            return found
    return None


def _has_tp(raw_items: Any) -> bool | None:
    if not isinstance(raw_items, dict):
        return None
    for value in raw_items.values():
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and ("tpscroll" in name or name in ("item_travel_boots", "item_travel_boots_2")):
            return True
    return False


def _join_sentences(items: Iterable[Any]) -> str:
    """Склеивает фразы через «; », убирая дублирующие точки на концах."""
    cleaned: list[str] = []
    for item in items:
        text = " ".join(str(item or "").split()).rstrip(".;!?…").strip()
        if text:
            cleaned.append(text)
    return "; ".join(cleaned)


def render_brief(brief: dict[str, Any], *, max_chars: int = 1100) -> str:
    """Рендерит бриф в компактный текст для промпта (одна-две строки на блок)."""
    if not isinstance(brief, dict) or not brief.get("available"):
        lines = ["Матч не запущен или GSI не получает данные — играй по общим принципам роли."]
        if brief.get("role"):
            lines.append(f"Роль: {brief.get('role')}.")
        return " ".join(lines)

    lines: list[str] = []
    head = f"Роль: {brief.get('role')}. Игра: {brief.get('clock')}, {brief.get('stage_ru')}."
    if brief.get("score"):
        head += f" Счёт {brief['score']} ({brief.get('my_team') or '?'})."
    lines.append(head)

    hero_bits: list[str] = []
    if brief.get("hero_name"):
        hero_bits.append(f"герой {brief['hero_name']}")
    if brief.get("hero_level"):
        hero_bits.append(f"ур. {brief['hero_level']}")
    if brief.get("kda"):
        hero_bits.append(f"KDA {brief['kda']}")
    if brief.get("hp_percent") is not None:
        hero_bits.append(f"HP {float(brief['hp_percent']):.0f}%")
    if brief.get("mana_percent") is not None:
        hero_bits.append(f"мана {float(brief['mana_percent']):.0f}%")
    if brief.get("alive") is False:
        hero_bits.append("мёртв")
    if hero_bits:
        lines.append("Игрок: " + ", ".join(hero_bits) + ".")
    money: list[str] = []
    if brief.get("gold") is not None:
        money.append(f"золото {brief['gold']}")
    if brief.get("net_worth"):
        money.append(f"NW {brief['net_worth']}")
    if brief.get("gpm"):
        money.append(f"GPM {brief['gpm']}")
    if brief.get("last_hits") is not None:
        money.append(f"ластхиты {brief['last_hits']}")
    if money:
        lines.append("Экономика: " + ", ".join(money) + ".")
    if brief.get("inventory"):
        lines.append("Инвентарь: " + ", ".join(brief["inventory"]) + ".")
    extras: list[str] = []
    if brief.get("neutral_item"):
        extras.append(f"нейтралка {brief['neutral_item']}")
    if brief.get("has_tp") is False:
        extras.append("НЕТ ТП")
    if brief.get("buyback_available") is False:
        extras.append("байбек недоступен")
    if brief.get("aghanims_scepter"):
        extras.append("есть аганим")
    if brief.get("aghanims_shard"):
        extras.append("есть шард")
    if brief.get("ult_name"):
        ult_cooldown = brief.get("ult_cooldown")
        if brief.get("ult_ready") is True:
            extras.append(f"ульт {brief['ult_name']} готов")
        elif isinstance(ult_cooldown, int) and ult_cooldown > 0:
            extras.append(f"ульт {brief['ult_name']} кд {ult_cooldown} с")
        else:
            extras.append(f"ульт {brief['ult_name']}")
    if extras:
        lines.append("Важно: " + ", ".join(extras) + ".")
    if brief.get("my_picks") or brief.get("enemy_picks"):
        draft_bits = []
        if brief.get("my_picks"):
            draft_bits.append("мои: " + ", ".join(brief["my_picks"]))
        if brief.get("enemy_picks"):
            draft_bits.append("враги: " + ", ".join(brief["enemy_picks"]))
        if brief.get("bans"):
            draft_bits.append("баны: " + ", ".join(brief["bans"]))
        lines.append("Драфт — " + "; ".join(draft_bits) + ".")
    if brief.get("threats"):
        lines.append("Угрозы: " + _join_sentences(brief["threats"]) + ".")
    if brief.get("power_spikes"):
        lines.append("Паверспайк: " + _join_sentences(brief["power_spikes"]) + ".")
    if brief.get("mistakes"):
        lines.append("Замеченные ошибки: " + _join_sentences(brief["mistakes"]) + ".")
    if brief.get("item_advice"):
        advice_text = _format_advice(brief.get("item_advice") or [], limit=2)
        if advice_text:
            lines.append("Закупка, следующий предмет: " + advice_text + ".")
        next_item = brief.get("next_item")
        warning = str(next_item.get("warning") or "").strip() if isinstance(next_item, Mapping) else ""
        if warning:
            lines.append("Важно по закупке: " + warning.rstrip(".") + ".")
    if brief.get("objectives"):
        lines.append("Цель: " + str(brief["objectives"]))
    if brief.get("focus"):
        lines.append("Фокус: " + str(brief["focus"]) + ".")

    text = "\n".join(lines)
    if len(text) > max_chars:
        cut = text[:max_chars]
        boundary = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("; "))
        if boundary > max_chars // 2:
            cut = cut[: boundary + 1]
        text = cut.rstrip() + " …"
    return text


# --------------------------------------------------------------------------- #
# 3. Доставание знаний из мета-базы
# --------------------------------------------------------------------------- #


def _legacy_hint_for(name: str, hints: Mapping[str, Any] | None) -> str | None:
    if not hints:
        return None
    key = normalize(name)
    if not key:
        return None
    for fragment, hint in hints.items():
        fragment_clean = str(fragment).lower()
        if fragment_clean and fragment_clean in key:
            return str(hint)
    return None


def _format_build_snippet(build: Mapping[str, Any]) -> str:
    parts: list[str] = []
    name = build.get("hero_ru_name") or build.get("hero_name") or "герой"
    role_name = build.get("role_name") or ""
    starting = ", ".join(str(i) for i in (build.get("starting") or [])[:5])
    core = ", ".join(
        f"{c.get('item')} ({c.get('timing')})"
        for c in (build.get("core") or [])[:3]
        if isinstance(c, Mapping)
    )
    situational = ", ".join(
        str(s.get("item"))
        for s in (build.get("situational") or [])[:2]
        if isinstance(s, Mapping) and s.get("item")
    )
    if starting:
        parts.append(f"старт: {starting}")
    if core:
        parts.append(f"кор: {core}")
    if situational:
        parts.append(f"ситуативно: {situational}")
    if build.get("shard"):
        parts.append(f"шард: {str(build['shard']).rstrip('.')}")
    if build.get("lane_tips"):
        parts.append(f"линия: {str(build['lane_tips']).rstrip('.')}")
    if not parts:
        return ""
    prefix = f"Эталонный билд {name}"
    if role_name:
        prefix += f" — {role_name}"
    return prefix + ": " + "; ".join(parts) + "."


def retrieve_knowledge(
    query: str,
    *,
    hero: Mapping[str, Any] | None = None,
    position: int = 1,
    enemy_heroes: Iterable[str] | None = None,
    legacy_counter_hints: Mapping[str, Any] | None = None,
    legacy_skill_tips: Mapping[str, Any] | None = None,
    lane_plan: str | None = None,
    catalog: HeroCatalog | None = None,
    max_snippets: int = 4,
) -> list[str]:
    """Возвращает только те знания, которые нужны для ответа на вопрос."""
    kind = detect_query_kind(query)
    hero_name = None
    if isinstance(hero, Mapping):
        hero_name = str(hero.get("localized_name") or hero.get("name") or "") or None
    hero_key = _hero_key(hero)
    enemies = [str(e) for e in (enemy_heroes or []) if str(e).strip()]
    snippets: list[str] = []

    try:
        from . import meta_db
    except Exception:  # noqa: BLE001
        meta_db = None  # type: ignore[assignment]

    # --- билд / предметы -------------------------------------------------- #
    if kind in ("build", "chat", "why") and hero_name and meta_db is not None:
        build = _safe_call(meta_db, "get_hero_build", hero_name, role=position, enemy_heroes=enemies)
        if isinstance(build, Mapping):
            snippet = _format_build_snippet(build)
            if snippet:
                snippets.append(snippet)

    # --- контр-предметы против врагов ------------------------------------ #
    if kind in ("counter", "why", "chat", "analysis"):
        for enemy in enemies[:3]:
            counters = None
            if meta_db is not None:
                counters = _safe_call(meta_db, "get_hero_counters", enemy)
            if isinstance(counters, Mapping):
                items_list = [str(i) for i in (counters.get("counter_items") or [])][:4]
                tip = str(counters.get("tip") or "").strip().rstrip(".")
                name = counters.get("ru_name") or enemy
                if items_list:
                    snippet = f"{name} — контр-предметы: {', '.join(items_list)}"
                    if tip:
                        snippet += f". {tip}"
                    legacy = _legacy_hint_for(enemy, legacy_counter_hints)
                    if legacy:
                        snippet += f". Классические варианты: {legacy.rstrip('.')}"
                    snippets.append(snippet + ".")
                    continue
            legacy = _legacy_hint_for(enemy, legacy_counter_hints)
            if legacy:
                snippets.append(f"Против {enemy}: {legacy}")

    # --- порядок прокачки ------------------------------------------------- #
    if kind == "skill" and hero_name:
        skill = None
        if meta_db is not None:
            skill = _safe_call(meta_db, "get_skill_order", hero_name)
        if isinstance(skill, Mapping):
            bits = []
            if skill.get("priority"):
                bits.append(f"максить: {skill['priority']}")
            if skill.get("level1"):
                bits.append(f"на 1-м: {skill['level1']}")
            if skill.get("tip"):
                bits.append(str(skill["tip"]))
            if bits:
                snippets.append("Прокачка: " + "; ".join(bits) + ".")
        if not snippets and legacy_skill_tips:
            tip = legacy_skill_tips.get(hero_key)
            if tip:
                snippets.append(str(tip))

    # --- контрпики -------------------------------------------------------- #
    if kind == "pick" and meta_db is not None:
        picks = _safe_call(meta_db, "recommend_picks", enemies, role=position, top_k=3)
        if isinstance(picks, list) and picks:
            bits = []
            for pick in picks:
                if not isinstance(pick, Mapping):
                    continue
                name = pick.get("ru_name") or pick.get("name") or "?"
                score = pick.get("score")
                cores = ", ".join(str(c) for c in (pick.get("core_items") or [])[:2])
                piece = f"{name} ({score}%)" if score is not None else str(name)
                if cores:
                    piece += f" — {cores}"
                bits.append(piece)
            if bits:
                snippets.append("Контрпики на поз. " + str(position) + ": " + "; ".join(bits) + ".")

    # --- линия ------------------------------------------------------------ #
    if kind == "lane" and hero_name and meta_db is not None:
        tip = _safe_call(meta_db, "get_lane_tips", hero_name, position, enemies)
        if isinstance(tip, str) and tip.strip():
            snippets.append(f"Лейн: {tip.strip()}")
        if lane_plan:
            plan_text = str(lane_plan).strip()
            for prefix in ("План на линию: ", "План на линию "):
                if plan_text.startswith(prefix):
                    plan_text = plan_text[len(prefix):]
                    break
            snippets.append(f"План на линию: {plan_text}")

    # --- «почему»: ключевые слоты/угрозы ---------------------------------- #
    if kind == "why" and hero_name and meta_db is not None and not snippets:
        build = _safe_call(meta_db, "get_hero_build", hero_name, role=position, enemy_heroes=enemies)
        if isinstance(build, Mapping):
            snippet = _format_build_snippet(build)
            if snippet:
                snippets.append(snippet)

    # --- тайминги --------------------------------------------------------- #
    if kind == "timing":
        hints: list[str] = []
        if hero_name and meta_db is not None:
            build = _safe_call(meta_db, "get_hero_build", hero_name, role=position, enemy_heroes=enemies)
            if isinstance(build, Mapping):
                for core in (build.get("core") or [])[:3]:
                    if isinstance(core, Mapping) and core.get("item"):
                        hints.append(f"{core['item']} к {core.get('timing', 'таймингу')}")
        if hero_name:
            hints.append("ключевые окна: Power Rune на 6:00, Wisdom Rune каждые 7 минут, Tormentor на 20:00")
        if hints:
            snippets.append("Тайминги: " + "; ".join(hints) + ".")

    # --- общий чат: короткая шпаргалка по угрозам ------------------------- #
    if kind == "chat" and not snippets and enemies:
        threats = _threats_fallback(enemies)
        if threats:
            snippets.append("Угрозы драфта: " + "; ".join(threats[:2]) + ".")

    # Убираем пустые/слишком длинные и дубли
    cleaned: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        text = " ".join(str(snippet).split())
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text[:360])
        if len(cleaned) >= max_snippets:
            break
    return cleaned


# --------------------------------------------------------------------------- #
# 4. Промпты
# --------------------------------------------------------------------------- #


_BEGINNER_RULES = (
    "ПРАВИЛА ДЛЯ НОВИЧКА:\n"
    "1. Игрок только учится: говори дружелюбно и поддерживающе, без снобизма, насмешек и давления рейтингом.\n"
    "2. Каждый незнакомый термин объясняй коротко в скобках: например, БКБ (золотой щит от магии).\n"
    "3. Дай ровно одно конкретное действие и короткое «зачем»: что сделать и какую пользу это даст.\n"
    "4. Не ругай за ошибки и не перегружай: без длинных лекций, максимум два-три совета.\n"
    "5. В конце предложи, как потренироваться: одно простое упражнение."
)

_EXPERIENCED_RULES_BODY = (
    "1. Игрок опытный: не объясняй базовые термины и прописные истины — сразу тактическая конкретика.\n"
    "2. Думай категориями таймингов паверспайков, темпа карты, окна драфта, лейн-равновесия и агро крипов.\n"
    "3. Указывай тайминги вижна и смоков, экономику байбека, контроль объектов: Рошан, аегис, вышки.\n"
    "4. Давай решение с окном времени и ожидаемым эффектом: что сделать, когда и зачем.\n"
    "5. Разбирай решения игрока, а не основы: темп, карта, экономика и контроль объектов важнее базовых советов.\n"
    "6. Не повторяй банальности — опирайся на тактический бриф и знания."
)


def _level_rules(level: str) -> str:
    """Дополнительные правила под уровень игрока (пустая строка — базовый стиль)."""
    normalized = normalize_skill_level(level)
    if normalized == "beginner":
        return _BEGINNER_RULES
    if normalized in ("advanced", "expert"):
        if normalized == "expert":
            header = "ПРАВИЛА ДЛЯ ИГРОКА УРОВНЯ ПРОФИ (EXPERT):"
        else:
            header = "ПРАВИЛА ДЛЯ ОПЫТНОГО ИГРОКА:"
        return header + "\n" + _EXPERIENCED_RULES_BODY
    return ""


def make_system_prompt(
    coach_name: str = "Астра",
    dynamic_context: str = "",
    *,
    skill_level: str = "beginner",
) -> str:
    """Базовый системный промпт личности тренера (Астра / Джарвис) под уровень игрока."""
    name_clean = (coach_name or "Астра").strip()
    is_jarvis = any(j in name_clean.lower() for j in ("джарвис", "jarvis", "жарвис"))
    if is_jarvis:
        persona = (
            "Ты — Джарвис (J.A.R.V.I.S.), боевой тактический ИИ-ассистент игрока по Dota 2. "
            "Твой стиль: спокойный, уверенный, высокотехнологичный, предельно вежливый и расчётливый тактик. "
            "Ты оперируешь вероятностями, таймингами ключевых способностей, паверспайками и боевой эффективностью."
        )
    else:
        persona = (
            "Ты — Астра, профессиональный аналитический ИИ-тренер по Dota 2 с рейтингом 7000+ MMR. "
            "Твой стиль: строгая, уверенная, энергичная и прямолинейная девушка-коуч. "
            "Ты держишь руку на пульсе карты: микро-контроль, отводы, смоки, ротации, срез фарма и тайминги артефактов."
        )

    rules = (
        "ГЛАВНЫЕ ПРАВИЛА:\n"
        "1. ОТВЕЧАЙ МАКСИМАЛЬНО КРАТКО, быстро и точно: строго 1–2 предложения (до 20–25 слов, озвучка 4–7 секунд).\n"
        "2. Отвечай СТРОГО на конкретный вопрос. Если спросили что купить на старте — назови ТОЛЬКО 3–4 стартовых предмета, никакого мида, лейта, прокачки скиллов или лекций!\n"
        "3. ОПИРАЙСЯ НА ФАКТЫ: блоки «ТАКТИЧЕСКИЙ БРИФ», «ЗНАНИЯ ПО ВОПРОСУ» и «ПРОФИЛЬ ИГРОКА» — твой источник правды. "
        "Не выдумывай предметы, тайминги, способности и цифры; если данных нет — скажи одной фразой, чего не хватает, и дай общий принцип по роли.\n"
        "4. БУДЬ КОНКРЕТНЫМ: называй конкретные предметы, способности и имена героев, а не «собери что-нибудь на выживание».\n"
        "5. Учитывай позицию игрока (1–5), текущую минуту, стадию игры и вражеский драфт. Совет должен быть применим ПРЯМО СЕЙЧАС.\n"
        "6. Никакой воды, вводных фраз («Я запомнил...», «Привет...», «Конечно...»), списков, нумерации, звездочек, эмодзи и markdown. Только чистый живой русский текст для озвучки.\n"
        "7. Не повторяй свои предыдущие ответы и учитывай «ДОЛГОВРЕМЕННУЮ ПАМЯТЬ»."
    )

    blocks = [persona, rules]
    level_rules = _level_rules(skill_level)
    if level_rules:
        blocks.append(level_rules)
    if dynamic_context:
        blocks.append(str(dynamic_context))
    return "\n\n".join(blocks)


_KIND_HINTS: dict[str, str] = {
    "build": "Дай конкретные предметы: старт/следующий слот с таймингом и коротким «почему».",
    "counter": "Назови 2–3 конкретных контр-предмета против названного героя и одну причину.",
    "skill": "Скажи, какую способность максить первой и что взять на 1-м уровне.",
    "pick": "Назови лучшего героя под драфт и позицию, коротко обоснуй.",
    "lane": "Дай конкретный план на линию: харасс, отводы, руны, позиция у вышки.",
    "timing": "Назови конкретное окно/тайминг и что успеть сделать.",
    "explain": "Объясни термин просто: что это и зачем это нужно в игре.",
    "why": "Объясни причину в 2–3 коротких предложениях, без лекций.",
    "analysis": "Дай 2–3 конкретные ошибки и одно действие на ближайшие минуты.",
    "status": "Назови ключевые факты и одно действие прямо сейчас.",
    "chat": "Ответь живо, по делу и с конкретикой под текущую ситуацию.",
}


def render_system_prompt(
    coach_name: str = "Астра",
    brief_text: str = "",
    knowledge: Iterable[str] | None = None,
    *,
    kind: str = "chat",
    profile_text: str = "",
    skill_level: str = "beginner",
) -> str:
    """Собирает системный промпт: личность + правила уровня + бриф + знания + формат."""
    level = normalize_skill_level(skill_level)
    parts = [make_system_prompt(coach_name, skill_level=level)]
    if profile_text:
        parts.append("ПРОФИЛЬ ИГРОКА И ДОЛГОВРЕМЕННАЯ ПАМЯТЬ:\n" + str(profile_text))
    if brief_text:
        parts.append("ТАКТИЧЕСКИЙ БРИФ (актуальное состояние матча):\n" + brief_text)
    knowledge_list = [str(k).strip() for k in (knowledge or []) if str(k).strip()]
    if knowledge_list:
        parts.append("ЗНАНИЯ ПО ВОПРОСУ:\n" + "\n".join(f"- {k}" for k in knowledge_list))
    hint = _KIND_HINTS.get(kind) or _KIND_HINTS["chat"]
    if level in ("advanced", "expert") and kind in ("chat", "explain"):
        parts.append(
            "ФОРМАТ ОТВЕТА: " + hint
            + " Допустимо 3–5 коротких предложений (до 500 символов) с конкретикой: тайминги, темп карты, решения. "
            "Без объяснения базовых терминов, без списков и markdown."
        )
    elif level == "beginner" and kind == "explain":
        parts.append(
            "ФОРМАТ ОТВЕТА: " + hint
            + " Объясни в 1–2 коротких предложениях и добавь короткий пример из игры. "
            "Только живой русский текст, без списков и markdown."
        )
    else:
        parts.append(
            "ФОРМАТ ОТВЕТА: " + hint + " Только живой русский текст, 1–2 предложения, без списков и markdown."
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 5. Санитайзер ответа для озвучки
# --------------------------------------------------------------------------- #

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_TOKEN_RE = re.compile(r"\*\*|__|~~|\*|`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET_RE = re.compile(r"^\s{0,3}[-•·*+]\s+", re.M)
_NUMBERED_RE = re.compile(r"^\s{0,3}\d{1,2}[.)]\s+", re.M)
_SPACES_RE = re.compile(r"[ \t]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")

_FILLER_PREFIXES = (
    "конечно,", "конечно", "разумеется,", "разумеется", "безусловно,", "безусловно",
    "определённо,", "определенно,", "отличный вопрос!", "отличный вопрос,",
    "хороший вопрос!", "хороший вопрос,", "прекрасный вопрос!", "прекрасный вопрос,",
    "спасибо за вопрос,", "спасибо за вопрос!", "я запомнил,", "я запомнил,",
    "запомнил,", "как ии,", "как языковая модель,", "как искусственный интеллект,",
)

_REFUSAL_MARKERS = (
    "как языковая модель", "как ии, я не", "я не могу помочь", "я не имею доступа",
    "я не обладаю", "извините, но я не могу", "к сожалению, я не могу",
)


def _strip_markdown(text: str) -> str:
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_TOKEN_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _NUMBERED_RE.sub("", text)
    return text


def _trim_sentences(text: str, max_sentences: int, max_chars: int) -> str:
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return ""
    if len(sentences) > max_sentences:
        sentences = sentences[:max_sentences]
    result = " ".join(sentences)
    if len(result) <= max_chars:
        return result
    cut = result[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("… "))
    if boundary >= int(max_chars * 0.5):
        return cut[: boundary + 1].strip()
    space = cut.rfind(" ")
    if space >= int(max_chars * 0.5):
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "."


def _capitalize(text: str) -> str:
    if not text:
        return text
    for index, ch in enumerate(text):
        if ch.isalpha():
            return text[:index] + ch.upper() + text[index + 1:]
    return text


def _answer_limits(
    skill_level: Any,
    kind: str,
    max_chars: int | None,
    max_sentences: int | None,
) -> tuple[int, int]:
    """Лимиты ответа с учётом уровня игрока и режима.

    - новичок + explain/chat: до 3 предложений / 320 символов;
    - опытный/профи + explain/chat (чат-режим): до 5 предложений / 500 символов;
    - остальные режимы: прежние лимиты (240 / 2), если вызывающий не задал свои.
    Явные меньшие лимиты вызывающего уважаются всегда.
    """
    level = normalize_skill_level(skill_level)
    cap_chars, cap_sentences = 240, 2
    if kind in ("explain", "chat"):
        if level == "beginner":
            cap_chars, cap_sentences = 320, 3
        elif level in ("advanced", "expert"):
            cap_chars, cap_sentences = 500, 5
    if max_chars is None:
        chars = cap_chars
    else:
        chars = min(int(max_chars), cap_chars) if kind in ("explain", "chat") and level != "amateur" else int(max_chars)
    if max_sentences is None:
        sentences = cap_sentences
    else:
        sentences = (
            min(int(max_sentences), cap_sentences)
            if kind in ("explain", "chat") and level != "amateur"
            else int(max_sentences)
        )
    return max(60, chars), max(1, sentences)


def sanitize_answer(
    text: str,
    *,
    max_chars: int | None = None,
    max_sentences: int | None = None,
    skill_level: str = "beginner",
    kind: str = "chat",
) -> str:
    """Чистит ответ LLM для голосовой озвучки.

    Убирает markdown/эмодзи/нумерацию, отрезает вводные фразы, обрезает по
    границе предложения. Лимиты зависят от уровня игрока и типа ответа
    (см. :func:`_answer_limits`). Возвращает ``""``, если ответ — пустая «вода»
    или отказ, чтобы Coach мог запросить повтор/взять запасной ответ.
    """
    if not isinstance(text, str):
        return ""
    max_chars_eff, max_sentences_eff = _answer_limits(skill_level, kind, max_chars, max_sentences)
    raw = _EMOJI_RE.sub(" ", text)
    raw = _strip_markdown(raw)
    raw = raw.replace("\r", " ").replace("\n", " ").replace("«", "").replace("»", "")
    raw = _SPACES_RE.sub(" ", raw).strip()
    lowered = raw.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS):
        return ""
    changed = True
    while changed:
        changed = False
        for prefix in _FILLER_PREFIXES:
            if lowered.startswith(prefix):
                raw = raw[len(prefix):].strip()
                lowered = raw.lower()
                changed = True
                break
    raw = raw.strip(" \t\"'“”„-—–!.,;:()[]")
    raw = _SPACES_RE.sub(" ", raw).strip()
    # После удаления вводных фраз в начале мог остаться маркер списка («1. ») —
    # прогоняем markdown-чистку ещё раз уже по новой строке.
    raw = _strip_markdown(raw)
    raw = raw.strip(" \t\"'“”„-—–!.,;:()[]")
    raw = _SPACES_RE.sub(" ", raw).strip()
    if not raw:
        return ""
    result = _trim_sentences(raw, max_sentences_eff, max_chars_eff)
    result = result.strip()
    if result and result[-1] not in ".!?…":
        result = result.rstrip(" ,;:—-") + "."
    return _capitalize(result)


def is_voice_safe(text: str, *, max_chars: int = 260) -> bool:
    """Быстрая проверка, что текст годится для озвучки (нет markdown/списков)."""
    if not isinstance(text, str) or not text.strip():
        return False
    if len(text) > max_chars:
        return False
    if _MD_TOKEN_RE.search(text) or "```" in text or _BULLET_RE.search(text) or _NUMBERED_RE.search(text):
        return False
    if _EMOJI_RE.search(text):
        return False
    return True
