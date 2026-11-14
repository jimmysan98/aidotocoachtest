"""Тесты для модуля aidota.windows_icon (интеграция иконки в Windows)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aidota.windows_icon import (
    APP_USER_MODEL_ID,
    apply_window_icon,
    get_app_icon_path,
    set_app_user_model_id,
    setup_window_handle,
)


class TestWindowsIcon(unittest.TestCase):
    def test_app_user_model_id_defined(self):
        self.assertTrue(APP_USER_MODEL_ID.startswith("AIDOTA.Dota2Coach.App."))

    def test_get_app_icon_path(self):
        icon_path = get_app_icon_path()
        self.assertIsNotNone(icon_path)
        self.assertTrue(isinstance(icon_path, Path))
        self.assertTrue(icon_path.is_file())
        self.assertEqual(icon_path.suffix.lower(), ".ico")

    def test_set_app_user_model_id(self):
        result = set_app_user_model_id()
        if sys.platform == "win32":
            self.assertTrue(result)
        else:
            self.assertFalse(result)

    def test_apply_window_icon_invalid_hwnd(self):
        self.assertFalse(apply_window_icon(0))
        self.assertFalse(apply_window_icon(-1))

    def test_setup_window_handle_none(self):
        self.assertFalse(setup_window_handle(None))

    def test_setup_window_handle_pywebview_mock(self):
        mock_window = MagicMock()
        mock_window.native.Handle.ToInt64.return_value = 12345
        with patch("aidota.windows_icon.apply_window_icon", return_value=True) as mock_apply:
            result = setup_window_handle(mock_window)
            if sys.platform == "win32":
                mock_apply.assert_called_once_with(12345)
                self.assertTrue(result)
            else:
                self.assertFalse(result)

    def test_setup_window_handle_tk_mock(self):
        mock_window = MagicMock(spec=["winfo_id"])
        mock_window.winfo_id.return_value = 67890
        with patch("aidota.windows_icon.apply_window_icon", return_value=True) as mock_apply:
            result = setup_window_handle(mock_window)
            if sys.platform == "win32":
                mock_apply.assert_called_once_with(67890)
                self.assertTrue(result)
            else:
                self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
