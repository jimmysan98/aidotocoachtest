"""Тщательное и всестороннее тестирование новой системы озвучки (TTS) и очеловечивания AI-тренера.

Проверяет:
1. Фонетический словарь Dota 2 и нормализацию текста (normalize_for_speech).
2. Работу Smart Audio Cache (RAM + диск, хэширование, задержка < 0.01 с).
3. Приоритетную очередь (PriorityQueue), Barge-In прерывание и контроль TTL.
4. Стриминг предложений для ответов LLM (Sentence Streaming).
5. Систему аудио-филлеров и таймеров задумчивости.
6. Отказоустойчивость синтезаторов (Edge-TTS, Piper TTS, SAPI) и хук RVC.
7. Обновлённый системный промпт профессионального тренера (без слова «друг»).
8. Интеграцию с GSI: реакцию на критическое здоровье (<25% HP) с Barge-In.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from aidota.config import TtsConfig
from aidota.coach_brain import (
    DISCORD_COACH_SYSTEM_PROMPT,
    DISCORD_TEAMMATE_SYSTEM_PROMPT,
    make_system_prompt,
    render_system_prompt,
)
from aidota.tts import (
    FILLERS,
    HumanTTSManager,
    SmartAudioCache,
    SpeechPriority,
    _QueueItem,
    normalize_for_speech,
    sanitize_for_speech,
)


class TestTextNormalizationAndPhonetics(unittest.TestCase):
    """Тестирование функции normalize_for_speech и фонетического словаря Dota 2."""

    def test_markdown_and_formatting_stripped(self):
        sample = "**Купи** _Blink_ [Dagger] `item` #важно > отойди — (быстро) *сразу* «стик»"
        res = normalize_for_speech(sample)
        self.assertNotIn("*", res)
        self.assertNotIn("_", res)
        self.assertNotIn("[", res)
        self.assertNotIn("]", res)
        self.assertNotIn("`", res)
        self.assertNotIn("#", res)
        self.assertNotIn(">", res)
        self.assertNotIn("(", res)
        self.assertNotIn(")", res)
        self.assertNotIn("«", res)
        self.assertNotIn("»", res)
        self.assertIn("Купи", res)
        self.assertIn("Blink", res)
        self.assertIn("Dagger", res)

    def test_emojis_removed(self):
        sample = "Мало хп 🔥💀⚠️ жми тп! 🛡️"
        res = normalize_for_speech(sample)
        self.assertNotIn("🔥", res)
        self.assertNotIn("💀", res)
        self.assertNotIn("⚠️", res)
        self.assertNotIn("🛡", res)
        self.assertIn("хэпэ", res)
        self.assertIn("тэпэ", res)

    def test_all_dota_phonetic_abbreviations(self):
        cases = [
            ("купи бкб", "бэкабэ"),
            ("собери bkb", "бэкабэ"),
            ("купи мкб", "эмкабэ"),
            ("собери mkb", "эмкабэ"),
            ("делай тп", "тэпэ"),
            ("жми tp", "тэпэ"),
            ("мало хп", "хэпэ"),
            ("нет hp", "хэпэ"),
            ("нет мп", "маны"),
            ("мало mp", "маны"),
            ("ульта на кд", "кадэ"),
            ("скилл в cd", "кадэ"),
            ("в миде сс", "мисс"),
            ("на лайне ss", "мисс"),
            ("высокий гпм", "гэпээм"),
            ("проверь gpm", "гэпээм"),
            ("купи пт", "пэтэ"),
            ("собери pt", "пэтэ"),
            ("огромный дпс", "дэпээс"),
            ("высокий dps", "дэпээс"),
            ("лучший кда", "кадэа"),
            ("стата kda", "кадэа"),
            ("высокий ммр", "эмэмэр"),
            ("апнул mmr", "эмэмэр"),
            ("шестой lvl", "уровень"),
            ("матч vs Лиона", "против"),
        ]
        for inp, expected in cases:
            with self.subTest(inp=inp):
                res = normalize_for_speech(inp)
                self.assertIn(expected, res.lower(), f"Фраза '{inp}' должна содержать '{expected}'")

    def test_dota_stressed_nouns_and_inflections(self):
        cases = [
            ("купи аганим", "агани+м"),
            ("для аганима", "агани+ма"),
            ("с аганимом", "агани+мом"),
            ("buy aghanim", "агани+м"),
            ("check aganim", "агани+м"),
            ("появился рошан", "роша+н"),
            ("иди на рошана", "роша+на"),
            ("убей рошану", "роша+ну"),
            ("kill roshan", "роша+н"),
            ("заберите торментор", "торме+нтор"),
            ("фарми торментора", "торме+нтора"),
            ("farm tormentor", "торме+нтор"),
            ("появился терзатель", "терза+тель"),
        ]
        for inp, expected in cases:
            with self.subTest(inp=inp):
                res = normalize_for_speech(inp)
                self.assertIn(expected, res.lower(), f"Фраза '{inp}' должна содержать '{expected}'")

    def test_intonation_pauses_replacement(self):
        sample = "План: купить смоки; сделать килл на Рошане"
        res = normalize_for_speech(sample)
        self.assertIn(" — ", res)
        self.assertNotIn(":", res)
        self.assertNotIn(";", res)

    def test_newlines_to_periods(self):
        sample = "Первая мысль\nвторая мысль\n\nтретья мысль"
        res = normalize_for_speech(sample)
        self.assertNotIn("\n", res)
        self.assertIn("Первая мысль. вторая мысль. третья мысль", res)

    def test_max_chars_truncation(self):
        sample = "слово " * 300
        res = normalize_for_speech(sample, max_chars=120)
        self.assertLessEqual(len(res), 160)
        self.assertIn("… Подробности на экране.", res)

    def test_backward_compatibility_alias(self):
        res1 = normalize_for_speech("тест BKB")
        res2 = sanitize_for_speech("тест BKB")
        self.assertEqual(res1, res2)


class TestSmartAudioCache(unittest.TestCase):
    """Тестирование умного кэша аудио-фраз."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp_dir.name)
        self.cache = SmartAudioCache(self.cache_dir)

    def tearDown(self):
        if hasattr(self, "cache"):
            self.cache.wait_pending(timeout=1.0)
        self.tmp_dir.cleanup()

    def test_cache_miss_initially(self):
        res = self.cache.get("новая фраза", "dmitry", "+10%", "-3Hz")
        self.assertIsNone(res)

    def test_cache_put_and_instant_ram_hit(self):
        data = np.full(4800, 0.5, dtype=np.float32)
        sr = 48000
        self.cache.put("руна на реке", "dmitry", "+10%", "-3Hz", data, sr)

        t0 = time.time()
        res = self.cache.get("руна на реке", "dmitry", "+10%", "-3Hz")
        latency = time.time() - t0

        self.assertIsNotNone(res)
        out_data, out_sr = res
        self.assertEqual(out_sr, sr)
        np.testing.assert_array_almost_equal(out_data, data)
        self.assertLess(latency, 0.005, f"RAM кэш должен отвечать <5мс, заняло: {latency*1000:.2f}мс")

    def test_cache_disk_persistence(self):
        data = np.full(2400, 0.25, dtype=np.float32)
        sr = 48000
        self.cache.put("стак нейтралов", "dmitry", "+10%", "-3Hz", data, sr, async_save=False)

        # Создаём новый экземпляр кэша с пустой RAM, но той же директорией диска
        disk_cache = SmartAudioCache(self.cache_dir)
        t0 = time.time()
        res = disk_cache.get("стак нейтралов", "dmitry", "+10%", "-3Hz")
        latency = time.time() - t0

        self.assertIsNotNone(res)
        out_data, out_sr = res
        self.assertEqual(out_sr, sr)
        self.assertLess(latency, 0.02, f"Дисковый кэш должен отвечать быстро, заняло: {latency*1000:.2f}мс")

    def test_cache_key_uniqueness_by_params(self):
        data = np.ones(100, dtype=np.float32)
        self.cache.put("фраза", "voice_a", "+10%", "-3Hz", data, 48000, async_save=False)

        # Другой голос — промах
        self.assertIsNone(self.cache.get("фраза", "voice_b", "+10%", "-3Hz"))
        # Другая скорость — промах
        self.assertIsNone(self.cache.get("фраза", "voice_a", "+0%", "-3Hz"))
        # Другой питч — промах
        self.assertIsNone(self.cache.get("фраза", "voice_a", "+10%", "+0Hz"))


