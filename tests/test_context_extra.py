"""Дополнительные тесты контекста GSI (контракт B из docs/COACH_UPGRADE.md).

Покрывают:
- B1: новые ключи ``get_live_gsi_snapshot`` (нейтралка, аганим/шард, байбек,
    ТП, составы команд, угрозы, цели) и обратную совместимость старых ключей.
- B2: ``analyze_enemy_threats`` на реальных драфтах и мусорном payload.
- B3: детектор ошибок (TP с 5-й минуты, позднее золото) без дублей.
- B4: ``AutonomousCoachAgent`` — роль/герой в совете по смертям, паверспайк
    врага, аегис/Рошан, защита от повторов советов.

Сетевых вызовов нет: только локальные payload'ы и герои.
"""

from __future__ import annotations

import unittest

from aidota.context import (
    AutonomousCoachAgent,
    analyze_enemy_threats,
    analyze_player_mistakes,
    estimate_net_worth,
    extract_own_abilities,
    extract_own_items,
    format_gsi_summary,
    get_live_gsi_snapshot,
    is_empty_item_name,
)
from aidota.heroes import HeroCatalog
from tests.helpers import catalog, gsi_payload

ITEMS = {
    "power_treads": "Power Treads",
    "bfury": "Battle Fury",
    "manta": "Manta Style",
    "tpscroll": "Town Portal Scroll",
    "occult_bracelet": "Occult Bracelet",
    "blink": "Blink Dagger",
    "black_king_bar": "BKB",
    "sheepstick": "Scythe of Vyse",
}

# Каталог с героями, на которых проверяем радар угроз.
THREAT_HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
    {"id": 10, "name": "npc_dota_hero_huskar", "localized_name": "Huskar"},
    {"id": 20, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin"},
    {"id": 30, "name": "npc_dota_hero_doom_bringer", "localized_name": "Doom"},
]


def threat_catalog() -> HeroCatalog:
    return HeroCatalog(THREAT_HEROES)


def threat_payload(enemy_ids=(10, 20, 30), clock: int = 850) -> dict:
    return {
        "player": {"team_name": "radiant"},
        "map": {"clock_time": clock, "game_mode": 1},
        "draft": {"team2": {"pick_ids": [1]}, "team3": {"pick_ids": list(enemy_ids)}},
    }


def full_payload() -> dict:
    payload = gsi_payload()
    payload["allplayers"] = {
        "player0": {
            "name": "Player",
            "team_name": "radiant",
            "hero": {
                "name": "npc_dota_hero_antimage",
                "level": 12,
                "kills": 4,
                "deaths": 1,
                "assists": 2,
                "alive": True,
                "respawn_seconds": 0,
                "net_worth": 9000,
                "items": {
                    "slot0": {"name": "item_power_treads"},
                    "slot1": {"name": "item_bfury"},
                },
            },
        },
        "player5": {
            "name": "Enemy",
            "team_name": "dire",
            "hero": {
                "name": "npc_dota_hero_phantom_assassin",
                "level": 11,
                "kills": 3,
                "deaths": 2,
                "assists": 1,
                "alive": False,
                "respawn_seconds": 20,
                "net_worth": 8000,
                "items": {
                    "slot0": {"name": "item_blink"},
                    "slot1": {"name": "item_black_king_bar"},
                },
            },
        },
    }
    return payload


def mistake_payload(
    clock: int,
    *,
    gold: int = 500,
    gpm: int = 600,
    last_hits: int = 80,
    deaths: int = 0,
    items: dict | None = None,
    alive: bool = True,
) -> dict:
    if items is None:
        items = {
            "slot0": {"name": "item_power_treads"},
            "slot1": {"name": "item_tpscroll"},
            "neutral0": {"name": "item_occult_bracelet"},
        }
    return {
        "map": {"clock_time": clock, "game_mode": 1},
        "player": {
            "gpm": gpm,
            "xpm": 600,
            "last_hits": last_hits,
            "deaths": deaths,
            "gold": gold,
            "team_name": "radiant",
        },
        "hero": {
            "name": "npc_dota_hero_antimage",
            "level": 10,
            "alive": alive,
            "items": items,
        },
    }


