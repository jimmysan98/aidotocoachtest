"""Тесты расшифровки встроенного API-ключа и сброса конфигурации к дефолтным значениям."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aidota.config import (
    Config,
    deobfuscate_api_key,
    load_config,
    load_default_preset,
    obfuscate_api_key,
    reset_to_defaults,
    validate_audio_devices,
)


class TestConfigPreset(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="aidota_test_preset_"))
        self.config_path = self.temp_dir / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_obfuscation_roundtrip(self) -> None:
        """Проверка симметричной обфускации и деобфускации API-ключа."""
        sample_key = "sk-or-v1-abcdef1234567890_test_key"
        encoded = obfuscate_api_key(sample_key)
        self.assertTrue(encoded.startswith("enc:"))
        self.assertNotEqual(encoded, sample_key)

        decoded = deobfuscate_api_key(encoded)
        self.assertEqual(decoded, sample_key)

    def test_deobfuscate_plain_text(self) -> None:
        """Если ключ не обфусцирован (обычный текст), он возвращается без изменений."""
        plain_key = "sk-plain-text-key"
        self.assertEqual(deobfuscate_api_key(plain_key), plain_key)
        self.assertEqual(deobfuscate_api_key(""), "")

    def test_default_preset_structure(self) -> None:
        """Эталонный пресет не должен содержать секретов и указывает на bundled-модель."""
        preset = load_default_preset()
        self.assertIsInstance(preset, dict)
        self.assertIn("gsi", preset)
        self.assertIn("audio", preset)
        self.assertIn("llm", preset)
        self.assertIn("player_profile", preset)

        llm = preset.get("llm", {})
        self.assertEqual(llm.get("api_key", ""), "", "Пресет не должен содержать API-ключ")
        for name, item in (llm.get("presets") or {}).items():
            if isinstance(item, dict):
                self.assertEqual(item.get("api_key", ""), "", f"Ключ в пресете {name} должен быть пустым")

        json_text = json.dumps(preset, ensure_ascii=False)
        self.assertNotIn("sk-or-v1", json_text)
        self.assertNotIn("enc:", json_text)

        # Модель Vosk в пресете — компактная, она упаковывается в установщик.
        self.assertEqual(preset["audio"]["model_path"], "vosk-model-small-ru-0.22")
        self.assertIn(preset["player_profile"]["default_position"], (1, 2, 3, 4, 5))

    def test_reset_to_defaults(self) -> None:
        """Кнопка 'Сбросить по умолчанию' корректно восстанавливает config.json из пресета."""
        # Создаем кастомный повреждённый/изменённый конфиг
        custom_data = {
            "gsi": {"port": 9999},
            "llm": {"model": "custom-broken-model", "api_key": "wrong_key"},
            "player_profile": {"coach_name": "Неизвестный", "default_position": 4},
        }
        self.config_path.write_text(json.dumps(custom_data), encoding="utf-8")

        # Выполняем сброс
        cfg = reset_to_defaults(self.config_path)
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.gsi.port, 3000)
        self.assertEqual(cfg.player_profile.coach_name, "Астра")
        self.assertEqual(cfg.llm.api_key, "", "Сброс к пресету не должен подставлять API-ключ")

        # Проверяем, что файл на диске также перезаписан
        disk_cfg = load_config(self.config_path)
        self.assertEqual(disk_cfg.gsi.port, 3000)
        self.assertEqual(disk_cfg.llm.model, "openai/gpt-4o-mini")

    def test_audio_device_fallback(self) -> None:
        """Несуществующее аудиоустройство автоматически сбрасывается на None (системное по умолчанию)."""
        cfg = Config()
        cfg.audio.input_device = 99999  # заведомо несуществующий ID
        cfg.tts.output_device = 88888   # заведомо несуществующий ID

        changed = validate_audio_devices(cfg)
        self.assertTrue(changed)
        self.assertIsNone(cfg.audio.input_device)
        self.assertIsNone(cfg.tts.output_device)


if __name__ == "__main__":
    unittest.main()
