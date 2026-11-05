"""Тесты веб-интерфейса и контроллера AidotaWebApi."""

import tempfile
import unittest
from pathlib import Path

from aidota.config import Config, load_config, save_config
from aidota.web_app import AidotaWebApi, WebGuiController


class TestWebApiController(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.config = Config()
        save_config(self.config, self.config_path)

        # Мокаем WebGuiController без запуска GUI цикла
        self.ctrl = WebGuiController(self.config, self.config_path)
        self.api = AidotaWebApi(self.ctrl)

    def tearDown(self):
        self.ctrl.tray.stop()
        self.temp_dir.cleanup()

    def test_get_state(self):
        state = self.api.get_state()
        self.assertIsInstance(state, dict)
        self.assertIn("coach", state)
        self.assertIn("position", state)
        self.assertIn("gsi", state)
        self.assertIn("audio_devices", state)
        self.assertEqual(state["coach"], "Астра")
        self.assertEqual(state["position"], 1)

    def test_set_coach(self):
        self.api.set_coach("Джарвис")
        self.assertEqual(self.ctrl.config.player_profile.coach_name, "Джарвис")
        self.assertEqual(self.ctrl.config.tts.edge_voice, "ru-RU-DmitryNeural")

        self.api.set_coach("Астра")
        self.assertEqual(self.ctrl.config.player_profile.coach_name, "Астра")
        self.assertEqual(self.ctrl.config.tts.edge_voice, "ru-RU-SvetlanaNeural")

    def test_set_position(self):
        self.api.set_position(4)
        self.assertEqual(self.ctrl.config.player_profile.default_position, 4)

    def test_save_settings(self):
        res = self.api.save_settings({
            "llm_base_url": "https://text.pollinations.ai/openai",
            "llm_model": "openai",
            "llm_api_key": "",
            "tts_provider": "edge",
            "edge_voice": "ru-RU-DmitryNeural",
            "autonomy_level": "high",
        })
        self.assertTrue(res.get("success"))
        self.assertEqual(self.ctrl.config.llm.base_url, "https://text.pollinations.ai/openai")
        self.assertEqual(self.ctrl.config.player_profile.autonomy_level, "high")

    def test_run_doctor(self):
        checks = self.api.run_doctor()
        self.assertIsInstance(checks, list)
        self.assertGreater(len(checks), 0)
        self.assertIn("name", checks[0])
        self.assertIn("status", checks[0])

    def test_get_draft_recommendation(self):
        res = self.api.get_draft_recommendation({
            "enemies": ["medusa", "storm_spirit"],
            "role": 1,
        })
        self.assertIsInstance(res, dict)
        self.assertIn("picks", res)
        self.assertGreater(len(res["picks"]), 0)
        self.assertEqual(res["picks"][0]["role"], 1)

    def test_get_hero_build_guide(self):
        res = self.api.get_hero_build_guide({
            "hero_name": "axe",
            "role": 3,
            "enemies": ["phantom_assassin"],
        })
        self.assertIsInstance(res, dict)
        self.assertIn("build", res)
        build = res["build"]
        self.assertEqual(build["hero_name"], "Axe")
        self.assertEqual(build["role"], 3)
        self.assertIn("starting", build)
        self.assertIn("core", build)

    def test_get_meta_heroes_list(self):
        heroes = self.api.get_meta_heroes_list()
        self.assertIsInstance(heroes, list)
        self.assertGreater(len(heroes), 5)
        self.assertIn("ru_name", heroes[0])
        self.assertIn("name", heroes[0])

    def test_get_live_draft_heroes(self):
        draft = self.api.get_live_draft_heroes()
        self.assertIsInstance(draft, dict)
        self.assertIn("enemies", draft)
        self.assertIn("allies", draft)

    def test_api_has_no_public_ctrl_attribute(self):
        """Проверяем, что js_api не содержит открытых ссылок на ctrl/window (защита от рекурсивной рефлексии pywebview)."""
        self.assertFalse(hasattr(self.api, "ctrl"))
        self.assertTrue(hasattr(self.api, "_ctrl"))
        public_members = [m for m in dir(self.api) if not m.startswith("_")]
        for m in public_members:
            attr = getattr(self.api, m)
            self.assertTrue(callable(attr), f"Публичный член {m} должен быть методом, а не объектом данных")

    def test_audio_devices_normalizes_tuple(self):
        import aidota.web_app as web_app_mod
        orig = web_app_mod.list_audio_devices
        try:
            web_app_mod.list_audio_devices = lambda: ([{"id": 0, "name": "Mic"}], [{"id": 1, "name": "Spk"}])
            self.ctrl._audio_cache = None
            devs = self.ctrl.audio_devices()
            self.assertIsInstance(devs, dict)
            self.assertIn("inputs", devs)
            self.assertIn("outputs", devs)
            self.assertEqual(len(devs["inputs"]), 1)
        finally:
            web_app_mod.list_audio_devices = orig

    def test_get_state_includes_rust_active(self):
        state = self.api.get_state()
        self.assertIn("rust_active", state)
        self.assertIsInstance(state["rust_active"], bool)


if __name__ == "__main__":
    unittest.main()