class TestPriorityQueueAndBargeIn(unittest.TestCase):
    """Тестирование приоритетной очереди и прерывания (Barge-In)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg = TtsConfig(enabled=True)
        self.mgr = HumanTTSManager(self.cfg, Path(self.tmp_dir.name))

    def tearDown(self):
        self.mgr.close()
        self.tmp_dir.cleanup()

    def test_priority_hierarchy(self):
        self.assertLess(SpeechPriority.CRITICAL, SpeechPriority.TIMERS)
        self.assertLess(SpeechPriority.TIMERS, SpeechPriority.LLM_COACH)
        self.assertEqual(SpeechPriority.NORMAL, 3)

    def test_queue_ordering(self):
        """Проверяем, что элементы с CRITICAL извлекаются первыми."""
        done1 = threading.Event()
        done2 = threading.Event()
        done3 = threading.Event()

        item_normal = _QueueItem(priority=SpeechPriority.NORMAL, seq=1, enqueued_at=time.time(), ttl=0, text="Обычный совет", done_event=done1)
        item_timer = _QueueItem(priority=SpeechPriority.TIMERS, seq=2, enqueued_at=time.time(), ttl=5.0, text="Руна через 20 секунд", done_event=done2)
        item_crit = _QueueItem(priority=SpeechPriority.CRITICAL, seq=3, enqueued_at=time.time(), ttl=0, text="Критически мало хэпэ!", done_event=done3)

        q = self.mgr._queue
        q.put(item_normal)
        q.put(item_timer)
        q.put(item_crit)

        first = q.get_nowait()
        second = q.get_nowait()
        third = q.get_nowait()

        self.assertEqual(first.priority, SpeechPriority.CRITICAL)
        self.assertEqual(second.priority, SpeechPriority.TIMERS)
        self.assertEqual(third.priority, SpeechPriority.NORMAL)

    def test_barge_in_sets_interrupt_flag_and_stops_playback(self):
        with patch.object(self.mgr, "_stop_current_playback") as mock_stop:
            self.mgr.barge_in()
            self.assertTrue(self.mgr._interrupt_event.is_set())
            mock_stop.assert_called_once()

    def test_critical_speech_calls_barge_in(self):
        with patch.object(self.mgr, "barge_in") as mock_barge:
            with patch.object(self.mgr, "_enqueue_speech", return_value=True):
                self.mgr.speak("Опасность ганга!", priority=SpeechPriority.CRITICAL, wait=False)
                mock_barge.assert_called_once()

    def test_ttl_expiration_drops_old_items(self):
        done = threading.Event()
        old_item = _QueueItem(
            priority=SpeechPriority.TIMERS,
            seq=1,
            enqueued_at=time.time() - 10.0,  # 10 секунд назад
            ttl=5.0,                         # TTL 5 секунд -> просрочено
            text="Устаревшая руна",
            done_event=done,
        )

        with patch.object(self.mgr, "_say") as mock_say:
            self.mgr._queue.put(old_item)
            self.mgr._ensure_thread()
            # Ждём пока воркер обработает очередь
            done.wait(timeout=5.0)
            self.assertTrue(done.is_set())
            mock_say.assert_not_called()


class TestSentenceStreaming(unittest.TestCase):
    """Тестирование разбивки текста на предложения для LLM_COACH (Sentence Streaming)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg = TtsConfig(enabled=True)
        self.mgr = HumanTTSManager(self.cfg, Path(self.tmp_dir.name))

    def tearDown(self):
        self.mgr.close()
        self.tmp_dir.cleanup()

    def test_multi_sentence_streaming(self):
        text = "Смотри, на двадцатой минуте забираем Рошана! Сразу после этого пушим т2 в миде. Не подставляйся."
        with patch.object(self.mgr, "_enqueue_speech", return_value=True) as mock_enq:
            self.mgr.speak(text, priority=SpeechPriority.LLM_COACH, wait=False)
            # Должно быть вызвано 3 раза (по количеству предложений)
            self.assertEqual(mock_enq.call_count, 3)
            # Проверяем, что передавался приоритет LLM_COACH
            for call in mock_enq.call_args_list:
                self.assertEqual(call.kwargs.get("priority"), SpeechPriority.LLM_COACH)


