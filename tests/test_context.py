"""Тесты построения контекста из GSI."""

from __future__ import annotations

import unittest

from aidota.context import (
    analyze_player_mistakes,
    build_gsi_context,
    collect_draft,
    current_hero_npc,
    format_clock,
    game_mode_label,
    get_game_mode,
    gsi_stale_hint,
    is_turbo,
    item_name,
    neutral_tier,
    objectives_context,
    turbo_factor,
)
from tests.helpers import catalog, gsi_payload

ITEMS = {"power_treads": "Power Treads", "bfury": "Battle Fury", "manta": "Manta Style"}


class ClockTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(format_clock(754), "12:34")

    def test_negative(self):
        self.assertEqual(format_clock(-65), "-1:05")

    def test_unknown(self):
        self.assertEqual(format_clock(None), "?")


class ItemNameTests(unittest.TestCase):
    def test_known(self):
        self.assertEqual(item_name("item_bfury", ITEMS), "Battle Fury")

    def test_unknown(self):
        self.assertEqual(item_name("item_smoke_of_deceit", ITEMS), "Smoke Of Deceit")

    def test_empty(self):
        self.assertEqual(item_name(None, ITEMS), "?")


class GsiContextTests(unittest.TestCase):
    def setUp(self):
        self.payload = gsi_payload()
        self.catalog = catalog()

    def test_full_context(self):
        text = build_gsi_context(self.payload, self.catalog, ITEMS, age_seconds=2)
        self.assertIn("Anti-Mage", text)
        self.assertIn("12:34", text)
        self.assertIn("Radiant 5 : Dire 3", text)
        self.assertIn("Power Treads", text)
        self.assertIn("Battle Fury", text)
        self.assertIn("Пики вашей команды", text)
        self.assertIn("Пики противника", text)
        self.assertIn("Баны", text)
        self.assertIn("ур. 12", text)

    def test_stale_warning(self):
        text = build_gsi_context(self.payload, self.catalog, ITEMS, age_seconds=999)
        self.assertIn("устарели", text)

    def test_empty_payload(self):
        text = build_gsi_context(None, self.catalog, ITEMS)
        self.assertIn("GSI не получает данных", text)
        self.assertIn("gamestateintegration", text)

    def test_malformed_payload_does_not_crash(self):
        text = build_gsi_context({"map": "oops", "hero": [1, 2], "player": 5}, self.catalog, ITEMS)
        self.assertIsInstance(text, str)

    def test_draft_collection(self):
        draft = collect_draft(self.payload, self.catalog)
        self.assertEqual(draft.my_team, "radiant")
        self.assertEqual(draft.my_picks, ["Anti-Mage", "Pudge", "Windranger"])
        self.assertEqual(draft.enemy_picks, ["Shadow Fiend", "Razor"])
        self.assertEqual(draft.bans, ["Pudge", "Shadow Fiend"])

    def test_unknown_hero_ids_are_shown_as_numbers(self):
        payload = {"player": {"team_name": "radiant"},
                   "draft": {"team2": {"pick_ids": [1, 999]}, "team3": {"pick_ids": [2]}}}
        draft = collect_draft(payload, self.catalog)
        self.assertEqual(draft.my_picks, ["Anti-Mage", "999"])
        self.assertEqual(draft.enemy_picks, ["Pudge"])

    def test_draft_empty(self):
        draft = collect_draft({}, self.catalog)
        self.assertTrue(draft.empty)

    def test_current_hero_npc(self):
        self.assertEqual(current_hero_npc(self.payload), "npc_dota_hero_antimage")
        self.assertIsNone(current_hero_npc(None))

    def test_stale_hint(self):
        self.assertIn("cfg", gsi_stale_hint(None))
        self.assertIn("закрыта", gsi_stale_hint(120))
        self.assertIsNone(gsi_stale_hint(2))


