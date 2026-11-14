"""Тесты интеграции с платформой Windows, иконок и системного трея (Area 5)."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aidota.tray import AidotaTray
from aidota.windows_icon import (
    apply_window_icon,
    get_app_icon_path,
    setup_window_handle,
)


class TestWindowsIconAndHwndRobustness(unittest.TestCase):
    def test_get_app_icon_path_pyinstaller_meipass(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = Path(tmp_dir)
            assets_dir = temp_path / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            icon_file = assets_dir / "icon.ico"
            icon_file.write_bytes(b"MOCK_ICO_DATA")

            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "_MEIPASS", str(temp_path), create=True):
                resolved = get_app_icon_path()
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved, icon_file.resolve())

    def test_get_app_icon_path_pyinstaller_internal_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = Path(tmp_dir)
            internal_assets = temp_path / "_internal" / "assets"
            internal_assets.mkdir(parents=True, exist_ok=True)
            icon_file = internal_assets / "icon.ico"
            icon_file.write_bytes(b"MOCK_INTERNAL_ICO")
            fake_exe = temp_path / "AIDOTA.exe"

            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)):
                # Ensure no _MEIPASS is present
                if hasattr(sys, "_MEIPASS"):
                    delattr(sys, "_MEIPASS")
                resolved = get_app_icon_path()
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved, icon_file.resolve())

    def test_get_app_icon_path_missing_all_candidates(self):
        with patch("pathlib.Path.is_file", return_value=False):
            self.assertIsNone(get_app_icon_path())

    def test_apply_window_icon_non_win32_platform(self):
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(apply_window_icon(12345))

    def test_apply_window_icon_closed_or_invalid_hwnd(self):
        mock_user32 = MagicMock()
        mock_user32.IsWindow.return_value = 0
        with patch.object(sys, "platform", "win32"), \
             patch("ctypes.windll.user32", mock_user32):
            self.assertFalse(apply_window_icon(99999))
            mock_user32.IsWindow.assert_called_with(99999)
            mock_user32.LoadImageW.assert_not_called()

    def test_apply_window_icon_load_image_failure(self):
        mock_user32 = MagicMock()
        mock_user32.IsWindow.return_value = 1
        mock_user32.LoadImageW.return_value = 0
        with patch.object(sys, "platform", "win32"), \
             patch("ctypes.windll.user32", mock_user32):
            self.assertFalse(apply_window_icon(12345))

    def test_apply_window_icon_success_and_setclasslongptr_fallback(self):
        mock_user32 = MagicMock()
        mock_user32.IsWindow.return_value = 1
        mock_user32.LoadImageW.side_effect = [3232, 1616]
        # Simulate SetClassLongPtrW failure and fallback to SetClassLongW
        mock_user32.SetClassLongPtrW.side_effect = AttributeError("SetClassLongPtrW not found")
        mock_user32.SetClassLongW.return_value = 1

        with patch.object(sys, "platform", "win32"), \
             patch("ctypes.windll.user32", mock_user32):
            res = apply_window_icon(12345)
            self.assertTrue(res)
            self.assertEqual(mock_user32.SendMessageW.call_count, 2)

    def test_setup_window_handle_toint64_fallback_to_toint32(self):
        mock_window = MagicMock()
        mock_window.native.Handle.ToInt64.side_effect = OverflowError("Int64 overflow")
        mock_window.native.Handle.ToInt32.return_value = 4242

        with patch.object(sys, "platform", "win32"), \
             patch("aidota.windows_icon.apply_window_icon", return_value=True) as mock_apply:
            res = setup_window_handle(mock_window)
            self.assertTrue(res)
            mock_apply.assert_called_once_with(4242)

    def test_setup_window_handle_invalid_tk_winfo_id(self):
        mock_window = MagicMock(spec=["winfo_id"])
        mock_window.winfo_id.side_effect = ValueError("not a valid window")

        with patch.object(sys, "platform", "win32"):
            self.assertFalse(setup_window_handle(mock_window))


class TestSystemTrayIntegration(unittest.TestCase):
    def test_tray_load_image_fallback_procedural_generation(self):
        tray = AidotaTray(icon_path="non_existent_icon_path.png")
        img = tray._load_image()
        self.assertIsNotNone(img)
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.mode, "RGBA")

    def test_tray_load_image_when_pystray_or_pillow_missing(self):
        with patch("aidota.tray._PYSTRAY_AVAILABLE", False):
            tray = AidotaTray()
            self.assertIsNone(tray._load_image())

    def test_tray_menu_actions_show_and_overlay(self):
        mock_show = MagicMock()
        mock_overlay = MagicMock()
        tray = AidotaTray(on_show=mock_show, on_toggle_overlay=mock_overlay)
        menu = tray._build_menu()
        self.assertIsNotNone(menu)

        item_map = {getattr(it, "text", ""): it for it in menu.items}
        self.assertIn("Открыть AIDOTA", item_map)
        self.assertIn("Показать / Скрыть игровой оверлей", item_map)

        # Trigger show action
        item_map["Открыть AIDOTA"](None)
        mock_show.assert_called_once()

        # Trigger overlay action
        item_map["Показать / Скрыть игровой оверлей"](None)
        mock_overlay.assert_called_once()

    def test_tray_menu_actions_coach_and_position(self):
        mock_set_coach = MagicMock()
        mock_set_pos = MagicMock()
        curr_coach = ["Астра"]
        curr_pos = [1]

        tray = AidotaTray(
            get_coach=lambda: curr_coach[0],
            set_coach=mock_set_coach,
            get_position=lambda: curr_pos[0],
            set_position=mock_set_pos,
        )
        menu = tray._build_menu()
        item_map = {getattr(it, "text", ""): it for it in menu.items}

        with patch.object(tray, "update_menu") as mock_update:
            coach_menu = item_map["Тренер"].submenu
            jarvis_item = [it for it in coach_menu.items if "Джарвис" in it.text][0]
            astra_item = [it for it in coach_menu.items if "Астра" in it.text][0]

            self.assertTrue(astra_item.checked)
            self.assertFalse(jarvis_item.checked)

            jarvis_item(None)
            mock_set_coach.assert_called_once_with("Джарвис")
            mock_update.assert_called_once()

            pos_menu = item_map["Моя позиция"].submenu
            pos3_item = [it for it in pos_menu.items if it.text.startswith("3")][0]
            pos1_item = [it for it in pos_menu.items if it.text.startswith("1")][0]

            self.assertTrue(pos1_item.checked)
            self.assertFalse(pos3_item.checked)

            pos3_item(None)
            mock_set_pos.assert_called_once_with(3)

    def test_tray_menu_actions_autonomy_and_mic(self):
        mock_set_autonomy = MagicMock()
        mock_toggle_mic = MagicMock()
        mic_state = [True]

        tray = AidotaTray(
            get_autonomy=lambda: "medium",
            set_autonomy=mock_set_autonomy,
            get_mic_enabled=lambda: mic_state[0],
            toggle_mic=mock_toggle_mic,
        )
        menu = tray._build_menu()
        item_map = {getattr(it, "text", ""): it for it in menu.items}

        # Mic action
        mic_item = item_map["Микрофон"]
        self.assertTrue(mic_item.checked)
        with patch.object(tray, "update_menu"):
            mic_item(None)
            mock_toggle_mic.assert_called_once()

        # Autonomy submenu
        autonomy_menu = item_map["Автономность тренера"].submenu
        high_item = [it for it in autonomy_menu.items if it.text.startswith("Высокая")][0]
        medium_item = [it for it in autonomy_menu.items if it.text.startswith("Средняя")][0]

        self.assertTrue(medium_item.checked)
        self.assertFalse(high_item.checked)

        with patch.object(tray, "update_menu"):
            high_item(None)
            mock_set_autonomy.assert_called_once_with("high")

    def test_tray_lifecycle_start_stop_idempotent(self):
        # a) Unavailable pystray
        with patch("aidota.tray._PYSTRAY_AVAILABLE", False):
            tray = AidotaTray()
            self.assertFalse(tray.start())

        # b) Already running icon
        tray_running = AidotaTray()
        mock_icon = MagicMock()
        tray_running._icon = mock_icon
        self.assertTrue(tray_running.start())

        # c) Stop resets icon
        tray_running.stop()
        mock_icon.stop.assert_called_once()
        self.assertIsNone(tray_running._icon)

    def test_tray_notify_minimized_safe_handling(self):
        tray = AidotaTray()
        mock_icon = MagicMock()
        tray._icon = mock_icon

        tray.notify_minimized("Тест Заголовок", "Тест Сообщение")
        mock_icon.notify.assert_called_once_with("Тест Сообщение", "Тест Заголовок")

        # Exception suppression
        mock_icon.notify.side_effect = RuntimeError("Notify failed")
        tray.notify_minimized("Тест", "Сбой")  # Does not raise


if __name__ == "__main__":
    unittest.main()