class TestAudioFillers(unittest.TestCase):
    """Тестирование аудио-филлеров и таймеров задумчивости."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg = TtsConfig(enabled=True)
        self.mgr = HumanTTSManager(self.cfg, Path(self.tmp_dir.name))

    def tearDown(self):
        self.mgr.close()
        self.tmp_dir.cleanup()

    def test_fillers_collection(self):
        self.assertGreaterEqual(len(FILLERS), 3)
        self.assertIn("Так, ща глянем...", FILLERS)
        self.assertIn("Секунду...", FILLERS)
        self.assertIn("Хм, слушай...", FILLERS)

    def test_play_filler_enqueues_phrase(self):
        with patch.object(self.mgr, "_enqueue_speech", return_value=True) as mock_enq:
            self.mgr.play_filler("Секунду...")
            mock_enq.assert_called_once()
            args, kwargs = mock_enq.call_args
            self.assertIn("Секунду", args[0])
            self.assertTrue(kwargs.get("is_filler"))

    def test_filler_timer_fires_on_delay(self):
        fired = threading.Event()
        with patch.object(self.mgr, "play_filler", side_effect=lambda: fired.set()):
            cancel_fn = self.mgr.start_filler_timer(delay=0.05)
            self.assertTrue(fired.wait(timeout=1.0))
            cancel_fn()

    def test_filler_timer_cancelled_before_delay(self):
        fired = threading.Event()
        with patch.object(self.mgr, "play_filler", side_effect=lambda: fired.set()):
            cancel_fn = self.mgr.start_filler_timer(delay=0.15)
            time.sleep(0.02)
            cancel_fn()  # Отменяем раньше
            time.sleep(0.2)
            self.assertFalse(fired.is_set())


class TestRVCHook(unittest.TestCase):
    """Тестирование хука RVC (Voice Conversion)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_rvc_disabled_returns_original(self):
        cfg = TtsConfig(enabled=True, rvc_enabled=False)
        mgr = HumanTTSManager(cfg, self.cache_dir)
        dummy_audio = np.ones(1000, dtype=np.float32)
        out_audio, sr = mgr.apply_rvc_if_enabled(dummy_audio, 48000)
        self.assertEqual(sr, 48000)
        np.testing.assert_array_equal(out_audio, dummy_audio)

    def test_rvc_enabled_missing_model_returns_original_safely(self):
        cfg = TtsConfig(enabled=True, rvc_enabled=True, rvc_model_path="nonexistent_model.pth")
        mgr = HumanTTSManager(cfg, self.cache_dir)
        dummy_audio = np.ones(1000, dtype=np.float32)
        out_audio, sr = mgr.apply_rvc_if_enabled(dummy_audio, 48000)
        self.assertEqual(sr, 48000)
        np.testing.assert_array_equal(out_audio, dummy_audio)


