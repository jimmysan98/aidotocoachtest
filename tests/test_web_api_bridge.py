"""Тесты API-моста pywebview (AidotaWebApi) и обработки веб-запросов (Area 4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aidota.config import Config, save_config
from aidota.tts import SpeechPriority
from aidota.web_app import AidotaWebApi, WebGuiController


class TestWebSocketsAndApiBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.config = Config()
        save_config(self.config, self.config_path)

        self.ctrl = WebGuiController(self.config, self.config_path, enable_tray=False)
        self.api = AidotaWebApi(self.ctrl)

    def tearDown(self):
        self.api.close()
        if self.ctrl.tray:
            self.ctrl.tray.stop()
        self.temp_dir.cleanup()

    def test_get_live_advice_empty_and_valid(self):
        # Case a: raw_gsi_payload returns None -> returns []
        self.ctrl.raw_gsi_payload = lambda: None
        self.assertEqual(self.api.get_live_advice(), [])

        # Case b: valid payload -> returns advice list
        sample_payload = {
            "available": True,
            "map": {"clock_time": 600, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "hero": {"name": "npc_dota_hero_antimage"},
            "draft": {"team3": {"pick_ids": [1]}},  # PA enemy
        }
        self.ctrl.raw_gsi_payload = lambda: sample_payload
        mock_advice = [
            {"item": "Monkey King Bar", "cost": 4050, "reason": "против PA", "affordable": True}
        ]
        with patch("aidota.web_app.build_item_advice", return_value=mock_advice):
            adv = self.api.get_live_advice()
            self.assertEqual(len(adv), 1)
            self.assertEqual(adv[0]["item"], "Monkey King Bar")

    def test_get_live_advice_resilience_on_exception(self):
        self.ctrl.raw_gsi_payload = lambda: {"available": True}
        with patch("aidota.web_app.build_item_advice", side_effect=RuntimeError("corrupted data")):
            res = self.api.get_live_advice()
            self.assertEqual(res, [])

    def test_get_hero_icon_memory_cache_hit(self):
        self.api._hero_icon_cache["antimage"] = "data:image/png;base64,CACHED_AM"
        uri = self.api.get_hero_icon("npc_dota_hero_antimage")
        self.assertEqual(uri, "data:image/png;base64,CACHED_AM")

    def test_get_hero_icon_invalid_inputs_safety(self):
        for bad_input in (None, "", "   ", 12345, "!@#$%^&*()"):
            self.assertEqual(self.api.get_hero_icon(bad_input), "")

    def test_get_hero_icon_broken_config_resilience(self):
        with patch.object(self.api, "_hero_icon_dirs", side_effect=OSError("Access denied")):
            uri = self.api.get_hero_icon("axe")
            # Should not raise exception; either empty or cdn url fallback
            self.assertIsInstance(uri, str)

    def test_get_item_icon_memory_cache_hit(self):
        self.api._item_icon_cache["blink"] = "data:image/png;base64,BLINK_CACHED"
        uri = self.api.get_item_icon("item_blink")
        self.assertEqual(uri, "data:image/png;base64,BLINK_CACHED")

    def test_get_item_icon_offline_fallback_does_not_pollute_cache(self):
        # Local file doesn't exist, download fails
        with patch("aidota.web_app.read_image_data_uri", return_value=""), \
             patch("aidota.web_app.download_image", return_value=False), \
             patch("aidota.web_app.item_icon_url", return_value="https://cdn.dota2.com/blink.png"):
            url = self.api.get_item_icon("blink")
            self.assertEqual(url, "https://cdn.dota2.com/blink.png")
            # Cache must remain empty for retry on reconnection
            self.assertNotIn("blink", self.api._item_icon_cache)

    def test_get_item_icon_invalid_and_empty_keys(self):
        for bad_key in ("", None, "   "):
            self.assertEqual(self.api.get_item_icon(bad_key), "")

    def test_save_settings_url_and_model_fallbacks(self):
        # 1) С sk- ключом и пустыми URL/моделью автодетект ставит DeepSeek (не утекает в Pollinations).
        self.ctrl.config.llm.base_url = ""
        self.ctrl.config.llm.model = ""

        res = self.api.save_settings({
            "llm_base_url": "",
            "llm_model": "",
            "llm_api_key": "sk-test",
        })
        self.assertTrue(res.get("success"))
        self.assertEqual(self.ctrl.config.llm.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(self.ctrl.config.llm.model, "deepseek-chat")
        self.assertEqual(self.ctrl.config.llm.api_key, "sk-test")

        # 2) Без API-ключа и без URL/модели сохраняется бесплатный резерв Pollinations.
        self.ctrl.config.llm.base_url = ""
        self.ctrl.config.llm.model = ""
        self.ctrl.config.llm.api_key = ""
        res_free = self.api.save_settings({
            "llm_base_url": "",
            "llm_model": "",
            "llm_api_key": "",
        })
        self.assertTrue(res_free.get("success"))
        self.assertEqual(self.ctrl.config.llm.base_url, "https://text.pollinations.ai/openai")
        self.assertEqual(self.ctrl.config.llm.model, "gemini")

    def test_save_settings_skill_level_normalization_and_propagation(self):
        mock_assistant = MagicMock()
        self.ctrl.assistant = mock_assistant

        res = self.api.save_settings({
            "skill_level": "grandmaster",  # invalid -> normalizes to beginner
            "autonomy_level": "high",
        })
        self.assertTrue(res.get("success"))
        self.assertEqual(self.ctrl.config.player_profile.skill_level, "beginner")
        mock_assistant._agent.set_autonomy_level.assert_called_with("high")
        mock_assistant._coach.set_skill_level.assert_called_with("beginner")

    def test_save_settings_exception_returns_failure_dict(self):
        with patch("aidota.web_app.save_config", side_effect=PermissionError("Disk write-protected")):
            res = self.api.save_settings({"llm_model": "test"})
            self.assertFalse(res.get("success"))
            self.assertIn("Disk write-protected", res.get("message"))

    def test_test_voice_active_assistant_vs_standalone_tts(self):
        # a) Active assistant with _tts
        mock_assistant = MagicMock()
        mock_assistant._tts.speak.return_value = True
        self.ctrl.assistant = mock_assistant

        ok1 = self.api.test_voice("Тест фразы")
        self.assertTrue(ok1)
        mock_assistant._tts.speak.assert_called_once_with("Тест фразы")

        # b) Without assistant -> fallback Tts class
        self.ctrl.assistant = None
        with patch("aidota.tts.Tts") as mock_tts_cls:
            mock_instance = MagicMock()
            mock_tts_cls.return_value = mock_instance
            ok2 = self.api.test_voice("")
            self.assertTrue(ok2)
            mock_instance.start.assert_called_once()
            mock_instance.speak.assert_called_once_with("Тест звуковой системы AIDOTA PRO")

    def test_send_chat_empty_query_and_active_coach(self):
        # a) Empty query
        reply_empty = self.api.send_chat("   ")
        self.assertEqual(reply_empty, "Да, я на связи. Что подсказать?")

        # b) Active assistant with coach
        mock_assistant = MagicMock()
        mock_assistant._coach.coach_chat.return_value = "Купи BKB против контроля."
        self.ctrl.assistant = mock_assistant

        reply = self.api.send_chat("что купить?")
        self.assertEqual(reply, "Купи BKB против контроля.")
        mock_assistant._coach.coach_chat.assert_called_once_with("что купить?")
        mock_assistant._tts.speak.assert_called_once_with(
            "Купи BKB против контроля.",
            wait=False,
            priority=SpeechPriority.LLM_COACH,
        )

    def test_toggle_assistant_and_toggle_overlay(self):
        # toggle_assistant: when not running -> starts
        self.ctrl.is_running = False
        with patch.object(self.ctrl, "start_assistant") as mock_start:
            res = self.api.toggle_assistant()
            self.assertTrue(res)
            mock_start.assert_called_once()

        # toggle_assistant: when running -> stops
        self.ctrl.is_running = True
        with patch.object(self.ctrl, "stop_assistant") as mock_stop:
            res = self.api.toggle_assistant()
            self.assertFalse(res)
            mock_stop.assert_called_once()

        # toggle_overlay: with overlay
        mock_assistant = MagicMock()
        self.ctrl.assistant = mock_assistant
        ok_overlay = self.api.toggle_overlay()
        self.assertTrue(ok_overlay)
        mock_assistant._overlay.toggle.assert_called_once()

        # toggle_overlay: without overlay
        self.ctrl.assistant = None
        self.assertFalse(self.api.toggle_overlay())

    def test_minimize_to_tray_and_close(self):
        # a) Window exists
        mock_window = MagicMock()
        self.ctrl.window = mock_window
        self.assertTrue(self.api.minimize_to_tray())
        mock_window.minimize.assert_called_once()

        # b) Window minimize raises exception
        mock_window.minimize.side_effect = RuntimeError("Window error")
        self.assertFalse(self.api.minimize_to_tray())

        # c) Window is None
        self.ctrl.window = None
        self.assertFalse(self.api.minimize_to_tray())

        # d) close() cleans up _icon_session_obj
        mock_session = MagicMock()
        self.api._icon_session_obj = mock_session
        self.api.close()
        mock_session.close.assert_called_once()
        self.assertIsNone(self.api._icon_session_obj)


if __name__ == "__main__":
    unittest.main()
