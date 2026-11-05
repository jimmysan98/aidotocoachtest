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

    def test_parse_combinations(self):
        from aidota.hotkeys import parse_combination

        vk, alt, ctrl, shift = parse_combination("alt+z")
        self.assertEqual(vk, ord("Z"))
        self.assertTrue(alt)
        self.assertFalse(ctrl)
        self.assertFalse(shift)

        vk, alt, ctrl, shift = parse_combination("ctrl+shift+f1")
        self.assertEqual(vk, 0x70)
        self.assertTrue(ctrl)
        self.assertTrue(shift)
        self.assertFalse(alt)

        self.assertEqual(format_key_name("alt+z"), "Alt+Z")

    def test_parse_cyrillic_keysyms_and_case(self):
        self.assertEqual(parse_key("cyrillic_em"), ord("V"))
        self.assertEqual(parse_key("М"), ord("V"))
        self.assertEqual(parse_key("cyrillic_ya"), ord("Z"))
        self.assertEqual(parse_key("я"), ord("Z"))

    def test_hotkey_listener_scan_code(self):
        from aidota.hotkeys import HotkeyListener
        listener = HotkeyListener("v")
        self.assertEqual(listener.vk, ord("V"))
        # Физический скан-код для V/М на ПК равен 47 (0x2F)
        self.assertEqual(listener.scan_code, 47)
        listener.set_key("м")
        self.assertEqual(listener.vk, ord("V"))
        self.assertEqual(listener.scan_code, 47)

    def test_dispatch_kb_scancode_matching(self):
        import ctypes
        from aidota.hotkeys import HotkeyListener, KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP

        down_events = []
        up_events = []
        listener = HotkeyListener("v", on_down=lambda: down_events.append(1), on_up=lambda: up_events.append(1))

        # Эмулируем KBDLLHOOKSTRUCT: скан-код 47 (физическая клавиша V / М), даже если vkCode неизвестен
        kb = KBDLLHOOKSTRUCT()
        kb.vkCode = 0
        kb.scanCode = 47
        kb.flags = 0
        kb.time = 0
        kb.dwExtraInfo = None

        listener._dispatch_kb(0, WM_KEYDOWN, ctypes.addressof(kb))
        self.assertEqual(len(down_events), 1)

        listener._dispatch_kb(0, WM_KEYUP, ctypes.addressof(kb))
        self.assertEqual(len(up_events), 1)


if __name__ == "__main__":
    unittest.main()

