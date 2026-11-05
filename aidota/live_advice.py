"""Живые подсказки по закупкам для ИИ-тренера AIDOTA.

Модуль превращает текущий GSI-снимок (герой, золото, нетворс, инвентарь,
стадия матча) и вражеский драфт в конкретные рекомендации формата
«предмет + почему». Работает без LLM и без сети: данные берутся из
``meta_db`` (теги угроз, ситуативные предметы сборки) и из таблицы
``ENEMY_ITEM_NOTES`` с точными объяснениями под конкретных героев.

Публичный контракт:
    build_item_advice(snapshot, ...) -> list[dict]
    format_item_advice(advice, limit=2) -> str

Подсказки потом используются:
- в веб-интерфейсе (карточка героя, лента советов);
- в тактическом брифе и системном промпте LLM (``coach_brain``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
PRIORITY_LABEL = {"HIGH": "важно", "MEDIUM": "полезно", "LOW": "на перспективу"}

# --------------------------------------------------------------------------- #
# Точечные знания под героев, где общие правила «тег → предмет» ошибаются.
# Формат: короткий npc-id -> {threat, items: [{item, why, priority}], warning}
# --------------------------------------------------------------------------- #
ENEMY_ITEM_NOTES: dict[str, dict[str, Any]] = {
    "juggernaut": {
        "name": "Juggernaut",
        "threat": "физический Omnislash и магический Blade Fury",
        "items": [
            {
                "item": "Ghost Scepter",
                "why": "эфирный облик делает тебя невосприимчивым к физическому урону — "
                "Omnislash по тебе не бьёт",
                "priority": "HIGH",
            },
            {
                "item": "Eul's Scepter of Divinity",
                "why": "самоциклон сбрасывает Omnislash и выигрывает время для команды",
                "priority": "HIGH",
            },
            {
                "item": "Aeon Disk",
                "why": "сейвит от прокаста Джаггернаута, когда ниже 70% HP",
                "priority": "MEDIUM",
            },
            {
                "item": "Black King Bar",
                "why": "держит Blade Fury и Nullifier, НО от Omnislash не спасает — "
                "он наносит физический урон",
                "priority": "MEDIUM",
            },
            {
                "item": "Heaven's Halberd",
                "why": "дизарм снимает автоатаки Джаггернаута вне ультимейта",
                "priority": "LOW",
            },
        ],
        "warning": "Linken's Sphere НЕ блокирует Omnislash — не трать на него слот "
        "только ради защиты от Джаггернаута.",
    },
    "legion_commander": {
        "name": "Legion Commander",
        "threat": "Duel (пробивает BKB)",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Duel — Легионка не сможет выбрать тебя целью",
                "priority": "HIGH",
            },
            {
                "item": "Lotus Orb",
                "why": "отражает Duel и снимает дебаффы, пока действует",
                "priority": "MEDIUM",
            },
            {
                "item": "Aeon Disk",
                "why": "не даёт умереть в дуэли, пока союзники не подойдут",
                "priority": "MEDIUM",
            },
            {
                "item": "Heaven's Halberd",
                "why": "дизарм до дуэли — Легионка не добьёт цель автоатаками",
                "priority": "LOW",
            },
        ],
        "warning": "BKB от Duel не спасает — дуэль пробивает невосприимчивость к магии.",
    },
    "doom_bringer": {
        "name": "Doom",
        "threat": "Doom (запрет способностей и предметов, пробивает BKB)",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Doom — самый надёжный сейв от ультимейта",
                "priority": "HIGH",
            },
            {
                "item": "Lotus Orb",
                "why": "отражает Doom обратно в Дуома",
                "priority": "HIGH",
            },
            {
                "item": "Aeon Disk",
                "why": "переживает прокаст и даёт время выйти из боя",
                "priority": "MEDIUM",
            },
        ],
        "warning": "BKB не спасает от Doom — ультимейт пробивает магиммунитет.",
    },
    "bane": {
        "name": "Bane",
        "threat": "Fiend's Grip и Enfeeble",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Fiend's Grip — Bane не удержит тебя на месте",
                "priority": "HIGH",
            },
            {
                "item": "Lotus Orb",
                "why": "отражает Grip и снимает Nightmare с союзника",
                "priority": "MEDIUM",
            },
            {
                "item": "Aeon Disk",
                "why": "не даёт умереть под полным контролем",
                "priority": "MEDIUM",
            },
        ],
        "warning": "BKB не снимает Fiend's Grip — это не магический контроль, а удержание.",
    },
    "beastmaster": {
        "name": "Beastmaster",
        "threat": "Primal Roar (пробивает BKB)",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Primal Roar — не будет стана и замедления",
                "priority": "HIGH",
            },
            {
                "item": "Lotus Orb",
                "why": "отражает Roar и снимает дебаффы",
                "priority": "MEDIUM",
            },
            {
                "item": "Aeon Disk",
                "why": "сейвит от инициации с саммонами",
                "priority": "MEDIUM",
            },
        ],
        "warning": "BKB от Primal Roar не защищает.",
    },
    "silencer": {
        "name": "Silencer",
        "threat": "Global Silence и снятие разума",
        "items": [
            {
                "item": "Black King Bar",
                "why": "сбрасывает и блокирует сайленсы, пока действует",
                "priority": "HIGH",
            },
            {
                "item": "Eul's Scepter of Divinity",
                "why": "самоциклон сбрасывает молчание",
                "priority": "MEDIUM",
            },
            {
                "item": "Manta Style",
                "why": "Manta снимает сайленс и даёт иллюзии",
                "priority": "MEDIUM",
            },
            {
                "item": "Lotus Orb",
                "why": "снимает молчание и отражает дебаффы",
                "priority": "LOW",
            },
        ],
    },
    "phantom_assassin": {
        "name": "Phantom Assassin",
        "threat": "уклонение Blur и криты",
        "items": [
            {
                "item": "Monkey King Bar",
                "why": "гарантированное попадание пробивает уклонение — ПА не уходит от атак",
                "priority": "HIGH",
            },
            {
                "item": "Silver Edge",
                "why": "истощение (Break) отключает Blur и криты на 5 секунд",
                "priority": "MEDIUM",
            },
            {
                "item": "Heaven's Halberd",
                "why": "дизарм не даёт реализовать урон с руки",
                "priority": "MEDIUM",
            },
        ],
    },
    "tinker": {
        "name": "Tinker",
        "threat": "Laser/Heat-Seeking Missile и March of the Machines",
        "items": [
            {
                "item": "Black King Bar",
                "why": "проживает прокаст Tinker и позволяет добраться до него",
                "priority": "HIGH",
            },
            {
                "item": "Pipe of Insight",
                "why": "срезает магический урон March и ракет",
                "priority": "MEDIUM",
            },
            {
                "item": "Force Staff",
                "why": "разрывает дистанцию, когда Tinker открывается",
                "priority": "LOW",
            },
        ],
    },
    "axe": {
        "name": "Axe",
        "threat": "Berserker's Call пробивает BKB",
        "items": [
            {
                "item": "Force Staff",
                "why": "вытаскивает тебя из Call и разрывает дистанцию",
                "priority": "MEDIUM",
            },
            {
                "item": "Eul's Scepter of Divinity",
                "why": "самоциклон переживает Call и Blade Mail",
                "priority": "MEDIUM",
            },
            {
                "item": "Aeon Disk",
                "why": "не даёт умереть под Call и Culling Blade",
                "priority": "MEDIUM",
            },
        ],
        "warning": "BKB не спасает от Berserker's Call — он пробивает магиммунитет.",
    },
    "huskar": {
        "name": "Huskar",
        "threat": "Berserker's Blood (реген и скорость атаки) и Burning Spear",
        "items": [
            {
                "item": "Spirit Vessel",
                "why": "срезает лечение Huskar — он больше не отхиливается на низком HP",
                "priority": "HIGH",
            },
            {
                "item": "Silver Edge",
                "why": "Break отключает Berserker's Blood",
                "priority": "HIGH",
            },
            {
                "item": "Eye of Skadi",
                "why": "замедление и срез регена не дают догнать и отхилиться",
                "priority": "MEDIUM",
            },
        ],
    },
    "medusa": {
        "name": "Medusa",
        "threat": "Mana Shield и Stone Gaze",
        "items": [
            {
                "item": "Diffusal Blade",
                "why": "сжигает ману — без маны у Медузы не работает щит",
                "priority": "HIGH",
            },
            {
                "item": "Eye of Skadi",
                "why": "замедление и срез регена не дают Медузе фармить и убегать",
                "priority": "MEDIUM",
            },
        ],
    },
    "spirit_breaker": {
        "name": "Spirit Breaker",
        "threat": "Charge of Darkness и Nether Strike",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Nether Strike и Charge",
                "priority": "HIGH",
            },
            {
                "item": "Eul's Scepter of Divinity",
                "why": "самоциклон сбивает разгон Charge",
                "priority": "MEDIUM",
            },
            {
                "item": "Force Staff",
                "why": "разрывает дистанцию и уводит из-под Charge",
                "priority": "LOW",
            },
        ],
    },
    "pudge": {
        "name": "Pudge",
        "threat": "Meat Hook и Dismember",
        "items": [
            {
                "item": "Linken's Sphere",
                "why": "блокирует Hook и Dismember — Пудж не зацепит",
                "priority": "HIGH",
            },
            {
                "item": "Force Staff",
                "why": "вытаскивает из хука и разрывает дистанцию",
                "priority": "MEDIUM",
            },
            {
                "item": "Lotus Orb",
                "why": "отражает Hook и Dismember",
                "priority": "LOW",
            },
        ],
    },
    "winter_wyvern": {
        "name": "Winter Wyvern",
        "threat": "Winter's Curse",
        "items": [
            {
                "item": "Aeon Disk",
                "why": "выживаешь под Winter's Curse, пока враги бьют друг друга",
                "priority": "HIGH",
            },
            {
                "item": "Linken's Sphere",
                "why": "блокирует Curse и Arctic Burn",
                "priority": "MEDIUM",
            },
        ],
    },
}

# Керри-лут, который бессмысленно советовать саппортам.
_HARD_CARRY_ITEMS = {
    "battle fury",
    "bfury",
    "manta style",
    "butterfly",
    "daedalus",
    "satanic",
    "mjollnir",
    "bloodthorn",
    "abyssal blade",
    "radiance",
    "diffusal blade",
    "monkey king bar",
    "nullifier",
    "greater critical",
    "meteor hammer",
    "dragon lance",
    "hurricane pike",
    "sange and yasha",
    "kaya and sange",
    "ethereal blade"
}

# Универсальные слоты, если по врагам/сборке советов не нашлось.
_ROLE_FALLBACK_ITEMS: dict[int, tuple[tuple[str, str], ...]] = {
    1: (("Black King Bar", "защита от магии и контроля в драках"),),
    2: (
        ("Black King Bar", "переживает прокаст врага в драке"),
        ("Eul's Scepter of Divinity", "сброс дебаффов и самоциклон"),
    ),
    3: (
        ("Black King Bar", "инициация без риска получить контроль"),
        ("Pipe of Insight", "аура магической защиты для команды"),
    ),
    4: (
        ("Force Staff", "спасает союзника и разрывает дистанцию"),
        ("Glimmer Cape", "сейв от физического и магического бурста"),
        ("Eul's Scepter of Divinity", "сброс дебаффов и самоциклон"),
    ),
    5: (
        ("Glimmer Cape", "сейв союзника и себя от бурста"),
        ("Force Staff", "вытаскивает союзника из-под контроля"),
        ("Ghost Scepter", "выживаем против мощных райткликеров"),
    ),
}

_SPLIT_RE = re.compile(r"\s*(?:/|\\|\bor\b|\bили\b|,)\s*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #
def _norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("npc_dota_hero_", "").replace("item_", "")
    text = text.replace("'", "").replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


def _norm_hero(value: Any) -> str:
    """Короткий id героя для поиска в справочниках."""
    text = _norm(value).replace(" ", "_")
    aliases = {
        "jugg": "juggernaut",
        "джагернаут": "juggernaut",
        "джагер": "juggernaut",
        "легионка": "legion_commander",
        "legion": "legion_commander",
        "doom": "doom_bringer",
        "дум": "doom_bringer",
        "па": "phantom_assassin",
        "pa": "phantom_assassin",
        "фантомка": "phantom_assassin",
        "мк": "monkey_king",
        "тб": "terrorblade",
        "вд": "witch_doctor",
        "цм": "crystal_maiden",
        "сф": "nevermore",
        "ам": "antimage",
    }
    return aliases.get(text, text)


def _primary_item(name: Any) -> str:
    """'Manta Style / BKB' -> 'Manta Style'; чистит пояснения в скобках."""
    text = str(name or "").strip()
    if not text:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text).strip()
    first = _SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return first or text


def _base_item(name: Any) -> str:
    return _norm(_primary_item(name))


# --------------------------------------------------------------------------- #
# Внутренние helpers
# --------------------------------------------------------------------------- #
def _normalize_position(position: Any) -> int:
    try:
        return max(1, min(5, int(position or 1)))
    except (TypeError, ValueError):
        return 1


def _snapshot_owned(snapshot: Mapping[str, Any]) -> set[str]:
    owned: set[str] = set()
    for key in ("inventory", "inventory_keys"):
        for name in snapshot.get(key) or []:
            clean = _base_item(name)
            if clean:
                owned.add(clean)
    neutral = snapshot.get("neutral_item")
    if neutral:
        owned.add(_base_item(neutral))
    return owned


def _item_cost(item: str, item_costs: Mapping[str, int] | None) -> int | None:
    if not item_costs:
        return None
    target = _base_item(item)
    if not target:
        return None
    for name, cost in item_costs.items():
        try:
            value = int(cost)
        except (TypeError, ValueError):
            continue
        if _base_item(name) == target:
            return value
    return None


def _cost_text(advice: Mapping[str, Any]) -> str:
    cost = advice.get("cost")
    if not cost:
        return ""
    gold = int(advice.get("gold_needed") or 0)
    if gold <= 0:
        return f"{int(cost)} — хватает золота"
    return f"{int(cost)} — не хватает {gold}"


def _enemy_display(enemy: str) -> str:
    return str(enemy or "").strip()


def _role_allows(item: str, position: int, *, source: str) -> bool:
    """Отсекаем керри-лут для позиций 4–5 (точечные контр-предметы разрешаем)."""
    if position <= 3:
        return True
    if source in ("counter", "fallback"):
        return True
    base = _base_item(item)
    return base not in _HARD_CARRY_ITEMS


def _adapt_composite(item: str) -> str:
    """'Spirit Vessel / Eye of Skadi' -> 'Spirit Vessel' (первый известный слот)."""
    return _primary_item(item)


def _advice_score(advice: Mapping[str, Any]) -> tuple[int, int]:
    priority = PRIORITY_ORDER.get(str(advice.get("priority") or "LOW"), 2)
    source_rank = {"counter": 0, "situational": 1, "core": 2, "role": 3}.get(
        str(advice.get("source") or ""), 3
    )
    return priority, source_rank


def _make_advice(
    item: str,
    reason: str,
    priority: str,
    source: str,
    *,
    enemies: Iterable[str] = (),
    warning: str | None = None,
    item_costs: Mapping[str, int] | None = None,
    gold: int = 0,
) -> dict[str, Any]:
    primary = _adapt_composite(item)
    cost = _item_cost(primary, item_costs)
    gold_needed = max(0, int(cost) - int(gold)) if cost else None
    return {
        "item": primary,
        "cost": int(cost) if cost else None,
        "gold_needed": gold_needed,
        "affordable": (gold_needed == 0) if gold_needed is not None else None,
        "priority": priority,
        "reason": " ".join(str(reason or "").split()),
        "enemies": [str(e) for e in enemies if str(e).strip()],
        "source": source,
        "warning": warning,
    }


def _meta_threats(enemy_heroes: list[str]) -> list[dict[str, Any]]:
    try:
        from . import meta_db

        threats = meta_db.get_hero_threats(enemy_heroes)
        return threats if isinstance(threats, list) else []
    except Exception:  # noqa: BLE001 — мета-база не должна ронять совет
        return []


def _meta_build(hero_name: str, position: int, enemy_heroes: list[str]) -> dict[str, Any]:
    if not hero_name:
        return {}
    try:
        from . import meta_db

        build = meta_db.get_hero_build(hero_name, role=position, enemy_heroes=enemy_heroes)
        return build if isinstance(build, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _counter_rules_by_tag() -> dict[str, dict[str, Any]]:
    try:
        from . import meta_db

        rules = getattr(meta_db, "SITUATIONAL_COUNTER_RULES", []) or []
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tags = rule.get("threat")
        if isinstance(tags, str) and tags:
            result[tags] = rule
    return result


# --------------------------------------------------------------------------- #
# Публичный API
# --------------------------------------------------------------------------- #
def build_item_advice(
    snapshot: dict,
    *,
    hero_name: str = "",
    position: Any = 1,
    enemy_heroes: list[str] | None = None,
    item_costs: Mapping[str, int] | None = None,
    max_items: int = 3,
) -> list[dict]:
    """Рекомендует до ``max_items`` предметов с объяснением «почему».

    Безопасен для offline/битых данных: в этом случае возвращает пустой список.
    """
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return []

    position = _normalize_position(position)
    inventory = _snapshot_owned(snapshot)
    gold = 0
    try:
        gold = max(0, int(snapshot.get("gold") or 0))
    except (TypeError, ValueError):
        gold = 0

    enemies = [
        str(enemy).strip()
        for enemy in (enemy_heroes if enemy_heroes is not None else snapshot.get("enemy_picks") or [])
        if str(enemy or "").strip()
    ]
    hero_name = str(hero_name or snapshot.get("hero_name") or "").strip()

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item_name: Any, reason: str, priority: str, source: str, *,
            enemies_hit: Iterable[str] = (), warning: str | None = None) -> None:
        primary = _primary_item(item_name)
        base = _base_item(primary)
        if not primary or not base or base in seen or base in inventory:
            return
        if not _role_allows(primary, position, source=source):
            return
        seen.add(base)
        candidates.append(
            _make_advice(
                primary,
                reason,
                priority,
                source,
                enemies=enemies_hit,
                warning=warning,
                item_costs=item_costs,
                gold=gold,
            )
        )

    # 1) Точечные знания под вражеских героев.
    for enemy in enemies:
        note = ENEMY_ITEM_NOTES.get(_norm_hero(enemy))
        if not note:
            continue
        for entry in note.get("items") or []:
            add(
                entry.get("item", ""),
                f"против {_enemy_display(enemy)}: {entry.get('why', '')}",
                str(entry.get("priority") or "MEDIUM"),
                "counter",
                enemies_hit=[enemy],
                warning=note.get("warning"),
            )

    # 2) Общие правила мета-базы: тег угрозы -> контр-предметы.
    rules = _counter_rules_by_tag()
    for threat in _meta_threats(enemies):
        tags = [str(tag) for tag in threat.get("tags") or []]
        enemy_name = str(threat.get("ru_name") or threat.get("hero_id") or "")
        for tag in tags:
            rule = rules.get(tag)
            if not rule:
                continue
            description = str(rule.get("description") or tag)
            tip = str(rule.get("tip") or "")
            for item_name in (rule.get("items") or [])[:2]:
                add(
                    item_name,
                    f"против {enemy_name}: {description}. {tip}",
                    "MEDIUM",
                    "situational",
                    enemies_hit=[enemy_name] if enemy_name else [],
                )

    # 3) Сборка самого героя: ситуативка под врагов и следующий кор-слот.
    build = _meta_build(hero_name, position, enemies)
    for entry in build.get("situational") or []:
        if not isinstance(entry, dict):
            continue
        item_name = entry.get("item", "")
        why = str(entry.get("why") or entry.get("trigger") or "").strip()
        add(
            item_name,
            f"под текущий матч: {why}" if why else "ситуативный слот под врагов",
            str(entry.get("priority") or "MEDIUM").upper(),
            "situational",
        )
    for entry in build.get("core") or []:
        if not isinstance(entry, dict):
            continue
        item_name = entry.get("item", "")
        tip = str(entry.get("tip") or "").strip()
        timing = str(entry.get("timing") or "").strip()
        reason = "ключевой слот сборки"
        if timing:
            reason += f" ({timing})"
        if tip:
            reason += f": {tip}"
        add(item_name, reason, "LOW", "core")

    # 4) Универсальный запас под роль.
    for item_name, why in _ROLE_FALLBACK_ITEMS.get(position, ()):
        add(item_name, f"универсальный слот для позиции {position}: {why}", "LOW", "fallback")

    candidates.sort(key=_advice_score)
    return candidates[: max(1, int(max_items or 1))]


def format_item_advice(advice: Iterable[Mapping[str, Any]], *, limit: int = 2) -> str:
    """Строка для промпта/озвучки: «X (цена, хватает) — причина; Y — причина»."""
    parts: list[str] = []
    for entry in list(advice or [])[: max(1, int(limit or 1))]:
        item = str(entry.get("item") or "").strip()
        if not item:
            continue
        price = _cost_text(entry)
        reason = str(entry.get("reason") or "").strip()
        chunk = item
        if price:
            chunk += f" ({price})"
        if reason:
            chunk += f" — {reason}"
        parts.append(chunk)
    return "; ".join(parts)


def describe_item_advice(advice: Mapping[str, Any]) -> str:
    """Короткая человекочитаемая строка для ленты/UI."""
    item = str(advice.get("item") or "").strip()
    reason = str(advice.get("reason") or "").strip()
    price = _cost_text(advice)
    head = item
    if price:
        head += f" ({price})"
    return f"{head} — {reason}" if reason else head