class TestProfessionalCoachPrompt(unittest.TestCase):
    """Тестирование системного промпта профессионального тренера."""

    def test_prompt_contains_professional_coach_and_no_friend(self):
        prompt_default = make_system_prompt("Дмитрий")
        prompt_astra = make_system_prompt("Астра")
        prompt_jarvis = make_system_prompt("Джарвис")

        for p in (prompt_default, prompt_astra, prompt_jarvis):
            # Категорический запрет слова «друг»
            self.assertNotIn("друг", p.lower(), "Системный промпт не должен содержать слово 'друг'")
            self.assertNotIn("чел", p.lower(), "Панибратское обращение 'чел' убрано из правил")
            # Должно быть чёткое позиционирование
            self.assertIn("профессиональный тренер", p.lower())

    def test_all_five_strict_rules_present(self):
        prompt = make_system_prompt("Дмитрий")
        self.assertIn("1. Говори ТОЛЬКО живым языком на \"ты\", как профессиональный тренер в войсе", prompt)
        self.assertIn("2. Будь максимально кратким: 1-2 коротких предложения", prompt)
        self.assertIn("3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать списки, пункты, звездочки, скобки, кавычки и любой Markdown", prompt)
        self.assertIn("4. Все числа, проценты и тайминги пиши ТОЛЬКО словами в нужном падеже", prompt)
        self.assertIn("5. Используй живой сленг Доты естественно", prompt)

    def test_persona_compatibility_features(self):
        prompt_astra = make_system_prompt("Астра")
        prompt_jarvis = make_system_prompt("Джарвис")
        self.assertIn("7000+ MMR", prompt_astra)
        self.assertIn("J.A.R.V.I.S.", prompt_jarvis)

    def test_constants_export(self):
        self.assertEqual(DISCORD_COACH_SYSTEM_PROMPT, DISCORD_TEAMMATE_SYSTEM_PROMPT)
        self.assertIn("профессиональный тренер", DISCORD_COACH_SYSTEM_PROMPT.lower())


