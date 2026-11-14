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

    def test_is_gsi_connected_uses_is_fresh(self):
        """Проверяем, что is_gsi_connected корректно опрашивает _gsi.is_fresh(45.0)."""
        assistant = Assistant(self.config, text_mode=True)
        self.assertFalse(assistant.is_gsi_connected())

        mock_gsi = MagicMock()
        mock_gsi.is_fresh.return_value = True
        assistant._gsi = mock_gsi
        self.assertTrue(assistant.is_gsi_connected())
        mock_gsi.is_fresh.assert_called_with(45.0)

    def test_stop_calls_shutdown_and_cleans_resources(self):
        """Проверяем, что stop() вызывает _shutdown() и освобождает потоки и устройства."""
        assistant = Assistant(self.config, text_mode=True)
        assistant._voice = MagicMock()
        assistant._tts = MagicMock()
        mock_gsi = MagicMock()
        assistant._gsi = mock_gsi

        assistant.stop()
        self.assertTrue(assistant.stop_event.is_set())
        assistant._voice.close.assert_called_once()
        assistant._tts.close.assert_called_once()
        mock_gsi.stop.assert_called_once()

    def test_say_lock_decoupled_from_tts_speak(self):
        """Проверяем, что _say_lock не удерживается во время выполнения tts.speak(wait=True)."""
        assistant = Assistant(self.config, text_mode=True)
        assistant._tts_enabled = True
        was_locked_during_speak = None

        def mock_speak(text, **kwargs):
            nonlocal was_locked_during_speak
            was_locked_during_speak = assistant._say_lock.locked()
            return True

        assistant._tts.speak = MagicMock(side_effect=mock_speak)
        assistant._say("Тестовая фраза", speak=True)
        self.assertFalse(was_locked_during_speak, "self._say_lock не должен удерживаться во время воспроизведения речи")


if __name__ == "__main__":
    unittest.main()
