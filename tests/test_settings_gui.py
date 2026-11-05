import unittest
from aidota.config import Config
from aidota.settings_gui import SettingsDialog


class TestSettingsGui(unittest.TestCase):
    def test_position_and_toggle_key(self) -> None:
        cfg = Config()
        cfg.player_profile.default_position = 3  # Оффлейн
        cfg.overlay.toggle_key = "alt+z"

        saved_data = {}

        def on_save(data: dict) -> None:
            nonlocal saved_data
            saved_data = data

        try:
            dlg = SettingsDialog(None, cfg, on_save=on_save)
            # Проверяем, что позиция загрузилась (3 -> index 2)
            self.assertEqual(dlg.position_combo.current(), 2)
            self.assertIn("3 — Оффлейн", dlg.position_combo.get())

            # Проверяем, что хоткей загрузился
            self.assertEqual(dlg.toggle_key_entry.get(), "Alt+Z")

            # Меняем позицию на Позицию 5 (Хард-саппорт / Полная поддержка -> index 4)
            dlg.position_combo.current(4)

            # Выбираем пресет
            dlg.toggle_preset_combo.set("F8")
            dlg._on_toggle_preset_selected()
            self.assertEqual(dlg.toggle_key_entry.get(), "F8")

            # Выбираем голос Джарвис (Нейросеть Дмитрий -> index 1)
            dlg.voice_combo.current(1)

            # Сохраняем настройки
            dlg._save_settings()

            self.assertEqual(saved_data.get("default_position"), 5)
            self.assertEqual(saved_data.get("toggle_key"), "F8")
            self.assertEqual(saved_data.get("coach_name"), "Джарвис")
            self.assertEqual(saved_data.get("edge_voice"), "ru-RU-DmitryNeural")
        finally:
            if 'dlg' in locals() and hasattr(dlg, "window"):
                dlg.window.destroy()


if __name__ == "__main__":
    unittest.main()