class SnapshotNewKeysTests(unittest.TestCase):
    def test_full_snapshot_has_new_keys(self) -> None:
        snapshot = get_live_gsi_snapshot(gsi_payload(), catalog(), ITEMS, position=2)
        self.assertTrue(snapshot["available"])
        for key in (
            "hero_items_display",
            "neutral_item",
            "aghanims_scepter",
            "aghanims_shard",
            "buyback_cost",
            "buyback_available",
            "has_tp",
            "my_team_players",
            "enemy_players",
            "enemy_threats",
            "objectives",
        ):
            self.assertIn(key, snapshot)
        # Старый ключ inventory сохраняет прежнюю семантику (только слоты).
        self.assertEqual(
            snapshot["inventory"], ["Power Treads", "Battle Fury", "Manta Style"]
        )
        self.assertEqual(snapshot["hero_items_display"], snapshot["inventory"])
        self.assertEqual(snapshot["neutral_item"], "Occult Bracelet")
        self.assertFalse(snapshot["aghanims_scepter"])
        self.assertFalse(snapshot["aghanims_shard"])
        self.assertEqual(snapshot["buyback_cost"], 300)
        self.assertTrue(snapshot["buyback_available"])
        self.assertFalse(snapshot["has_tp"])
        self.assertTrue(snapshot["objectives"].startswith("Ближайшие цели"))
        self.assertIsInstance(snapshot["enemy_threats"], list)
        self.assertTrue(snapshot["enemy_threats"])
        # Без allplayers собственный герой всё равно попадает в состав команды.
        self.assertEqual(len(snapshot["my_team_players"]), 1)
        self.assertEqual(snapshot["my_team_players"][0]["level"], 12)
        self.assertEqual(snapshot["enemy_players"], [])

    def test_offline_snapshot_has_new_keys(self) -> None:
        snapshot = get_live_gsi_snapshot(None, catalog(), ITEMS)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["game_stage"], "offline")
        for key in (
            "hero_items_display",
            "neutral_item",
            "aghanims_scepter",
            "aghanims_shard",
            "buyback_cost",
            "buyback_available",
            "has_tp",
            "my_team_players",
            "enemy_players",
            "enemy_threats",
        ):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["hero_items_display"], [])
        self.assertIsNone(snapshot["neutral_item"])
        self.assertIsNone(snapshot["buyback_cost"])
        self.assertFalse(snapshot["has_tp"])
        self.assertEqual(snapshot["enemy_threats"], [])
        self.assertEqual(snapshot["objectives"], "")

    def test_old_keys_and_summary_format_preserved(self) -> None:
        snapshot = get_live_gsi_snapshot(gsi_payload(), catalog(), ITEMS, position=1)
        for key in (
            "available",
            "clock_seconds",
            "clock_formatted",
            "game_stage",
            "radiant_score",
            "dire_score",
            "my_team",
            "hero_name",
            "hero_level",
            "alive",
            "health_percent",
            "mana_percent",
            "kills",
            "deaths",
            "assists",
            "gold",
            "net_worth",
            "gpm",
            "xpm",
            "last_hits",
            "inventory",
            "allied_picks",
            "enemy_picks",
            "position",
        ):
            self.assertIn(key, snapshot)
        summary = format_gsi_summary(snapshot)
        self.assertIn("GSI Снимок", summary)
        self.assertIn("12:34", summary)
        self.assertIn("KDA 4/1/2", summary)
        self.assertIn("Anti-Mage", summary)
        self.assertIn("Power Treads", summary)
        offline = format_gsi_summary(get_live_gsi_snapshot(None))
        self.assertEqual(offline, "[GSI: матч не запущен или нет соединения]")

    def test_team_players_parsed_from_allplayers(self) -> None:
        snapshot = get_live_gsi_snapshot(full_payload(), catalog(), ITEMS)
        self.assertEqual(len(snapshot["my_team_players"]), 1)
        ally = snapshot["my_team_players"][0]
        self.assertEqual(ally["name"], "Player")
        self.assertEqual(ally["level"], 12)
        self.assertEqual((ally["kills"], ally["deaths"], ally["assists"]), (4, 1, 2))
        self.assertTrue(ally["alive"])
        self.assertEqual(ally["respawn_seconds"], 0)
        self.assertEqual(ally["net_worth"], 9000)
        self.assertEqual(ally["items"], ["Power Treads", "Battle Fury"])

        self.assertEqual(len(snapshot["enemy_players"]), 1)
        enemy = snapshot["enemy_players"][0]
        self.assertEqual(enemy["name"], "Enemy")
        self.assertFalse(enemy["alive"])
        self.assertEqual(enemy["respawn_seconds"], 20)
        self.assertEqual(enemy["items"], ["Blink Dagger", "BKB"])
        self.assertTrue(snapshot["enemy_threats"])

    def test_garbage_payloads_do_not_crash(self) -> None:
        garbage_payloads = [
            None,
            "junk",
            [1, 2, 3],
            {},
            {"map": "oops", "hero": [1, 2], "player": 5},
            {"hero": {"items": {"slot0": 5, "x": 1}}, "map": {"clock_time": "abc"}},
            {"draft": "junk", "allplayers": "junk", "players": [1, 2]},
            {"allplayers": {"player0": {"hero": "junk", "items": [1, 2]}}},
        ]
        for payload in garbage_payloads:
            with self.subTest(payload=repr(payload)):
                snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
                self.assertIsInstance(snapshot, dict)
                self.assertIn("enemy_threats", snapshot)
                self.assertIn("my_team_players", snapshot)
                # summary тоже не должен падать
                self.assertIsInstance(format_gsi_summary(snapshot), str)

    def test_has_tp_and_buyback_flags(self) -> None:
        payload = gsi_payload()
        payload["hero"]["items"]["slot3"] = {"name": "item_tpscroll"}
        payload["hero"]["buyback_cooldown"] = 60
        snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
        self.assertTrue(snapshot["has_tp"])
        self.assertFalse(snapshot["buyback_available"])

        payload["hero"]["buyback_cooldown"] = 0
        payload["player"]["gold"] = 100
        snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
        self.assertFalse(snapshot["buyback_available"])


