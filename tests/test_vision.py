"""Тесты определения героя под курсором (без реального экрана)."""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from aidota.config import VisionConfig
from aidota.heroes import HeroCatalog
from aidota.vision import HeroVision, count_icon_files
from tests.helpers import make_temp_dir, remove_temp_dir

try:
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001
    cv2 = None
    np = None


@unittest.skipIf(cv2 is None, "OpenCV не установлен")
class VisionTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.icons = self.root / "hero_icons"
        (self.icons / "icons").mkdir(parents=True)
        # Простой градиентный шаблон: histogram equalization его не ломает.
        self.template = np.tile(np.arange(32, dtype=np.uint8) * 8, (32, 1))
        ok, encoded = cv2.imencode(".png", self.template)
        self.assertTrue(ok)
        (self.icons / "icons" / "antimage.png").write_bytes(encoded.tobytes())
        self.catalog = HeroCatalog([
            {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"}
        ])
        self.vision = HeroVision(
            VisionConfig(icons_dir=str(self.icons), threshold=0.6),
            self.root,
            self.catalog,
        )

    def tearDown(self):
        remove_temp_dir(self.root)

    def _shot_with_template(self, x: int = 74, y: int = 74):
        frame = np.zeros((180, 180, 3), dtype=np.uint8)
        frame[y:y + 32, x:x + 32] = cv2.cvtColor(self.template, cv2.COLOR_GRAY2BGR)
        return types.SimpleNamespace(
            rgb=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).tobytes(),
            width=180,
            height=180,
        )

    def test_templates_loaded(self):
        self.assertTrue(self.vision.load())
        self.assertEqual(self.vision.template_count, 1)
        self.assertIn("1 шаблон", self.vision.describe())

    def test_detects_hero(self):
        self.vision._grab = lambda *args: self._shot_with_template()
        result = self.vision.detect_at(90, 90)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Anti-Mage")
        self.assertGreater(result["score"], 0.5)

    def test_dark_frame_gives_hint(self):
        dark = types.SimpleNamespace(rgb=b"\x00" * (180 * 180 * 3), width=180, height=180)
        self.vision._grab = lambda *args: dark
        self.assertIsNone(self.vision.detect_at(90, 90))
        self.assertIn("полноэкранном", self.vision.error)

    def test_missing_icons(self):
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        vision = HeroVision(VisionConfig(icons_dir=str(empty_dir)), self.root, self.catalog)
        self.assertFalse(vision.load())
        self.assertIn("--fetch-icons", vision.error or "")

    def test_count_icon_files(self):
        self.assertEqual(count_icon_files(self.icons / "icons"), 1)
        self.assertEqual(count_icon_files(self.root / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