class GameModeTests(unittest.TestCase):
    def test_is_turbo(self):
        self.assertTrue(is_turbo(23))
        self.assertFalse(is_turbo(1))
        self.assertTrue(is_turbo("turbo"))

    def test_turbo_factor(self):
        self.assertEqual(turbo_factor(23), 1.6)
        self.assertEqual(turbo_factor(1), 1.0)

    def test_game_mode_label(self):
        self.assertEqual(game_mode_label(23), "Turbo")
        self.assertEqual(game_mode_label(1), "All Pick")
        self.assertEqual(game_mode_label(None), "неизвестен")

    def test_get_game_mode_from_map(self):
        self.assertEqual(get_game_mode({"map": {"game_mode": 23}}), 23)
        self.assertEqual(get_game_mode({"map": {"customgamename": "turbo"}}), 23)
        self.assertEqual(get_game_mode({"map": {"customgamename": "All Pick"}}), 1)
        self.assertIsNone(get_game_mode({}))

    def test_objectives_contain_timings(self):
        text = objectives_context(720, 1)  # 12:00
        self.assertIn("руны мудрости", text)
        self.assertIn("Терзатель", text)
        self.assertIn("Рошан", text)

    def test_neutral_tier(self):
        self.assertEqual(neutral_tier(10, False), 1)
        self.assertEqual(neutral_tier(20, False), 2)
        self.assertEqual(neutral_tier(30, False), 3)
        self.assertEqual(neutral_tier(40, False), 4)
        self.assertEqual(neutral_tier(8, True), 2)


class MistakeDetectorTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "map": {"clock_time": 600, "game_mode": 1},
            "player": {
                "gpm": 250, "xpm": 300, "last_hits": 20, "denies": 2,
                "deaths": 4, "gold": 3000, "team_name": "radiant",
            },
            "hero": {
                "name": "npc_dota_hero_antimage", "level": 9, "alive": True,
                "items": {
                    "slot0": {"name": "item_power_treads"},
                    "neutral0": {"name": "item_empty"},
                },
            },
        }
        payload.update(overrides)
        return payload

    def test_core_low_farm(self):
        mistakes = analyze_player_mistakes(self._payload(), 1, 1)
        text = "\n".join(mistakes)
        self.assertIn("GPM", text)
        self.assertIn("добиваний", text)

    def test_feeding_detected(self):
        payload = self._payload()
        payload["map"]["clock_time"] = 540  # 9:00 — до 10-й минуты лимит смертей 3
        mistakes = analyze_player_mistakes(payload, 1, 1)
        text = "\n".join(mistakes)
        self.assertIn("смертей", text)

    def test_no_tp_detected(self):
        mistakes = analyze_player_mistakes(self._payload(), 1, 1)
        text = "\n".join(mistakes)
        self.assertIn("телепортаци", text)

    def test_empty_neutral_detected(self):
        mistakes = analyze_player_mistakes(self._payload(), 1, 1)
        text = "\n".join(mistakes)
        self.assertIn("Нейтральный слот пуст", text)

    def test_gold_hoarding_detected(self):
        mistakes = analyze_player_mistakes(self._payload(), 1, 1)
        text = "\n".join(mistakes)
        self.assertIn("незатраченным", text)

    def test_turbo_raises_farm_bar(self):
        # 8 минут Turbo: цель выше, поэтому даже 400 GPM считаются низкими.
        payload = self._payload()
        payload["map"]["clock_time"] = 480
        payload["map"]["game_mode"] = 23
        payload["player"]["gpm"] = 400
        payload["player"]["deaths"] = 1
        payload["player"]["gold"] = 500
        mistakes = analyze_player_mistakes(payload, 1, 23)
        text = "\n".join(mistakes)
        self.assertIn("Turbo", text)
        self.assertIn("GPM", text)

    def test_no_mistakes_for_good_game(self):
        payload = self._payload()
        payload["player"].update({"gpm": 700, "last_hits": 60, "deaths": 0, "gold": 800})
        payload["hero"]["items"] = {
            "slot0": {"name": "item_power_treads"},
            "slot1": {"name": "item_tpscroll"},
            "neutral0": {"name": "item_occult_bracelet"},
        }
        mistakes = analyze_player_mistakes(payload, 1, 1)
        self.assertEqual(mistakes, [])

    def test_support_not_flagged_for_farm(self):
        mistakes = analyze_player_mistakes(self._payload(), 5, 1)
        text = "\n".join(mistakes)
        self.assertNotIn("GPM", text)


if __name__ == "__main__":
    unittest.main()