class TopLevelGsiSectionsTests(unittest.TestCase):
    """Контракт v1.2: реальный GSI кладёт items/abilities на верхний уровень."""

    def test_snapshot_uses_top_level_items_and_abilities(self) -> None:
        payload = {
            "map": {"clock_time": 600, "game_mode": 1},
            "player": {"team_name": "radiant", "gold": 100},
            "hero": {"name": "npc_dota_hero_antimage", "level": 9, "alive": True},
            "items": {
                "slot0": {"name": "item_power_treads"},
                "slot1": {"name": "item_empty"},
                "teleport0": {"name": "item_tpscroll"},
                "neutral0": {"name": "item_occult_bracelet"},
            },
            "abilities": {
                "ability0": {"name": "antimage_mana_break", "ultimate": False},
                "ability3": {
                    "name": "antimage_mana_void",
                    "ultimate": True,
                    "can_cast": True,
                    "cooldown": 30,
                },
            },
        }
        snapshot = get_live_gsi_snapshot(
            payload, catalog(), ITEMS, item_costs={"Power Treads": 1400}
        )
        self.assertEqual(snapshot["inventory"], ["Power Treads"])
        self.assertEqual(snapshot["inventory_keys"], ["item_power_treads"])
        self.assertTrue(snapshot["has_tp"])
        self.assertEqual(snapshot["neutral_item"], "Occult Bracelet")
        self.assertEqual(snapshot["net_worth"], 100 + 1400)
        self.assertTrue(snapshot["net_worth_estimated"])
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_power_treads")
        self.assertEqual(extract_own_abilities(payload)["ability3"]["cooldown"], 30)

    def test_empty_item_names_are_ignored(self) -> None:
        for empty_name in ("empty", "item_empty", "", None):
            with self.subTest(empty_name=repr(empty_name)):
                payload = {
                    "map": {"clock_time": 600},
                    "hero": {"name": "npc_dota_hero_antimage"},
                    "items": {
                        "slot0": {"name": empty_name},
                        "neutral0": {"name": empty_name},
                    },
                }
                snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
                self.assertEqual(snapshot["inventory"], [])
                self.assertEqual(snapshot["inventory_keys"], [])
                self.assertIsNone(snapshot["neutral_item"])
                self.assertFalse(snapshot["has_tp"])
                self.assertTrue(is_empty_item_name(empty_name))

    def test_estimate_net_worth_prefers_gsi_value(self) -> None:
        payload = {
            "player": {"gold": 500, "net_worth": 8000},
            "hero": {"items": {"slot0": {"name": "item_power_treads"}}},
        }
        self.assertEqual(
            estimate_net_worth(payload, {"Power Treads": 1400}, items=ITEMS),
            (8000, False),
        )
        payload["player"]["net_worth"] = 0
        self.assertEqual(
            estimate_net_worth(payload, {"Power Treads": 1400}, items=ITEMS),
            (1900, True),
        ), "оценка = золото + цена предметов в слотах"


