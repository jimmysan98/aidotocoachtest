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


if __name__ == "__main__":
    unittest.main()
