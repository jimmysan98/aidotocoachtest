"""Тесты аудиоподсистемы AIDOTA: распознавание, PTT, таймаут тишины."""

from __future__ import annotations

import json
import queue
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aidota.audio import VoiceInput
from aidota.config import AudioConfig


class DummyRecognizer:
    def __init__(self) -> None:
        self.accept_result = False
        self.result_data = json.dumps({"text": ""})
        self.partial_data = json.dumps({"partial": ""})
        self.reset_called = False

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802
        return self.accept_result

    def Result(self) -> str:  # noqa: N802
        return self.result_data

    def PartialResult(self) -> str:  # noqa: N802
        return self.partial_data

    def Reset(self) -> None:  # noqa: N802
        self.reset_called = True


class TestVoiceInputSilenceEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AudioConfig(
            model_path="dummy",
            sample_rate=16000,
            block_size=4000,
            listen_timeout=0.2,  # быстрый таймаут для тестов
            push_to_talk=False,
        )
        self.voice = VoiceInput(self.config, Path("."))
        self.rec = DummyRecognizer()
        self.voice._recognizer = self.rec

    def test_phrase_queue_priority(self) -> None:
        """Фраза из phrase_queue (например, при отпускании PTT) отдаётся сразу."""
        self.voice.phrase_queue.put("астра помощь")
        partial_mock = MagicMock()
        self.voice.on_partial = partial_mock

        res = self.voice.listen()
        self.assertEqual(res, "астра помощь")
        partial_mock.assert_called_with("")

    def test_accept_waveform_finalization(self) -> None:
        """Если Vosk вернул True в AcceptWaveform, возвращается результат Result()."""
        self.rec.accept_result = True
        self.rec.result_data = json.dumps({"text": "джарвис статус"})
        self.voice._queue.put(b"\x00" * 8000)

        partial_mock = MagicMock()
        self.voice.on_partial = partial_mock

        res = self.voice.listen()
        self.assertEqual(res, "джарвис статус")
        partial_mock.assert_called_with("")

    def test_silence_timeout_finalizes_partial(self) -> None:
        """Если частичный результат не меняется в течение silence_timeout, фраза финализируется."""
        self.rec.accept_result = False
        self.rec.partial_data = json.dumps({"partial": "астра что покупать"})
        self.rec.result_data = json.dumps({"text": "астра что покупать"})

        # Отправляем 2 пакета данных
        self.voice._queue.put(b"\x00" * 4000)
        self.voice._queue.put(b"\x00" * 4000)

        partial_history: list[str] = []
        self.voice.on_partial = lambda p: partial_history.append(p)

        # Запускаем в отдельном потоке или с эмуляцией времени
        # Так как таймаут 0.2с, мы подаём ещё порции звука после паузы 0.25с
        def feed_after_delay() -> None:
            time.sleep(0.25)
            self.voice._queue.put(b"\x00" * 4000)

        import threading
        t = threading.Thread(target=feed_after_delay, daemon=True)
        t.start()

        res = self.voice.listen()
        self.assertEqual(res, "астра что покупать")
        self.assertIn("астра что покупать", partial_history)
        self.assertEqual(partial_history[-1], "")
        self.assertTrue(self.rec.reset_called)

    def test_discard_pending(self) -> None:
        """discard_pending очищает очереди, сбрасывает распознаватель и partial в оверлее."""
        self.voice._queue.put(b"\x01\x02")
        self.voice.phrase_queue.put("старая фраза")
        partial_mock = MagicMock()
        self.voice.on_partial = partial_mock

        self.voice.discard_pending()
        self.assertTrue(self.voice._queue.empty())
        self.assertTrue(self.voice.phrase_queue.empty())
        self.assertTrue(self.rec.reset_called)
        partial_mock.assert_called_with("")


if __name__ == "__main__":
    unittest.main()
