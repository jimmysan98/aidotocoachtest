"""Контракт v1.2: живые данные из реального GSI и их отдача в веб-UI.

Проверяют:
- ``extract_own_items`` / ``extract_own_abilities`` для playing-формата
  (секции ``items``/``abilities`` на верхнем уровне), spectator-формата
  (``team2/team3 → playerN``) и устаревшего ``hero.items``;
- пустые слоты (``empty``/``item_empty``/``""``/``None``) во всех местах;
- ``estimate_net_worth``: GSI-значение, иначе оценка ``gold + цена слотов/стэша``;
- новые ключи ``get_live_gsi_snapshot``: ``inventory_keys``, ``net_worth``,
  ``net_worth_estimated``;
- новые поля ``AidotaWebApi.get_state()["gsi"]``: ``hero_icon_key``,
  ``inventory_items``, ``networth_estimated``, ``enemy_picks``, ``item_advice``
  и ультимейт из верхнеуровневой секции ``abilities``.

Сеть не используется: только локальные словари и фейковый DotaApi.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from aidota.config import Config, save_config
from aidota.context import (
    analyze_player_mistakes,
    estimate_net_worth,
    extract_own_abilities,
    extract_own_items,
    format_gsi_summary,
    get_live_gsi_snapshot,
    is_empty_item_name,
)
from aidota.heroes import HeroCatalog
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import make_temp_dir, remove_temp_dir

ROOT = Path(__file__).resolve().parent.parent

ITEMS = {
    "magic_wand": "Magic Wand",
    "tranquil_boots": "Tranquil Boots",
    "light_collector": "Light Collector",
    "tpscroll": "Town Portal Scroll",
    "occult_bracelet": "Occult Bracelet",
    "blink": "Blink Dagger",
}

COSTS = {
    "Magic Wand": 460,
    "Tranquil Boots": 925,
    "Light Collector": 900,
    "Town Portal Scroll": 100,
    "Occult Bracelet": 500,
    "Blink Dagger": 2250,
    "Ghost Scepter": 1500,
    "Eul's Scepter of Divinity": 2475,
    "Aeon Disk": 3000,
}

HEROES = [
    {"id": 86, "name": "npc_dota_hero_rubick", "localized_name": "Rubick"},
    {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut"},
]


def catalog() -> HeroCatalog:
    return HeroCatalog(HEROES)


def realistic_payload(top_level: bool = True) -> dict:
    """Rubick pos 5 на 8:45 против Juggernaut — как в реальном GSI-пакете."""
    payload = {
        "map": {
            "matchid": "8123456789",
            "clock_time": 525,
            "radiant_score": 11,
            "dire_score": 20,
            "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
        },
        "player": {
            "steamid": "76561198000000001",
            "accountid": 39734238,
            "name": "Player",
            "team_name": "dire",
            "kills": 2,
            "deaths": 3,
            "assists": 3,
            "gold": 940,
            "net_worth": 4230,
            "gpm": 355,
            "xpm": 380,
            "last_hits": 19,
            "denies": 1,
        },
        "hero": {
            "id": 86,
            "name": "npc_dota_hero_rubick",
            "level": 10,
            "alive": True,
            "buyback_cost": 200,
            "buyback_cooldown": 0,
            "health_percent": 74.5,
            "mana_percent": 62.5,
        },
        "items": {
            "slot0": {"name": "item_magic_wand"},
            "slot1": {"name": "item_tranquil_boots"},
            "slot2": {"name": "item_light_collector"},
            "slot3": {"name": "item_empty"},
            "slot4": {"name": "item_empty"},
            "slot5": {"name": "item_empty"},
            "teleport0": {"name": "item_tpscroll"},
            "neutral0": {"name": "item_empty"},
        },
        "abilities": {
            "ability0": {"name": "rubick_telekinesis", "ultimate": False, "cooldown": 0},
            "ability1": {"name": "rubick_fade_bolt", "ultimate": False, "cooldown": 0},
            "ability2": {"name": "rubick_arcane_supremacy", "ultimate": False, "cooldown": 0},
            "ability3": {
                "name": "rubick_spell_steal",
                "ultimate": True,
                "can_cast": True,
                "cooldown": 12,
            },
        },
        "draft": {
            "team2": {"pick_ids": [8]},   # Radiant: Juggernaut — враг
            "team3": {"pick_ids": [86]},  # Dire: Rubick — мой герой
        },
    }
    if not top_level:
        payload["hero"]["items"] = payload.pop("items")
        payload["hero"]["abilities"] = payload.pop("abilities")
    return payload


class EmptyItemNameTests(unittest.TestCase):
    def test_empty_variants(self) -> None:
        for value in (None, "", "empty", "item_empty", "  item_empty  "):
            with self.subTest(value=value):
                self.assertTrue(is_empty_item_name(value))

    def test_real_items_are_not_empty(self) -> None:
        for value in ("item_blink", "blink", "Magic Wand", "item_black_king_bar"):
            with self.subTest(value=value):
                self.assertFalse(is_empty_item_name(value))


class ExtractOwnItemsTests(unittest.TestCase):
    def test_playing_format_top_level_section(self) -> None:
        payload = realistic_payload()
        items = extract_own_items(payload)
        self.assertIsInstance(items, dict)
        self.assertEqual(items["slot0"]["name"], "item_magic_wand")
        self.assertEqual(items["teleport0"]["name"], "item_tpscroll")

    def test_top_level_wins_over_legacy_hero_items(self) -> None:
        payload = realistic_payload()
        payload["hero"]["items"] = {"slot0": {"name": "item_blink"}}
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_magic_wand")

    def test_legacy_hero_items_fallback(self) -> None:
        payload = realistic_payload(top_level=False)
        items = extract_own_items(payload)
        self.assertEqual(items["slot0"]["name"], "item_magic_wand")

    def test_spectator_format_by_accountid(self) -> None:
        payload = {
            "player": {"accountid": 42, "steamid": "76561198000000042", "team_name": "radiant"},
            "items": {
                "team2": {
                    "player0": {
                        "accountid": 42,
                        "steamid": "76561198000000042",
                        "items": {"slot0": {"name": "item_blink"}},
                    },
                    "player1": {
                        "accountid": 77,
                        "items": {"slot0": {"name": "item_black_king_bar"}},
                    },
                },
                "team3": {
                    "player0": {"accountid": 99, "items": {"slot0": {"name": "item_bfury"}}},
                },
            },
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_spectator_format_by_steamid_direct_slots(self) -> None:
        payload = {
            "player": {"steamid": "76561198000000042"},
            "items": {
                "team3": {
                    "player0": {"steamid": "76561198000000000", "slot0": {"name": "item_bfury"}},
                    "player2": {
                        "steamid": "76561198000000042",
                        "slot0": {"name": "item_blink"},
                        "slot1": {"name": "item_empty"},
                    },
                }
            },
        }
        items = extract_own_items(payload)
        self.assertEqual(items, {"slot0": {"name": "item_blink"}, "slot1": {"name": "item_empty"}})

    def test_spectator_format_by_player_slot(self) -> None:
        payload = {
            "player": {"player_slot": 129},
            "items": {
                "team3": {
                    "player0": {"slot0": {"name": "item_bfury"}},
                    "player1": {"slot0": {"name": "item_blink"}},
                }
            },
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_spectator_format_identity_from_allplayers(self) -> None:
        payload = {
            "player": {"accountid": 42},
            "allplayers": {"player0": {"accountid": 42}},
            "items": {
                "team2": {
                    "player0": {"slot0": {"name": "item_blink"}},
                    "player1": {"slot0": {"name": "item_bfury"}},
                }
            },
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_spectator_team_level_section_matches_own_side(self) -> None:
        payload = {
            "player": {"team_name": "radiant"},
            "items": {
                "team2": {"slot0": {"name": "item_blink"}},
                "team3": {"slot0": {"name": "item_bfury"}},
            },
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_spectator_players_list(self) -> None:
        payload = {
            "player": {"accountid": 42},
            "items": {
                "team2": {
                    "players": [
                        {"accountid": 7, "slot0": {"name": "item_bfury"}},
                        {"accountid": 42, "slot0": {"name": "item_blink"}},
                    ]
                }
            },
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_spectator_single_player_is_used(self) -> None:
        payload = {
            "player": {},
            "items": {"team2": {"player0": {"slot0": {"name": "item_blink"}}}},
        }
        self.assertEqual(extract_own_items(payload)["slot0"]["name"], "item_blink")

    def test_junk_returns_empty_dict(self) -> None:
        for payload in (None, "junk", [], {}, {"items": "junk"}, {"hero": "junk"}):
            with self.subTest(payload=repr(payload)):
                self.assertEqual(extract_own_items(payload), {})


class ExtractOwnAbilitiesTests(unittest.TestCase):
    def test_top_level_abilities(self) -> None:
        abilities = extract_own_abilities(realistic_payload())
        self.assertTrue(abilities["ability3"]["ultimate"])
        self.assertEqual(abilities["ability3"]["cooldown"], 12)

    def test_legacy_hero_abilities_fallback(self) -> None:
        abilities = extract_own_abilities(realistic_payload(top_level=False))
        self.assertEqual(abilities["ability3"]["cooldown"], 12)

    def test_top_level_wins_over_legacy(self) -> None:
        payload = realistic_payload()
        payload["hero"]["abilities"] = {"ability0": {"name": "legacy", "ultimate": True}}
        self.assertIn("ability3", extract_own_abilities(payload))

    def test_junk_returns_empty_dict(self) -> None:
        for payload in (None, "junk", {}, {"abilities": "junk"}, {"hero": 5}):
            with self.subTest(payload=repr(payload)):
                self.assertEqual(extract_own_abilities(payload), {})


class EmptySlotsSnapshotTests(unittest.TestCase):
    def test_empty_names_do_not_leak_into_inventory(self) -> None:
        payload = realistic_payload()
        payload["items"] = {
            "slot0": {"name": "item_magic_wand"},
            "slot1": {"name": "item_empty"},
            "slot2": {"name": "empty"},
            "slot3": {"name": ""},
            "slot4": {"name": None},
            "slot5": "item_empty",
            "teleport0": {"name": "item_empty"},
            "neutral0": {"name": "empty"},
        }
        snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
        self.assertEqual(snapshot["inventory"], ["Magic Wand"])
        self.assertEqual(snapshot["inventory_keys"], ["item_magic_wand"])
        self.assertIsNone(snapshot["neutral_item"])
        self.assertFalse(snapshot["has_tp"])

    def test_tp_and_neutral_from_top_level_section(self) -> None:
        payload = realistic_payload()
        payload["items"]["teleport0"] = {"name": "item_tpscroll"}
        payload["items"]["neutral0"] = {"name": "item_occult_bracelet"}
        snapshot = get_live_gsi_snapshot(payload, catalog(), ITEMS)
        self.assertTrue(snapshot["has_tp"])
        self.assertEqual(snapshot["neutral_item"], "Occult Bracelet")

    def test_mistake_detector_sees_top_level_items(self) -> None:
        payload = {
            "map": {"clock_time": 900, "game_mode": 1},
            "player": {"gold": 500, "team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_rubick", "alive": True},
            "items": {
                "slot0": {"name": "item_tpscroll"},
                "neutral0": {"name": "item_occult_bracelet"},
            },
        }
        mistakes = analyze_player_mistakes(payload, 1, 1)
        self.assertFalse(any("телепортаци" in m for m in mistakes), mistakes)
        self.assertFalse(any("Нейтральный слот пуст" in m for m in mistakes), mistakes)

        payload["items"].pop("teleport0", None)
        payload["items"]["slot0"] = {"name": "item_empty"}
        payload["items"]["neutral0"] = {"name": "item_empty"}
        mistakes = analyze_player_mistakes(payload, 1, 1)
        self.assertTrue(any("телепортаци" in m for m in mistakes), mistakes)
        self.assertTrue(any("Нейтральный слот пуст" in m for m in mistakes), mistakes)


class NetWorthTests(unittest.TestCase):
    def test_gsi_net_worth_is_used_as_is(self) -> None:
        value, estimated = estimate_net_worth(realistic_payload(), COSTS, items=ITEMS)
        self.assertEqual(value, 4230)
        self.assertFalse(estimated)

    def test_fallback_estimate_slots_and_stash(self) -> None:
        payload = realistic_payload()
        payload["player"].pop("net_worth")
        payload["items"]["stash0"] = {"name": "item_blink"}
        value, estimated = estimate_net_worth(payload, COSTS, items=ITEMS)
        self.assertTrue(estimated)
        self.assertEqual(value, 940 + 460 + 925 + 900 + 2250)

    def test_neutral_and_tp_are_not_counted(self) -> None:
        payload = realistic_payload()
        payload["player"].pop("net_worth")
        payload["items"]["teleport0"] = {"name": "item_tpscroll"}
        payload["items"]["neutral0"] = {"name": "item_occult_bracelet"}
        value, estimated = estimate_net_worth(payload, COSTS, items=ITEMS)
        self.assertTrue(estimated)
        self.assertEqual(value, 940 + 460 + 925 + 900)

    def test_slug_only_cost_keys_found_by_prefix(self) -> None:
        payload = {
            "player": {"gold": 500},
            "items": {"slot0": {"name": "item_blink"}},
        }
        value, estimated = estimate_net_worth(payload, {"Blink Dagger": 2250})
        self.assertEqual((value, estimated), (2750, True))

    def test_zero_net_worth_triggers_estimate(self) -> None:
        payload = realistic_payload()
        payload["player"]["net_worth"] = 0
        value, estimated = estimate_net_worth(payload, COSTS, items=ITEMS)
        self.assertTrue(estimated)
        self.assertEqual(value, 940 + 460 + 925 + 900)

    def test_junk_payloads(self) -> None:
        self.assertEqual(estimate_net_worth(None, COSTS), (0, False))
        self.assertEqual(estimate_net_worth("junk", COSTS), (0, False))
        self.assertEqual(estimate_net_worth({}, COSTS), (0, True))


class SnapshotContractTests(unittest.TestCase):
    def test_snapshot_top_level_items_and_net_worth(self) -> None:
        snapshot = get_live_gsi_snapshot(
            realistic_payload(), catalog(), ITEMS, position=5, item_costs=COSTS
        )
        self.assertTrue(snapshot["available"])
        self.assertEqual(
            snapshot["inventory"],
            ["Magic Wand", "Tranquil Boots", "Light Collector"],
        )
        self.assertEqual(
            snapshot["inventory_keys"],
            [
                "item_magic_wand",
                "item_tranquil_boots",
                "item_light_collector",
            ],
        )
        self.assertEqual(snapshot["net_worth"], 4230)
        self.assertFalse(snapshot["net_worth_estimated"])
        self.assertTrue(snapshot["has_tp"])

    def test_snapshot_flags_estimated_net_worth(self) -> None:
        payload = realistic_payload()
        payload["player"].pop("net_worth")
        snapshot = get_live_gsi_snapshot(
            payload, catalog(), ITEMS, position=5, item_costs=COSTS
        )
        self.assertTrue(snapshot["net_worth_estimated"])
        self.assertEqual(snapshot["net_worth"], 940 + 460 + 925 + 900)

    def test_offline_snapshot_has_new_keys(self) -> None:
        snapshot = get_live_gsi_snapshot(None, catalog(), ITEMS)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["inventory_keys"], [])
        self.assertEqual(snapshot["net_worth"], 0)
        self.assertFalse(snapshot["net_worth_estimated"])

    def test_summary_mentions_live_inventory(self) -> None:
        snapshot = get_live_gsi_snapshot(
            realistic_payload(), catalog(), ITEMS, position=5, item_costs=COSTS
        )
        summary = format_gsi_summary(snapshot)
        self.assertIn("Rubick", summary)
        self.assertIn("Magic Wand", summary)
        self.assertIn("NW 4230", summary)


class _FakeCatalog:
    available = True

    def display_name(self, npc: str) -> str:
        for hero in HEROES:
            if hero["name"] == npc:
                return hero["localized_name"]
        return ""

    def by_npc(self, npc: str):
        for hero in HEROES:
            if hero["name"] == npc:
                return hero
        return None

    def by_id(self, hero_id):
        for hero in HEROES:
            if hero["id"] == int(hero_id or 0):
                return hero
        return None


class _FakeApi:
    """DotaApi-замена без сети: только словари и цены."""

    def __init__(self, items: dict | None = None, costs: dict | None = None):
        self._items = dict(items if items is not None else ITEMS)
        self._costs = dict(costs if costs is not None else COSTS)

    def items(self):
        return dict(self._items)

    def item_costs(self):
        return dict(self._costs)

    def item_slug(self, name):
        candidate = str(name or "")
        if candidate.startswith("npc_dota_item_"):
            candidate = candidate[len("npc_dota_item_"):]
        if candidate.startswith("item_"):
            candidate = candidate[5:]
        if candidate in self._items:
            return candidate
        for slug, display in self._items.items():
            if str(display).casefold() == str(name).strip().casefold():
                return slug
        return None

    def item_cost(self, name):
        slug = self.item_slug(name)
        if slug is None:
            return None
        display = self._items.get(slug)
        return self._costs.get(display)

    def item_info(self, name):
        slug = self.item_slug(name)
        if slug is None:
            return None
        display = self._items.get(slug)
        return {"slug": slug, "display": display, "cost": self._costs.get(display)}


class _FakeCoach:
    def __init__(self, payload: dict, api: _FakeApi | None = None):
        self._payload = payload
        self.catalog = _FakeCatalog()
        self.api = api or _FakeApi()

    def gsi_payload(self):
        return self._payload

    def gsi_age(self):
        return 2.5


class _FakeGsi:
    def __init__(self, payload: dict | None):
        self._payload = payload

    def snapshot(self):
        return self._payload

    def age(self):
        return 2.5


class WebStateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.config_path = self.tmp / "config.json"
        self.config = Config()
        self.config.player_profile.default_position = 5
        save_config(self.config, self.config_path)
        self.ctrl = WebGuiController(self.config, self.config_path)
        self.api = AidotaWebApi(self.ctrl)
        self.ctrl.assistant = SimpleNamespace(
            _coach=_FakeCoach(realistic_payload()),
            _gsi=_FakeGsi(realistic_payload()),
            _catalog=_FakeCatalog(),
            _api=_FakeApi(),
        )
        self.ctrl.is_running = True

    def tearDown(self) -> None:
        self.ctrl.tray.stop()
        self.ctrl.assistant = None
        remove_temp_dir(self.tmp)

    def test_get_state_contract_fields(self) -> None:
        state = self.api.get_state()
        gsi = state["gsi"]
        self.assertTrue(gsi["connected"])
        self.assertEqual(gsi["hero_name"], "Rubick")
        self.assertEqual(gsi["hero_icon"], "")
        self.assertEqual(gsi["hero_icon_key"], "rubick")
        self.assertEqual(gsi["networth"], 4230)
        self.assertFalse(gsi["networth_estimated"])
        self.assertEqual(gsi["ult_cooldown"], 12)
        self.assertFalse(gsi["ult_ready"])
        self.assertEqual(gsi["enemy_picks"], ["Juggernaut"])
        self.assertEqual(
            [slot["display"] for slot in gsi["inventory_items"]],
            ["Magic Wand", "Tranquil Boots", "Light Collector"],
        )
        first = gsi["inventory_items"][0]
        self.assertEqual(first["key"], "item_magic_wand")
        self.assertEqual(first["slug"], "magic_wand")
        self.assertEqual(first["cost"], 460)
        self.assertTrue(gsi["item_advice"])
        for advice in gsi["item_advice"]:
            self.assertTrue(advice.get("reason"), advice)
        # Советы привязаны к врагу из драфта (Juggernaut).
        text = " ".join(a.get("reason", "") for a in gsi["item_advice"])
        self.assertIn("Juggernaut", text)
        self.assertIn("Ghost Scepter", " ".join(a.get("item", "") for a in gsi["item_advice"]))

    def test_get_state_estimated_networth(self) -> None:
        payload = realistic_payload()
        payload["player"].pop("net_worth")
        self.ctrl.assistant = SimpleNamespace(
            _coach=_FakeCoach(payload),
            _gsi=_FakeGsi(payload),
            _catalog=_FakeCatalog(),
            _api=_FakeApi(),
        )
        gsi = self.api.get_state()["gsi"]
        self.assertTrue(gsi["networth_estimated"])
        self.assertEqual(gsi["networth"], 940 + 460 + 925 + 900)

    def test_get_state_legacy_nested_sections_still_work(self) -> None:
        payload = realistic_payload(top_level=False)
        self.ctrl.assistant = SimpleNamespace(
            _coach=_FakeCoach(payload),
            _gsi=_FakeGsi(payload),
            _catalog=_FakeCatalog(),
            _api=_FakeApi(),
        )
        gsi = self.api.get_state()["gsi"]
        self.assertEqual(gsi["hero_icon_key"], "rubick")
        self.assertEqual(gsi["ult_cooldown"], 12)
        self.assertEqual(len(gsi["inventory_items"]), 3)

    def test_get_live_advice_fresh(self) -> None:
        advice = self.api.get_live_advice()
        self.assertTrue(advice)
        self.assertTrue(all(entry.get("reason") for entry in advice))

    def test_api_without_assistant_never_raises(self) -> None:
        self.ctrl.assistant = None

        class _OfflineSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("offline")

        self.api._icon_session_obj = _OfflineSession()
        state = self.api.get_state()
        self.assertFalse(state["gsi"]["connected"])
        self.assertEqual(state["gsi"]["hero_icon_key"], "")
        self.assertIsInstance(state["gsi"]["item_advice"], list)
        self.assertEqual(self.api.get_live_advice(), [])
        self.assertIsInstance(self.api.get_hero_icon("rubick"), str)
        self.assertIsInstance(self.api.get_item_icon("item_magic_wand"), str)


if __name__ == "__main__":
    unittest.main()
