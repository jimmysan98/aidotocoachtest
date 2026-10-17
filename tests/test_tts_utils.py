"""Тесты вспомогательных функций озвучки и установки GSI."""

from __future__ import annotations

import unittest

from aidota.dota_install import gsi_config_text
from aidota.tts import sanitize_for_speech


class SanitizeTests(unittest.TestCase):
    def test_removes_markdown(self):
        text = sanitize_for_speech("**Blink** и _Manta_ — купить")
        self.assertNotIn("*", text)
        self.assertNotIn("_", text)
        self.assertIn("Blink", text)

    def test_removes_newlines(self):
        text = sanitize_for_speech("Первая строка\nвторая строка")
        self.assertNotIn("\n", text)
        self.assertIn("Первая строка. вторая строка", text)

    def test_truncates_long_text(self):
        text = sanitize_for_speech("слово " * 1000, max_chars=100)
        self.assertLessEqual(len(text), 130)
        self.assertIn("Подробности на экране", text)

    def test_short_text_unchanged(self):
        self.assertEqual(sanitize_for_speech("Привет"), "Привет")

    def test_empty(self):
        self.assertEqual(sanitize_for_speech("   "), "")


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
