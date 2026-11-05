"""Синтез речи через нейросетевой Microsoft Edge TTS с автоматическим откатом на Windows SAPI / pyttsx3.

Голос «Астра» по умолчанию использует нейросеть Microsoft Edge (ru-RU-SvetlanaNeural),
звучащую естественно и качественно, как в современных голосовых компаньонах (например, Astra / knice.tech).
При отсутствии интернета или недоступности Edge TTS происходит автоматический откат на Windows SAPI / pyttsx3.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import queue
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

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


_DOTA_PHONETIC_REPLACEMENTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bBKB\b", re.IGNORECASE), "БКБ"),
    (re.compile(r"\bPT\b", re.IGNORECASE), "ПТ"),
    (re.compile(r"\bDPS\b", re.IGNORECASE), "ДПС"),
    (re.compile(r"\bKDA\b", re.IGNORECASE), "КДА"),
    (re.compile(r"\bMMR\b", re.IGNORECASE), "ММР"),
    (re.compile(r"\bHP\b", re.IGNORECASE), "хп"),
    (re.compile(r"\bMP\b", re.IGNORECASE), "мп"),
    (re.compile(r"\bXP\b", re.IGNORECASE), "опыт"),
    (re.compile(r"\bvs\.?\b", re.IGNORECASE), "против"),
    (re.compile(r"\blvl\b", re.IGNORECASE), "уровень"),
)


def sanitize_for_speech(text: str, max_chars: int = 700) -> str:
    """Убирает markdown/эмодзи, нормализует аббревиатуры и делает текст естественным для нейросети."""
    # Убираем ссылки и markdown
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    cleaned = re.sub(r"[*_#`~>]+", " ", cleaned)
    # Убираем списочные дефисы и маркеры в начале строк
    cleaned = re.sub(r"(?:^|\n)\s*[-•–—*]\s*", "\n", cleaned)
    cleaned = re.sub(r"(?:^|\n)\s*\d+[\.)]\s*", "\n", cleaned)
    # Заменяем двоеточия после вводных слов на точки для естественной интонационной паузы
    cleaned = re.sub(r":\s+", ". ", cleaned)
    # Заменяем английские игровые сокращения на русские фонетические аналоги
    for pattern, replacement in _DOTA_PHONETIC_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = re.sub(r"\n+", ". ", cleaned)
    cleaned = re.sub(r"(\.\s*){2,}", ". ", cleaned)
    cleaned = re.sub(r"\s+([,.:;?!])", r"\1", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars].rsplit(" ", 1)[0]
        cleaned = cut.rstrip(".,;:") + "… Подробности на экране."
    return cleaned


class Tts:
    """Очередь фраз + один рабочий поток."""

    def __init__(self, config: TtsConfig, cache_dir: Path | None = None):
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._engine = None
        self._pyttsx_engine = None
        self._error: str | None = None
        self._voices: list[str] = []
        self._backend: str | None = None
        self._speak_failed = False
        self._init_done = threading.Event()

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

    # ------------------------------------------------------------------ #
    def speak(self, text: str, wait: bool = True, timeout: float = 180.0) -> bool:
        """Озвучивает текст. Возвращает True, если удалось запустить синтез."""
        if not self.enabled or not text or not text.strip():
            return False
        self._ensure_thread()
        done = threading.Event()
        self._queue.put((sanitize_for_speech(text, self.config.max_chars), done))
        if wait:
            done.wait(timeout)
        return self._error is None or self._backend is not None

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="aidota-tts", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
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
                item = self._queue.get()
                if item is _TTS_STOP:
                    break
                text, done = item
                try:
                    self._say(text)
                finally:
                    done.set()
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

    def _dispatch_sapi(self, prog_id: str = "SAPI.SpVoice"):
        import win32com.client  # type: ignore

        try:
            return win32com.client.Dispatch(prog_id)
        except Exception:
            return win32com.client.dynamic.Dispatch(prog_id)

    def _init_engine(self) -> None:
        _ensure_gen_py()

        # 1. Попытка Windows SAPI через win32com (как локальный бэкенд)
        try:
            engine = self._dispatch_sapi("SAPI.SpVoice")
            voices = engine.GetVoices()
            self._voices = []
            chosen = None
            hint = (self.config.voice_hint or "Irina").lower()
            for index in range(voices.Count):
                try:
                    voice = voices.Item(index)
                    description = str(voice.GetDescription())
                except Exception:  # noqa: BLE001
                    continue
                self._voices.append(description)
                if chosen is None and hint and hint in description.lower():
                    chosen = voice
                if chosen is None and "russian" in description.lower():
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
            self._backend = "Edge (нейросеть Astra)" if getattr(self.config, "provider", "edge") == "edge" else "SAPI"
            self._error = None
            log.info(
                "Озвучка готова, бэкенд: %s, SAPI-голосов: %d",
                self._backend,
                len(self._voices),
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("SAPI через win32com недоступен: %s. Пробую запасной вариант (pyttsx3)…", exc)

        # 2. Запасной вариант: pyttsx3
        try:
            import pyttsx3

            py_engine = pyttsx3.init()
            self._voices = []
            chosen_id = None
            hint = (self.config.voice_hint or "Irina").lower()
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
                self._backend = "Edge (нейросеть Astra)"
                self._error = None
            else:
                self._error = f"Системный синтез речи недоступен: {exc}."

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

    def _synthesize_edge_audio(self, text: str) -> tuple[Any, int]:
        """Синтезирует речь через edge-tts и возвращает (numpy_data, samplerate)."""
        import edge_tts
        import soundfile as sf

        voice = getattr(self.config, "edge_voice", "ru-RU-SvetlanaNeural") or "ru-RU-SvetlanaNeural"

        async def _fetch():
            comm = edge_tts.Communicate(text, voice)
            buf = io.BytesIO()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            buf.seek(0)
            return buf

        # Создаём или получаем event loop в текущем потоке
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        audio_buf = loop.run_until_complete(_fetch())
        data, samplerate = sf.read(audio_buf)
        return data, samplerate

    def _say(self, text: str) -> None:
        # 1. Приоритетный режим: нейросетевой голос Astra (edge-tts)
        if getattr(self.config, "provider", "edge") == "edge":
            try:
                data, samplerate = self._synthesize_edge_audio(text)
                from .audio import resolve_device
                import sounddevice as sd

                dev_id, _ = resolve_device(self.config.output_device, kind="output")
                sd.play(data, samplerate, device=dev_id)
                sd.wait()
                self._backend = "Edge (нейросеть Astra)"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез через edge-tts не удался (%s), переключаюсь на локальный SAPI…", exc)

        # 2. Локальный бэкенд SAPI
        if self._engine is not None:
            if self.config.output_device is None:
                try:
                    self._engine.Speak(text, 0)
                    self._backend = "SAPI"
                    self._speak_failed = False
                    self._error = None
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("Прямое озвучивание SAPI не удалось: %s", exc)
            else:
                try:
                    self._apply_audio_output(self._engine, self.config.output_device)
                    self._engine.Speak(text, 0)
                    self._backend = "SAPI"
                    self._speak_failed = False
                    self._error = None
                    return
                except Exception as exc:  # noqa: BLE001
                    log.debug("SAPI Speak с AudioOutput не удался: %s, пробую WAV", exc)

            try:
                wav_path = self._synthesize_wav(text)
                self._play_wav(wav_path, device=self.config.output_device)
                self._backend = "SAPI->WAV"
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез в WAV не удался: %s", exc)

        # 3. Запасной бэкенд pyttsx3
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
            self._error = (
                "Не удалось воспроизвести речь. Проверьте подключение к сети и устройство вывода; текст остаётся на экране."
            )
            log.warning(self._error)
        self._speak_failed = True

    def _synthesize_wav(self, text: str) -> Path:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        engine = self._dispatch_sapi("SAPI.SpVoice")
        if self._voices and self._engine is not None:
            voices = engine.GetVoices()
            for index in range(voices.Count):
                try:
                    if str(voices.Item(index).GetDescription()) == str(self._engine.Voice.GetDescription()):
                        engine.Voice = voices.Item(index)
                        break
                except Exception:
                    pass
        engine.Rate = max(-10, min(10, int(self.config.rate)))
        engine.Volume = max(0, min(100, int(self.config.volume)))
        fmt = self._dispatch_sapi("SAPI.SpAudioFormat")
        fmt.Type = 10  # SAFT22kHz16BitMono
        stream = self._dispatch_sapi("SAPI.SpFileStream")
        stream.Format = fmt

        fd, tmp_name = tempfile.mkstemp(prefix="aidota-tts-", suffix=".wav", dir=self.cache_dir)
        os.close(fd)
        path = Path(tmp_name)
        stream.Open(str(path), 3, False)  # 3 = SSFMCreateForWrite
        try:
            engine.AudioOutputStream = stream
            engine.Speak(text, 0)
        finally:
            stream.Close()
        return path

    @staticmethod
    def _play_wav(path: Path, device: int | str | None = None) -> None:
        played = False
        try:
            import wave

            import numpy as np
            import sounddevice as sd
            from .audio import resolve_device

            dev_id, _ = resolve_device(device, kind="output")

            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                channels = wav.getnchannels()
                frames = wav.readframes(wav.getnframes())
            data = np.frombuffer(frames, dtype=np.int16)
            if channels > 1:
                data = data.reshape(-1, channels)
            sd.play(data, rate, device=dev_id)
            sd.wait()
            played = True
        except Exception as exc:  # noqa: BLE001
            log.debug("sounddevice воспроизведение не удалось: %s, пробую winsound…", exc)

        if not played and sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME)
                played = True
            except Exception as exc:  # noqa: BLE001
                log.warning("winsound воспроизведение не удалось: %s", exc)
        try:
            path.unlink()
        except OSError:
            pass
        if not played:
            raise RuntimeError("Не удалось воспроизвести WAV-файл через sounddevice и winsound")

    # ------------------------------------------------------------------ #
    def probe(self, timeout: float = 8.0) -> dict:
        """Инициализирует движок (для самодиагностики) и возвращает статус."""
        if self.enabled:
            self._ensure_thread()
            self._init_done.wait(timeout)
        return self.status()

    def close(self) -> None:
        self._stop.set()
        self._queue.put(_TTS_STOP)
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
