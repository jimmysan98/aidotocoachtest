"""Тесты устойчивости к сбоям аудио, stdin и фонового потока."""

import queue
import sys
import unittest
from unittest.mock import MagicMock, patch

from aidota.app import Assistant
from aidota.audio import AudioError
from aidota.config import Config


class TestResilience(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.tts.enabled = False
        self.config.gsi.enabled = False
        self.config.vision.enabled = False
        self.config.overlay.enabled = False

    def test_read_command_without_stdin_does_not_crash(self):
        """Проверяем, что в GUI-режиме (sys.stdin = None) _read_command не бросает RuntimeError."""
        assistant = Assistant(self.config, text_mode=True)
        assistant._use_voice = False

        orig_stdin = sys.stdin
        try:
            sys.stdin = None
            # Пустая очередь -> возвращает ""
            cmd = assistant._read_command()
            self.assertEqual(cmd, "")

            # Добавляем команду в очередь
            assistant._text_queue.put("помощь")
            cmd = assistant._read_command()
            self.assertEqual(cmd, "помощь")
        finally:
            sys.stdin = orig_stdin

    def test_voice_device_fallback_on_start(self):
        """Если микрофон с заданным ID недоступен, _start_voice откатывается на дефолтный."""
        self.config.audio.input_device = 999
        assistant = Assistant(self.config, text_mode=False)

        def mock_start():
            if assistant._voice.config.input_device is not None:
                raise AudioError("Invalid device ID 999")
            return None

        assistant._voice.load_model = MagicMock()
        assistant._voice.start = MagicMock(side_effect=mock_start)
        assistant._status = MagicMock()

        assistant._start_voice()
        self.assertTrue(assistant._use_voice)
        self.assertIsNone(assistant.config.audio.input_device)

    def test_voice_failure_falls_back_to_text_mode_without_raising(self):
        """Если аудиосистема полностью недоступна, _start_voice переходит в текстовый режим."""
        assistant = Assistant(self.config, text_mode=False)
        assistant._voice.load_model = MagicMock(side_effect=AudioError("No Vosk model"))
        assistant._status = MagicMock()
        assistant._say = MagicMock()

        assistant._start_voice()
        self.assertFalse(assistant._use_voice)
        assistant._say.assert_called()
        self.assertIn("Голосовой ввод недоступен", assistant._say.call_args[0][0])

    def test_submit_text_command_enqueues(self):
        """Проверяем, что submit_text_command помещает команду в очередь."""
        assistant = Assistant(self.config, text_mode=False)
        assistant.submit_text_command("тест")
        self.assertEqual(assistant._text_queue.get_nowait(), "тест")


if __name__ == "__main__":
    unittest.main()
