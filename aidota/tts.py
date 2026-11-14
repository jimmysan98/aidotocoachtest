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
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from .config import TtsConfig

log = logging.getLogger("aidota.tts")

_TTS_STOP = object()


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
    (re.compile(r"\blvl\b", re.IGNORECASE), "уровень"),
    (re.compile(r"\bvs\.?\b", re.IGNORECASE), "против"),
    # Ударения и специфика имён/объектов
    (re.compile(r"\bаганим([а-яё]*)\b", re.IGNORECASE), r"агани+м\1"),
    (re.compile(r"\b(aghanim|aganim)([a-z]*)\b", re.IGNORECASE), r"агани+м\2"),
    (re.compile(r"\bрошан([а-яё]*)\b", re.IGNORECASE), r"роша+н\1"),
    (re.compile(r"\broshan([a-z]*)\b", re.IGNORECASE), r"роша+н\1"),
    (re.compile(r"\bторментор([а-яё]*)\b", re.IGNORECASE), r"торме+нтор\1"),
    (re.compile(r"\btormentor([a-z]*)\b", re.IGNORECASE), r"торме+нтор\1"),
    (re.compile(r"\bтерзатель([а-яё]*)\b", re.IGNORECASE), r"терза+тель\1"),
)

# Предзаписанные/кэшируемые фразы задумчивости (аудио-филлеры)
FILLERS: tuple[str, ...] = (
    "Так, ща глянем...",
    "Секунду...",
    "Хм, слушай...",
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

    # 1. Ссылки и markdown
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", raw)
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


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    enqueued_at: float = field(compare=False)
    ttl: float = field(compare=False)
    text: str = field(compare=False)
    done_event: threading.Event = field(compare=False)
    is_filler: bool = field(default=False, compare=False)


class SmartAudioCache:
    """Локальный Smart Cache аудио-фраз на диске (cache/audio/) и в оперативной памяти."""

    def __init__(self, cache_dir: Path | None = None):
        if cache_dir:
            p = Path(cache_dir)
            self.disk_dir = p if p.name == "audio" else p / "audio"
        else:
            self.disk_dir = Path(__file__).resolve().parent.parent / "cache" / "audio"
        try:
            self.disk_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        self._ram: dict[str, tuple[Any, int]] = {}
        self._save_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _hash_key(self, text: str, voice: str, rate: str, pitch: str) -> str:
        content = f"{voice}|{rate}|{pitch}|{text.strip().lower()}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def get(self, text: str, voice: str, rate: str, pitch: str) -> tuple[Any, int] | None:
        """Возвращает (numpy_data, samplerate) за <0.01 сек, если фраза уже кэшировалась."""
        key = self._hash_key(text, voice, rate, pitch)
        with self._lock:
            if key in self._ram:
                return self._ram[key]

        wav_path = self.disk_dir / f"{key}.wav"
        if wav_path.is_file():
            try:
                import soundfile as sf

                data, sr = sf.read(str(wav_path), dtype="float32")
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
        async_save: bool = True,
    ) -> None:
        """Сохраняет аудио в RAM и асинхронно на диск."""
        key = self._hash_key(text, voice, rate, pitch)
        with self._lock:
            if len(self._ram) > 500:
                self._ram.clear()
            self._ram[key] = (data, sr)

        def _save_disk():
            try:
                import soundfile as sf

                wav_path = self.disk_dir / f"{key}.wav"
                sf.write(str(wav_path), data, sr, format="WAV")
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось записать аудио-кэш %s: %s", key, exc)

        if async_save:
            t = threading.Thread(target=_save_disk, name="aidota-cache-saver", daemon=True)
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
        self._queue_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._interrupt_event = threading.Event()
        self._is_speaking_event = threading.Event()

        self._engine = None
        self._pyttsx_engine = None
        self._error: str | None = None
        self._voices: list[str] = []
        self._backend: str | None = None
        self._speak_failed = False
        self._init_done = threading.Event()

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
    def barge_in(self) -> None:
        """Мгновенно прерывает текущее воспроизведение (Level 1 CRITICAL)."""
        log.info("Barge-In: прерывание текущего аудиопотока")
        self._interrupt_event.set()
        self._stop_current_playback()

    def _stop_current_playback(self) -> None:
        """Останавливает звук на текущем бэкенде."""
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
    ) -> bool:
        """Озвучивает текст с учётом приоритетов и нормализации.

        - priority=SpeechPriority.CRITICAL: немедленно прерывает текущую речь (Barge-In).
        - priority=SpeechPriority.TIMERS: TTL по умолчанию 5.0 секунд (устаревшие фразы отбрасываются).
        - priority=SpeechPriority.LLM_COACH: стриминг по предложениям.
        """
        if not self.enabled or not text or not str(text).strip():
            return False

        norm = normalize_for_speech(text, self.config.max_chars)
        if not norm:
            return False

        # CRITICAL: немедленно глушим текущий звук
        if priority == SpeechPriority.CRITICAL:
            self.barge_in()

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
                    )
                    if not ok:
                        last_ok = False
                return last_ok

        return self._enqueue_speech(norm, priority=priority, ttl=ttl, wait=wait, timeout=timeout)

    def _enqueue_speech(
        self,
        norm_text: str,
        priority: int,
        ttl: float,
        wait: bool,
        timeout: float,
        is_filler: bool = False,
    ) -> bool:
        self._ensure_thread()
        done = threading.Event()
        with self._queue_lock:
            self._seq += 1
            item = _QueueItem(
                priority=int(priority),
                seq=self._seq,
                enqueued_at=time.time(),
                ttl=float(ttl),
                text=norm_text,
                done_event=done,
                is_filler=is_filler,
            )
            self._queue.put(item)

        if wait:
            done.wait(timeout)
        return self._error is None or self._backend is not None

    def play_filler(self, filler: str | None = None) -> bool:
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
        )

    def start_filler_timer(self, delay: float = 0.8) -> Callable[[], None]:
        """Запускает таймер аудио-филлера, если генерация LLM длится дольше delay секунд.

        Возвращает функцию отмены таймера.
        """
        cancelled = threading.Event()

        def _worker():
            if cancelled.wait(delay):
                return
            if not self.enabled:
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

                if item is _TTS_STOP:
                    break

                if not isinstance(item, _QueueItem):
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
                        item.done_event.set()
                        continue

                # Сбрасываем флаг прерывания перед началом озвучивания
                self._interrupt_event.clear()
                self._is_speaking_event.set()
                try:
                    self._say(item.text)
                finally:
                    self._is_speaking_event.clear()
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
            self._backend = "Edge (нейросеть Dmitry)" if getattr(self.config, "provider", "edge") == "edge" else "SAPI"
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
        try:
            try:
                audio_buf = loop.run_until_complete(_fetch(pitch))
            except Exception as exc:
                if pitch != "+0Hz" and "NoAudioReceived" in type(exc).__name__:
                    log.debug("Edge-TTS отклонил pitch='%s', повторяю с '+0Hz'", pitch)
                    audio_buf = loop.run_until_complete(_fetch("+0Hz"))
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

    def _synthesize_piper_audio(self, text: str) -> tuple[Any, int]:
        """Офлайн-синтез речи через Piper TTS: noise_scale=0.75, length_scale=0.92, sentence_silence=0.1."""
        import soundfile as sf

        piper_exe, model_path = self._find_piper_binary_and_model()
        if piper_exe is None or model_path is None:
            raise FileNotFoundError("Piper TTS бинарник или модель не найдены")

        noise_scale = str(getattr(self.config, "piper_noise_scale", 0.75))
        length_scale = str(getattr(self.config, "piper_length_scale", 0.92))

        cmd = [
            str(piper_exe),
            "--model", str(model_path),
            "--output_file", "-",
            "--noise_scale", noise_scale,
            "--length_scale", length_scale,
            "--sentence_silence", "0.1",
        ]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        wav_bytes, err = proc.communicate(input=text.encode("utf-8"), timeout=30.0)
        if proc.returncode != 0:
            raise RuntimeError(f"Piper error ({proc.returncode}): {err.decode('utf-8', errors='ignore')}")

        buf = io.BytesIO(wav_bytes)
        data, samplerate = sf.read(buf, dtype="float32")
        return data, samplerate

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

        # Защита от клиппинга
        peak = float(np.max(np.abs(arr)))
        if peak > 0.88:
            arr = arr * (0.88 / peak)

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
            arr = np.interp(x_target, x_orig, arr).astype(np.float32)
            samplerate = target_sr

        if arr.ndim == 1:
            arr = np.column_stack([arr, arr])

        sd.play(arr, samplerate, device=dev_id)
        while sd.get_stream() and sd.get_stream().active and not self._interrupt_event.is_set() and not self._stop.is_set():
            time.sleep(0.02)

        if self._interrupt_event.is_set() or self._stop.is_set():
            sd.stop()

    # ------------------------------------------------------------------ #
    def _say(self, text: str) -> None:
        """Синтезирует и озвучивает фразу с каскадным откатом и кэшированием."""
        voice = getattr(self.config, "edge_voice", "ru-RU-DmitryNeural") or "ru-RU-DmitryNeural"
        rate = getattr(self.config, "rate_str", "+10%")
        pitch = getattr(self.config, "pitch_str", "-3Hz")

        # Проверка Smart Cache
        cached = self._cache.get(text, voice, rate, pitch)
        if cached is not None:
            data, sr = cached
            self._play_audio(data, sr)
            self._backend = "Smart Cache"
            self._speak_failed = False
            self._error = None
            return

        # 1. Основной режим: Edge-TTS (In-Memory Streaming)
        if getattr(self.config, "provider", "edge") == "edge":
            try:
                data, samplerate = self._synthesize_edge_audio(text)
                data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                self._cache.put(text, voice, rate, pitch, data, samplerate)
                self._play_audio(data, samplerate)
                self._backend = f"Edge ({voice})"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез через edge-tts не удался (%s), переключаюсь на резервный Piper TTS…", exc)

        # 2. Офлайн / Fallback: Piper TTS
        piper_exe, _ = self._find_piper_binary_and_model()
        if getattr(self.config, "provider", "edge") == "piper" or piper_exe is not None:
            try:
                data, samplerate = self._synthesize_piper_audio(text)
                data, samplerate = self.apply_rvc_if_enabled(data, samplerate)
                self._cache.put(text, voice, rate, pitch, data, samplerate)
                self._play_audio(data, samplerate)
                self._backend = "Piper (офлайн-нейросеть)"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез через Piper TTS не удался (%s), переключаюсь на системный SAPI…", exc)

        # 3. Локальный SAPI
        if self._engine is not None:
            try:
                self._engine.Speak(text, 0)
                self._backend = "SAPI"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("SAPI Speak не удался: %s", exc)

        # 4. pyttsx3
        if self._pyttsx_engine is not None:
            try:
                self._pyttsx_engine.say(text)
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
        self._queue.put(_TTS_STOP)
        self._stop_current_playback()
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