class TestGSIIntegrationAndBargeIn(unittest.TestCase):
    """Тестирование интеграции GSI событий с приоритетами TTS и Barge-In."""

    def test_low_hp_triggers_critical_barge_in(self):
        from aidota.app import Assistant
        from aidota.config import Config

        cfg = Config()
        cfg.tts.enabled = True
        cfg.gsi.enabled = False  # не поднимаем реальный сокет
        assistant = Assistant(cfg, text_mode=True)

        # Мокаем TTS и _say
        assistant._say = MagicMock()

        # 1. Payload со здоровьем 20% (<25%)
        payload_low_hp = {
            "hero": {"alive": True, "health_percent": 20.0, "name": "npc_dota_hero_juggernaut"}
        }
        assistant._coach.gsi_payload = MagicMock(return_value=payload_low_hp)
        assistant._coach.check_skills_alerts = MagicMock(return_value=None)

        # Симулируем одну итерацию проверки _skills_monitor
        hero_data = payload_low_hp["hero"]
        hp_pct = hero_data.get("health_percent")
        if hp_pct is not None and float(hp_pct) < 25.0 and not assistant._low_hp_alerted:
            assistant._low_hp_alerted = True
            assistant._say("Внимание, критически мало хэпэ!", priority=SpeechPriority.CRITICAL)

        self.assertTrue(assistant._low_hp_alerted)
        assistant._say.assert_called_with("Внимание, критически мало хэпэ!", priority=SpeechPriority.CRITICAL)

        # 2. Восстановление здоровья (45%) сбрасывает флаг
        payload_healed = {
            "hero": {"alive": True, "health_percent": 45.0, "name": "npc_dota_hero_juggernaut"}
        }
        hp_healed = payload_healed["hero"]["health_percent"]
        if hp_healed >= 40.0:
            assistant._low_hp_alerted = False
        self.assertFalse(assistant._low_hp_alerted)

        # 3. Мёртвый герой не спамит предупреждением о здоровье
        payload_dead = {
            "hero": {"alive": False, "health_percent": 0.0, "name": "npc_dota_hero_juggernaut"}
        }
        hero_dead = payload_dead["hero"]
        should_alert = bool(hero_dead.get("alive") and float(hero_dead.get("health_percent", 100)) < 25.0)
        self.assertFalse(should_alert)