class EnemyThreatsTests(unittest.TestCase):
    def test_huskar_pa_doom_threats(self) -> None:
        threats = analyze_enemy_threats(threat_payload(), threat_catalog(), ITEMS)
        self.assertEqual(len(threats), 3)
        text = " ".join(threats)
        self.assertIn("Huskar", text)
        self.assertIn("реген", text)
        self.assertIn("Spirit Vessel", text)
        self.assertIn("Phantom Assassin", text)
        self.assertIn("уворот", text)
        self.assertIn("Monkey King Bar", text)
        self.assertIn("Doom", text)
        self.assertIn("Linken", text)
        self.assertIn("Lotus", text)

    def test_no_enemies_returns_empty(self) -> None:
        self.assertEqual(analyze_enemy_threats({}, catalog(), ITEMS), [])
        self.assertEqual(analyze_enemy_threats(None, catalog(), ITEMS), [])
        self.assertEqual(analyze_enemy_threats("junk", catalog(), ITEMS), [])
        self.assertEqual(analyze_enemy_threats([], catalog(), ITEMS), [])
        # Только свои пики, врагов нет.
        self.assertEqual(
            analyze_enemy_threats(threat_payload(enemy_ids=()), threat_catalog(), ITEMS), []
        )

    def test_duplicate_heroes_do_not_duplicate_phrases(self) -> None:
        threats = analyze_enemy_threats(
            threat_payload(enemy_ids=(10, 10, 20, 20, 30)), threat_catalog(), ITEMS
        )
        self.assertEqual(len(threats), 3)
        self.assertEqual(sum("Huskar" in t for t in threats), 1)
        self.assertEqual(sum("Phantom Assassin" in t for t in threats), 1)
        self.assertLessEqual(len(threats), 5)

    def test_threats_from_allplayers_without_draft(self) -> None:
        payload = {
            "player": {"team_name": "radiant"},
            "map": {"clock_time": 850},
            "allplayers": {
                "player0": {
                    "name": "Ally",
                    "team_name": "radiant",
                    "hero": {"name": "npc_dota_hero_antimage", "level": 12},
                },
                "player5": {
                    "name": "Enemy",
                    "team_name": "dire",
                    "hero": {"name": "npc_dota_hero_huskar", "level": 15},
                },
            },
        }
        threats = analyze_enemy_threats(payload, threat_catalog(), ITEMS)
        self.assertTrue(any("Huskar" in t for t in threats))
        self.assertLessEqual(len(threats), 5)

    def test_visible_enemy_items_add_warning(self) -> None:
        payload = {
            "player": {"team_name": "radiant"},
            "map": {"clock_time": 850},
            "allplayers": {
                "player5": {
                    "name": "Enemy",
                    "team_name": "dire",
                    "items": {
                        "slot0": {"name": "item_blink"},
                        "slot1": {"name": "item_black_king_bar"},
                    },
                }
            },
        }
        threats = analyze_enemy_threats(payload, catalog(), ITEMS)
        self.assertEqual(len(threats), 1)
        self.assertIn("BKB", threats[0])
        self.assertIn("Blink Dagger", threats[0])

    def test_snapshot_enemy_threats_uses_same_analyzer(self) -> None:
        payload = full_payload()
        snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
        expected = analyze_enemy_threats(payload, catalog(), ITEMS)
        self.assertEqual(snapshot["enemy_threats"], expected)
        self.assertLessEqual(len(snapshot["enemy_threats"]), 5)


