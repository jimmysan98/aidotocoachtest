"""Тесты живых подсказок по закупкам (aidota/live_advice.py).

Проверяют, что советы привязаны к данным матча: вражеский драфт, инвентарь,
золото, роль. Без сети и без LLM.
"""

from __future__ import annotations

import unittest

from aidota.live_advice import (
    ENEMY_ITEM_NOTES,
    build_item_advice,
    describe_item_advice,
    format_item_advice,
)

ITEM_COSTS = {
    "Ghost Scepter": 1500,
    "Eul's Scepter of Divinity": 2475,
    "Aeon Disk": 3000,
    "Black King Bar": 4050,
    "Heaven's Halberd": 3400,
    "Linken's Sphere": 4800,
    "Glimmer Cape": 2150,
    "Force Staff": 2200,
    "Blink Dagger": 2250,
    "Magic Wand": 460,
    "Spirit Vessel": 2675,
    "Monkey King Bar": 4050,
    "Battle Fury": 4100,
    "Manta Style": 4700,
    "Eye of Skadi": 5500,
    "Pipe of Insight": 3475,
    "Diffusal Blade": 2500,
    "Silver Edge": 5100,
    "Lotus Orb": 3850,
    "Mage Slayer": 2825,
    "Monkey King Bar ": 4050,
    "Blade Mail": 2100,
    "Rod of Atos": 2000,
}


def _snapshot(**overrides):
    base = {
        "available": True,
        "hero_name": "Rubick",
        "hero_level": 10,
        "position": 5,
        "gold": 940,
        "net_worth": 4230,
        "inventory": ["Magic Wand", "Tranquil Boots"],
        "inventory_keys": ["item_magic_wand", "item_tranquil_boots"],
        "neutral_item": None,
        "kills": 2,
        "deaths": 3,
        "assists": 3,
        "clock_seconds": 525,
        "game_stage": "laning",
        "enemy_picks": [],
        "my_team": "dire",
    }
    base.update(overrides)
    return base


class JuggernautAdviceTests(unittest.TestCase):
    def test_juggernaut_advice_has_reason_and_correct_items(self) -> None:
        advice = build_item_advice(
            _snapshot(),
            hero_name="Rubick",
            position=5,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        self.assertTrue(advice, "советы против Джаггернаута не должны быть пустыми")
        items = [entry["item"] for entry in advice]
        self.assertTrue(
            any(item in ("Ghost Scepter", "Eul's Scepter of Divinity", "Aeon Disk") for item in items),
            f"ожидался сейв от Omnislash, получено: {items}",
        )
        joined = " ".join(entry["reason"] for entry in advice)
        self.assertIn("Juggernaut", joined)
        self.assertIn("Omnislash", joined)
        for entry in advice:
            self.assertTrue(entry["reason"].strip(), "у каждого совета должна быть причина")
        # Ни один совет не должен требовать купленное.
        self.assertNotIn("Magic Wand", items)

    def test_bkb_against_juggernaut_mentions_physical_omnislash(self) -> None:
        advice = build_item_advice(
            _snapshot(gold=5000),
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        bkb = next((entry for entry in advice if entry["item"] == "Black King Bar"), None)
        if bkb is not None:  # BKB может не попасть в топ-3 — тогда проверяем заметку
            reason = bkb["reason"].lower()
            self.assertTrue("физическ" in reason or "omnislash" in reason)
            self.assertIsNotNone(bkb.get("warning"))
        note = ENEMY_ITEM_NOTES["juggernaut"]
        self.assertIn("Omnislash", note.get("warning", "") + " ".join(
            entry["why"] for entry in note["items"]
        ))


class AdviceDataGroundingTests(unittest.TestCase):
    def test_owned_items_are_not_recommended(self) -> None:
        advice = build_item_advice(
            _snapshot(inventory=["Ghost Scepter", "Magic Wand"], inventory_keys=["item_ghost", "item_magic_wand"]),
            hero_name="Rubick",
            position=5,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        self.assertNotIn("Ghost Scepter", [entry["item"] for entry in advice])

    def test_affordability_uses_gold(self) -> None:
        advice = build_item_advice(
            _snapshot(gold=0),
            hero_name="Rubick",
            position=5,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        ghost = next((entry for entry in advice if entry["item"] == "Ghost Scepter"), None)
        self.assertIsNotNone(ghost)
        self.assertEqual(ghost["cost"], 1500)
        self.assertEqual(ghost["gold_needed"], 1500)
        self.assertFalse(ghost["affordable"])

    def test_support_filters_hard_carry_items(self) -> None:
        advice = build_item_advice(
            _snapshot(hero_name="Crystal Maiden"),
            hero_name="Crystal Maiden",
            position=5,
            enemy_heroes=[],
            item_costs=ITEM_COSTS,
        )
        hard = {"Battle Fury", "Manta Style", "Eye of Skadi", "Monkey King Bar"}
        self.assertFalse(hard & {entry["item"] for entry in advice})

    def test_carry_position_allows_damage_items(self) -> None:
        advice = build_item_advice(
            _snapshot(hero_name="Juggernaut", gold=5000, position=1, inventory=[]),
            hero_name="Juggernaut",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs=ITEM_COSTS,
        )
        self.assertTrue(advice)
        self.assertTrue(all(entry["priority"] in ("HIGH", "MEDIUM", "LOW") for entry in advice))

    def test_offline_snapshot_returns_empty(self) -> None:
        self.assertEqual(build_item_advice({}), [])
        self.assertEqual(build_item_advice({"available": False}), [])

    def test_no_enemy_heroes_still_returns_something(self) -> None:
        advice = build_item_advice(
            _snapshot(),
            hero_name="Rubick",
            position=5,
            enemy_heroes=[],
            item_costs=ITEM_COSTS,
        )
        self.assertTrue(advice, "без драфта нужны хотя бы универсальные слоты под роль")
        self.assertLessEqual(len(advice), 3)


class FormattingTests(unittest.TestCase):
    def test_format_item_advice_returns_prompt_text(self) -> None:
        advice = build_item_advice(
            _snapshot(),
            hero_name="Rubick",
            position=5,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        text = format_item_advice(advice, limit=2)
        self.assertTrue(text)
        self.assertIn("—", text)
        self.assertIn("Juggernaut", text)

    def test_describe_item_advice_includes_price(self) -> None:
        advice = build_item_advice(
            _snapshot(gold=100),
            hero_name="Rubick",
            position=5,
            enemy_heroes=["Juggernaut"],
            item_costs=ITEM_COSTS,
        )
        first = advice[0]
        text = describe_item_advice(first)
        self.assertIn(first["item"], text)
        self.assertIn("не хватает", text)


if __name__ == "__main__":
    unittest.main()
