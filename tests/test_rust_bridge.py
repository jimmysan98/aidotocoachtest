"""Тесты для модуля aidota.rust_bridge (безопасная загрузка Rust DLL и fallback)."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from aidota import rust_bridge


class TestRustBridge(unittest.TestCase):
    def setUp(self):
        # Сбрасываем кэш библиотеки перед тестом
        rust_bridge._LIB = None
        rust_bridge._INIT_ATTEMPTED = False

    def tearDown(self):
        rust_bridge._LIB = None
        rust_bridge._INIT_ATTEMPTED = False

    def test_is_rust_available_returns_bool(self):
        result = rust_bridge.is_rust_available()
        self.assertIsInstance(result, bool)

    def test_safe_load_cdll_nonexistent_file(self):
        res = rust_bridge._safe_load_cdll("nonexistent_dll_12345.dll")
        self.assertIsNone(res)

    def test_compute_macro_timers_rust_fallback(self):
        rust_bridge._LIB = None
        rust_bridge._INIT_ATTEMPTED = True
        res = rust_bridge.compute_macro_timers_rust(60)
        self.assertIsNone(res)

    def test_build_advice_rust_fallback(self):
        rust_bridge._LIB = None
        rust_bridge._INIT_ATTEMPTED = True
        res = rust_bridge.build_advice_rust("npc_dota_hero_pudge", 1000, [], [])
        self.assertIsNone(res)

    def test_disable_rust_env_var(self):
        with patch.dict(os.environ, {"AIDOTA_DISABLE_RUST": "1"}):
            rust_bridge._LIB = None
            rust_bridge._INIT_ATTEMPTED = False
            self.assertFalse(rust_bridge.is_rust_available())
            self.assertIsNone(rust_bridge._get_lib())


if __name__ == "__main__":
    unittest.main()
