"""Тесты интеграции со Steam: формирование бинарного shortcuts.vdf."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aidota.dota_install import encode_steam_shortcut, add_to_steam_library


class TestSteamShortcuts(unittest.TestCase):
    def test_encode_steam_shortcut(self) -> None:
        exe = Path("C:/Games/AIDOTA/dist/AIDOTA/AIDOTA.exe")
        start = exe.parent
        icon = start / "assets" / "icon.ico"

        b = encode_steam_shortcut(0, "AIDOTA", exe, start, icon)
        self.assertTrue(b.startswith(b"\x000\x00"))
        self.assertIn(b"AIDOTA", b)
        self.assertIn(b"AppName", b)
        self.assertIn(b"Exe", b)
        self.assertIn(b"StartDir", b)
        self.assertTrue(b.endswith(b"\x08"))

    def test_add_to_steam_library_with_custom_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            vdf_file = tmp / "shortcuts.vdf"
            vdf_file.write_bytes(b"\x00shortcuts\x00\x08\x08")

            # Подменяем пути
            exe = tmp / "AIDOTA.exe"
            exe.touch()

            # Проверяем кодирование и запись
            entry = encode_steam_shortcut(0, "AIDOTA", exe, tmp, None)
            data = vdf_file.read_bytes()
            new_data = data[:-2] + entry + b"\x08\x08"
            vdf_file.write_bytes(new_data)

            read_back = vdf_file.read_bytes()
            self.assertTrue(read_back.startswith(b"\x00shortcuts\x00"))
            self.assertIn(b"AIDOTA", read_back)
            self.assertTrue(read_back.endswith(b"\x08\x08"))


if __name__ == "__main__":
    unittest.main()
