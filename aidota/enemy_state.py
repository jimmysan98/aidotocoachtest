"""aidota.enemy_state — Трёхзначная модель состояния врагов (Fork E1).

Отслеживает состояние противников по трём эпистемическим статусам:
  - CONFIRMED: подтверждено игроком (голос/WebUI), спектатором или драфтом;
  - INFERRED: метовые тайминги предметов по минуте матча и данным меты;
  - UNKNOWN: туман войны по умолчанию в обычном матчмейкинге (НЕ означает отсутствие предмета).

Модуль строго разделяет подтверждённые факты и предположения, предотвращая
галлюцинации и ложную уверенность в голосовых советах и UI-карточках.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .meta_db import (
    META_HEROES_DB,
    _norm_hero_key,
    _resolve_hero_id,
    get_hero_meta_entry,
)

log = logging.getLogger("aidota.enemy_state")

STATE_CONFIRMED = "confirmed"
STATE_INFERRED = "inferred"
STATE_UNKNOWN = "unknown"

VALID_STATES = frozenset({STATE_CONFIRMED, STATE_INFERRED, STATE_UNKNOWN})

# ---------------------------------------------------------------------------
# Эталонные окна метовых таймингов угроз (секунды от старта матча)
# ---------------------------------------------------------------------------
_META_TIMING_WINDOWS: dict[str, tuple[int, int, tuple[str, ...], str]] = {
    "blink": (
        540,
        900,
        (
            "Axe", "Magnus", "Tidehunter", "Centaur Warrunner", "Slardar",
            "Sand King", "Earthshaker", "Pudge", "Mars", "Batrider", "Legion Commander",
        ),
        "опасность внезапной инициации",
    ),
    "black_king_bar": (
        1080,
        1500,
        (
            "Phantom Assassin", "Sven", "Ursa", "Troll Warlord", "Shadow Fiend",
            "Leshrac", "Invoker", "Muerta", "Gyrocopter", "Razor", "Outworld Destroyer",
        ),
        "иммунитет к магии и контролю",
    ),
    "orchid": (
        720,
        1080,
        ("Storm Spirit", "Queen of Pain", "Clinkz", "Silencer", "Nature's Prophet"),
        "сайленс и соло-килл потенциал",
    ),
    "diffusal_blade": (
        660,
        960,
        ("Phantom Lancer", "Slark", "Riki", "Spectre", "Weaver"),
        "сжигание маны и замедление",
    ),
    "battle_fury": (
        720,
        1020,
        ("Anti-Mage", "Phantom Assassin", "Juggernaut", "Ursa"),
        "разгон фарма и сплит-пуш",
    ),
    "manta_style": (
        1080,
        1440,
        ("Anti-Mage", "Phantom Lancer", "Terrorblade", "Spectre", "Medusa", "Juggernaut"),
        "развеивание дебаффов и урон иллюзиями",
    ),
    "silver_edge": (
        900,
        1380,
        ("Slark", "Sniper", "Dragon Knight", "Kunkka", "Shadow Fiend"),
        "невидимость и истощение (Break пассивок)",
    ),
    "blade_mail": (
        480,
        840,
        ("Axe", "Legion Commander", "Spectre", "Bristleback", "Clockwerk"),
        "возврат урона при дуэли/агрессии",
    ),
    "spirit_vessel": (
        660,
        1080,
        ("Invoker", "Pudge", "Bounty Hunter", "Ancient Apparition", "Spirit Breaker"),
        "урезание регенерации и процентный урон",
    ),
    "armlet": (
        420,
        780,
        ("Huskar", "Wraith King", "Lifestealer", "Chaos Knight", "Kunkka"),
        "резкий прирост силы и выживаемости",
    ),
    "sheepstick": (
        1500,
        2400,
        ("Tinker", "Storm Spirit", "Invoker", "Lina", "Nature's Prophet", "Puck", "Windranger"),
        "мгновенный хекс без шанса нажать BKB",
    ),
}


@dataclass
class EnemyItemRecord:
    """Запись о предмете вражеского героя."""

    item_key: str
    item_slug: str
    display_name: str
    state: str = STATE_UNKNOWN  # confirmed | inferred | unknown
    source: str = "default_unknown"  # player_callout | spectator_gsi | meta_timing | default_unknown
    detected_sec: int | None = None
    timing_window_sec: tuple[int, int] | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnemyHeroState:
    """Состояние одного героя противника."""

    hero_id: str
    hero_name: str
    level: int | None = None
    items: list[EnemyItemRecord] = field(default_factory=list)
    last_seen_sec: int | None = None
    ult_cooldown_until_sec: int | None = None
    is_picked: bool = True

    def get_item(self, slug_or_key: str) -> EnemyItemRecord | None:
        target = slug_or_key.strip().lower()
        if target.startswith("item_"):
            target = target[5:]
        for item in self.items:
            if item.item_slug.lower() == target or item.item_key.lower() == target:
                return item
        return None

    def confirmed_items(self) -> list[EnemyItemRecord]:
        return [it for it in self.items if it.state == STATE_CONFIRMED]

    def inferred_items(self) -> list[EnemyItemRecord]:
        return [it for it in self.items if it.state == STATE_INFERRED]

    def unknown_items(self) -> list[EnemyItemRecord]:
        return [it for it in self.items if it.state == STATE_UNKNOWN]

    def overall_badge(self) -> str:
        """Сводный статус для UI-карточки героя."""
        if any(it.state == STATE_CONFIRMED for it in self.items):
            return STATE_CONFIRMED
        if any(it.state == STATE_INFERRED for it in self.items):
            return STATE_INFERRED
        return STATE_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "hero_id": self.hero_id,
            "hero_name": self.hero_name,
            "level": self.level,
            "badge": self.overall_badge(),
            "items": [it.to_dict() for it in self.items],
            "last_seen_sec": self.last_seen_sec,
            "ult_cooldown_until_sec": self.ult_cooldown_until_sec,
            "is_picked": self.is_picked,
        }


# ---------------------------------------------------------------------------
# Эпистемические формулировки для голоса и промпта
# ---------------------------------------------------------------------------

def format_epistemic_item_notice(hero_name: str, item_name: str, state: str, notes: str = "") -> str:
    """Строит строгое речевое уведомление с точным эпистемическим статусом."""
    clean_notes = f" ({notes})" if notes else ""
    if state == STATE_CONFIRMED:
        return f"У {hero_name} подтверждён {item_name}{clean_notes}."
    if state == STATE_INFERRED:
        return f"К этой минуте у {hero_name} возможен {item_name}{clean_notes} — проверь слоты перед инициацией."
    return f"Слоты {hero_name} в тумане войны — исходи из худшего сценария, не считай, что предметов нет."


def format_enemy_state_prompt_block(hero_states: list[EnemyHeroState], clock_sec: int) -> str:
    """Формирует сжатый блок для Seg 2 промпта с чётким разделением confirmed/inferred/unknown."""
    if not hero_states:
        return "Враги: драфт не определён."

    lines: list[str] = [
        "СОСТОЯНИЕ ВРАГОВ (ЭПИСТЕМИКА: confirmed=факт, inferred=вероятный тайминг, unknown=туман):"
    ]
    for hero in hero_states:
        if not hero.is_picked:
            continue
        conf = [it.display_name for it in hero.confirmed_items()]
        inf = [it.display_name for it in hero.inferred_items()]
        
        parts: list[str] = []
        if conf:
            parts.append(f"ФАКТ: {', '.join(conf)}")
        if inf:
            parts.append(f"ТАЙМИНГ (проверь): {', '.join(inf)}")
        if not conf and not inf:
            parts.append("ТУМАН ВОЙНЫ (инвентарь скрыт)")

        ult_info = ""
        if hero.ult_cooldown_until_sec and hero.ult_cooldown_until_sec > clock_sec:
            left = hero.ult_cooldown_until_sec - clock_sec
            ult_info = f" [Ульт на КД: ещё ~{left}с]"

        lvl_info = f" Lvl {hero.level}" if hero.level else ""
        lines.append(f"- {hero.hero_name}{lvl_info}: {'; '.join(parts)}{ult_info}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Класс трекера состояния врагов
# ---------------------------------------------------------------------------

class EnemyStateTracker:
    """Канонический менеджер состояния врагов с поддержкой GSI, WebUI и эпистемики."""

    def __init__(self) -> None:
        self._match_id: str | None = None
        self._enemies: dict[str, EnemyHeroState] = {}
        # Зафиксированные игроком предметы: hero_key -> {item_slug: EnemyItemRecord}
        self._player_confirmations: dict[str, dict[str, EnemyItemRecord]] = {}
        # Зафиксированные кулдауны ультов: hero_key -> expire_clock_sec
        self._ult_cooldowns: dict[str, int] = {}

    def sync_match_id(self, match_id: Any) -> None:
        """Сбрасывает состояние при смене матча."""
        str_id = str(match_id or "").strip()
        if not str_id or str_id == "0":
            return
        if self._match_id != str_id:
            log.info("EnemyStateTracker: смена матча %s -> %s, сброс состояния врагов", self._match_id, str_id)
            self._match_id = str_id
            self._enemies.clear()
            self._player_confirmations.clear()
            self._ult_cooldowns.clear()

    def reset(self) -> None:
        """Полный сброс трекера."""
        self._match_id = None
        self._enemies.clear()
        self._player_confirmations.clear()
        self._ult_cooldowns.clear()

    def _normalize_item_slug(self, raw_item: str) -> str:
        name = raw_item.strip().lower()
        if name.startswith("item_"):
            name = name[5:]
        return re.sub(r"[^a-z0-9_]+", "_", name).strip("_")

    def _clean_display_name(self, slug: str, raw_name: str | None = None) -> str:
        if raw_name and raw_name.strip() and not raw_name.startswith("item_"):
            return raw_name.strip()
        # Стандартные красивые имена для частых угроз
        pretty = {
            "black_king_bar": "Black King Bar",
            "blink": "Blink Dagger",
            "orchid": "Orchid Malevolence",
            "diffusal_blade": "Diffusal Blade",
            "battle_fury": "Battle Fury",
            "manta_style": "Manta Style",
            "silver_edge": "Silver Edge",
            "blade_mail": "Blade Mail",
            "spirit_vessel": "Spirit Vessel",
            "armlet": "Armlet of Mordiggian",
            "sheepstick": "Scythe of Vyse",
            "abyssal_blade": "Abyssal Blade",
            "lotus_orb": "Lotus Orb",
            "linkens_sphere": "Linken's Sphere",
            "sphere": "Linken's Sphere",
            "nullifier": "Nullifier",
            "bloodthorn": "Bloodthorn",
            "heart": "Heart of Tarrasque",
            "butterfly": "Butterfly",
        }
        return pretty.get(slug, slug.replace("_", " ").title())

    def confirm_enemy_item(
        self,
        hero_key: str,
        item_key: str,
        clock_sec: int | None = None,
        source: str = "player_callout",
        notes: str = "",
    ) -> bool:
        """Подтверждает наличие предмета у врага (голос игрока, клик WebUI или спектатор)."""
        hero_id = _resolve_hero_id(hero_key) or _norm_hero_key(hero_key)
        if not hero_id:
            return False

        from .item_validator import resolve_item_slug_and_name
        slug, display, _ = resolve_item_slug_and_name(item_key)

        record = EnemyItemRecord(
            item_key=f"item_{slug}",
            item_slug=slug,
            display_name=display,
            state=STATE_CONFIRMED,
            source=source,
            detected_sec=clock_sec,
            notes=notes or "Зафиксировано игроком",
        )

        if hero_id not in self._player_confirmations:
            self._player_confirmations[hero_id] = {}
        self._player_confirmations[hero_id][slug] = record

        # Если герой ещё не в словаре (например, до GSI-драфта) — создаём его сразу
        if hero_id not in self._enemies:
            meta = get_hero_meta_entry(hero_id) or META_HEROES_DB.get(hero_id) or {}
            display_hero_name = str(meta.get("name") or hero_key)
            self._enemies[hero_id] = EnemyHeroState(
                hero_id=hero_id,
                hero_name=display_hero_name,
                is_picked=True,
            )

        hero = self._enemies[hero_id]
        existing = hero.get_item(slug)
        if existing:
            existing.state = STATE_CONFIRMED
            existing.source = source
            existing.detected_sec = clock_sec
            if notes:
                existing.notes = notes
        else:
            hero.items.append(record)
        return True

    def dismiss_enemy_item(self, hero_key: str, item_key: str) -> bool:
        """Сбрасывает или удаляет подтверждённый/предполагаемый предмет врага."""
        hero_id = _resolve_hero_id(hero_key) or _norm_hero_key(hero_key)
        if not hero_id:
            return False

        slug = self._normalize_item_slug(item_key)
        if hero_id in self._player_confirmations:
            self._player_confirmations[hero_id].pop(slug, None)

        if hero_id in self._enemies:
            hero = self._enemies[hero_id]
            hero.items = [it for it in hero.items if it.item_slug != slug]
        return True

    def record_enemy_ult(
        self,
        hero_key: str,
        cooldown_sec: int,
        clock_sec: int,
    ) -> None:
        """Фиксирует использование ультимейта врагом."""
        hero_id = _resolve_hero_id(hero_key) or _norm_hero_key(hero_key)
        if not hero_id:
            return
        expire = clock_sec + max(1, cooldown_sec)
        self._ult_cooldowns[hero_id] = expire
        if hero_id in self._enemies:
            self._enemies[hero_id].ult_cooldown_until_sec = expire

    def sync_gsi(
        self,
        payload: dict[str, Any] | None,
        clock_sec: int,
        enemy_picks: list[str] | None = None,
        catalog: Any = None,
    ) -> None:
        """Синхронизирует состояние врагов по тику GSI."""
        if not isinstance(payload, dict):
            return

        map_obj = payload.get("map") or {}
        match_id = str(map_obj.get("matchid") or "")
        if match_id and match_id != "0":
            self.sync_match_id(match_id)

        # 1. Извлекаем пики врагов
        picks = list(enemy_picks or [])
        if not picks:
            try:
                from .context import collect_draft
                draft = collect_draft(payload, catalog)
                picks = list(draft.enemy_picks)
            except Exception:  # noqa: BLE001
                picks = []

        # 2. Проверяем режим спектатора/лобби (allplayers с полными слотами врагов)
        allplayers = payload.get("allplayers") or {}
        spectator_enemy_items: dict[str, list[dict[str, str]]] = {}
        if isinstance(allplayers, dict):
            for _player_slot, pdata in allplayers.items():
                if not isinstance(pdata, dict):
                    continue
                # Если в allplayers видны слоты вражеской команды
                raw_items = pdata.get("items")
                if isinstance(raw_items, dict):
                    hero_raw = str(pdata.get("hero_name") or pdata.get("name") or "")
                    if hero_raw:
                        h_id = _resolve_hero_id(hero_raw) or _norm_hero_key(hero_raw)
                        if h_id:
                            item_list: list[dict[str, str]] = []
                            for slot_k, slot_v in raw_items.items():
                                if str(slot_k).startswith("slot") and isinstance(slot_v, dict):
                                    iname = str(slot_v.get("name") or "")
                                    if iname and iname != "empty" and not iname.startswith("item_empty"):
                                        item_list.append({"key": iname, "slug": self._normalize_item_slug(iname)})
                            if item_list:
                                spectator_enemy_items[h_id] = item_list

        # 3. Обновляем/создаём объекты EnemyHeroState
        active_ids: set[str] = set()
        for hero_raw in picks:
            h_id = _resolve_hero_id(hero_raw) or _norm_hero_key(hero_raw)
            if not h_id:
                continue
            active_ids.add(h_id)
            meta = get_hero_meta_entry(h_id) or META_HEROES_DB.get(h_id) or {}
            display_name = str(meta.get("name") or hero_raw)

            hero_state = self._enemies.get(h_id)
            if hero_state is None:
                hero_state = EnemyHeroState(
                    hero_id=h_id,
                    hero_name=display_name,
                    is_picked=True,
                )
                self._enemies[h_id] = hero_state

            hero_state.hero_name = display_name
            hero_state.is_picked = True

            # Восстанавливаем сохранённый КД ульта
            if h_id in self._ult_cooldowns:
                hero_state.ult_cooldown_until_sec = self._ult_cooldowns[h_id]

            # 4. Обновляем предметы героя:
            # Спектаторские подтверждённые предметы
            spec_items = spectator_enemy_items.get(h_id)
            if spec_items is not None:
                # Заменяем неизвестные на подтверждённые спектатором
                new_items: list[EnemyItemRecord] = []
                for spec_it in spec_items:
                    new_items.append(
                        EnemyItemRecord(
                            item_key=spec_it["key"],
                            item_slug=spec_it["slug"],
                            display_name=self._clean_display_name(spec_it["slug"], spec_it["key"]),
                            state=STATE_CONFIRMED,
                            source="spectator_gsi",
                            detected_sec=clock_sec,
                            notes="Подтверждено спектатором GSI",
                        )
                    )
                hero_state.items = new_items
            else:
                # Обычный матчмейкинг: туман войны + подтверждения игрока + inferred тайминги
                self._update_matchmaking_hero_items(hero_state, clock_sec)

    def _update_matchmaking_hero_items(self, hero_state: EnemyHeroState, clock_sec: int) -> None:
        """Обновляет предметы в обычном матчмейкинге (с туманом войны и таймингами)."""
        h_id = hero_state.hero_id
        hero_name = hero_state.hero_name

        existing_items = {it.item_slug: it for it in hero_state.items}
        confirmed_by_player = self._player_confirmations.get(h_id, {})

        # 1. Применяем подтверждения игрока (CONFIRMED)
        for slug, rec in confirmed_by_player.items():
            existing_items[slug] = rec

        # 2. Оцениваем метовые тайминги (INFERRED)
        for timing_slug, (t_start, t_end, heroes_list, note) in _META_TIMING_WINDOWS.items():
            # Если герой входит в список типичных носителей этого предмета
            hero_matches = any(
                h.lower() == hero_name.lower() or h.lower() in hero_name.lower()
                for h in heroes_list
            )
            if not hero_matches:
                continue

            # Если предмет уже подтверждён игроком — не трогаем
            if timing_slug in existing_items and existing_items[timing_slug].state == STATE_CONFIRMED:
                continue

            # Если матч достиг окна тайминга — статус INFERRED
            if clock_sec >= t_start:
                timing_rec = EnemyItemRecord(
                    item_key=f"item_{timing_slug}",
                    item_slug=timing_slug,
                    display_name=self._clean_display_name(timing_slug),
                    state=STATE_INFERRED,
                    source="meta_timing",
                    detected_sec=clock_sec,
                    timing_window_sec=(t_start, t_end),
                    notes=f"Окно {t_start // 60}–{t_end // 60} мин: {note}",
                )
                existing_items[timing_slug] = timing_rec

        hero_state.items = list(existing_items.values())

    def get_enemy_states(self) -> list[EnemyHeroState]:
        """Возвращает список всех активных состояний врагов."""
        return list(self._enemies.values())

    def get_hero_state(self, hero_key: str) -> EnemyHeroState | None:
        """Возвращает состояние конкретного героя."""
        h_id = _resolve_hero_id(hero_key) or _norm_hero_key(hero_key)
        return self._enemies.get(h_id) if h_id else None

    def to_ui_payload(self) -> list[dict[str, Any]]:
        """Формирует данные для WebUI с бейджами статусов."""
        return [hero.to_dict() for hero in self._enemies.values()]

    def build_prompt_context(self, clock_sec: int) -> str:
        """Строит строгий контекст для передачи в системный промпт LLM."""
        return format_enemy_state_prompt_block(list(self._enemies.values()), clock_sec)
