"""Тесты клиента OpenDota с подменённой сетью."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aidota.dota_api import DotaApi, POPULARITY_KEYS
from tests.helpers import FAKE_HEROES, FakeSession, make_temp_dir, remove_temp_dir

ITEMS = {"blink": {"dname": "Blink Dagger"}, "tango": {"dname": "Tango"}}
POPULARITY = {
    "start_game_items": {"tango": 100, "branches": 80},
    "early_game_items": {"boots": 50},
    "mid_game_items": {},
    "late_game_items": {"blink": 10},
}


class DotaApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "heroes.json").write_text(
            json.dumps(FAKE_HEROES, ensure_ascii=False), encoding="utf-8"
        )
        (self.data / "items.json").write_text(
            json.dumps({"blink": "Blink Dagger", "tango": "Tango"}), encoding="utf-8"
        )

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def api(self, routes=None):
        routes = routes or {}
        session = FakeSession(routes)
        api = DotaApi(self.data, session=session)
        api.load_snapshots()
        return api, session

    def test_snapshots_loaded(self):
        api, _ = self.api()
        self.assertEqual(len(api.heroes), len(FAKE_HEROES))
        self.assertEqual(api.items()["blink"], "Blink Dagger")

    def test_item_display(self):
        api, _ = self.api()
        self.assertEqual(api.item_display("item_blink"), "Blink Dagger")
        self.assertEqual(api.item_display("blink"), "Blink Dagger")
        self.assertEqual(api.item_display("item_smoke_of_deceit"), "Smoke Of Deceit")
        self.assertEqual(api.item_display(None), "?")

    def _write_items(self, raw: dict) -> None:
        (self.data / "items.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

    def test_item_costs_loaded_and_resolvable(self):
        """data/items.json с cost: display/npc/slug → цена, слаг и item_info."""
        self._write_items(
            {
                "blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250},
                "magic_wand": {"dname": "Magic Wand", "id": 36, "cost": 460},
            }
        )
        api, _ = self.api()
        self.assertEqual(api.item_cost("Blink Dagger"), 2250)
        self.assertEqual(api.item_cost("item_blink"), 2250)
        self.assertEqual(api.item_cost("blink"), 2250)
        self.assertIsNone(api.item_cost("Чепуха"))
        self.assertIsNone(api.item_cost(None))
        self.assertEqual(api.item_slug("Blink Dagger"), "blink")
        self.assertEqual(api.item_slug("item_magic_wand"), "magic_wand")
        self.assertIsNone(api.item_slug("Чепуха"))
        self.assertEqual(
            api.item_costs(), {"Blink Dagger": 2250, "Magic Wand": 460}
        )
        self.assertEqual(
            api.item_info("npc_dota_item_blink"),
            {"slug": "blink", "display": "Blink Dagger", "cost": 2250},
        )
        self.assertIsNone(api.item_info("Чепуха"))

    def test_item_cost_missing_in_legacy_format(self):
        """Старый формат {name: dname} читается, но цена у предметов неизвестна."""
        api, _ = self.api()
        self.assertIsNone(api.item_cost("Blink Dagger"))
        self.assertEqual(api.item_slug("Blink Dagger"), "blink")
        self.assertEqual(
            api.item_info("item_blink"),
            {"slug": "blink", "display": "Blink Dagger", "cost": None},
        )
        self.assertEqual(api.item_costs(), {})

    def test_refresh_snapshots_writes_cost(self):
        routes = {
            "/heroStats": FAKE_HEROES,
            "/constants/items": {
                "blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250},
            },
        }
        api, _ = self.api(routes)
        self.assertTrue(api.refresh_snapshots())
        saved = json.loads((self.data / "items.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["blink"]["dname"], "Blink Dagger")
        self.assertEqual(saved["blink"]["id"], 1)
        self.assertEqual(saved["blink"]["cost"], 2250)
        self.assertEqual(api.item_cost("item_blink"), 2250)

    def test_refresh_snapshots_cost_missing_is_none(self):
        routes = {
            "/heroStats": FAKE_HEROES,
            "/constants/items": {"blink": {"dname": "Blink Dagger", "id": 1}},
        }
        api, _ = self.api(routes)
        self.assertTrue(api.refresh_snapshots())
        saved = json.loads((self.data / "items.json").read_text(encoding="utf-8"))
        self.assertIsNone(saved["blink"]["cost"])
        self.assertIsNone(api.item_cost("blink"))

    def test_winrate_prefers_pro(self):
        pct, kind, games = DotaApi.winrate(FAKE_HEROES[0])
        self.assertEqual(kind, "pro")
        self.assertAlmostEqual(pct, 50.0)
        self.assertEqual(games, 50)

    def test_winrate_pub_fallback(self):
        hero = {"pub_pick": 10, "pub_win": 6}
        pct, kind, games = DotaApi.winrate(hero)
        self.assertEqual(kind, "pub")
        self.assertAlmostEqual(pct, 60.0)
        self.assertEqual(games, 10)

    def test_meta_sorted(self):
        api, _ = self.api()
        top = api.meta(n=3, min_pro_picks=5)
        names = [item["hero"]["localized_name"] for item in top]
        self.assertEqual(names[0], "Shadow Fiend")  # 60%
        self.assertEqual(len(names), 3)

    def test_popularity_cached(self):
        api, session = self.api({"/itemPopularity": POPULARITY})
        first = api.item_popularity(1)
        second = api.item_popularity(1)
        self.assertEqual(first, second)
        self.assertEqual(len([call for call in session.calls if "itemPopularity" in call]), 1)
        self.assertEqual(set(first), set(POPULARITY_KEYS))

    def test_popularity_error_returns_empty(self):
        api, _ = self.api({"/itemPopularity": RuntimeError("нет сети")})
        self.assertEqual(api.item_popularity(1), {})

    def test_top_items(self):
        api, _ = self.api()
        self.assertEqual(api.top_items(POPULARITY, "start_game_items", 1), ["Tango"])

    def test_top_items_resolves_numeric_ids(self):
        (self.data / "items.json").write_text(
            json.dumps({"blink": {"dname": "Blink Dagger", "id": 1}}, ensure_ascii=False),
            encoding="utf-8",
        )
        api, _ = self.api()
        popularity = {"mid_game_items": {"1": 100}}
        self.assertEqual(api.top_items(popularity, "mid_game_items"), ["Blink Dagger"])

    def test_refresh_snapshots_writes_files(self):
        routes = {
            "/heroStats": FAKE_HEROES,
            "/constants/items": ITEMS,
        }
        api, _ = self.api(routes)
        self.assertTrue(api.refresh_snapshots())
        saved = json.loads((self.data / "heroes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved), len(FAKE_HEROES))
        saved_items = json.loads((self.data / "items.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_items["blink"]["dname"], "Blink Dagger")

    def test_refresh_failure_keeps_snapshots(self):
        api, _ = self.api({"/heroStats": RuntimeError("boom")})
        self.assertFalse(api.refresh_snapshots())
        self.assertEqual(len(api.heroes), len(FAKE_HEROES))

    def test_ensure_loaded_without_snapshots(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        session = FakeSession({"/heroStats": FAKE_HEROES, "/constants/items": ITEMS})
        api = DotaApi(empty, session=session)
        api.ensure_loaded()
        self.assertTrue(api.heroes)

    def test_online(self):
        api, _ = self.api({"/heroStats": FAKE_HEROES})
        self.assertTrue(api.online())

    def test_online_false(self):
        api, _ = self.api({"/heroStats": RuntimeError("no")})
        self.assertFalse(api.online())

    def test_recent_matches_success(self):
        fake_matches = [{"match_id": 12345, "hero_id": 1}, {"match_id": 12346, "hero_id": 2}]
        api, _ = self.api({"/players/70388657/recentMatches": fake_matches})
        matches = api.recent_matches(70388657, limit=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_id"], 12345)

    def test_recent_matches_error_returns_empty(self):
        api, _ = self.api({"/players/70388657/recentMatches": RuntimeError("offline")})
        self.assertEqual(api.recent_matches(70388657), [])
        self.assertEqual(api.recent_matches("invalid"), [])

    def test_match_details_success(self):
        fake_match = {"match_id": 99999, "radiant_win": True, "players": [{"hero_id": 1}]}
        api, _ = self.api({"/matches/99999": fake_match})
        data = api.match_details(99999)
        self.assertIsNotNone(data)
        self.assertEqual(data["match_id"], 99999)

    def test_match_details_error(self):
        api, _ = self.api({"/matches/99999": RuntimeError("offline")})
        self.assertIsNone(api.match_details(99999))
        self.assertIsNone(api.match_details("abc"))

    def test_parse_match_analysis(self):
        (self.data / "items.json").write_text(
            json.dumps({"blink": {"dname": "Blink Dagger", "id": 1}}, ensure_ascii=False),
            encoding="utf-8",
        )
        api, _ = self.api()
        raw_match = {
            "match_id": 77777,
            "radiant_win": True,
            "duration": 1835,  # 30:35
            "radiant_score": 35,
            "dire_score": 20,
            "players": [
                {
                    "account_id": 123456,
                    "player_slot": 0,  # Radiant
                    "hero_id": 1,
                    "kills": 12,
                    "deaths": 2,
                    "assists": 8,
                    "gold_per_min": 650,
                    "xp_per_min": 720,
                    "net_worth": 21000,
                    "last_hits": 280,
                    "denies": 15,
                    "hero_damage": 28000,
                    "tower_damage": 4500,
                    "hero_healing": 0,
                    "item_0": 1,
                    "item_1": 0,
                    "benchmarks": {
                        "gold_per_min": {"pct": 0.85},
                        "xp_per_min": {"pct": 0.90},
                        "hero_damage_per_min": {"pct": 0.78},
                        "tower_damage": {"pct": 0.82},
                    },
                    "purchase_log": [{"time": 720, "key": "blink"}],
                },
                {
                    "account_id": 654321,
                    "player_slot": 128,  # Dire
                    "hero_id": 2,
                    "kills": 4,
                    "deaths": 10,
                    "assists": 6,
                },
            ],
        }
        res = api.parse_match_analysis(raw_match, account_id=123456)
        self.assertEqual(res["match_id"], 77777)
        self.assertEqual(res["hero_name"], "Anti-Mage")
        self.assertTrue(res["is_win"])
        self.assertEqual(res["result_str"], "Победа")
        self.assertEqual(res["duration_str"], "30:35")
        self.assertEqual(res["kills"], 12)
        self.assertEqual(res["deaths"], 2)
        self.assertEqual(res["assists"], 8)
        self.assertAlmostEqual(res["kda_ratio"], 10.0)
        self.assertEqual(res["items"], ["Blink Dagger"])
        self.assertEqual(res["benchmarks"]["gpm_pct"], 85)
        self.assertIn("Pudge", res["enemies"])


if __name__ == "__main__":
    unittest.main()

