# -*- coding: utf-8 -*-
"""Живые советы по закупкам в промпте ИИ-тренера (контракт v1.2).

Проверяем главное для пользователя: тренер видит **реальный** GSI-матч
(верхнеуровневые ``items``/``abilities``, золото, нетворс, врагов) и в
системный промпт попадает обоснованный совет вида
«Закупка, следующий предмет: Ghost Scepter (1500 — не хватает 560) —
против Juggernaut: ...», а не общая фраза.

Сеть и LLM не используются: только локальные fake-объекты и чистые функции
``aidota.context`` / ``aidota.coach_brain`` / ``aidota.live_advice``.
"""

from __future__ import annotations

import copy
import json
import unittest

from aidota.coach import Coach, _ENEMY_ITEM_HINTS
from aidota.coach_brain import build_brief, render_brief
from aidota.config import LlmConfig, PlayerProfile
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from tests.helpers import FakeSession, make_temp_dir, remove_temp_dir

# --------------------------------------------------------------------------- #
# Фикстуры: реалистичный GSI-пакет за Rubick (поз. 5) на 8:45 против Juggernaut.
# ВАЖНО: items и abilities — верхнеуровневые секции, как в настоящем GSI.
# --------------------------------------------------------------------------- #
HEROES = [
    {"id": 86, "name": "npc_dota_hero_rubick", "localized_name": "Rubick"},
    {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut"},
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
    {"id": 2, "name": "npc_dota_hero_pudge", "localized_name": "Pudge"},
    {"id": 7, "name": "npc_dota_hero_windrunner", "localized_name": "Windranger"},
    {"id": 11, "name": "npc_dota_hero_nevermore", "localized_name": "Shadow Fiend"},
    {"id": 15, "name": "npc_dota_hero_razor", "localized_name": "Razor"},
    {"id": 44, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin"},
]

# Словарь предметов в формате DotaApi.items(): slug -> display.
ITEM_NAMES = {
    "magic_wand": "Magic Wand",
    "tranquil_boots": "Tranquil Boots",
    "light_collector": "Light Collector",
    "tpscroll": "Town Portal Scroll",
    "occult_bracelet": "Occult Bracelet",
    "power_treads": "Power Treads",
    "bfury": "Battle Fury",
    "manta": "Manta Style",
}

# Цены предметов в формате DotaApi.item_costs(): display -> cost.
ITEM_COSTS = {
    "Ghost Scepter": 1500,
    "Eul's Scepter of Divinity": 2475,
    "Aeon Disk": 3000,
    "Black King Bar": 4050,
    "Heaven's Halberd": 3400,
    "Linken's Sphere": 4800,
    "Force Staff": 2200,
    "Glimmer Cape": 2150,
    "Magic Wand": 460,
    "Tranquil Boots": 900,
    "Power Treads": 1400,
    "Battle Fury": 4100,
    "Manta Style": 4700,
    "Town Portal Scroll": 90,
    "Blade Mail": 2100,
}


def catalog() -> HeroCatalog:
    return HeroCatalog(HEROES)


def live_payload() -> dict:
    """Свежий пакет: предметы и способности в top-level секциях, net_worth нет."""
    return {
        "provider": {"name": "Dota 2", "appid": 570, "version": 45},
        "map": {
            "clock_time": 525,
            "radiant_score": 11,
            "dire_score": 20,
            "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
        },
        "player": {
            "team_name": "dire",
            "kills": 2,
            "deaths": 3,
            "assists": 3,
            "gold": 940,
            "gpm": 355,
            "xpm": 380,
            "last_hits": 19,
        },
        "hero": {
            "id": 86,
            "name": "npc_dota_hero_rubick",
            "level": 10,
            "alive": True,
            "health_percent": 74.5,
            "mana_percent": 62.5,
            "buyback_cost": 200,
            "buyback_cooldown": 0,
            "aghanims_scepter": False,
            "aghanims_shard": False,
        },
        "items": {
            "slot0": {"name": "item_magic_wand", "cooldown": 0},
            "slot1": {"name": "item_tranquil_boots", "cooldown": 0},
            "slot2": {"name": "item_light_collector", "cooldown": 0},
            "slot3": {"name": "item_empty"},
            "slot4": {"name": "item_empty"},
            "slot5": {"name": "item_empty"},
            "teleport0": {"name": "item_tpscroll", "cooldown": 0},
            "neutral0": {"name": "item_occult_bracelet", "passive": True},
        },
        "abilities": {
            "ability0": {
                "name": "rubick_telekinesis", "level": 4, "ultimate": False,
                "cooldown": 0, "can_cast": True,
            },
            "ability3": {
                "name": "rubick_spell_steal", "level": 1, "ultimate": True,
                "cooldown": 12, "can_cast": True,
            },
        },
        "draft": {
            # team2 = radiant (враги игрока), team3 = dire (его команда).
            "team2": {"pick_ids": [8]},
            "team3": {"pick_ids": [1]},
        },
    }


def nested_payload() -> dict:
    """Тот же пакет, но предметы/способности вложены в ``hero`` (старый формат)."""
    payload = copy.deepcopy(live_payload())
    payload["hero"]["items"] = payload.pop("items")
    payload["hero"]["abilities"] = payload.pop("abilities")
    return payload


class FakeGsi:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 1.5


# --------------------------------------------------------------------------- #
# 1. Предметы/ульта из верхнеуровневых секций GSI
# --------------------------------------------------------------------------- #
class TopLevelItemsTests(unittest.TestCase):
    def test_inventory_and_neutral_from_top_level_items(self):
        brief = build_brief(live_payload(), catalog(), ITEM_NAMES, position=5)
        self.assertTrue(brief["available"])
        self.assertEqual(brief["hero_name"], "Rubick")
        # hero.items в пейлоаде нет — инвентарь обязан прийти из payload["items"].
        self.assertEqual(
            brief["inventory"],
            ["Magic Wand", "Tranquil Boots", "Light Collector"],
        )
        self.assertEqual(
            brief["inventory_keys"],
            ["item_magic_wand", "item_tranquil_boots", "item_light_collector"],
        )
        self.assertEqual(brief["neutral_item"], "Occult Bracelet")
        self.assertTrue(brief["has_tp"])

    def test_nested_legacy_layout_still_works(self):
        brief = build_brief(nested_payload(), catalog(), ITEM_NAMES, position=5)
        self.assertEqual(
            brief["inventory"],
            ["Magic Wand", "Tranquil Boots", "Light Collector"],
        )
        self.assertEqual(brief["neutral_item"], "Occult Bracelet")

    def test_ult_from_top_level_abilities(self):
        brief = build_brief(live_payload(), catalog(), ITEM_NAMES, position=5)
        self.assertEqual(brief["ult_name"], "Spell Steal")
        self.assertEqual(brief["ult_cooldown"], 12)
        self.assertFalse(brief["ult_ready"])
        text = render_brief(brief)
        self.assertIn("Spell Steal", text)
        self.assertIn("ульт", text.lower())

        nested = build_brief(nested_payload(), catalog(), ITEM_NAMES, position=5)
        self.assertEqual(nested["ult_name"], "Spell Steal")
        self.assertEqual(nested["ult_cooldown"], 12)

    def test_offline_brief_has_new_keys(self):
        brief = build_brief(None, catalog(), ITEM_NAMES, position=5)
        self.assertFalse(brief["available"])
        for key in ("item_advice", "next_item", "ult_name", "net_worth_estimated", "inventory_keys"):
            self.assertIn(key, brief)


# --------------------------------------------------------------------------- #
# 2. Нетворс: живой GSI/игрок или оценка по золоту и предметам
# --------------------------------------------------------------------------- #
class NetWorthTests(unittest.TestCase):
    def test_net_worth_estimated_when_player_gsi_has_no_net_worth(self):
        brief = build_brief(
            live_payload(), catalog(), ITEM_NAMES, position=5, item_costs=ITEM_COSTS
        )
        self.assertTrue(brief["net_worth_estimated"])
        self.assertIsNotNone(brief["net_worth"])
        # Оценка = золото + стоимость надетых предметов (как минимум Magic Wand).
        self.assertGreaterEqual(int(brief["net_worth"]), int(brief["gold"]) + 460)
        self.assertIn("NW", render_brief(brief))

    def test_player_net_worth_takes_priority(self):
        payload = live_payload()
        payload["player"]["net_worth"] = 4230
        brief = build_brief(
            payload, catalog(), ITEM_NAMES, position=5, item_costs=ITEM_COSTS
        )
        self.assertEqual(brief["net_worth"], 4230)
        self.assertFalse(brief["net_worth_estimated"])


# --------------------------------------------------------------------------- #
# 3. Обоснованный совет по закупке против Juggernaut
# --------------------------------------------------------------------------- #
class ItemAdvicePromptTests(unittest.TestCase):
    def setUp(self):
        self.brief = build_brief(
            live_payload(),
            catalog(),
            ITEM_NAMES,
            position=5,
            item_costs=ITEM_COSTS,
        )

    def test_advice_is_grounded_in_match(self):
        advice = self.brief["item_advice"]
        self.assertTrue(advice, "советы по закупкам не должны быть пустыми")
        self.assertLessEqual(len(advice), 3)
        items = [entry["item"] for entry in advice]
        self.assertTrue(
            any(
                item in ("Ghost Scepter", "Eul's Scepter of Divinity", "Aeon Disk")
                for item in items
            ),
            f"ожидался сейв от Omnislash, получено: {items}",
        )
        for entry in advice:
            self.assertTrue(str(entry.get("reason") or "").strip())
        joined = " ".join(entry["reason"] for entry in advice)
        self.assertIn("Juggernaut", joined)
        self.assertIn("Omnislash", joined)
        # Купленное не советуем повторно.
        self.assertNotIn("Magic Wand", items)
        self.assertNotIn("Tranquil Boots", items)

    def test_next_item_has_price_and_gold_shortfall(self):
        next_item = self.brief["next_item"]
        self.assertIsNotNone(next_item)
        self.assertIs(next_item, self.brief["item_advice"][0])
        self.assertEqual(next_item["item"], "Ghost Scepter")
        self.assertEqual(next_item["cost"], 1500)
        self.assertEqual(next_item["gold_needed"], 560)  # 1500 - 940 золота
        self.assertFalse(next_item["affordable"])
        self.assertIn("Juggernaut", next_item["reason"])

    def test_render_brief_contains_purchase_reason_and_enemy(self):
        text = render_brief(self.brief)
        self.assertIn("Закупка", text)
        self.assertIn("предмет", text.lower())
        self.assertIn("Ghost Scepter", text)
        self.assertIn("Juggernaut", text)
        self.assertIn("Omnislash", text)
        self.assertIn("1500", text)
        self.assertIn("не хватает 560", text)
        # Оговорка про Linken's (и BKB) из ENEMY_ITEM_NOTES тоже доходит до промпта.
        self.assertIn("Linken", text)
        self.assertIn("НЕ блокирует", text)

    def test_render_brief_ignores_empty_advice(self):
        brief = copy.deepcopy(self.brief)
        brief["item_advice"] = []
        brief["next_item"] = None
        text = render_brief(brief)
        self.assertNotIn("Закупка", text)


# --------------------------------------------------------------------------- #
# 4. Точечные подсказки _ENEMY_ITEM_HINTS (BKB не спасает и т.п.)
# --------------------------------------------------------------------------- #
class EnemyItemHintTests(unittest.TestCase):
    def test_hints_cover_hard_control_heroes(self):
        hints = dict(_ENEMY_ITEM_HINTS)
        fragments = [fragment for fragment, _ in _ENEMY_ITEM_HINTS]
        self.assertEqual(len(fragments), len(set(fragments)), "дубликаты героев в подсказках")

        juggernaut = hints["juggernaut"]
        for token in ("Ghost Scepter", "Eul's Scepter", "Aeon Disk", "Omnislash"):
            self.assertIn(token, juggernaut)
        self.assertIn("BKB", juggernaut)
        self.assertIn("Linken", juggernaut)
        self.assertIn("НЕ блокирует", juggernaut)

        legion = hints["legion_commander"]
        self.assertIn("Duel", legion)
        self.assertIn("BKB", legion)
        self.assertIn("Linken's Sphere", legion)
        self.assertIn("Aeon Disk", legion)

        self.assertIn("Doom", hints["doom_bringer"] if "doom_bringer" in hints else hints["doom"])
        self.assertIn("Fiend's Grip", hints["bane"])
        self.assertIn("Primal Roar", hints["beastmaster"])
        axe = hints["axe"]
        self.assertIn("Berserker's Call", axe)
        self.assertIn("Force Staff", axe)
        self.assertIn("BKB", axe)

    def test_coach_enemy_hints_returns_juggernaut_line(self):
        tmp = make_temp_dir()
        try:
            payload = live_payload()
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                catalog(),
                FakeGsi(payload),
                LlmConfig(model="gemma3"),
                PlayerProfile(default_position=5),
                session=FakeSession({}),
                memory_path=tmp / "coach_memory.json",
            )
            hints = coach._enemy_hints()
        finally:
            remove_temp_dir(tmp)
        juggernaut_lines = [line for line in hints if line.startswith("Juggernaut")]
        self.assertTrue(juggernaut_lines, hints)
        joined = " ".join(juggernaut_lines)
        self.assertIn("Ghost Scepter", joined)
        self.assertIn("Omnislash", joined)
        self.assertIn("Linken", joined)


# --------------------------------------------------------------------------- #
# 5. Coach: цены и советы попадают в динамический контекст (промпт LLM)
# --------------------------------------------------------------------------- #
class CoachDynamicContextTests(unittest.TestCase):
    def _make_coach(self, tmp):
        data = tmp / "data"
        data.mkdir()
        (data / "heroes.json").write_text(
            json.dumps(HEROES, ensure_ascii=False), encoding="utf-8"
        )
        items_snapshot = {slug: {"dname": name} for slug, name in ITEM_NAMES.items()}
        (data / "items.json").write_text(
            json.dumps(items_snapshot, ensure_ascii=False), encoding="utf-8"
        )
        api = DotaApi(data, session=FakeSession({}))
        api.load_snapshots()
        # Эмулируем DotaApi.item_costs() из контракта v1.2 (backend-live).
        api.item_costs = lambda: dict(ITEM_COSTS)  # type: ignore[method-assign]
        coach = Coach(
            api,
            catalog(),
            FakeGsi(live_payload()),
            LlmConfig(model="gemma3"),
            PlayerProfile(default_position=5),
            session=FakeSession({}),
            memory_path=tmp / "coach_memory.json",
        )
        return coach

    def test_dynamic_context_has_grounded_purchase_advice(self):
        tmp = make_temp_dir()
        try:
            coach = self._make_coach(tmp)
            context = coach.get_dynamic_context()
        finally:
            remove_temp_dir(tmp)
        self.assertIn("GSI Live", context)
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", context)
        self.assertIn("Закупка", context)
        self.assertIn("Juggernaut", context)
        self.assertIn("Omnislash", context)
        # Цены пришли из api.item_costs() -> совет показывает стоимость и нехватку.
        self.assertIn("1500", context)
        self.assertIn("не хватает 560", context)

    def test_dynamic_context_survives_api_without_item_costs(self):
        tmp = make_temp_dir()
        try:
            coach = self._make_coach(tmp)
            del coach.api.item_costs  # старый API без цен: бриф не должен падать
            context = coach.get_dynamic_context()
        finally:
            remove_temp_dir(tmp)
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", context)
        self.assertIn("Закупка", context)

    def test_enemy_hint_nuance_reaches_llm_system_prompt(self):
        """«BKB не спасает от Omnislash» доходит до промпта, а не только до fallback."""
        tmp = make_temp_dir()
        try:
            coach = self._make_coach(tmp)
            captured: dict = {}

            def fake_chat(prompt, system=None, **kwargs):
                captured["system"] = system
                return "Бери Ghost Scepter или Eul, а не один BKB."

            coach.chat = fake_chat  # type: ignore[method-assign]
            coach.coach_chat("зачем покупать bkb против джаггернаута")
        finally:
            remove_temp_dir(tmp)
        system = captured.get("system") or ""
        self.assertIn("ЗНАНИЯ ПО ВОПРОСУ", system)
        self.assertIn("Juggernaut", system)
        self.assertIn("BKB не спасает", system)
        self.assertIn("Omnislash", system)
        self.assertIn("НЕ блокирует", system)


if __name__ == "__main__":
    unittest.main()
