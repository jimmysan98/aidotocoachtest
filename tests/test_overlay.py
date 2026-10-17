import unittest
from pathlib import Path

from aidota.config import OverlayConfig
from aidota.overlay import Overlay


class TestOverlay(unittest.TestCase):
    def test_queue_messages(self):
        cfg = OverlayConfig(enabled=True)
        overlay = Overlay(cfg, Path("."))
        self.assertFalse(overlay.started)

        overlay.add_user_message("какой следующий предмет")
        overlay.add_ai_message("Покупай БКБ", status="Готов")
        overlay.update_partial("какой")
        overlay.update_ptt_key("v", True)
        overlay.clear()

        # Check that items were queued
        items = []
        while not overlay._queue.empty():
            items.append(overlay._queue.get_nowait())

        self.assertEqual(items[0], ("USER_MSG", "какой следующий предмет"))
        self.assertEqual(items[1], ("AI_MSG", "Покупай БКБ", "Готов"))
        self.assertEqual(items[2], ("PARTIAL_MSG", "какой"))
        self.assertEqual(items[3], ("SET_KEY", "v", True))
        self.assertEqual(items[4], ("CLEAR",))


if __name__ == "__main__":
    unittest.main()
