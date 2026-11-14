"""Тесты краевых случаев live_advice и meta_db (Area 2)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from aidota.live_advice import (
    _snapshot_owned,
    build_item_advice,
    describe_item_advice,
    format_item_advice,
)
from aidota.meta_db import (
    META_HEROES_DB,
    get_hero_build,
    get_hero_counters,
    get_hero_meta_entry,
    get_hero_threats,
    get_lane_tips,
    recommend_picks,
)


class TestFullInventoryAndGoldDeficit(unittest.TestCase):
    def setUp(self):
        self.six_slot_inv = [
            "Power Treads",
            "Battle Fury",
            "Manta Style",
            "Butterfly",
            "Abyssal Blade",
            "Satanic",
        ]

    def test_full_six_slot_inventory_with_zero_gold(self):
        snapshot = {
            "available": True,
            "inventory": list(self.six_slot_inv),
            "gold": 0,
        }
        item_costs = {"Monkey King Bar": 4050}
        advice = build_item_advice(
            snapshot,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs=item_costs,
        )
        self.assertGreaterEqual(len(advice), 1)
        top = advice[0]
        self.assertEqual(top["item"], "Monkey King Bar")
        self.assertEqual(top["cost"], 4050)
        self.assertEqual(top["gold_needed"], 4050)
        self.assertFalse(top["affordable"])

        desc = describe_item_advice(top)
        self.assertIn("4050 — не хватает 4050", desc)

    def test_full_inventory_with_partial_gold(self):
        snapshot = {
            "available": True,
            "inventory": list(self.six_slot_inv),
            "gold": 1500,
        }
        item_costs = {"Monkey King Bar": 4050}
        advice = build_item_advice(
            snapshot,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs=item_costs,
        )
        top = advice[0]
        self.assertEqual(top["cost"], 4050)
        self.assertEqual(top["gold_needed"], 2550)
        self.assertFalse(top["affordable"])

        fmt = format_item_advice(advice)
        self.assertIn("4050 — не хватает 2550", fmt)

    def test_full_inventory_with_exact_and_excess_gold(self):
        item_costs = {"Monkey King Bar": 4050}

        # Exact gold
        snap_exact = {
            "available": True,
            "inventory": list(self.six_slot_inv),
            "gold": 4050,
        }
        adv_exact = build_item_advice(
            snap_exact,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs=item_costs,
        )
        top_exact = adv_exact[0]
        self.assertEqual(top_exact["gold_needed"], 0)
        self.assertTrue(top_exact["affordable"])
        self.assertIn("4050 — хватает золота", describe_item_advice(top_exact))

        # Excess gold
        snap_excess = {
            "available": True,
            "inventory": list(self.six_slot_inv),
            "gold": 7000,
        }
        adv_excess = build_item_advice(
            snap_excess,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs=item_costs,
        )
        top_excess = adv_excess[0]
        self.assertEqual(top_excess["gold_needed"], 0)
        self.assertTrue(top_excess["affordable"])
        self.assertIn("4050 — хватает золота", describe_item_advice(top_excess))

    def test_backpack_and_stash_items_filtered_from_recommendations(self):
        # When snapshot inventory includes backpack/stash items or inventory_keys
        snapshot = {
            "available": True,
            "inventory": list(self.six_slot_inv) + ["Town Portal Scroll", "Blink Dagger", "Ghost Scepter"],
            "inventory_keys": ["item_blink", "item_ghost"],
            "neutral_item": "Tier 1 Broom Handle",
            "gold": 5000,
        }
        owned = _snapshot_owned(snapshot)
        self.assertIn("blink dagger", owned)
        self.assertIn("ghost scepter", owned)

        advice = build_item_advice(
            snapshot,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin", "Ursa"],
        )
        recommended_items = [entry["item"].lower() for entry in advice]
        self.assertNotIn("blink dagger", recommended_items)
        self.assertNotIn("ghost scepter", recommended_items)

    def test_corrupted_gold_types_graceful_fallback(self):
        item_costs = {"Monkey King Bar": 4050}
        for bad_gold in (None, -500, "unreliable_gold", [1000]):
            snapshot = {
                "available": True,
                "inventory": list(self.six_slot_inv),
                "gold": bad_gold,
            }
            advice = build_item_advice(
                snapshot,
                hero_name="Anti-Mage",
                position=1,
                enemy_heroes=["Phantom Assassin"],
                item_costs=item_costs,
            )
            self.assertGreaterEqual(len(advice), 1)
            top = advice[0]
            self.assertEqual(top["cost"], 4050)
            self.assertEqual(top["gold_needed"], 4050)
            self.assertFalse(top["affordable"])

    def test_item_without_cost_in_dictionary(self):
        snapshot = {
            "available": True,
            "inventory": list(self.six_slot_inv),
            "gold": 1000,
        }
        advice = build_item_advice(
            snapshot,
            hero_name="Anti-Mage",
            position=1,
            enemy_heroes=["Phantom Assassin"],
            item_costs={},
        )
        self.assertGreaterEqual(len(advice), 1)
        top = advice[0]
        self.assertIsNone(top["cost"])
        self.assertIsNone(top["gold_needed"])
        self.assertIsNone(top["affordable"])

        fmt = format_item_advice(advice)
        self.assertNotIn("()", fmt)
        self.assertIn(top["item"], fmt)


class TestAll127HeroesReaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        heroes_path = Path(__file__).resolve().parent.parent / "data" / "heroes.json"
        with open(heroes_path, encoding="utf-8") as f:
            cls.heroes = json.load(f)

    def test_all_127_heroes_produce_valid_item_advice(self):
        self.assertEqual(len(self.heroes), 127)
        snapshot = {"available": True, "inventory": []}

        for h in self.heroes:
            adv = build_item_advice(
                snapshot,
                hero_name="Rubick",
                position=5,
                enemy_heroes=[h["localized_name"]],
            )
            self.assertGreaterEqual(
                len(adv), 1, f"Empty advice for enemy hero {h['localized_name']}"
            )
            for entry in adv:
                self.assertTrue(entry.get("item"))
                self.assertTrue(entry.get("reason"))

    def test_all_127_heroes_resolved_by_id_name_and_russian_alias(self):
        self.assertEqual(len(META_HEROES_DB), 127)
        for h in self.heroes:
            short_id = h["name"].replace("npc_dota_hero_", "")
            entry_short = get_hero_meta_entry(short_id)
            self.assertIsNotNone(entry_short, f"Failed short_id resolution for {short_id}")

            entry_npc = get_hero_meta_entry(h["name"])
            self.assertIsNotNone(entry_npc, f"Failed npc resolution for {h['name']}")
            self.assertEqual(entry_short["id"], entry_npc["id"])

            entry_ru = get_hero_meta_entry(entry_short["ru_name"])
            self.assertIsNotNone(entry_ru, f"Failed ru_name resolution for {entry_short['ru_name']}")
            self.assertEqual(entry_short["id"], entry_ru["id"])

    def test_all_127_heroes_have_valid_counters_and_lane_tips(self):
        for h in self.heroes:
            short_id = h["name"].replace("npc_dota_hero_", "")
            counters = get_hero_counters(short_id)
            self.assertIsNotNone(counters)
            self.assertGreaterEqual(len(counters.get("counter_items", [])), 1)
            self.assertGreaterEqual(len(counters.get("counter_heroes", [])), 1)

            for role in range(1, 6):
                tip = get_lane_tips(short_id, role=role)
                self.assertIsInstance(tip, str)
                self.assertGreaterEqual(
                    len(tip), 20, f"Lane tip too short for {short_id} role {role}"
                )

    def test_unknown_hero_fallback_build_generation(self):
        build = get_hero_build("nonexistent_custom_hero", role=2)
        self.assertIsInstance(build, dict)
        self.assertIn("starting", build)
        self.assertIn("early_game", build)
        self.assertIn("core", build)
        self.assertIn("situational", build)
        self.assertIn("lane_tips", build)
        self.assertEqual(build.get("hero_name"), "Nonexistent_custom_hero")


class TestMultiCarryCombinedThreats(unittest.TestCase):
    def test_multi_carry_threat_aggregation(self):
        enemies = ["Phantom Assassin", "Morphling", "Storm Spirit"]
        threats = get_hero_threats(enemies)
        self.assertEqual(len(threats), 3)

        # Check PA tags
        pa_threat = threats[0]
        self.assertEqual(pa_threat["hero_id"], "phantom_assassin")
        self.assertIn("evasion", pa_threat["tags"])
        self.assertIn("passives", pa_threat["tags"])

        # Check Morphling tags
        morph_threat = threats[1]
        self.assertEqual(morph_threat["hero_id"], "morphling")
        self.assertIn("heal_regen", morph_threat["tags"])

        # Check Storm Spirit tags
        storm_threat = threats[2]
        self.assertEqual(storm_threat["hero_id"], "storm_spirit")
        self.assertIn("high_mobility", storm_threat["tags"])

    def test_multi_carry_adaptive_situational_build(self):
        enemies = ["Phantom Assassin", "Morphling", "Bristleback", "Doom"]
        build = get_hero_build("antimage", role=1, enemy_heroes=enemies)
        situational_items = [entry["item"] for entry in build.get("situational", [])]

        self.assertIn("Monkey King Bar", situational_items)
        self.assertIn("Spirit Vessel / Eye of Skadi", situational_items)
        self.assertIn("Silver Edge", situational_items)
        self.assertIn("Linken's Sphere", situational_items)

    def test_multi_carry_draft_recommendation_scoring(self):
        enemies = ["phantom_assassin", "medusa", "storm_spirit"]
        picks = recommend_picks(enemy_heroes=enemies, role=1, top_k=5)
        self.assertGreaterEqual(len(picks), 1)

        top_pick = picks[0]
        self.assertGreater(top_pick["score"], 50)
        self.assertIsInstance(top_pick["strong_against"], list)
        self.assertGreaterEqual(len(top_pick["strong_against"]), 1)

    def test_support_role_filters_carry_items_under_multi_carry_threat(self):
        enemies = ["Phantom Assassin", "Sven"]
        snapshot = {"available": True, "inventory": []}
        advice = build_item_advice(
            snapshot,
            hero_name="Rubick",
            position=5,
            enemy_heroes=enemies,
            max_items=5,
        )
        rec_items = [entry["item"].lower() for entry in advice]

        # Hard carry items must not be recommended
        self.assertNotIn("battle fury", rec_items)
        self.assertNotIn("manta style", rec_items)
        self.assertNotIn("eye of skadi", rec_items)
        self.assertNotIn("satanic", rec_items)

        # Counter utility items allowed
        self.assertTrue(
            any(name in rec_items for name in ("heaven's halberd", "ghost scepter", "monkey king bar", "silver edge"))
        )


if __name__ == "__main__":
    unittest.main()