class MistakeDetectorExtraTests(unittest.TestCase):
    def test_no_tp_after_five_minutes_single_message(self) -> None:
        items = {
            "slot0": {"name": "item_power_treads"},
            "neutral0": {"name": "item_occult_bracelet"},
        }
        mistakes = analyze_player_mistakes(mistake_payload(300, items=items), 1, 1)
        tp_messages = [m for m in mistakes if "телепортаци" in m]
        self.assertEqual(len(tp_messages), 1)
        self.assertIn("5-й минуты", tp_messages[0])

    def test_no_tp_not_flagged_before_five_minutes(self) -> None:
        items = {"slot0": {"name": "item_power_treads"}}
        mistakes = analyze_player_mistakes(mistake_payload(240, items=items), 1, 1)
        self.assertFalse(any("телепортаци" in m for m in mistakes))

    def test_late_game_gold_hoarding(self) -> None:
        mistakes = analyze_player_mistakes(
            mistake_payload(1800, gold=3500, last_hits=250), 1, 1
        )
        gold_messages = [m for m in mistakes if "золот" in m]
        self.assertEqual(len(gold_messages), 1)
        self.assertIn("лейте", gold_messages[0])
        self.assertIn("байбек", gold_messages[0])

    def test_late_game_gold_below_threshold_not_flagged(self) -> None:
        mistakes = analyze_player_mistakes(
            mistake_payload(1800, gold=2500, last_hits=250), 1, 1
        )
        self.assertFalse(any("золот" in m for m in mistakes))

    def test_early_hoarding_still_reported_once(self) -> None:
        mistakes = analyze_player_mistakes(
            mistake_payload(600, gold=3000, last_hits=120), 1, 1
        )
        gold_messages = [m for m in mistakes if "золот" in m]
        self.assertEqual(len(gold_messages), 1)
        self.assertIn("незатраченным", gold_messages[0])

    def test_clean_game_has_no_mistakes(self) -> None:
        mistakes = analyze_player_mistakes(
            mistake_payload(600, gold=500, last_hits=120, gpm=700), 1, 1
        )
        self.assertEqual(mistakes, [])


