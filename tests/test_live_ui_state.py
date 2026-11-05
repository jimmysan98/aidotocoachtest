"""Живые данные матча в UI: GSI-мост, HUD оверлея и удалённое окно настроек."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from aidota import __version__
from aidota.app import Assistant
from aidota.config import Config, OverlayConfig, save_config
from aidota.overlay import Overlay
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import make_temp_dir, remove_temp_dir

ROOT = Path(__file__).resolve().parent.parent


class _FakeCatalog:
    available = True

    def display_name(self, npc: str) -> str:
        return {"npc_dota_hero_antimage": "Anti-Mage"}.get(npc, "")

    def by_npc(self, npc: str):
        if str(npc).startswith("npc_dota_hero_antimage"):
            return {"name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"}
        return None

    def by_id(self, hero_id):
        if int(hero_id or 0) == 1:
            return {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"}
        return None


class _FakeApi:
    def items(self):
        return {"power_treads": "Power Treads", "battle_fury": "Battle Fury"}

    def item_popularity(self, hero_id):  # pragma: no cover - не используется
        return {}


class _FakeCoach:
    def __init__(self, payload):
        self._payload = payload
        self.catalog = _FakeCatalog()
        self.api = _FakeApi()

    def gsi_payload(self):
        return self._payload

    def gsi_age(self):
        return 2.5


class _FakeGsi:
    def __init__(self, payload):
        self._payload = payload

    def snapshot(self):
        return self._payload

    def age(self):
        return 2.5

    def last_state(self):  # намеренно отсутствует в реальном GsiServer
        raise AssertionError("last_state() не должен вызываться")


GSI_PAYLOAD = {
    "map": {
        "clock_time": 754,
        "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
        "radiant_score": 12,
        "dire_score": 9,
    },
    "player": {
        "team_name": "radiant",
        "kills": 5,
        "deaths": 2,
        "assists": 7,
        "gold": 1450,
        "net_worth": 8200,
        "gpm": 520,
        "xpm": 580,
        "last_hits": 87,
        "denies": 9,
    },
    "hero": {
        "name": "npc_dota_hero_antimage",
        "level": 14,
        "alive": True,
        "items": {
            "slot0": {"name": "item_power_treads"},
            "slot1": {"name": "item_battle_fury"},
        },
        "abilities": {
            "ability0": {"name": "antimage_mana_break", "ultimate": False},
            "ability3": {"name": "antimage_mana_void", "ultimate": True, "cooldown": 42},
        },
    },
}


class _StateTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.config_path = self.tmp / "config.json"
        self.config = Config()
        save_config(self.config, self.config_path)
        self.ctrl = WebGuiController(self.config, self.config_path)
        self.api = AidotaWebApi(self.ctrl)

    def tearDown(self) -> None:
        self.ctrl.tray.stop()
        self.ctrl.assistant = None
        remove_temp_dir(self.tmp)


class WebLiveStateTests(_StateTestsBase):
    def test_get_state_returns_live_gsi_data(self) -> None:
        """Герой, уровень, золото, KDA и время матча попадают в состояние UI."""
        self.ctrl.assistant = SimpleNamespace(_coach=_FakeCoach(GSI_PAYLOAD), _gsi=_FakeGsi(GSI_PAYLOAD))
        self.ctrl.is_running = True
        state = self.api.get_state()

        self.assertTrue(state["assistant_running"])
        gsi = state["gsi"]
        self.assertTrue(gsi["connected"])
        self.assertEqual(gsi["hero_name"], "Anti-Mage")
        self.assertEqual(gsi["hero_level"], 14)
        self.assertEqual(gsi["kills"], 5)
        self.assertEqual(gsi["deaths"], 2)
        self.assertEqual(gsi["assists"], 7)
        self.assertEqual(gsi["gold"], 1450)
        self.assertEqual(gsi["networth"], 8200)
        self.assertEqual(gsi["last_hits"], 87)
        self.assertEqual(gsi["denies"], 9)
        self.assertEqual(gsi["clock_time"], 754)
        self.assertEqual(gsi["radiant_score"], 12)
        # Контракт v1.2: иконку фронт берёт через API по короткому ключу героя.
        self.assertEqual(gsi["hero_icon"], "")
        self.assertEqual(gsi["hero_icon_key"], "antimage")
        self.assertIn("Power Treads", " ".join(gsi["inventory"]))
        self.assertEqual(
            [slot["key"] for slot in gsi["inventory_items"]],
            ["item_power_treads", "item_battle_fury"],
        )
        self.assertEqual(gsi["inventory_items"][0]["slug"], "power_treads")
        self.assertIsInstance(gsi["inventory_items"][0]["cost"], (int, type(None)))
        self.assertFalse(gsi["networth_estimated"])
        self.assertEqual(gsi["networth"], 8200)
        self.assertIsInstance(gsi["enemy_picks"], list)
        self.assertIsInstance(gsi["item_advice"], list)
        for advice in gsi["item_advice"]:
            self.assertTrue(advice.get("reason"), advice)
            self.assertIn("item", advice)
        self.assertEqual(gsi["ult_cooldown"], 42)
        self.assertFalse(gsi["ult_ready"])

    def test_get_state_works_with_legacy_gsi_without_last_state(self) -> None:
        """Мост не падает, если у ассистента нет `last_state` и даже Coach."""
        fake_coach = _FakeCoach(GSI_PAYLOAD)
        self.ctrl.assistant = SimpleNamespace(
            _coach=fake_coach,
            _gsi=_FakeGsi(GSI_PAYLOAD),
            _catalog=_FakeCatalog(),
        )
        state = self.api.get_state()  # не должно быть исключения
        self.assertTrue(state["gsi"]["connected"])

        self.ctrl.assistant = SimpleNamespace(_coach=None, _gsi=_FakeGsi(GSI_PAYLOAD), _catalog=_FakeCatalog())
        state2 = self.api.get_state()
        self.assertTrue(state2["gsi"]["connected"])
        self.assertEqual(state2["gsi"]["hero_name"], "Anti-Mage")

    def test_get_state_without_assistant(self) -> None:
        state = self.api.get_state()
        self.assertFalse(state["gsi"]["connected"])
        self.assertEqual(state["gsi"]["hero_name"], "")
        self.assertIn("audio_devices", state)

    def test_audio_devices_are_cached(self) -> None:
        import aidota.web_app as web_app_module

        calls: list[int] = []
        original = web_app_module.list_audio_devices

        def fake_devices(*args, **kwargs):
            calls.append(1)
            return {"inputs": [], "outputs": []}

        web_app_module.list_audio_devices = fake_devices
        try:
            self.ctrl._audio_cache = None
            self.api.get_state()
            self.api.get_state()
            self.api.get_state()
        finally:
            web_app_module.list_audio_devices = original
        self.assertEqual(len(calls), 1, "список устройств должен кэшироваться")

    def test_raw_gsi_payload_helper(self) -> None:
        self.ctrl.assistant = SimpleNamespace(_coach=_FakeCoach(GSI_PAYLOAD), _gsi=_FakeGsi(GSI_PAYLOAD))
        self.assertIs(self.ctrl.raw_gsi_payload(), GSI_PAYLOAD)


class OverlayHudTests(unittest.TestCase):
    def test_update_hud_before_gui_queues_item(self) -> None:
        overlay = Overlay(OverlayConfig(enabled=True), ROOT)
        overlay.update_hud("Anti-Mage · ур.14 · 12:34")
        cmd, text = overlay._queue.get_nowait()
        self.assertEqual(cmd, "HUD")
        self.assertIn("Anti-Mage", text)

    def test_app_hud_text_formatting(self) -> None:
        fake_self = SimpleNamespace(_catalog=_FakeCatalog())
        text = Assistant._overlay_hud_text(fake_self, GSI_PAYLOAD)
        self.assertIn("Anti-Mage", text)
        self.assertIn("ур.14", text)
        self.assertIn("12:34", text)
        self.assertIn("1450", text)
        self.assertIn("KDA 5/2/7", text)

    def test_settings_dialog_entry_points_removed(self) -> None:
        overlay_source = (ROOT / "aidota" / "overlay.py").read_text(encoding="utf-8")
        self.assertNotIn("SettingsDialog", overlay_source)
        self.assertNotIn("settings_btn", overlay_source)
        self.assertNotIn("_pulse_status", overlay_source)
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('"--settings"', main_source)
        self.assertNotIn("SettingsDialog", main_source)

    def test_version_bumped(self) -> None:
        self.assertEqual(__version__, "1.2.0")
        installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
        self.assertIn('MyAppVersion "1.2.0"', installer)


if __name__ == "__main__":
    unittest.main()
