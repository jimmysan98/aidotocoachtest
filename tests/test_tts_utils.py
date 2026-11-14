"""Тесты вспомогательных функций озвучки и установки GSI."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import numpy as np

from aidota.config import TtsConfig
from aidota.dota_install import gsi_config_text
from aidota.tts import (
    FILLERS,
    HumanTTSManager,
    SmartAudioCache,
    SpeechPriority,
    normalize_for_speech,
    sanitize_for_speech,
)


class NormalizeForSpeechTests(unittest.TestCase):
    def test_removes_markdown_and_brackets(self):
        text = normalize_for_speech("**Blink** и _Manta_ [Dagger] (BKB) — купить! 🔥")
        self.assertNotIn("*", text)
        self.assertNotIn("_", text)
        self.assertNotIn("[", text)
        self.assertNotIn("]", text)
        self.assertNotIn("(", text)
        self.assertNotIn(")", text)
        self.assertNotIn("🔥", text)
        self.assertIn("Blink", text)
        self.assertIn("Manta", text)

    def test_dota_phonetics_replacements(self):
        # бкб / bkb -> бэкабэ
        self.assertIn("бэкабэ", normalize_for_speech("купи бкб и BKB"))
        # мкб / mkb -> эмкабэ
        self.assertIn("эмкабэ", normalize_for_speech("собери мкб и MKB"))
        # тп / tp -> тэпэ
        self.assertIn("тэпэ", normalize_for_speech("делай тп и TP на базу"))
        # хп / hp -> хэпэ
        self.assertIn("хэпэ", normalize_for_speech("мало хп и HP"))
        # мп / mp -> маны
        self.assertIn("маны", normalize_for_speech("нет мп и MP"))
        # кд / cd -> кадэ
        self.assertIn("кадэ", normalize_for_speech("ульта на кд и CD"))
        # сс / ss -> мисс
        self.assertIn("мисс", normalize_for_speech("в миде сс и SS"))
        # гпм / gpm -> гэпээм
        self.assertIn("гэпээм", normalize_for_speech("высокий гпм и GPM"))
        # аганим -> агани+м, рошан -> роша+н, торментор -> торме+нтор
        self.assertIn("агани+м", normalize_for_speech("купи аганим"))
        self.assertIn("роша+н", normalize_for_speech("появился рошан"))
        self.assertIn("торме+нтор", normalize_for_speech("заберите торментор"))

    def test_intonation_pauses(self):
        text = normalize_for_speech("План: купить смоки; забрать Рошана")
        self.assertIn(" — ", text)
        self.assertNotIn(":", text)
        self.assertNotIn(";", text)

    def test_removes_newlines(self):
        text = normalize_for_speech("Первая строка\nвторая строка")
        self.assertNotIn("\n", text)
        self.assertIn("Первая строка. вторая строка", text)

    def test_truncates_long_text(self):
        text = normalize_for_speech("слово " * 1000, max_chars=100)
        self.assertLessEqual(len(text), 130)
        self.assertIn("Подробности на экране", text)

    def test_short_text_unchanged(self):
        self.assertEqual(normalize_for_speech("Привет"), "Привет")

    def test_empty(self):
        self.assertEqual(normalize_for_speech("   "), "")


class SmartCacheAndPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp_dir.name)
        self.cache = SmartAudioCache(self.cache_path)

    def tearDown(self):
        if hasattr(self, "cache"):
            self.cache.wait_pending(timeout=1.0)
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_smart_cache_put_and_get(self):
        dummy_data = np.zeros(1000, dtype=np.float32)
        sr = 48000

        # Сначала промах
        self.assertIsNone(self.cache.get("руна мудрости", "dmitry", "+10%", "-3Hz"))

        # Сохраняем в кэш
        self.cache.put("руна мудрости", "dmitry", "+10%", "-3Hz", dummy_data, sr)

        # Мгновенное чтение из RAM
        cached = self.cache.get("руна мудрости", "dmitry", "+10%", "-3Hz")
        self.assertIsNotNone(cached)
        res_data, res_sr = cached
        self.assertEqual(res_sr, sr)
        self.assertEqual(len(res_data), len(dummy_data))

    def test_priority_queue_order(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg, self.cache_path)

        # Проверяем, что CRITICAL=1 имеет наивысший приоритет
        self.assertLess(SpeechPriority.CRITICAL, SpeechPriority.TIMERS)
        self.assertLess(SpeechPriority.TIMERS, SpeechPriority.LLM_COACH)

    def test_fillers_list_not_empty(self):
        self.assertTrue(len(FILLERS) >= 3)
        self.assertIn("Так, ща глянем...", FILLERS)

    def test_rvc_disabled_returns_original(self):
        cfg = TtsConfig(enabled=True, rvc_enabled=False)
        mgr = HumanTTSManager(cfg, self.cache_path)
        data = np.ones(500, dtype=np.float32)
        out_data, out_sr = mgr.apply_rvc_if_enabled(data, 48000)
        self.assertEqual(out_sr, 48000)
        np.testing.assert_array_equal(out_data, data)


class GsiConfigTests(unittest.TestCase):
    def test_contains_token_and_port(self):
        text = gsi_config_text("secret-token", 3000)
        self.assertIn('"token" "secret-token"', text)
        self.assertIn("http://127.0.0.1:3000/", text)
        self.assertIn('"draft" "1"', text)
        self.assertIn('"hero" "1"', text)

    def test_custom_port(self):
        self.assertIn(":4123/", gsi_config_text("t", 4123))

    def test_valid_braces(self):
        text = gsi_config_text("t", 3000)
        self.assertEqual(text.count("{"), text.count("}"))


if __name__ == "__main__":
    unittest.main()