class AutonomousAgentExtraTests(unittest.TestCase):
    def _death_tick(
        self,
        agent: AutonomousCoachAgent,
        clock: int,
        deaths: int,
        *,
        hero: str | None = None,
        position: int = 1,
    ) -> str | None:
        payload: dict = {
            "map": {"clock_time": clock},
            "player": {"deaths": deaths, "team_name": "radiant"},
        }
        if hero:
            payload["hero"] = {"name": hero}
        return agent.process_tick(payload, catalog(), ITEMS, position=position)

    def test_death_streak_advice_uses_core_role_and_hero(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        self.assertIsNone(self._death_tick(agent, 830, 0))
        advice = self._death_tick(
            agent, 850, 2, hero="npc_dota_hero_antimage", position=1
        )
        self.assertIsNotNone(advice)
        self.assertIn("Anti-Mage", advice)
        self.assertIn("Не тильтуй", advice)
        self.assertTrue("линию" in advice.lower() or "лес" in advice.lower())

    def test_death_streak_advice_uses_support_role(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        self.assertIsNone(self._death_tick(agent, 835, 0, position=5))
        advice = self._death_tick(agent, 855, 2, position=5)
        self.assertIsNotNone(advice)
        self.assertIn("вард", advice.lower())

    def test_same_advice_not_repeated_within_ttl(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        self.assertIsNone(self._death_tick(agent, 830, 0))
        first = self._death_tick(agent, 850, 2)
        self.assertIsNotNone(first)
        self.assertIn("Не тильтуй", first)
        # Тот же самый текст должен быть подавлен кэшем 3–5 минут.
        self.assertIsNone(self._death_tick(agent, 870, 4))
        # После снятия TTL совет снова разрешён.
        agent._advice_ttl_seconds = 0.0
        again = self._death_tick(agent, 910, 6)
        self.assertIsNotNone(again)
        self.assertIn("Не тильтуй", again)

    def test_reset_clears_advice_cache(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        self.assertIsNone(self._death_tick(agent, 830, 0))
        self.assertIsNotNone(self._death_tick(agent, 850, 2))
        self.assertIsNone(self._death_tick(agent, 870, 4))
        agent.reset()
        self.assertIsNone(self._death_tick(agent, 830, 0))
        self.assertIsNotNone(self._death_tick(agent, 850, 2))

    def test_enemy_power_spike_detected_once(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        hero_catalog = threat_catalog()

        def payload_with(enemy_items: dict) -> dict:
            return {
                "map": {"clock_time": 850},
                "player": {"team_name": "radiant"},
                "allplayers": {
                    "player0": {
                        "name": "Me",
                        "team_name": "radiant",
                        "hero": {
                            "name": "npc_dota_hero_antimage",
                            "items": {"slot0": {"name": "item_power_treads"}},
                        },
                    },
                    "player5": {
                        "name": "Enemy",
                        "team_name": "dire",
                        "hero": {
                            "name": "npc_dota_hero_phantom_assassin",
                            "items": enemy_items,
                        },
                    },
                },
            }

        self.assertIsNone(
            agent.process_tick(
                payload_with({"slot0": {"name": "item_power_treads"}}),
                hero_catalog,
                ITEMS,
            )
        )
        advice = agent.process_tick(
            payload_with(
                {
                    "slot0": {"name": "item_power_treads"},
                    "slot1": {"name": "item_blink"},
                }
            ),
            hero_catalog,
            ITEMS,
        )
        self.assertIsNotNone(advice)
        self.assertIn("Blink Dagger", advice)
        self.assertIn("Phantom Assassin", advice)
        self.assertIn("враг", advice.lower())
        # Тот же предмет повторно не анонсируется.
        self.assertIsNone(
            agent.process_tick(
                payload_with(
                    {
                        "slot0": {"name": "item_power_treads"},
                        "slot1": {"name": "item_blink"},
                    }
                ),
                hero_catalog,
                ITEMS,
            )
        )

    def test_aegis_detected_once(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="medium", cooldown_seconds=0.0)
        payload = {
            "map": {"clock_time": 1000, "aegis": "dire"},
            "player": {"team_name": "radiant"},
        }
        advice = agent.process_tick(payload, catalog(), ITEMS)
        self.assertIsNotNone(advice)
        self.assertIn("аегис", advice.lower())
        self.assertIn("враг", advice.lower())
        self.assertIsNone(agent.process_tick(payload, catalog(), ITEMS))

    def test_own_aegis_detected(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="medium", cooldown_seconds=0.0)
        payload = {
            "map": {"clock_time": 1000, "aegis": "radiant"},
            "player": {"team_name": "radiant"},
        }
        advice = agent.process_tick(payload, catalog(), ITEMS)
        self.assertIsNotNone(advice)
        self.assertIn("У тебя аегис", advice)

    def test_roshan_timer_detected_once(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="medium", cooldown_seconds=0.0)
        payload = {
            "map": {"clock_time": 1000, "roshan_respawn_timer": 300},
            "player": {"team_name": "radiant"},
        }
        advice = agent.process_tick(payload, catalog(), ITEMS)
        self.assertIsNotNone(advice)
        self.assertIn("Рошан", advice)
        self.assertIsNone(agent.process_tick(payload, catalog(), ITEMS))
        # Таймер закончился — следующий Рошан снова даст подсказку.
        expired = {
            "map": {"clock_time": 1000, "roshan_respawn_timer": 0},
            "player": {"team_name": "radiant"},
        }
        self.assertIsNone(agent.process_tick(expired, catalog(), ITEMS))
        # Новый Рошан: другие остатки таймера -> новый текст совета.
        next_roshan = {
            "map": {"clock_time": 1000, "roshan_respawn_timer": 240},
            "player": {"team_name": "radiant"},
        }
        self.assertIsNotNone(agent.process_tick(next_roshan, catalog(), ITEMS))

    def test_garbage_ticks_and_autonomy_off(self) -> None:
        agent = AutonomousCoachAgent(autonomy_level="medium", cooldown_seconds=0.0)
        for junk in (None, "junk", [1, 2], {}, {"map": {"clock_time": "abc"}}):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(agent.process_tick(junk, catalog(), ITEMS))

        greedy_payload = {
            "map": {"clock_time": 800},
            "player": {"gold": 5000, "team_name": "radiant"},
            "hero": {"alive": True},
        }
        advice = agent.process_tick(greedy_payload, catalog(), ITEMS)
        self.assertIsNotNone(advice)
        self.assertIn("золот", advice)

        agent.set_autonomy_level("off")
        self.assertIsNone(agent.process_tick(greedy_payload, catalog(), ITEMS))


if __name__ == "__main__":
    unittest.main()
