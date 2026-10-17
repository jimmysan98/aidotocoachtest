"""Тесты парсинга и форматирования клавиш push-to-talk."""

from __future__ import annotations

import unittest

from aidota.hotkeys import (
    VK_MBUTTON,
    VK_XBUTTON1,
    VK_XBUTTON2,
    format_key_name,
    parse_key,
)


class HotkeyTests(unittest.TestCase):
    def test_parse_alphanumeric(self):
        self.assertEqual(parse_key("v"), ord("V"))
        self.assertEqual(parse_key("V"), ord("V"))
        self.assertEqual(parse_key("c"), ord("C"))
        self.assertEqual(parse_key("1"), ord("1"))

    def test_parse_russian_layout(self):
        # 'м' соответствует 'v'
        self.assertEqual(parse_key("м"), ord("V"))
        # 'с' соответствует 'c'
        self.assertEqual(parse_key("с"), ord("C"))

    def test_parse_f_keys(self):
        self.assertEqual(parse_key("f1"), 0x70)
        self.assertEqual(parse_key("f8"), 0x77)
        self.assertEqual(parse_key("F12"), 0x7B)
        self.assertEqual(parse_key("ф8"), 0x77)

    def test_parse_named_keys(self):
        self.assertEqual(parse_key("capslock"), 0x14)
        self.assertEqual(parse_key("space"), 0x20)
        self.assertEqual(parse_key("alt"), 0x12)
        self.assertEqual(parse_key("ctrl"), 0x11)
        self.assertEqual(parse_key("shift"), 0x10)

    def test_parse_mouse_buttons(self):
        self.assertEqual(parse_key("mouse4"), VK_XBUTTON1)
        self.assertEqual(parse_key("m4"), VK_XBUTTON1)
        self.assertEqual(parse_key("mouse5"), VK_XBUTTON2)
        self.assertEqual(parse_key("m5"), VK_XBUTTON2)
        self.assertEqual(parse_key("mouse3"), VK_MBUTTON)
        self.assertEqual(parse_key("middle"), VK_MBUTTON)

    def test_format_key_name(self):
        self.assertEqual(format_key_name("v"), "V")
        self.assertEqual(format_key_name("f8"), "F8")
        self.assertEqual(format_key_name("capslock"), "Caps Lock")
        self.assertIn("Mouse 4", format_key_name("mouse4"))
        self.assertIn("Mouse 5", format_key_name("mouse5"))

    def test_invalid_key_raises(self):
        with self.assertRaises(ValueError):
            parse_key("nonexistent_key_12345")


if __name__ == "__main__":
    unittest.main()
