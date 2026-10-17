"""Синтез речи через Windows SAPI с запасным воспроизведением через sounddevice / pyttsx3.

Почему так сложно: SAPI иногда не видит устройство вывода (или оно занято),
хотя умеет синтезировать звук в файл. Тогда синтезируем WAV и проигрываем
его сами. Если не работает ничего — остаётся текст на экране.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import sys
import tempfile
import threading
from pathlib import Path

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


def sanitize_for_speech(text: str, max_chars: int = 700) -> str:
    """Убирает markdown/эмодзи и обрезает слишком длинный текст."""
    cleaned = re.sub(r"[*_#`>]+", " ", text)
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned)
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = cleaned.replace("\n", ". ")
    cleaned = re.sub(r"(\.\s*){2,}", ". ", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars].rsplit(" ", 1)[0]
        cleaned = cut.rstrip(".,;:") + "… Подробности на экране."
    return cleaned


class Tts:
    """Очередь фраз + один рабочий поток (COM требует инициализации в потоке)."""

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
        return (self._engine is not None or self._pyttsx_engine is not None) and self._error is None

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
                    if (self._engine is not None or self._pyttsx_engine is not None) and not self._error:
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

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, tuple):
                item[1].set()

    def _dispatch_sapi(self, prog_id: str = "SAPI.SpVoice"):
        import win32com.client  # type: ignore

        try:
            return win32com.client.Dispatch(prog_id)
        except Exception:
            return win32com.client.dynamic.Dispatch(prog_id)

    def _init_engine(self) -> None:
        _ensure_gen_py()

        # 1. Попытка Windows SAPI через win32com
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

            self._engine = engine
            self._backend = "SAPI"
            self._error = None
            log.info(
                "Озвучка готова (SAPI), голосов: %d, выбран: %s",
                len(self._voices),
                str(engine.Voice.GetDescription()) if chosen is not None else "по умолчанию",
            )
            # Также инициализируем pyttsx3 как резерв на случай сбоя SAPI
            try:
                import pyttsx3

                self._pyttsx_engine = pyttsx3.init()
            except Exception:  # noqa: BLE001
                pass
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
            self._error = (
                f"Системный синтез речи недоступен: {exc}. "
                "Текст будет выводиться на экран; проверьте устройства вывода звука в Windows."
            )
            log.warning(self._error)

    def _say(self, text: str) -> None:
        if self._engine is not None:
            try:
                self._engine.Speak(text, 0)  # 0 = синхронно
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Прямое озвучивание SAPI не удалось: %s", exc)

            # Запасной путь 1: синтез в WAV + воспроизведение sounddevice / winsound
            try:
                wav_path = self._synthesize_wav(text)
                self._play_wav(wav_path)
                if not self._speak_failed:
                    self._backend = "SAPI->WAV"
                    log.info("Использую запасной путь озвучки: SAPI -> WAV")
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Синтез в WAV не удался: %s", exc)

        if self._pyttsx_engine is not None:
            try:
                self._pyttsx_engine.say(text)
                self._pyttsx_engine.runAndWait()
                self._speak_failed = False
                self._error = None
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("pyttsx3 say() не удался: %s", exc)

        if not self._speak_failed:
            self._error = (
                "Не удалось воспроизвести речь. Проверьте устройство вывода; текст остаётся на экране."
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
    def _play_wav(path: Path) -> None:
        played = False
        try:
            import wave

            import numpy as np
            import sounddevice as sd

            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                channels = wav.getnchannels()
                frames = wav.readframes(wav.getnframes())
            data = np.frombuffer(frames, dtype=np.int16)
            if channels > 1:
                data = data.reshape(-1, channels)
            sd.play(data, rate)
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
