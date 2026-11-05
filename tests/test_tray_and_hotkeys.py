"""Тесты горячих клавиш, системного трея и интеграции с Windows."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from aidota.dota_install import acquire_single_instance, is_autostart_enabled, set_autostart
from aidota.hotkeys import MOD_ALT, MOD_NOREPEAT, format_key_name, parse_hotkey
from aidota.tray import AidotaTray


class TestHotkeysAndLayout(unittest.TestCase):
    def test_parse_hotkey_vk_code(self) -> None:
        """Проверка разбора Alt+Z в нативный Win32 vkCode."""
        mods, vk = parse_hotkey("alt+z")
        self.assertEqual(mods, MOD_ALT | MOD_NOREPEAT)
        self.assertEqual(vk, 0x5A)  # VK_Z = 0x5A

    def test_layout_independence_ru_en(self) -> None:
        """Проверка независимости от русской раскладки клавиатуры: 'Alt+Я' == 'Alt+Z'."""
        mods_en, vk_en = parse_hotkey("alt+z")
        mods_ru, vk_ru = parse_hotkey("alt+я")
        self.assertEqual(mods_en, mods_ru)
        self.assertEqual(vk_en, vk_ru)

    def test_format_key_name(self) -> None:
        """Форматирование названий клавиш для UI."""
        self.assertEqual(format_key_name("alt+z"), "Alt+Z")
        self.assertEqual(format_key_name("v"), "V")
        self.assertEqual(format_key_name("f8"), "F8")


class TestWindowsIntegration(unittest.TestCase):
    def test_single_instance_mutex(self) -> None:
        """Проверка получения уникального мьютекса (первый запуск успешен)."""
        import uuid
        import ctypes
        mutex_name = f"Local\\Test_AIDOTA_{uuid.uuid4().hex}"
        has_mutex, handle = acquire_single_instance(mutex_name)
        try:
            self.assertTrue(has_mutex)
        finally:
            if handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass

    @patch("winreg.OpenKey")
    def test_autostart_check_mock(self, mock_open) -> None:
        """Проверка статуса автозапуска через winreg."""
        mock_open.return_value.__enter__.return_value = MagicMock()
        # Не должно вызывать исключений
        res = is_autostart_enabled()
        self.assertIsInstance(res, bool)


class TestTrayStructure(unittest.TestCase):
    def test_tray_menu_construction(self) -> None:
        """Проверка корректной сборки меню системного трея."""
        coach_val = "Астра"
        pos_val = 1
        mic_val = True

        tray = AidotaTray(
            get_coach=lambda: coach_val,
            set_coach=lambda name: None,
            get_position=lambda: pos_val,
            set_position=lambda pos: None,
            get_mic_enabled=lambda: mic_val,
            toggle_mic=lambda: None,
        )

        menu = tray._build_menu()
        self.assertIsNotNone(menu)
        items = list(menu.items)
        # Проверяем ключевые пункты меню
        item_texts = [i.text for i in items if hasattr(i, "text")]
        self.assertIn("Открыть AIDOTA", item_texts)
        self.assertIn("Показать / Скрыть игровой оверлей", item_texts)
        self.assertIn("Тренер", item_texts)
        self.assertIn("Моя позиция", item_texts)
        self.assertIn("Микрофон", item_texts)
        self.assertIn("⚙ Настройки", item_texts)
        self.assertIn("🩺 Диагностика", item_texts)
        self.assertIn("Полный выход", item_texts)


if __name__ == "__main__":
    unittest.main()