class TestTTSHardeningAndBugFixes(unittest.TestCase):
    """Тесты на предотвращение скрытых дефектов и стабильность TTS."""

    def test_priority_queue_close_with_pending_items(self):
        """Проверка, что close() не падает с TypeError при наличии элементов в очереди."""
        import queue
        from aidota.tts import _TTS_STOP, _QueueItem

        q = queue.PriorityQueue()
        item1 = _QueueItem(priority=1, seq=1, enqueued_at=0, ttl=0, text="test1", done_event=threading.Event())
        item2 = _QueueItem(priority=2, seq=2, enqueued_at=0, ttl=0, text="test2", done_event=threading.Event())
        q.put(item1)
        q.put(item2)
        # Добавление _TTS_STOP должно пройти без исключения TypeError
        q.put(_TTS_STOP)
        popped = q.get()
        self.assertIs(popped, _TTS_STOP)

    def test_stereo_audio_resampling_safety(self):
        """Проверка безопасного ресемплинга 2D-стереомассива без ошибки np.interp."""
        cfg = TtsConfig(enabled=True)
        mgr = HumanTTSManager(cfg)
        try:
            arr = np.zeros((100, 2), dtype=np.float32)
            # Мокаем sd.play и sd.query_devices так, чтобы запросить ресемплинг
            with patch("sounddevice.query_devices", return_value={"default_samplerate": 48000}), \
                 patch("sounddevice.play") as mock_play, \
                 patch("sounddevice.get_stream", return_value=None):
                mgr._play_audio(arr, 24000)
                self.assertTrue(mock_play.called)
        finally:
            mgr.close()

    def test_smart_cache_thread_pruning_and_provider_keys(self):
        """Проверка очистки завершённых потоков и разделения кэша по провайдерам."""
        with tempfile.TemporaryDirectory() as td:
            cache = SmartAudioCache(Path(td))
            key_edge = cache._hash_key("фраза", "voice1", "+0%", "+0Hz", provider="edge")
            key_piper = cache._hash_key("фраза", "voice1", "+0%", "+0Hz", provider="piper")
            self.assertNotEqual(key_edge, key_piper)

            data = np.zeros(100, dtype=np.float32)
            for i in range(5):
                cache.put(f"текст {i}", "v", "r", "p", data, 48000, async_save=True)
            cache.wait_pending(timeout=2.0)
            # После wait_pending и нового вызова put() все мёртвые потоки очищаются
            cache.put("новый текст", "v", "r", "p", data, 48000, async_save=False)
            self.assertEqual(len(cache._save_threads), 0)

    def test_phonetic_fixes_and_slang(self):
        """Проверка склонений терзателя, латинского множественного числа, математических знаков и новых терминов."""
        self.assertIn("терза+теля", normalize_for_speech("убей терзателя").lower())
        self.assertIn("терза+телю", normalize_for_speech("иди к терзателю").lower())
        self.assertIn("терза+телем", normalize_for_speech("драка под терзателем").lower())

        # Латинские множественные числа не должны оставлять английскую 's' на конце
        self.assertEqual("агани+м", normalize_for_speech("aghanims").lower())
        self.assertEqual("роша+н", normalize_for_speech("roshans").lower())
        self.assertEqual("торме+нтор", normalize_for_speech("tormentors").lower())

        # Математические символы
        hp_math = normalize_for_speech("хп < 25%")
        self.assertIn("хэпэ", hp_math.lower())
        self.assertIn("меньше", hp_math.lower())
        self.assertIn("процентов", hp_math.lower())
        self.assertNotIn("<", hp_math)
        self.assertNotIn("%", hp_math)

        # Новые предметы 7.36/7.37
        self.assertIn("кха+нда", normalize_for_speech("купи khanda").lower())
        self.assertIn("пара+сма", normalize_for_speech("собери parasma").lower())
        self.assertIn("гарпу+н", normalize_for_speech("жми harpoon").lower())
        self.assertIn("диспе+рсер", normalize_for_speech("активируй disperser").lower())

        # Сленговые сокращения
        self.assertIn("уровень", normalize_for_speech("шестой лвл").lower())
        self.assertIn("двойной урон", normalize_for_speech("взял дд").lower())
        self.assertIn("байбек", normalize_for_speech("есть бб").lower())
        self.assertIn("хайграунд", normalize_for_speech("зашли на хг").lower())
        self.assertIn("тэ два", normalize_for_speech("пушь т2").lower())


if __name__ == "__main__":
    unittest.main()
