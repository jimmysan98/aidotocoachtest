"""Тесты аудио-пайплайна, HumanTTS, нормализации и SmartAudioCache (Area 3)."""

from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from aidota.config import TtsConfig
from aidota.tts import (
    FILLERS,
    HumanTTSManager,
    SmartAudioCache,
    SpeechPriority,
    _is_corrupted_static_waveform,
    _QueueItem,
    normalize_for_speech,
)


class TestAudioFillersAdvanced(unittest.TestCase):
    def test_filler_random_choice_distribution_and_fallback(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            observed: list[str] = []

            def fake_enqueue(norm, **kwargs):
                observed.append(norm)
                self.assertTrue(kwargs.get("is_filler"))
                self.assertEqual(kwargs.get("priority"), SpeechPriority.NORMAL)
                self.assertEqual(kwargs.get("ttl"), 2.0)
                return True

            with patch.object(mgr, "_enqueue_speech", side_effect=fake_enqueue):
                # Call play_filler(None) multiple times to check distribution
                for _ in range(40):
                    mgr.play_filler(None)

                # Fallback on empty string
                mgr.play_filler("")

            valid_fillers = {normalize_for_speech(f) for f in FILLERS}
            self.assertTrue(set(observed).issubset(valid_fillers))
            # Across 41 calls all 3 fillers should be chosen
            self.assertEqual(set(observed), valid_fillers)
        finally:
            mgr.close()

    def test_filler_timer_suppressed_when_is_speaking(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            mgr._is_speaking_event.set()
            with patch.object(mgr, "play_filler") as mock_play:
                cancel = mgr.start_filler_timer(delay=0.03)
                time.sleep(0.08)
                cancel()
                mock_play.assert_not_called()
        finally:
            mgr.close()

    def test_filler_timer_suppressed_when_tts_disabled(self):
        cfg = TtsConfig(enabled=False)
        mgr = HumanTTSManager(cfg)
        try:
            with patch.object(mgr, "play_filler") as mock_play:
                cancel = mgr.start_filler_timer(delay=0.02)
                time.sleep(0.06)
                cancel()
                mock_play.assert_not_called()
        finally:
            mgr.close()

    def test_filler_ttl_discard_on_backlog(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            # Enqueue expired filler item directly into queue
            done_ev = threading.Event()
            expired_item = _QueueItem(
                priority=SpeechPriority.NORMAL,
                seq=1,
                text="устаревший филлер",
                ttl=1.0,
                enqueued_at=time.time() - 3.0,
                done_event=done_ev,
                is_filler=True,
            )

            with patch.object(mgr, "_say") as mock_say:
                mgr._ensure_thread()
                mgr._queue.put(expired_item)
                done_ev.wait(timeout=2.0)
                mock_say.assert_not_called()
                self.assertFalse(expired_item.success)
        finally:
            mgr.close()


class TestComplexPhoneticNormalization(unittest.TestCase):
    def test_normalization_math_comparisons_and_percentages(self):
        text = "Здоровье <= 25% и мана < 10%, кд 3.5 сек, дистанция > 500, урон >= 150, статы 15-20"
        res = normalize_for_speech(text)
        self.assertIn("меньше 25 процентов", res)
        self.assertIn("меньше 10 процентов", res)
        self.assertIn("кадэ 3.5 сек", res)
        self.assertIn("больше 500", res)
        self.assertIn("больше 150", res)
        self.assertIn("15-20", res)
        self.assertNotIn("<", res)
        self.assertNotIn(">", res)
        self.assertNotIn("%", res)

    def test_normalization_mixed_slang_towers_and_patch_items(self):
        text = "Собери bkb и мкб, враги снесли т1-т2 и т3 на миде, у них khanda, parasma, harpoon и disperser против рошана и 2 tormentors"
        res = normalize_for_speech(text)
        self.assertIn("бэкабэ", res)
        self.assertIn("эмкабэ", res)
        self.assertIn("тэ один-тэ два", res)
        self.assertIn("тэ три", res)
        self.assertIn("кха+нда", res)
        self.assertIn("пара+сма", res)
        self.assertIn("гарпу+н", res)
        self.assertIn("диспе+рсер", res)
        self.assertIn("против роша+на", res)
        self.assertIn("торме+нтор", res)

    def test_normalization_quotes_dashes_clocks_and_breathing_pauses(self):
        text = "Счет «5:3» в тайминг 12:30: план: купить 'смок'; забрать “Аегис”… быстро — не стой!"
        res = normalize_for_speech(text)
        for quote in ("«", "»", "“", "”", "'", '"'):
            self.assertNotIn(quote, res)
        # Game clock colon preserved
        self.assertIn("12:30", res)
        # Colon followed by whitespace replaced by breathing pause em-dash
        self.assertIn("план — купить", res)
        # Semicolon replaced by dash
        self.assertIn("смок — забрать", res)
        # Em-dash has surrounding whitespace
        self.assertIn(" — не стой!", res)

    def test_normalization_list_bullet_stripping_and_empty_inputs(self):
        text = "- первый шаг\n• второй шаг\n1. купить стики\n2) отойти на хг"
        res = normalize_for_speech(text)
        self.assertNotIn("•", res)
        self.assertNotIn("1.", res)
        self.assertNotIn("2)", res)
        self.assertIn("первый шаг", res)
        self.assertIn("второй шаг", res)
        self.assertIn("купить стики", res)
        self.assertIn("отойти на хайграунд", res)

        for empty in ("", "   ", "\n\t", None):
            self.assertEqual(normalize_for_speech(empty), "")


class TestSmartAudioCacheHardening(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp_dir.name)
        self.cache = SmartAudioCache(self.cache_dir)

    def tearDown(self):
        self.cache.wait_pending(timeout=1.0)
        self.tmp_dir.cleanup()

    def test_smart_cache_ram_eviction_threshold_500(self):
        dummy_audio = np.zeros(64, dtype=np.float32)
        sr = 48000

        # Insert 501 items without async saving
        for i in range(501):
            self.cache.put(f"phrase_{i}", "voice", "+0%", "+0Hz", dummy_audio, sr, async_save=False)
        self.assertEqual(len(self.cache._ram), 501)

        # 502nd item triggers eviction (> 500), clears RAM and inserts 502nd
        self.cache.put("phrase_501", "voice", "+0%", "+0Hz", dummy_audio, sr, async_save=False)
        self.assertEqual(len(self.cache._ram), 1)

        # Accessing an evicted item reloads it from disk back into RAM
        loaded = self.cache.get("phrase_10", "voice", "+0%", "+0Hz")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(self.cache._ram), 2)

    def test_smart_cache_corrupted_disk_file_handling(self):
        key = self.cache._hash_key("corrupted_phrase", "voice", "+0%", "+0Hz")
        bad_file = self.cache.disk_dir / f"{key}.wav"
        bad_file.write_bytes(b"CORRUPTED_NON_WAV_BINARY_DATA")

        res = self.cache.get("corrupted_phrase", "voice", "+0%", "+0Hz")
        self.assertIsNone(res)

    def test_smart_cache_multithreaded_concurrency_and_thread_pruning(self):
        dummy_audio = np.zeros(64, dtype=np.float32)
        sr = 48000
        threads = []

        def worker(start_idx: int):
            for i in range(start_idx, start_idx + 10):
                self.cache.put(f"thread_phrase_{i}", "v", "+0%", "+0Hz", dummy_audio, sr, async_save=True)

        for t_idx in range(10):
            t = threading.Thread(target=worker, args=(t_idx * 10,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.cache.wait_pending(timeout=3.0)
        # Put one more item synchronously to trigger pruning of dead threads
        self.cache.put("sync_final", "v", "+0%", "+0Hz", dummy_audio, sr, async_save=False)
        self.assertEqual(len(self.cache._save_threads), 0)

        # Verify all 100 files exist on disk
        wav_files = list(self.cache.disk_dir.glob("*.wav"))
        self.assertGreaterEqual(len(wav_files), 100)


class TestConcurrencyAndPriorityQueue(unittest.TestCase):
    def test_priority_queue_strict_preemption_and_barge_in(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            interrupted_flag = threading.Event()
            speak2_completed = threading.Event()

            def fake_say(text: str):
                if "длинная речь" in text:
                    for _ in range(50):
                        if mgr._interrupt_event.is_set():
                            interrupted_flag.set()
                            return
                        time.sleep(0.01)
                elif "опасность" in text:
                    speak2_completed.set()

            with patch.object(mgr, "_say", side_effect=fake_say):
                t1 = threading.Thread(
                    target=mgr.speak,
                    args=("длинная речь",),
                    kwargs={"priority": SpeechPriority.NORMAL, "wait": True},
                )
                t1.start()
                # Give t1 time to start playback
                time.sleep(0.05)

                t2 = threading.Thread(
                    target=mgr.speak,
                    args=("опасность ганга",),
                    kwargs={"priority": SpeechPriority.CRITICAL, "wait": True},
                )
                t2.start()

                t1.join(timeout=2.0)
                t2.join(timeout=2.0)

                self.assertTrue(interrupted_flag.is_set())
                self.assertTrue(speak2_completed.is_set())
        finally:
            mgr.close()

    def test_concurrent_multi_priority_ordering_and_fifo_tiebreak(self):
        cfg = TtsConfig(enabled=False)
        mgr = HumanTTSManager(cfg)
        try:
            now = time.time()
            items = [
                _QueueItem(priority=SpeechPriority.NORMAL, seq=1, enqueued_at=now, ttl=0.0, text="n1", done_event=threading.Event()),
                _QueueItem(priority=SpeechPriority.TIMERS, seq=2, enqueued_at=now, ttl=0.0, text="t1", done_event=threading.Event()),
                _QueueItem(priority=SpeechPriority.CRITICAL, seq=3, enqueued_at=now, ttl=0.0, text="c1", done_event=threading.Event()),
                _QueueItem(priority=SpeechPriority.NORMAL, seq=4, enqueued_at=now, ttl=0.0, text="n2", done_event=threading.Event()),
                _QueueItem(priority=SpeechPriority.TIMERS, seq=5, enqueued_at=now, ttl=0.0, text="t2", done_event=threading.Event()),
                _QueueItem(priority=SpeechPriority.CRITICAL, seq=6, enqueued_at=now, ttl=0.0, text="c2", done_event=threading.Event()),
            ]
            for it in items:
                mgr._queue.put(it)

            popped = []
            while not mgr._queue.empty():
                popped.append(mgr._queue.get())

            popped_texts = [p.text for p in popped]
            # CRITICAL first (c1, c2), then TIMERS (t1, t2), then NORMAL (n1, n2)
            self.assertEqual(popped_texts, ["c1", "c2", "t1", "t2", "n1", "n2"])
        finally:
            mgr.close()

    def test_concurrent_speak_wait_blocking_and_cancellation_on_close(self):
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            blocker = threading.Event()
            say_started = threading.Event()

            def blocked_say(text: str):
                say_started.set()
                blocker.wait(timeout=2.0)

            with patch.object(mgr, "_say", side_effect=blocked_say):
                results = []
                threads = []

                def requester(idx: int):
                    ok = mgr.speak(f"запрос {idx}", wait=True, timeout=5.0)
                    results.append(ok)

                # Start first request to occupy _say
                t_first = threading.Thread(target=requester, args=(0,))
                threads.append(t_first)
                t_first.start()
                say_started.wait(timeout=1.0)

                # Start other requests waiting in queue
                for i in range(1, 4):
                    t = threading.Thread(target=requester, args=(i,))
                    threads.append(t)
                    t.start()

                time.sleep(0.05)
                # Now close manager while requests are pending
                mgr.close()
                blocker.set()

                for t in threads:
                    t.join(timeout=2.0)
                    self.assertFalse(t.is_alive())

                # Cancelled queue items return False
                self.assertIn(False, results)
        finally:
            mgr.close()


class TestPiperAndStaticNoiseProtection(unittest.TestCase):
    def test_piper_writes_to_temp_wav_file_not_stdout_dash(self):
        """Piper обязан писать в temp .wav, а не stdout '-' (защита от CRLF-шума на Windows)."""
        import soundfile as sf

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg = TtsConfig(enabled=True, provider="piper")
            mgr = HumanTTSManager(cfg, cache_dir=td_path)
            observed_cmds: list[list[str]] = []

            try:
                fake_exe = td_path / "piper.exe"
                fake_model = td_path / "voice.onnx"
                fake_exe.write_bytes(b"")
                fake_model.write_bytes(b"")

                t = np.linspace(0, 0.25, int(22050 * 0.25), endpoint=False)
                clean_sine = (0.15 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

                class _FakeProc:
                    returncode = 0

                    def __init__(self, cmd, **_kwargs):
                        observed_cmds.append(list(cmd))
                        self._out_file = cmd[cmd.index("--output_file") + 1]

                    def communicate(self, input=None, timeout=None):  # noqa: A002
                        sf.write(self._out_file, clean_sine, 22050, format="WAV", subtype="PCM_16")
                        return b"", b""

                with patch.object(mgr, "_find_piper_binary_and_model", return_value=(fake_exe, fake_model)), \
                     patch("aidota.tts.subprocess.Popen", side_effect=_FakeProc):
                    data, sr = mgr._synthesize_piper_audio("Привет!")

                self.assertEqual(sr, 22050)
                self.assertFalse(_is_corrupted_static_waveform(data))
                self.assertEqual(len(observed_cmds), 1)
                out_arg = observed_cmds[0][observed_cmds[0].index("--output_file") + 1]
                self.assertNotEqual(out_arg, "-")
                self.assertTrue(out_arg.endswith(".wav"))
                # Временный файл удаляется в finally
                self.assertFalse(Path(out_arg).exists())
            finally:
                mgr.close()

    def test_corrupted_static_waveform_blocked_in_cache_and_playback(self):
        """Белый шум / повреждённый кэш отвергается, удаляется с диска и не воспроизводится."""
        import soundfile as sf

        rng = np.random.default_rng(42)
        white_noise = rng.uniform(-1.0, 1.0, size=22050).astype(np.float32)
        self.assertTrue(_is_corrupted_static_waveform(white_noise))

        with tempfile.TemporaryDirectory() as td:
            cache = SmartAudioCache(Path(td))
            # 1. put() отказывает в сохранении шума
            cache.put("битая фраза", "dmitry", "+10%", "-3Hz", white_noise, 22050, provider="piper", async_save=False)
            self.assertIsNone(cache.get("битая фраза", "dmitry", "+10%", "-3Hz", provider="piper"))

            # 2. Заранее повреждённый .wav на диске отбрасывается и удаляется при get()
            key = cache._hash_key("старая фраза", "dmitry", "+10%", "-3Hz", provider="piper")
            bad_wav = cache.disk_dir / f"{key}.wav"
            bad_wav.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(bad_wav), white_noise, 22050, format="WAV", subtype="PCM_16")
            self.assertTrue(bad_wav.is_file())
            self.assertIsNone(cache.get("старая фраза", "dmitry", "+10%", "-3Hz", provider="piper"))
            self.assertFalse(bad_wav.exists())

            # 3. _play_audio отменяет воспроизведение при белом шуме
            cfg = TtsConfig(enabled=True)
            mgr = HumanTTSManager(cfg, cache_dir=Path(td))
            try:
                with patch("sounddevice.play") as mock_play:
                    mgr._play_audio(white_noise, 22050)
                    mock_play.assert_not_called()
            finally:
                mgr.close()

    def test_real_piper_binary_if_installed_produces_clean_audio(self):
        """Если локальный piper/piper.exe установлен, синтез даёт чистую речь без шума."""
        cfg = TtsConfig(enabled=True, provider="piper")
        with tempfile.TemporaryDirectory() as td:
            mgr = HumanTTSManager(cfg, cache_dir=Path(td))
            try:
                exe, model = mgr._find_piper_binary_and_model()
                if exe is None or model is None:
                    self.skipTest("piper.exe не установлен локально")
                data, sr = mgr._synthesize_piper_audio("Проверка чистого звука.")
                self.assertEqual(sr, 22050)
                self.assertGreater(len(data), 5000)
                self.assertFalse(_is_corrupted_static_waveform(data))
            finally:
                mgr.close()


if __name__ == "__main__":
    unittest.main()
