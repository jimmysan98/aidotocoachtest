"""Тесты для улучшений GSI приёма, базы данных под текущий патч (7.36/7.37) и оптимизаций.

Проверяет:
1. Высокопроизводительный deep_merge и кэширование снимков GsiServer (snapshot cache).
2. O(1) поиск героев через _HERO_FAST_INDEX и кэширование рекомендаций драфта (recommend_picks).
3. Актуальные предметы патча 7.36/7.37 (Khanda, Parasma, Disperser, Harpoon).
4. Определение логова Рошана по времени суток (Day/Night cycle) и новые нейтральные тайминги.
5. Интеграцию аспектов (Facets) и статусных эффектов (CC/Break) в контекст GSI.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from aidota.context import (
    build_gsi_context,
    neutral_tier,
    objectives_context,
    roshan_pit_location,
)
from aidota.gsi import GsiServer, deep_merge
from aidota.meta_db import (
    META_HEROES_DB,
    SITUATIONAL_COUNTER_RULES,
    TAG_COUNTER_ITEMS,
    get_hero_build,
    get_hero_counters,
    get_hero_meta_entry,
    recommend_picks,
)


class TestGsiOptimizations(unittest.TestCase):
    """Тестирование оптимизированного deep_merge и GsiServer."""

    def test_deep_merge_non_mutation_and_speed(self):
        base = {
            "hero": {"id": 8, "health": 1000},
            "items": {"slot0": {"name": "item_tango"}},
            "draft": [{"id": 1}],
        }
        update = {
            "hero": {"health": 950, "mana": 400},
            "items": {"slot0": {"name": "item_bfury"}},
            "draft": [{"id": 2}],
        }
        merged = deep_merge(base, update)

        # Проверка корректности слияния
        self.assertEqual(merged["hero"]["health"], 950)
        self.assertEqual(merged["hero"]["mana"], 400)
        self.assertEqual(merged["hero"]["id"], 8)
        self.assertEqual(merged["items"]["slot0"]["name"], "item_bfury")
        self.assertEqual(merged["draft"], [{"id": 2}])

        # Исходный base не должен мутироваться
        self.assertEqual(base["hero"]["health"], 1000)
        self.assertNotIn("mana", base["hero"])
        self.assertEqual(base["draft"], [{"id": 1}])

    def test_gsi_server_snapshot_caching(self):
        server = GsiServer("127.0.0.1", 3099, "token123")
        self.assertIsNone(server.snapshot())

        payload1 = {"hero": {"name": "npc_dota_hero_juggernaut", "level": 5}}
        server._store(payload1)

        t0 = time.perf_counter()
        snap1 = server.snapshot()
        t1 = time.perf_counter()
        snap2 = server.snapshot()
        t2 = time.perf_counter()

        self.assertIsNotNone(snap1)
        self.assertEqual(snap1["hero"]["level"], 5)
        self.assertEqual(snap2["hero"]["level"], 5)

        # Повторный вызов должен быть мгновенным (<0.5 мс)
        self.assertLess(t2 - t1, 0.005)

        # Обновление должно инвалидировать кэш
        payload2 = {"hero": {"level": 6}}
        server._store(payload2)
        snap3 = server.snapshot()
        self.assertEqual(snap3["hero"]["level"], 6)


class TestCurrentPatchDatabase(unittest.TestCase):
    """Тестирование базы данных под актуальный патч."""

    def test_items_json_contains_modern_patch_items(self):
        items_path = Path(__file__).resolve().parent.parent / "data" / "items.json"
        with open(items_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        # Канонические ключи Valve/OpenDota в data/items.json:
        # Khanda -> angels_demise, Parasma -> devastator.
        self.assertIn("angels_demise", items)
        self.assertEqual(items["angels_demise"]["dname"], "Khanda")
        self.assertEqual(items["angels_demise"]["cost"], 5600)
        self.assertIn("devastator", items)
        self.assertEqual(items["devastator"]["dname"], "Parasma")
        self.assertEqual(items["devastator"]["cost"], 5975)
        self.assertIn("harpoon", items)
        self.assertEqual(items["harpoon"]["cost"], 4700)
        self.assertIn("disperser", items)
        self.assertEqual(items["disperser"]["cost"], 6100)

    def test_meta_db_tag_counters_contain_modern_items(self):
        self.assertIn("Khanda", TAG_COUNTER_ITEMS["burst"])
        self.assertIn("Parasma", TAG_COUNTER_ITEMS["magic_burst_cc"])
        self.assertIn("Disperser", TAG_COUNTER_ITEMS["high_mobility"])
        self.assertIn("Harpoon", TAG_COUNTER_ITEMS["right_clickers"])

    def test_situational_rules_have_modern_items(self):
        burst_rule = next(r for r in SITUATIONAL_COUNTER_RULES if r["threat"] == "burst")
        self.assertIn("Khanda", burst_rule["items"])

        magic_rule = next(r for r in SITUATIONAL_COUNTER_RULES if r["threat"] == "magic_burst_cc")
        self.assertIn("Parasma", magic_rule["items"])

        mobility_rule = next(r for r in SITUATIONAL_COUNTER_RULES if r["threat"] == "high_mobility")
        self.assertIn("Disperser", mobility_rule["items"])

    def test_hero_fast_index_lookups(self):
        # 1. По каноническому ID
        entry = get_hero_meta_entry("juggernaut")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "juggernaut")

        # 2. По префиксу npc_dota_hero_
        entry_npc = get_hero_meta_entry("npc_dota_hero_antimage")
        self.assertIsNotNone(entry_npc)
        self.assertEqual(entry_npc["id"], "antimage")

        # 3. По русскому имени
        entry_ru = get_hero_meta_entry("Джаггернаут")
        self.assertIsNotNone(entry_ru)
        self.assertEqual(entry_ru["id"], "juggernaut")

        # 4. По русскому прозвищу (алиасу)
        entry_alias = get_hero_meta_entry("ам")
        self.assertIsNotNone(entry_alias)
        self.assertEqual(entry_alias["id"], "antimage")

        # 5. Новые герои
        entry_kez = get_hero_meta_entry("кез")
        self.assertIsNotNone(entry_kez)
        self.assertEqual(entry_kez["id"], "kez")

        entry_ring = get_hero_meta_entry("рингмастер")
        self.assertIsNotNone(entry_ring)
        self.assertEqual(entry_ring["id"], "ringmaster")

    def test_recommend_picks_caching_and_speed(self):
        enemies = ["sniper", "pudge"]
        allies = ["crystal_maiden"]

        t0 = time.perf_counter()
        picks1 = recommend_picks(enemies, allies, role=1, top_k=3)
        t1 = time.perf_counter()
        picks2 = recommend_picks(enemies, allies, role=1, top_k=3)
        t2 = time.perf_counter()

        self.assertEqual(len(picks1), 3)
        self.assertEqual(picks1, picks2)
        # Повторный вызов из кэша должен быть быстрее 0.1 мс
        self.assertLess(t2 - t1, 0.001)


class TestCurrentPatchContextAndTimers(unittest.TestCase):
    """Тестирование механик карты, таймингов и аспектов."""

    def test_roshan_pit_day_night_detection(self):
        # День: нижнее логово (Radiant)
        self.assertIn("нижн", roshan_pit_location(clock_seconds=100, is_daytime=True))
        self.assertIn("нижн", roshan_pit_location(clock_seconds=120))  # 2:00 -> День

        # Ночь: верхнее логово (Dire)
        self.assertIn("верхн", roshan_pit_location(clock_seconds=350, is_daytime=False))
        self.assertIn("верхн", roshan_pit_location(clock_seconds=420))  # 7:00 -> Ночь

    def test_neutral_tiers_modern_patch(self):
        # Обычный режим
        self.assertEqual(neutral_tier(5.0, False), 1)
        self.assertEqual(neutral_tier(16.0, False), 2)
        self.assertEqual(neutral_tier(26.0, False), 3)
        self.assertEqual(neutral_tier(36.0, False), 4)
        self.assertEqual(neutral_tier(65.0, False), 5)

        # Турбо
        self.assertEqual(neutral_tier(8.0, True), 2)
        self.assertEqual(neutral_tier(15.0, True), 3)
        self.assertEqual(neutral_tier(32.0, True), 5)

    def test_build_gsi_context_with_facets_and_statuses(self):
        payload = {
            "map": {
                "clock_time": 450,
                "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                "daytime": False,
                "radiant_score": 10,
                "dire_score": 8,
            },
            "player": {"kills": 2, "deaths": 1, "assists": 4, "gold": 1200},
            "hero": {
                "name": "npc_dota_hero_juggernaut",
                "level": 11,
                "facet": 1,
                "stunned": True,
                "silenced": True,
                "health_percent": 65,
                "mana_percent": 50,
            },
            "items": {"slot0": {"name": "item_phase_boots"}},
        }
        ctx = build_gsi_context(payload, catalog=None)
        self.assertIn("аспект 1", ctx)
        self.assertIn("в стане", ctx)
        self.assertIn("в сайленсе", ctx)
        self.assertIn("верхнем логове", ctx)

    def test_roshan_pit_immune_to_night_stalker_ult(self):
        """Дневной ульт Night Stalker (daytime=False при clock=120) не должен сбивать логово Рошана."""
        pit = roshan_pit_location(clock_seconds=120, is_daytime=False)
        self.assertIn("нижн", pit)

        # Fallback при отсутствии clock_seconds
        pit_fallback = roshan_pit_location(clock_seconds=None, is_daytime=False)
        self.assertIn("верхн", pit_fallback)

    def test_cc_statuses_rooted_muted_taunted_in_context_and_snapshot(self):
        """Проверка детекции эффектов контроля: rooted, muted, taunted."""
        from aidota.context import get_live_gsi_snapshot

        payload = {
            "map": {"clock_time": 200, "daytime": True},
            "hero": {
                "name": "npc_dota_hero_axe",
                "level": 6,
                "facet": 2,
                "rooted": True,
                "muted": True,
                "taunted": True,
                "alive": True,
            },
            "player": {"gold": 500},
        }
        ctx = build_gsi_context(payload, catalog=None)
        self.assertIn("в оцепенении", ctx)
        self.assertIn("предметы заглушены", ctx)
        self.assertIn("в таунте", ctx)

        snapshot = get_live_gsi_snapshot(payload)
        self.assertEqual(snapshot["facet"], 2)
        self.assertIn("в оцепенении", snapshot["statuses"])
        self.assertIn("предметы заглушены", snapshot["statuses"])
        self.assertIn("в таунте", snapshot["statuses"])
        self.assertIn("нижнем логове", snapshot["roshan_pit"])

    def test_item_costs_cleaned_up(self):
        """Проверка актуальных стоимостей предметов в обновлённом снимке data/items.json."""
        items_path = Path(__file__).resolve().parent.parent / "data" / "items.json"
        with open(items_path, "r", encoding="utf-8") as f:
            items = json.load(f)
        self.assertEqual(items["angels_demise"]["cost"], 5600)
        self.assertEqual(
            items["phylactery"]["cost"] + items["soul_booster"]["cost"],
            items["angels_demise"]["cost"],
        )
        self.assertEqual(items["devastator"]["cost"], 5975)
        self.assertEqual(items["recipe_devastator"]["cost"], 400)
        self.assertEqual(
            items["witch_blade"]["cost"]
            + items["mystic_staff"]["cost"]
            + items["recipe_devastator"]["cost"],
            items["devastator"]["cost"],
        )
        self.assertEqual(items["harpoon"]["cost"], 4700)

    def test_macro_timer_neutrals_736_timings(self):
        """Проверка таймингов нейтральных предметов 7.36/7.37 (7, 15, 25, 35, 60 мин) и Турбо."""
        from aidota.context import MidTimersTracker

        skill = MidTimersTracker()
        # 15 минут (900 сек) -> 2-й разряд
        alert_15m = skill.check_macro_timers(901, turbo=False)
        self.assertIsNotNone(alert_15m)
        self.assertIn("2-й разряд", alert_15m)

        # Турбо: 3.5 мин (210 сек) -> 1-й разряд
        skill_turbo = MidTimersTracker()
        alert_turbo = skill_turbo.check_macro_timers(211, turbo=True)
        self.assertIsNotNone(alert_turbo)
        self.assertIn("1-го разряда", alert_turbo)

    def test_aegis_in_inventory_detected(self):
        """Проверка детекции аегиса непосредственно в инвентаре игрока."""
        from aidota.context import AutonomousCoachAgent

        agent = AutonomousCoachAgent()
        payload = {
            "map": {"clock_time": 1200},
            "items": {
                "slot0": {"name": "item_aegis"},
                "slot1": {"name": "item_blink"},
            },
            "player": {"team_name": "radiant"},
        }
        res = agent._check_aegis_and_roshan(payload, catalog=None, my_team="radiant")
        self.assertIsNotNone(res)
        self.assertIn("У тебя аегис", res)


if __name__ == "__main__":
    unittest.main()
