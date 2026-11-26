"""Модернизированная система синтеза речи (TTS) для AI-коуча Dota 2.

Поддерживает:
1. Живой гибридный TTS: In-Memory Streaming через edge-tts (ru-RU-DmitryNeural / Svetlana)
   с разговорной просодией (rate="+10%", pitch="-3Hz") без записи временных файлов на диск.
2. Офлайн / Fallback режим: локальный Piper TTS с параметрами noise_scale=0.75, length_scale=0.92
   и автоматическим каскадным откатом на SAPI/pyttsx3 при любых сбоях.
3. Опциональный хук под Voice Conversion (RVC) через rvc-python.
4. Фонетический словарь Dota 2 и интонационная нормализация (normalize_for_speech).
5. Локальный Smart Cache частых фраз (cache/audio/) по MD5-хэшу (<0.01 сек отклик).
6. Приоритетная очередь фраз (PriorityQueue) с Barge-In (прерывание при HP < 25% / ганге)
   и контролем времени жизни (TTL = 5 сек для таймеров).
7. Аудио-филлеры («Так, ща глянем...», «Секунду...», «Хм, слушай...») при задержке LLM > 0.8 сек.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from .config import TtsConfig

log = logging.getLogger("aidota.tts")


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    enqueued_at: float = field(compare=False)
    ttl: float = field(compare=False)
    text: str = field(compare=False)
    done_event: threading.Event = field(compare=False)
    generation_id: int = field(default=0, compare=False)
    is_filler: bool = field(default=False, compare=False)
    is_stop: bool = field(default=False, compare=False)
    success: bool = field(default=False, compare=False)


_TTS_STOP = _QueueItem(
    priority=-999999,
    seq=-1,
    enqueued_at=0.0,
    ttl=0.0,
    text="",
    done_event=threading.Event(),
    is_stop=True,
)


def _ensure_gen_py() -> None:
    """Создаёт заглушку для gen_py во временном каталоге, чтобы win32com не падал с Errno 2."""
    try:
        tmp = Path(tempfile.gettempdir()) / "gen_py" / f"{sys.version_info.major}.{sys.version_info.minor}"
        tmp.mkdir(parents=True, exist_ok=True)
        init_file = tmp / "__init__.py"
        if not init_file.is_file():
            init_file.write_text("# gen_py stub\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


class SpeechPriority(IntEnum):
    """Уровни приоритета воспроизведения речи."""

    CRITICAL = 1   # Низкое ХП (<25%), опасность ганга. Мгновенно прерывает текущую речь (Barge-In).
    TIMERS = 2     # Руны, стаки, Рошан, Торментор. Ждет окончания фразы, отбрасывается по TTL (5 с).
    LLM_COACH = 3  # Макро-советы и сборка предметов от LLM. Озвучивается по предложениям (streaming).
    NORMAL = 3


# Фонетический словарь Dota 2: русские и английские термины -> удобное для синтезатора произношение
_DOTA_PHONETIC_REPLACEMENTS: tuple[tuple[re.Pattern, str], ...] = (
    # Аббревиатуры
    (re.compile(r"\b(бкб|bkb)\b", re.IGNORECASE), "бэкабэ"),
    (re.compile(r"\b(мкб|mkb)\b", re.IGNORECASE), "эмкабэ"),
    (re.compile(r"\b(тп|tp)\b", re.IGNORECASE), "тэпэ"),
    (re.compile(r"\b(хп|hp)\b", re.IGNORECASE), "хэпэ"),
    (re.compile(r"\b(мп|mp)\b", re.IGNORECASE), "маны"),
    (re.compile(r"\b(кд|cd)\b", re.IGNORECASE), "кадэ"),
    (re.compile(r"\b(сс|ss)\b", re.IGNORECASE), "мисс"),
    (re.compile(r"\b(гпм|gpm)\b", re.IGNORECASE), "гэпээм"),
    (re.compile(r"\b(пт|pt)\b", re.IGNORECASE), "пэтэ"),
    (re.compile(r"\b(дпс|dps)\b", re.IGNORECASE), "дэпээс"),
    (re.compile(r"\b(кда|kda)\b", re.IGNORECASE), "кадэа"),
    (re.compile(r"\b(ммр|mmr)\b", re.IGNORECASE), "эмэмэр"),
    (re.compile(r"\b(lvl|лвл)\b", re.IGNORECASE), "уровень"),
    (re.compile(r"\bvs\.?\b", re.IGNORECASE), "против"),
    (re.compile(r"\b(дд|dd)\b", re.IGNORECASE), "двойной урон"),
    (re.compile(r"\bбб\b", re.IGNORECASE), "байбек"),
    (re.compile(r"\b(хг|hg)\b", re.IGNORECASE), "хайграунд"),
    (re.compile(r"\b[тt]1\b", re.IGNORECASE), "тэ один"),
    (re.compile(r"\b[тt]2\b", re.IGNORECASE), "тэ два"),
    (re.compile(r"\b[тt]3\b", re.IGNORECASE), "тэ три"),
    (re.compile(r"\b[тt]4\b", re.IGNORECASE), "тэ четыре"),
    # Ударения и специфика имён/объектов
    (re.compile(r"\bаганим([а-яё]*)\b", re.IGNORECASE), r"агани+м\1"),
    (re.compile(r"\b(aghanim|aganim)s?\b", re.IGNORECASE), "агани+м"),
    (re.compile(r"\bрошан([а-яё]*)\b", re.IGNORECASE), r"роша+н\1"),
    (re.compile(r"\broshans?\b", re.IGNORECASE), "роша+н"),
    (re.compile(r"\bторментор([а-яё]*)\b", re.IGNORECASE), r"торме+нтор\1"),
    (re.compile(r"\btormentors?\b", re.IGNORECASE), "торме+нтор"),
    (re.compile(r"\bтерзател([ьяеию]|ь|ем)?\b", re.IGNORECASE), r"терза+тел\1"),
    # Предметы патчей 7.36/7.37
    (re.compile(r"\b(кханда|khanda)\b", re.IGNORECASE), "кха+нда"),
    (re.compile(r"\b(парасма|parasma)\b", re.IGNORECASE), "пара+сма"),
    (re.compile(r"\b(гарпун|harpoon)\b", re.IGNORECASE), "гарпу+н"),
    (re.compile(r"\b(дисперсер|disperser)\b", re.IGNORECASE), "диспе+рсер"),
)

# Предзаписанные/кэшируемые фразы задумчивости (аудио-филлеры)
FILLERS: tuple[str, ...] = (
    "Так, ща глянем...",
    "Секунду...",
    "Хм, слушай...",
)

# Детерминированные фразы Контура 0 для предварительного кэширования на диск в AppData
REFLEX_CACHED_PHRASES: tuple[str, ...] = (
    *FILLERS,
    "Внимание, критически мало хэпэ! Отойди под вышку или жми стики!",
    "Не хватает маны на ульт — заряди стики или отступи под вышку.",
    "Забыт ТП-свиток — доложи в быстрый закуп и привези курьером.",
    "Внимание: через 30 секунд пропадёт Аегис!",
    "Внимание: через 30 секунд возможно появление Рошана!",
    "Внимание! Вражеский мидер достиг шестого уровня! Возможен ганг на боковых линиях.",
    "Через 20 секунд водные руны на миду, запушь пачку.",
    "Через 20 секунд активная руна на реке — помогите законтролить спавн.",
    "Через 20 секунд появятся лотосы (лечение и мана) и руны богатства.",
    "Через 20 секунд появится Терзатель на двадцатой минуте! Соберитесь за бесплатным Шардом.",
    "Через 20 секунд волна катапульт — можно давить вышку!",
    "53-я секунда — время стакать нейтралов.",
    "Седьмая минута — появились нейтральные предметы 1-го разряда! Зачищайте лесные лагеря за жетонами.",
)


def normalize_for_speech(text: str, max_chars: int = 700) -> str:
    """Нормализует текст строго ПЕРЕД отправкой в TTS:

    1. Очистка: удаляет markdown (*, _, #, `, ~, >), списочные маркеры, скобки, кавычки, эмодзи.
    2. Фонетический словарь Dota 2: заменяет бкб, тп, хп, мп, кд, сс, гпм, аганим, рошан, торментор.
    3. Интонационная разметка: заменяет двоеточия и точки с запятой на тире/многоточия для дыхательных пауз.
    """
    if not text or not str(text).strip():
        return ""

    raw = str(text)

    # 0. Математические символы и проценты
    cleaned = re.sub(r"<\s*=?\s*", "меньше ", raw)
    cleaned = re.sub(r">\s*=?\s*(?=\d)", "больше ", cleaned)
    cleaned = re.sub(r"%", " процентов", cleaned)

    # 1. Ссылки и markdown
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned)
    cleaned = re.sub(r"[*_#`~>]+", " ", cleaned)

    # Маркеры списков и нумерация в начале строк
    cleaned = re.sub(r"(?:^|\n)\s*[-•–—*]\s*", "\n", cleaned)
    cleaned = re.sub(r"(?:^|\n)\s*\d+[\.)]\s*", "\n", cleaned)

    # Скобки и кавычки (оставляем внутренний текст)
    cleaned = re.sub(r"[(){}\[\]\"«»'“”]", " ", cleaned)

    # 3. Интонационная разметка: двоеточия и точки с запятой заменяем на тире для дыхательной паузы
    cleaned = re.sub(r"[:;]\s+", " — ", cleaned)

    # 2. Фонетические замены Dota 2
    for pattern, replacement in _DOTA_PHONETIC_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)

    # Убираем эмодзи и спецсимволы, оставляя буквы, цифры, пробелы и пунктуацию
    cleaned = re.sub(r"[^\w\s.,!?:;—–…\-\+]", "", cleaned)

    # Нормализация переносов строк и многоточий
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = re.sub(r"\n+", ". ", cleaned)
    cleaned = re.sub(r"(\.\s*){2,}", "... ", cleaned)
    cleaned = re.sub(r"\s+([,.:;?!])", r"\1", cleaned)
    cleaned = re.sub(r"\s*([—–])\s*", r" \1 ", cleaned)
    cleaned = cleaned.strip()

    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars].rsplit(" ", 1)[0]
        cleaned = cut.rstrip(".,;:— ") + "… Подробности на экране."

    return cleaned


# Алиас для обратной совместимости со старыми модулями
sanitize_for_speech = normalize_for_speech


def _is_corrupted_static_waveform(data: Any) -> bool:
    """Обнаруживает битый PCM-сигнал (нефинитные сэмплы или CRLF-сдвинутый белый шум)."""
    try:
        import numpy as np

        arr = np.asarray(data, dtype=np.float32)
        if arr.size == 0:
            return False
        if not bool(np.isfinite(arr).all()):
            return True
        if arr.size < 512:
            return False
        mono = arr.mean(axis=1) if arr.ndim == 2 else arr.ravel()
        rms = float(np.sqrt(np.mean(mono * mono)))
        if rms <= 0.28:
            return False
        diff = np.diff(mono)
        diff_rms = float(np.sqrt(np.mean(diff * diff)))
        hf_ratio = diff_rms / max(rms, 1e-6)
        return hf_ratio > 1.10
    except Exception:  # noqa: BLE001
        return False


class SmartAudioCache:
    """Локальный Smart Cache аудио-фраз на диске (cache/audio/) и в оперативной памяти."""

    def __init__(self, cache_dir: Path | None = None, *, max_disk_files: int = 800):
        if cache_dir:
            p = Path(cache_dir)
            self.disk_dir = p if p.name == "audio" else p / "audio"
        else:
            self.disk_dir = Path(__file__).resolve().parent.parent / "cache" / "audio"
        try:
            self.disk_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        self._max_disk_files = max(50, int(max_disk_files))
        self._ram: dict[str, tuple[Any, int]] = {}
        self._save_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _hash_key(self, text: str, voice: str, rate: str, pitch: str, provider: str = "edge") -> str:
        content = f"{provider}|{voice}|{rate}|{pitch}|{text.strip().lower()}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def has(self, text: str, voice: str, rate: str, pitch: str, provider: str = "edge") -> bool:
        """Быстрая проверка наличия фразы в RAM или дисковом кэше без декодирования."""
        key = self._hash_key(text, voice, rate, pitch, provider=provider)
        with self._lock:
            if key in self._ram:
                return True
        return (self.disk_dir / f"{key}.wav").is_file()

    def get(self, text: str, voice: str, rate: str, pitch: str, provider: str = "edge") -> tuple[Any, int] | None:
        """Возвращает (numpy_data, samplerate) за <0.01 сек, если фраза уже кэшировалась."""
        key = self._hash_key(text, voice, rate, pitch, provider=provider)
        with self._lock:
            if key in self._ram:
                return self._ram[key]

        wav_path = self.disk_dir / f"{key}.wav"
        if wav_path.is_file():
            try:
                import soundfile as sf

                data, sr = sf.read(str(wav_path), dtype="float32")
                if _is_corrupted_static_waveform(data):
                    log.warning("Удаляю повреждённый кэш аудио %s (обнаружен белый шум)", wav_path)
                    try:
                        wav_path.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                    return None
                with self._lock:
                    if len(self._ram) > 500:
                        self._ram.clear()
                    self._ram[key] = (data, sr)
                return data, sr
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось прочитать дисковый кэш %s: %s", wav_path, exc)
        return None

    def put(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        data: Any,
        sr: int,
        *,
        provider: str = "edge",
        async_save: bool = True,
    ) -> None:
        """Сохраняет аудио в RAM и асинхронно на диск."""
        if _is_corrupted_static_waveform(data):
            log.warning("Отказ в кэшировании повреждённого аудио (обнаружен шум/нефинитные сэмплы)")
            return
        key = self._hash_key(text, voice, rate, pitch, provider=provider)
        with self._lock:
            # Очищаем завершившиеся потоки для предотвращения утечки объектов Thread в памяти
            self._save_threads = [t for t in self._save_threads if t.is_alive()]
            if len(self._ram) > 500:
                self._ram.clear()
            self._ram[key] = (data, sr)

        def _save_disk():
            try:
                import soundfile as sf

                wav_path = self.disk_dir / f"{key}.wav"
                sf.write(str(wav_path), data, sr, format="WAV")
                try:
                    existing = sorted(self.disk_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
                    excess = len(existing) - self._max_disk_files
                    for stale in existing[: max(0, excess)]:
                        stale.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось записать аудио-кэш %s: %s", key, exc)

        if async_save:
            t = threading.Thread(target=_save_disk, name="aidota-cache-saver", daemon=True)
            with self._lock:
                self._save_threads.append(t)
            t.start()
        else:
            _save_disk()

    def wait_pending(self, timeout: float = 1.0) -> None:
        """Ожидает завершения фоновых потоков записи на диск."""
        for t in list(self._save_threads):
            if t.is_alive():
                t.join(timeout=timeout)
        self._save_threads = [t for t in self._save_threads if t.is_alive()]


class HumanTTSManager:
    """Модернизированный гибридный TTS-менеджер с приоритетами, In-Memory стримингом,

    Barge-In прерыванием и мгновенным кэшированием.
    """

    def __init__(self, config: TtsConfig, cache_dir: Path | None = None):
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        self._cache = SmartAudioCache(self.cache_dir)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._generation_id = 0
        self._queue_lock = threading.Lock()
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None
        self.on_speaking_changed: Callable[[bool], None] | None = None
        self._active_deadline: float | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._interrupt_event = threading.Event()
        self._is_speaking_event = threading.Event()
        self._active_priority: int = int(SpeechPriority.NORMAL)
        self._active_generation: int = 0
        self._local_adapters: list[tuple[str, Callable[[str], tuple[Any, int]]]] = []

        self._engine = None
        self._pyttsx_engine = None
        self._error: str | None = None
        self._voices: list[str] = []
        self._backend: str | None = None
        self._speak_failed = False
        self._init_done = threading.Event()
        self._edge_consecutive_failures: int = 0
        self._edge_cooldown_until: float = 0.0
        self._edge_failure_threshold: int = 2
        self._edge_cooldown_seconds: float = 180.0

        # Предварительно определяем наличие pygame
        self._pygame_mixer = None
        self._init_audio_backend()

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str | None:
        return self._backend

    @property
    def voices(self) -> list[str]:
        return list(self._voices)

    def is_speaking(self) -> bool:
        """Возвращает True, если звук активно воспроизводится в данный момент."""
        return self._is_speaking_event.is_set()

    # ------------------------------------------------------------------ #
    def _init_audio_backend(self) -> None:
        """Инициализирует pygame.mixer, если библиотека установлена."""
        try:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=48000, size=-16, channels=2, buffer=512)
            self._pygame_mixer = pygame.mixer
            log.info("Аудио-бэкенд: pygame.mixer инициализирован успешно")
        except Exception as exc:  # noqa: BLE001
            self._pygame_mixer = None
            log.debug("pygame.mixer недоступен (%s), используется sounddevice", exc)

    # ------------------------------------------------------------------ #
    def current_generation(self) -> int:
        with self._queue_lock:
            return self._generation_id

    def bump_generation(self) -> int:
        """Увеличивает поколение речи и очищает устаревшую очередь."""
        with self._queue_lock:
            self._generation_id += 1
            gen = self._generation_id
            dropped = self._drain_stale_locked(min_generation=gen)
        if dropped:
            self._emit_event("purged_stale", generation_id=gen, count=dropped)
        return gen

    def _emit_event(self, kind: str, **payload: Any) -> None:
        cb = self.on_event
        if callable(cb):
            try:
                cb(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def _notify_speaking(self, speaking: bool) -> None:
        cb = self.on_speaking_changed
        if callable(cb):
            try:
                cb(bool(speaking))
            except Exception:  # noqa: BLE001
                pass

    def _drain_stale_locked(
        self, *, min_generation: int, preserve_critical: bool = False
    ) -> int:
        """Очищает из очереди элементы ниже ``min_generation`` (под ``_queue_lock``)."""
        kept: list[_QueueItem] = []
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if not isinstance(item, _QueueItem) or item.is_stop:
                kept.append(item)
                continue
            is_critical = item.priority <= SpeechPriority.CRITICAL
            if preserve_critical and is_critical and item.generation_id >= min_generation:
                kept.append(item)
                continue
            if item.generation_id < min_generation or (not preserve_critical and item.priority > SpeechPriority.CRITICAL):
                item.success = False
                item.done_event.set()
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            self._queue.put(item)
        return dropped

    def barge_in(self, *, preserve_critical: bool = False) -> None:
        """Мгновенно прерывает текущее воспроизведение и очищает устаревшие фразы (Level 1 CRITICAL)."""
        log.info("Barge-In: прерывание текущего аудиопотока (preserve_critical=%s)", preserve_critical)
        with self._queue_lock:
            self._generation_id += 1
            gen = self._generation_id
            dropped = self._drain_stale_locked(
                min_generation=gen, preserve_critical=preserve_critical
            )
            critical_active = (
                preserve_critical
                and self._is_speaking_event.is_set()
                and int(getattr(self, "_active_priority", SpeechPriority.NORMAL))
                <= int(SpeechPriority.CRITICAL)
            )
        if not critical_active:
            self._interrupt_event.set()
            self._stop_current_playback()
        self._emit_event(
            "barge_in",
            generation_id=gen,
            purged=dropped,
            preserved_critical=critical_active,
        )

    def _stop_current_playback(self) -> None:
        """Останавливает звук на текущем бэкенде."""
        if self._engine is not None:
            try:
                # SVSFlagsAsync | SVSFPurgeBeforeSpeak
                self._engine.Speak("", 3)
            except Exception:  # noqa: BLE001
                pass

        if self._pygame_mixer is not None:
            try:
                self._pygame_mixer.stop()
            except Exception:  # noqa: BLE001
                pass

        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def speak(
        self,
        text: str,
        wait: bool = True,
        timeout: float = 180.0,
        priority: int = SpeechPriority.NORMAL,
        ttl: float = 0.0,
        generation_id: int | None = None,
    ) -> bool:
        """Озвучивает текст с учётом приоритетов, generation_id и нормализации.

        - priority=SpeechPriority.CRITICAL: немедленно прерывает текущую речь (Barge-In).
        - priority=SpeechPriority.TIMERS: TTL по умолчанию 5.0 секунд (устаревшие фразы отбрасываются).
        - priority=SpeechPriority.LLM_COACH: стриминг по предложениям.
        """
        if not self.enabled or not text or not str(text).strip():
            return False

        norm = normalize_for_speech(text, self.config.max_chars)
        if not norm:
            return False

        # CRITICAL: немедленно глушим текущий звук и очищаем старую очередь
        if priority == SpeechPriority.CRITICAL:
            self.barge_in()

        eff_gen = self.current_generation() if generation_id is None else int(generation_id)
        if eff_gen < self.current_generation():
            self._emit_event("dropped_preempted", text=norm, generation_id=eff_gen)
            return False

        # TIMERS: по умолчанию TTL = 5.0 секунд
        if priority == SpeechPriority.TIMERS and ttl <= 0.0:
            ttl = 5.0

        # LLM_COACH: разбиваем длинный текст на предложения для sentence streaming
        if priority == SpeechPriority.LLM_COACH:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", norm) if s.strip()]
            if len(sentences) > 1:
                last_ok = True
                for i, sent in enumerate(sentences):
                    # Ждём завершения только последнего предложения, если wait=True
                    is_last = (i == len(sentences) - 1)
                    ok = self._enqueue_speech(
                        sent,
                        priority=priority,
                        ttl=ttl,
                        wait=(wait if is_last else False),
                        timeout=timeout,
                        generation_id=eff_gen,
                    )
                    if not ok:
                        last_ok = False
                return last_ok

        return self._enqueue_speech(
            norm,
            priority=priority,
            ttl=ttl,
            wait=wait,
            timeout=timeout,
            generation_id=eff_gen,
        )

    def _enqueue_speech(
        self,
        norm_text: str,
        priority: int,
        ttl: float,
        wait: bool,
        timeout: float,
        is_filler: bool = False,
        generation_id: int | None = None,
    ) -> bool:
        self._ensure_thread()
        done = threading.Event()
        with self._queue_lock:
            eff_gen = self._generation_id if generation_id is None else int(generation_id)
            if eff_gen < self._generation_id:
                return False
            self._seq += 1
            item = _QueueItem(
                priority=int(priority),
                seq=self._seq,
                enqueued_at=time.time(),
                ttl=float(ttl),
                text=norm_text,
                done_event=done,
                generation_id=eff_gen,
                is_filler=is_filler,
            )
            self._queue.put(item)

        if wait:
            finished = done.wait(timeout)
            if not finished:
                return False
            return getattr(item, "success", False)
        return not self._speak_failed and self._error is None

    def play_filler(
        self,
        filler: str | None = None,
        *,
        generation_id: int | None = None,
    ) -> bool:
        """Проигрывает короткую фразу задумчивости (аудио-филлер) из кэша."""
        phrase = filler or random.choice(FILLERS)
        norm = normalize_for_speech(phrase)
        log.debug("Воспроизведение аудио-филлера: %s", norm)
        return self._enqueue_speech(
            norm,
            priority=SpeechPriority.NORMAL,
            ttl=2.0,
            wait=False,
            timeout=5.0,
            is_filler=True,
            generation_id=generation_id,
        )

    def start_filler_timer(
        self,
        delay: float = 0.8,
        *,
        generation_id: int | None = None,
    ) -> Callable[[], None]:
        """Запускает таймер аудио-филлера, если генерация LLM длится дольше delay секунд.

        Возвращает функцию отмены таймера.
        """
        cancelled = threading.Event()
        start_gen = self.current_generation() if generation_id is None else int(generation_id)

        def _worker():
            if cancelled.wait(delay):
                return
            if not self.enabled:
                return
            if self.current_generation() != start_gen:
                return
            if not self.is_speaking():
                self.play_filler()

        threading.Thread(target=_worker, name="aidota-filler-timer", daemon=True).start()

        def _cancel():
            cancelled.set()

        return _cancel

    # ------------------------------------------------------------------ #
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="aidota-tts", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        _ensure_gen_py()
        try:
            import pythoncom  # type: ignore

            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pythoncom = None

        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.ole32.CoInitialize(None)
            except Exception:  # noqa: BLE001
                pass

        try:
            self._init_engine()
        finally:
            self._init_done.set()

        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if item is _TTS_STOP or getattr(item, "is_stop", False):
                    break

                if not isinstance(item, _QueueItem):
                    continue

                # Проверка актуальности поколения (Fork B / Preemption)
                if item.generation_id < self.current_generation():
                    self._emit_event("skipped_preempted", text=item.text, generation_id=item.generation_id)
                    item.success = False
                    item.done_event.set()
                    continue

                # Проверка TTL: если фраза устарела — отбрасываем
                if item.ttl > 0.0:
                    age = time.time() - item.enqueued_at
                    if age > item.ttl:
                        log.info(
                            "Фраза устарела по TTL (%.1fs > %.1fs): «%s», пропускаю",
                            age,
                            item.ttl,
                            item.text,
                        )
                        self._emit_event("dropped_ttl_expired", text=item.text, age=age, ttl=item.ttl)
                        item.success = False
                        item.done_event.set()
                        continue

                # Сбрасываем флаг прерывания перед началом озвучивания
                self._interrupt_event.clear()
                self._active_deadline = (item.enqueued_at + item.ttl) if item.ttl > 0.0 else None
                self._active_generation = item.generation_id
                self._active_priority = int(item.priority)
                self._is_speaking_event.set()
                self._notify_speaking(True)
                t_tts_start = time.monotonic()
                try:
                    self._say(item.text)
                    item.success = not self._speak_failed
                except Exception:
                    item.success = False
                finally:
                    ms_tts = int((time.monotonic() - t_tts_start) * 1000)
                    self._active_deadline = None
                    self._active_priority = int(SpeechPriority.NORMAL)
                    self._is_speaking_event.clear()
                    self._notify_speaking(False)
                    self._emit_event(
                        "spoken" if item.success else "speak_failed",
                        text=item.text,
                        ms_tts=ms_tts,
                        backend=self._backend,
                        generation_id=item.generation_id,
                        priority=int(item.priority),
                    )
                    item.done_event.set()
        finally:
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass
            if sys.platform == "win32":
                try:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    def _init_engine(self) -> None:
        """Инициализация вспомогательных системных бэкендов SAPI/pyttsx3."""
        _ensure_gen_py()

        # 1. Windows SAPI через win32com
        try:
            engine = self._dispatch_sapi("SAPI.SpVoice")
            voices = engine.GetVoices()
            self._voices = []
            chosen = None
            hint = (self.config.voice_hint or "Dmitry").lower()
            for index in range(voices.Count):
                try:
                    voice = voices.Item(index)
                    desc = str(voice.GetDescription())
                except Exception:  # noqa: BLE001
                    continue
                self._voices.append(desc)
                if chosen is None and hint and hint in desc.lower():
                    chosen = voice
                if chosen is None and "russian" in desc.lower():
                    chosen = voice
            if chosen is not None:
                engine.Voice = chosen
            try:
                engine.Rate = max(-10, min(10, int(self.config.rate)))
                engine.Volume = max(0, min(100, int(self.config.volume)))
            except Exception:  # noqa: BLE001
                pass

            if self.config.output_device is not None:
                self._apply_audio_output(engine, self.config.output_device)

            self._engine = engine
            prov = str(getattr(self.config, "provider", "edge") or "edge").lower()
            if prov == "edge":
                self._backend = "Edge (нейросеть Dmitry)"
            elif prov == "piper" and self._find_piper_binary_and_model()[0] is not None:
                self._backend = "Piper (офлайн-нейросеть)"
            else:
                self._backend = "SAPI"
            self._error = None
            log.info("Озвучка готова, бэкенд: %s, SAPI-голосов: %d", self._backend, len(self._voices))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("SAPI через win32com недоступен: %s. Пробую pyttsx3…", exc)

        # 2. pyttsx3
        try:
            import pyttsx3

            py_engine = pyttsx3.init()
            self._voices = []
            chosen_id = None
            hint = (self.config.voice_hint or "Dmitry").lower()
            for v in py_engine.getProperty("voices"):
                name = getattr(v, "name", str(v))
                vid = getattr(v, "id", name)
                self._voices.append(name)
                if chosen_id is None and hint and hint in name.lower():
                    chosen_id = vid
                if chosen_id is None and "russian" in name.lower():
                    chosen_id = vid
            if chosen_id:
                py_engine.setProperty("voice", chosen_id)

            self._pyttsx_engine = py_engine
            self._backend = "pyttsx3"
            self._error = None
            log.info("Озвучка готова (pyttsx3), голосов: %d", len(self._voices))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Локальные SAPI/pyttsx3 недоступны: %s", exc)
            if getattr(self.config, "provider", "edge") == "edge":
                self._backend = "Edge (нейросеть Dmitry)"
                self._error = None
            else:
                self._error = f"Системный синтез речи недоступен: {exc}."

    def _dispatch_sapi(self, prog_id: str = "SAPI.SpVoice"):
        import win32com.client  # type: ignore

        try:
            return win32com.client.Dispatch(prog_id)
        except Exception:
            return win32com.client.dynamic.Dispatch(prog_id)

    def _apply_audio_output(self, engine: Any, device: int | str | None) -> bool:
        if device is None or str(device).strip().lower() in ("default", "none", "null", ""):
            return False
        try:
            outputs = engine.GetAudioOutputs()
            from .audio import resolve_device

            _, dev_name = resolve_device(device, kind="output")
            target_str = (dev_name or str(device)).lower()

            for index in range(outputs.Count):
                try:
                    item = outputs.Item(index)
                    desc = str(item.GetDescription()).lower()
                    if target_str in desc or desc in target_str:
                        engine.AudioOutput = item
                        log.info("SAPI устройство вывода установлено: %s", item.GetDescription())
                        return True
                except Exception:
                    continue
        except Exception as exc:
            log.warning("Не удалось применить AudioOutput к SAPI: %s", exc)
        return False

    def set_output_device(self, device: int | str | None) -> None:
        self.config.output_device = device
        if self._engine is not None:
            self._apply_audio_output(self._engine, device)
        log.info("Устройство вывода TTS установлено: %s", device)

    def register_local_backend(
        self,
        name: str,
        synthesizer: Callable[[str], tuple[Any, int]],
    ) -> None:
        """Регистрирует опциональный локальный офлайн-адаптер TTS (Silero / Coqui / др.)."""
        clean_name = str(name or "").strip()
        if not clean_name or not callable(synthesizer):
            raise ValueError("Укажите непустое имя и вызываемый синтезатор (text -> (audio, sr))")
        with self._queue_lock:
            self._local_adapters = [
                (n, fn) for n, fn in self._local_adapters if n != clean_name
            ]
            self._local_adapters.append((clean_name, synthesizer))

    def prewarm_phrase_cache(
        self,
        phrases: tuple[str, ...] | list[str] | None = None,
        *,
        background: bool = True,
    ) -> int:
        """Предзагружает частые детерминированные фразы Контура 0 в дисковый кэш AppData."""
        if not self.enabled or not bool(getattr(self.config, "cache_audio", True)):
            return 0
        target_phrases = tuple(phrases) if phrases is not None else REFLEX_CACHED_PHRASES
        voice = getattr(self.config, "edge_voice", "ru-RU-DmitryNeural") or "ru-RU-DmitryNeural"
        rate = getattr(self.config, "rate_str", "+10%")
        pitch = getattr(self.config, "pitch_str", "-3Hz")
        current_provider = getattr(self.config, "provider", "edge")
        piper_exe, _ = self._find_piper_binary_and_model()

        pending: list[str] = []
        for raw_phrase in target_phrases:
            norm = normalize_for_speech(raw_phrase)
            if not norm:
                continue
            if self._cache.has(norm, voice, rate, pitch, provider=current_provider):
                continue
            if piper_exe is not None and self._cache.has(norm, voice, rate, pitch, provider="piper"):
                continue
            pending.append(norm)

        if not pending:
            return 0

        def _worker() -> None:
            for phrase in pending:
                if self._stop.is_set():
                    break
                try:
                    with self._queue_lock:
                        adapters = list(self._local_adapters)
                    if current_provider == "local" and adapters:
                        adapter_name, fn = adapters[0]
                        data, sr = fn(phrase)
                        self._cache.put(
                            phrase, voice, rate, pitch, data, sr,
                            provider=f"local:{adapter_name}", async_save=False,
                        )
                    elif piper_exe is not None and current_provider in ("piper", "edge"):
                        data, sr = self._synthesize_piper_audio(phrase)
                        self._cache.put(
                            phrase, voice, rate, pitch, data, sr,
                            provider="piper", async_save=False,
                        )
                    elif current_provider == "edge" and time.monotonic() >= float(self._edge_cooldown_until or 0.0):
                        data, sr = self._synthesize_edge_audio(phrase)
                        self._cache.put(
                            phrase, voice, rate, pitch, data, sr,
                            provider="edge", async_save=False,
                        )
                except Exception:  # noqa: BLE001
                    continue

        if background:
            t = threading.Thread(target=_worker, name="aidota-tts-prewarm", daemon=True)
            t.start()
        else:
            _worker()
        return len(pending)

    # ------------------------------------------------------------------ #
    # Хук под RVC (Voice Conversion)
    # ------------------------------------------------------------------ #
    def apply_rvc_if_enabled(self, audio_data: Any, samplerate: int) -> tuple[Any, int]:
        """Опциональный хук под RVC (Voice Conversion).

        При наличии локальной .pth модели и включённом rvc_enabled
        конвертирует голос через rvc-python. При отсутствии библиотек
        или ошибке возвращает исходные аудио-данные без сбоев.
        """
        if not getattr(self.config, "rvc_enabled", False):
            return audio_data, samplerate

        model_path = getattr(self.config, "rvc_model_path", "")
        if not model_path or not Path(model_path).is_file():
            log.debug("RVC включен, но модель '%s' не найдена", model_path)
            return audio_data, samplerate

        try:
            import rvc_python

            converted = rvc_python.infer_audio(audio_data, samplerate, model_path)
            if converted is not None:
                return converted
        except ImportError:
            log.warning("Библиотека rvc-python не установлена, пропускаю RVC")
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка обработки через RVC: %s", exc)

        return audio_data, samplerate

    # ------------------------------------------------------------------ #
    # Синтез через Edge-TTS (In-Memory Streaming)
    # ------------------------------------------------------------------ #
    def _synthesize_edge_audio(self, text: str) -> tuple[Any, int]:
        """Синтезирует речь через edge-tts в оперативной памяти (io.BytesIO) без записи .mp3 на диск."""
        import edge_tts
        import soundfile as sf

        voice = getattr(self.config, "edge_voice", "ru-RU-DmitryNeural") or "ru-RU-DmitryNeural"
        rate = getattr(self.config, "rate_str", "+10%") or "+10%"
        pitch = getattr(self.config, "pitch_str", "-3Hz") or "-3Hz"
        clean_text = text.replace("+", "")

        async def _fetch(p_val: str):
            comm = edge_tts.Communicate(
                clean_text,
                voice,
                rate=rate,
                pitch=p_val,
            )
            buf = io.BytesIO()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            buf.seek(0)
            return buf

        loop = asyncio.new_event_loop()
        has_piper = self._find_piper_binary_and_model()[0] is not None
        try:
            try:
                audio_buf = loop.run_until_complete(asyncio.wait_for(_fetch(pitch), timeout=10.0))
            except Exception as exc:
                if (
                    not has_piper
                    and pitch != "+0Hz"
                    and "NoAudioReceived" in type(exc).__name__
                ):
                    log.debug("Edge-TTS отклонил pitch='%s', повторяю с '+0Hz'", pitch)
                    audio_buf = loop.run_until_complete(asyncio.wait_for(_fetch("+0Hz"), timeout=10.0))
                else:
                    raise
        finally:
            loop.close()
        if audio_buf.getbuffer().nbytes == 0:
            raise RuntimeError("Edge-TTS вернул пустой буфер аудио")

        data, samplerate = sf.read(audio_buf, dtype="float32")
        return data, samplerate

    # ------------------------------------------------------------------ #
    # Синтез через Piper TTS (Офлайн / Fallback)
    # ------------------------------------------------------------------ #
    def _find_piper_binary_and_model(self) -> tuple[Path | None, Path | None]:
        root = Path(__file__).resolve().parent.parent
        piper_exe = root / "piper" / "piper.exe"
        if not piper_exe.is_file():
            return None, None

        models_dir = root / "piper" / "models"
        if not models_dir.is_dir():
            return None, None

        hint = str(getattr(self.config, "piper_voice", "") or getattr(self.config, "voice_hint", "") or "").lower()
        if "dmit" in hint or "jarvis" in hint or "джарвис" in hint:
            preferred = models_dir / "ru_RU-dmitri-medium.onnx"
            if preferred.is_file():
                return piper_exe, preferred

        irina = models_dir / "ru_RU-irina-medium.onnx"
        if irina.is_file():
            return piper_exe, irina

        onnx_files = list(models_dir.glob("*.onnx"))
        if onnx_files:
            return piper_exe, onnx_files[0]

        return None, None

    def _pick_piper_temp_dir(self) -> Path:
        """Выбирает временный каталог (предпочтительно ASCII-путь для нативного piper.exe)."""
        candidates: list[Path] = []
        sys_temp = Path(tempfile.gettempdir())
        if all(ord(c) < 128 for c in str(sys_temp)):
            candidates.append(sys_temp)
        if getattr(self, "cache_dir", None):
            cache_p = Path(self.cache_dir)
            if all(ord(c) < 128 for c in str(cache_p)):
                candidates.append(cache_p)
        candidates.append(sys_temp)
        for cand in candidates:
            try:
                cand.mkdir(parents=True, exist_ok=True)
                return cand
            except Exception:  # noqa: BLE001
                continue
        return sys_temp

    def _synthesize_piper_audio(self, text: str) -> tuple[Any, int]:
        """Офлайн-синтез речи через Piper TTS: noise_scale=0.75, length_scale=0.92, sentence_silence=0.1.

        Записывает результат во временный WAV-файл вместо stdout (``--output_file -``),
        так как на Windows текстовый режим stdout конвертирует ``0x0A`` в ``0x0D 0x0A``
        и сдвигает 16-битные PCM-сэмплы, превращая речь в громкий белый шум.
        """
        import soundfile as sf

        piper_exe, model_path = self._find_piper_binary_and_model()
        if piper_exe is None or model_path is None:
            raise FileNotFoundError("Piper TTS бинарник или модель не найдены")

        noise_scale = str(getattr(self.config, "piper_noise_scale", 0.75))
        length_scale = str(getattr(self.config, "piper_length_scale", 0.92))

        tmp_dir = self._pick_piper_temp_dir()
        fd, tmp_name = tempfile.mkstemp(prefix="aidota_piper_", suffix=".wav", dir=str(tmp_dir))
        os.close(fd)
        tmp_path = Path(tmp_name)

        cmd = [
            str(piper_exe),
            "--model", str(model_path),
            "--output_file", str(tmp_path),
            "--noise_scale", noise_scale,
            "--length_scale", length_scale,
            "--sentence_silence", "0.1",
        ]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        clean_piper_text = text.replace("+", "")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            try:
                _out, err = proc.communicate(input=clean_piper_text.encode("utf-8"), timeout=30.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"Piper error ({proc.returncode}): {err.decode('utf-8', errors='ignore')}")
            if not tmp_path.is_file() or tmp_path.stat().st_size <= 44:
                raise RuntimeError("Piper TTS не создал корректный WAV-файл")

            data, samplerate = sf.read(str(tmp_path), dtype="float32")
            if _is_corrupted_static_waveform(data):
                raise RuntimeError("Piper TTS вернул повреждённый аудиосигнал (белый шум)")
            return data, samplerate
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # Воспроизведение аудио с поддержкой Barge-In
    # ------------------------------------------------------------------ #
    def _play_audio(self, data: Any, samplerate: int) -> None:
        """Воспроизводит аудио через pygame.mixer (или sounddevice) с поддержкой немедленного Barge-In."""
        import numpy as np

        if self._interrupt_event.is_set():
            return

        arr = np.asarray(data, dtype=np.float32)
        if arr.size == 0:
            return
        if _is_corrupted_static_waveform(arr):
            log.warning("Отмена воспроизведения: обнаружен повреждённый сигнал (белый шум)")
            return

        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)

        # Защита от клиппинга до масштабирования громкости
        peak = float(np.max(np.abs(arr)))
        if peak > 0.88:
            arr = arr * (0.88 / peak)

        # Применение громкости (self.config.volume: 0..100)
        vol = max(0.0, min(1.0, float(getattr(self.config, "volume", 100)) / 100.0))
        arr = arr * vol

        # 1. Попытка воспроизведения через pygame.mixer
        if self._pygame_mixer is not None:
            try:
                import soundfile as sf

                buf = io.BytesIO()
                # Переводим в 16-битный PCM WAV для pygame.mixer.Sound
                int_data = (arr * 32767.0).clip(-32768, 32767).astype(np.int16)
                sf.write(buf, int_data, samplerate, format="WAV", subtype="PCM_16")
                buf.seek(0)

                sound = self._pygame_mixer.Sound(buf)
                channel = sound.play()
                if channel is not None:
                    while channel.get_busy() and not self._interrupt_event.is_set() and not self._stop.is_set():
                        time.sleep(0.02)
                    if self._interrupt_event.is_set() or self._stop.is_set():
                        channel.stop()
                    return
            except Exception as exc:  # noqa: BLE001
                log.debug("Воспроизведение через pygame.mixer не удалось: %s, перехожу на sounddevice", exc)

        # 2. Воспроизведение через sounddevice
        import sounddevice as sd

        from .audio import resolve_device

        dev_id, _ = resolve_device(self.config.output_device, kind="output")

        # Согласование частоты дискретизации
        target_sr = samplerate
        try:
            dev_info = sd.query_devices(dev_id, kind="output")
            native_sr = int(dev_info.get("default_samplerate", 0))
            if native_sr in (44100, 48000, 96000) and native_sr != samplerate:
                target_sr = native_sr
        except Exception:  # noqa: BLE001
            pass

        if target_sr != samplerate and arr.size > 0:
            num_target = int(round(len(arr) * target_sr / samplerate))
            x_orig = np.linspace(0, 1, len(arr), endpoint=False)
            x_target = np.linspace(0, 1, num_target, endpoint=False)
            if arr.ndim == 2:
                arr = np.column_stack([np.interp(x_target, x_orig, arr[:, c]) for c in range(arr.shape[1])]).astype(np.float32)
            else:
                arr = np.interp(x_target, x_orig, arr).astype(np.float32)
            samplerate = target_sr

        if arr.ndim == 1:
            arr = np.column_stack([arr, arr])

        sd.play(arr, samplerate, device=dev_id)
        while sd.get_stream() and sd.get_stream().active and not self._interrupt_event.is_set() and not self._stop.is_set():
            time.sleep(0.02)

        if self._interrupt_event.is_set() or self._stop.is_set():
            sd.stop()

    def _play_if_timely(self, data: Any, samplerate: int) -> bool:
        """Проверяет поколение и TTL после сетевого синтеза перед воспроизведением."""
        if self._interrupt_event.is_set():
            return False
        active_gen = getattr(self, "_active_generation", None)
        if active_gen is not None and int(active_gen) < self.current_generation():
            return False
        deadline = getattr(self, "_active_deadline", None)
        if deadline is not None and time.time() > float(deadline):
            self._emit_event("dropped_ttl_expired_after_synth", deadline=deadline)
            return False
        self._play_audio(data, samplerate)
        return not self._interrupt_event.is_set()

    # ------------------------------------------------------------------ #
    def _say(self, text: str) -> None:
        """Синтезирует и озвучивает фразу с каскадным откатом и кэшированием."""
        voice = getattr(self.config, "edge_voice", "ru-RU-DmitryNeural") or "ru-RU-DmitryNeural"
        rate = getattr(self.config, "rate_str", "+10%")
        pitch = getattr(self.config, "pitch_str", "-3Hz")
        cache_enabled = bool(getattr(self.config, "cache_audio", True))
        current_provider = getattr(self.config, "provider", "edge")

        piper_exe, _ = self._find_piper_binary_and_model()
        has_local_fallback = (piper_exe is not None) or (self._engine is not None) or (self._pyttsx_engine is not None)

        # Проверка Smart Cache (с учётом флага cache_audio, провайдера и локального кэша Контура 0)
        if cache_enabled:
            cached = self._cache.get(text, voice, rate, pitch, provider=current_provider)
            if cached is None and piper_exe is not None and current_provider != "piper":
                cached = self._cache.get(text, voice, rate, pitch, provider="piper")
            if cached is not None:
                data, sr = cached
                if self._play_if_timely(data, sr):
                    self._backend = "Smart Cache"
                    self._speak_failed = False
                    self._error = None
                return

        with self._queue_lock:
            local_adapters = list(self._local_adapters)

        # Опциональный явный локальный провайдер (если выбран current_provider == "local")
        if current_provider == "local" and local_adapters:
            for adapter_name, adapter_fn in local_adapters:
                if cache_enabled:
                    cached_loc = self._cache.get(
                        text, voice, rate, pitch, provider=f"local:{adapter_name}"
                    )
                    if cached_loc is not None:
                        data, sr = cached_loc
                        if self._play_if_timely(data, sr):
                            self._backend = f"Smart Cache ({adapter_name})"
                            self._speak_failed = False
                            self._error = None
                        return
                try:
                    data, samplerate = adapter_fn(text)
                    data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                    if cache_enabled:
                        self._cache.put(
                            text, voice, rate, pitch, data, samplerate, provider=f"local:{adapter_name}"
                        )
                    if self._play_if_timely(data, samplerate):
                        self._backend = f"Local ({adapter_name})"
                        self._speak_failed = False
                        self._error = None
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("Локальный TTS-адаптер %s не удался: %s", adapter_name, exc)

        # 1. Основной режим: Edge-TTS (In-Memory Streaming) с cooldown при повторных сбоях
        if current_provider == "edge":
            in_cooldown = has_local_fallback and (time.monotonic() < float(self._edge_cooldown_until or 0.0))
            if in_cooldown:
                log.debug("Edge-TTS на cooldown, сразу использую локальный синтез")
            else:
                try:
                    data, samplerate = self._synthesize_edge_audio(text)
                    self._edge_consecutive_failures = 0
                    self._edge_cooldown_until = 0.0
                    data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                    if cache_enabled:
                        self._cache.put(text, voice, rate, pitch, data, samplerate, provider="edge")
                    if self._play_if_timely(data, samplerate):
                        self._backend = f"Edge ({voice})"
                        self._speak_failed = False
                        self._error = None
                    return
                except Exception as exc:  # noqa: BLE001
                    self._edge_consecutive_failures += 1
                    if has_local_fallback and self._edge_consecutive_failures >= max(1, int(self._edge_failure_threshold)):
                        self._edge_cooldown_until = time.monotonic() + float(self._edge_cooldown_seconds)
                        log.warning(
                            "Синтез через edge-tts не удался (%s), включаю cooldown %.0fс и переключаюсь на локальный TTS…",
                            exc,
                            self._edge_cooldown_seconds,
                        )
                    else:
                        log.warning("Синтез через edge-tts не удался (%s), переключаюсь на резервный Piper TTS…", exc)

        # 2. Офлайн / Fallback: Piper TTS
        if current_provider == "piper" or piper_exe is not None:
            if cache_enabled and current_provider != "piper":
                cached_piper = self._cache.get(text, voice, rate, pitch, provider="piper")
                if cached_piper is not None:
                    data, sr = cached_piper
                    if self._play_if_timely(data, sr):
                        self._backend = "Smart Cache"
                        self._speak_failed = False
                        self._error = None
                    return
            try:
                data, samplerate = self._synthesize_piper_audio(text)
                data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                if cache_enabled:
                    self._cache.put(text, voice, rate, pitch, data, samplerate, provider="piper")
                if self._play_if_timely(data, samplerate):
                    self._backend = "Piper (офлайн-нейросеть)"
                    self._speak_failed = False
                    self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез через Piper TTS не удался (%s), переключаюсь на локальный резерв…", exc)

        # 2.5 Зарегистрированные локальные офлайн-адаптеры (если не сработали основные)
        if current_provider != "local" and local_adapters:
            for adapter_name, adapter_fn in local_adapters:
                try:
                    data, samplerate = adapter_fn(text)
                    data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                    if cache_enabled:
                        self._cache.put(
                            text, voice, rate, pitch, data, samplerate, provider=f"local:{adapter_name}"
                        )
                    if self._play_if_timely(data, samplerate):
                        self._backend = f"Local ({adapter_name})"
                        self._speak_failed = False
                        self._error = None
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("Локальный TTS-адаптер %s не удался: %s", adapter_name, exc)

        # 3. Локальный SAPI с поддержкой Barge-In прерывания
        if self._engine is not None:
            try:
                sapi_text = text.replace("+", "")
                # SVSFlagsAsync = 1
                self._engine.Speak(sapi_text, 1)
                while not self._interrupt_event.is_set() and not self._stop.is_set():
                    try:
                        done = self._engine.WaitUntilDone(100)
                        if done:
                            break
                    except Exception:
                        break
                if self._interrupt_event.is_set() or self._stop.is_set():
                    try:
                        # SVSFlagsAsync | SVSFPurgeBeforeSpeak = 3
                        self._engine.Speak("", 3)
                    except Exception:
                        pass
                self._backend = "SAPI"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("SAPI Speak не удался: %s", exc)

        # 4. pyttsx3
        if self._pyttsx_engine is not None:
            try:
                pyttsx_text = text.replace("+", "")
                self._pyttsx_engine.say(pyttsx_text)
                self._pyttsx_engine.runAndWait()
                self._backend = "pyttsx3"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("pyttsx3 say() не удался: %s", exc)

        if not self._speak_failed:
            self._error = "Не удалось воспроизвести речь ни одним из методов."
            log.warning(self._error)
        self._speak_failed = True

    # ------------------------------------------------------------------ #
    def probe(self, timeout: float = 8.0) -> dict:
        if self.enabled:
            self._ensure_thread()
            self._init_done.wait(timeout)
        return self.status()

    def close(self) -> None:
        self._stop.set()
        self._interrupt_event.set()
        while True:
            try:
                it = self._queue.get_nowait()
                if isinstance(it, _QueueItem) and not it.is_stop:
                    it.success = False
                    it.done_event.set()
            except queue.Empty:
                break
        self._queue.put(_TTS_STOP)
        self._stop_current_playback()
        self._cache.wait_pending()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "backend": self._backend,
            "voices": self._voices,
            "error": self._error,
        }


class Tts(HumanTTSManager):
    """Класс Tts для 100% обратной совместимости со всеми существующими модулями."""
