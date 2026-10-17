"""Тесты конфигурации."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aidota.config import Config, ConfigError, load_config, save_config, save_llm_only
from tests.helpers import make_temp_dir, remove_temp_dir


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = make_temp_dir()

    def tearDown(self):
        remove_temp_dir(self.dir)

    def _write(self, data: dict, name: str = "config.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_defaults_created_when_missing(self):
        path = self.dir / "new.json"
        config = load_config(path)
        self.assertIsInstance(config, Config)
        self.assertTrue(path.is_file())
        self.assertEqual(config.gsi.port, 3000)
        self.assertEqual(config.audio.model_path, "vosk-model-ru-0.22")

    def test_partial_override_keeps_defaults(self):
        path = self._write({"gsi": {"port": 3010}, "ollama": {"model": "llama3"}})
        config = load_config(path)
        self.assertEqual(config.gsi.port, 3010)
        self.assertEqual(config.gsi.host, "127.0.0.1")  # из умолчаний
        self.assertEqual(config.ollama.model, "llama3")
        self.assertEqual(config.llm.model, "llama3")  # миграция из старого блока ollama
        self.assertEqual(config.overlay.width, 520)

    def test_type_coercion(self):
        path = self._write({"gsi": {"port": "3011"}, "tts": {"enabled": "false"}})
        config = load_config(path)
        self.assertEqual(config.gsi.port, 3011)
        self.assertIs(config.tts.enabled, False)

    def test_null_optional_value(self):
        path = self._write({"audio": {"input_device": None}})
        config = load_config(path)
        self.assertIsNone(config.audio.input_device)

    def test_bad_json(self):
        path = self.dir / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_bad_type(self):
        path = self._write({"gsi": {"port": "не число"}})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_save_roundtrip(self):
        config = Config()
        config.gsi.port = 3999
        path = self.dir / "saved.json"
        save_config(config, path)
        loaded = load_config(path)
        self.assertEqual(loaded.gsi.port, 3999)

    def test_paths_resolve_relative_to_project(self):
        config = Config()
        self.assertTrue(config.path("data").is_absolute())
        self.assertEqual(config.path(Path("C:/absolute/path")).as_posix(), "C:/absolute/path")

    def test_unknown_key_is_ignored(self):
        path = self._write({"unknown_section": {"x": 1}, "gsi": {"port": 3001}})
        config = load_config(path)  # печатает предупреждение в stderr
        self.assertEqual(config.gsi.port, 3001)

    # ------------------------------------------------------------------ #
    def test_llm_migrates_from_ollama(self):
        path = self._write({"ollama": {"model": "llama3", "host": "http://127.0.0.1:9999"}})
        config = load_config(path)
        self.assertEqual(config.llm.provider, "ollama")
        self.assertEqual(config.llm.model, "llama3")
        self.assertEqual(config.llm.base_url, "http://127.0.0.1:9999")

    def test_explicit_llm_not_overridden_by_ollama(self):
        path = self._write({
            "ollama": {"model": "llama3"},
            "llm": {"provider": "openai_compatible", "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1"},
        })
        config = load_config(path)
        self.assertEqual(config.llm.provider, "openai_compatible")
        self.assertEqual(config.llm.model, "deepseek-chat")
        self.assertEqual(config.llm.base_url, "https://api.deepseek.com/v1")

    def test_llm_presets_preserved(self):
        path = self._write({"llm": {"presets": {"deepseek": {"model": "deepseek-chat"}}}})
        config = load_config(path)
        self.assertEqual(config.llm.presets["deepseek"]["model"], "deepseek-chat")

    def test_player_profile(self):
        path = self._write({"player_profile": {
            "default_position": 3, "mmr_bracket": "Legend",
            "hero_pool": {"1": ["Slark"], "3": ["Axe", "Mars"]},
        }})
        config = load_config(path)
        self.assertEqual(config.player_profile.default_position, 3)
        self.assertEqual(config.player_profile.mmr_bracket, "Legend")
        self.assertEqual(config.player_profile.hero_pool["3"], ["Axe", "Mars"])

    def test_player_profile_pool_keys_coerced_to_str(self):
        path = self._write({"player_profile": {"hero_pool": {1: ["Slark"]}}})
        config = load_config(path)
        self.assertIn("1", config.player_profile.hero_pool)

    def test_invalid_position_raises(self):
        path = self._write({"player_profile": {"default_position": 9}})
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_save_llm_only_preserves_other_sections(self):
        path = self._write({
            "gsi": {"port": 4011},
            "tts": {"enabled": False},
            "llm": {"model": "gemma3"},
        })
        config = load_config(path)
        config.llm.model = "qwen2.5"
        save_llm_only(config, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["model"], "qwen2.5")
        self.assertEqual(raw["gsi"]["port"], 4011)  # не тронуто
        self.assertEqual(raw["tts"]["enabled"], False)  # не тронуто


if __name__ == "__main__":
    unittest.main()
