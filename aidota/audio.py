"""Распознавание речи: Vosk + sounddevice + глобальный Push-to-Talk.

Модель грузится долго (2.5 ГБ), поэтому загрузка вынесена в отдельный
метод: приложение показывает прогресс и может работать в текстовом режиме,
пока модель недоступна.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .config import AudioConfig
from .hotkeys import HotkeyListener, format_key_name, parse_key

log = logging.getLogger("aidota.audio")


class AudioError(RuntimeError):
    """Понятная пользователю ошибка аудиоподсистемы."""


@contextmanager
def _suppress_native_stderr():
    """Глушит шумные логи Kaldi при загрузке модели."""
    try:
        if sys.stderr is None or not hasattr(sys.stderr, "fileno"):
            yield
            return
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        os.dup2(devnull, 2)
        try:
            yield
        finally:
            try:
                os.dup2(saved, 2)
                os.close(saved)
                os.close(devnull)
            except Exception:
                pass
    except Exception:  # noqa: BLE001
        yield


def safe_model_path_str(p: Path) -> str:
    """Возвращает безопасный строковый путь для vosk.Model.

    В Windows C-библиотека Kaldi/libvosk не поддерживает UTF-8 пути (Cyrillic,
    пробелы, символы вроде «Документы»). Для таких путей мы получаем короткое
    DOS-имя (8.3 GetShortPathNameW), с которым C-библиотека работает штатно.
    """
    p_res = p.resolve()
    if sys.platform == "win32":
        p_str = str(p_res)
        if all(ord(c) < 128 for c in p_str):
            return p_str

        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            ret = ctypes.windll.kernel32.GetShortPathNameW(p_str, buf, 1024)
            if ret > 0 and buf.value:
                return buf.value
        except Exception as exc:  # noqa: BLE001
            log.debug("GetShortPathNameW error: %s", exc)

        try:
            rel = p.relative_to(Path.cwd())
            rel_str = str(rel)
            if all(ord(c) < 128 for c in rel_str):
                return rel_str
        except Exception:  # noqa: BLE001
            pass

    return str(p_res)


def resolve_device(spec: int | str | None, kind: str = "input") -> tuple[int | None, str]:
    """Разрешает спецификацию устройства (int, str или None) в (device_id, device_name)."""
    try:
        import sounddevice as sd
    except Exception:
        return None, "по умолчанию"

    if spec is None or spec == "" or str(spec).strip().lower() in ("default", "none", "null"):
        try:
            info = sd.query_devices(kind=kind)
            name = str(info.get("name", "по умолчанию"))
            return None, name
        except Exception:
            return None, "по умолчанию"

    if isinstance(spec, int):
        try:
            info = sd.query_devices(spec)
            return spec, str(info.get("name", f"устройство #{spec}"))
        except Exception:
            return spec, f"устройство #{spec}"

    s = str(spec).strip()
    if s.isdigit():
        idx = int(s)
        try:
            info = sd.query_devices(idx)
            return idx, str(info.get("name", f"устройство #{idx}"))
        except Exception:
            return idx, f"устройство #{idx}"

    try:
        devices = sd.query_devices()
        target = s.lower()
        for i, d in enumerate(devices):
            if kind == "input" and d.get("max_input_channels", 0) <= 0:
                continue
            if kind == "output" and d.get("max_output_channels", 0) <= 0:
                continue
            name = str(d.get("name", ""))
            if target == name.lower() or target in name.lower() or name.lower() in target:
                return i, name
    except Exception as exc:
        log.debug("Ошибка поиска устройства «%s»: %s", spec, exc)

    return None, str(spec)


def list_audio_devices(kind: str | None = None) -> Any:
    """Возвращает кортеж (inputs, outputs) или список устройств для kind ('input'/'output')."""
    inputs: list[dict] = []
    outputs: list[dict] = []
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device

        for i, d in enumerate(devices):
            api_idx = d.get("hostapi", 0)
            api_name = hostapis[api_idx]["name"] if api_idx < len(hostapis) else ""
            name = str(d.get("name", f"Device #{i}"))
            label = f"[{i}] {name} ({api_name})" if api_name else f"[{i}] {name}"

            if d.get("max_input_channels", 0) > 0:
                inputs.append({
                    "id": i,
                    "index": i,
                    "name": name,
                    "hostapi": api_name,
                    "label": label,
                    "is_default": (i == default_in),
                })

            if d.get("max_output_channels", 0) > 0:
                outputs.append({
                    "id": i,
                    "index": i,
                    "name": name,
                    "hostapi": api_name,
                    "label": label,
                    "is_default": (i == default_out),
                })
    except Exception as exc:
        log.warning("Не удалось получить список аудиоустройств: %s", exc)

    if kind == "input":
        return inputs
    if kind == "output":
        return outputs
    return inputs, outputs



def is_voice_active(chunk: bytes, sample_rate: int = 16000, rms_threshold: float = 300.0) -> bool:
    """Фильтр активности голоса (VAD): webrtcvad или адаптивный RMS порог."""
    if not chunk:
        return False
    try:
        import webrtcvad

        vad = webrtcvad.Vad(2)
        frame_len = int(sample_rate * 0.02 * 2)  # 20ms in 16-bit PCM
        if len(chunk) >= frame_len:
            sub = chunk[:frame_len]
            return vad.is_speech(sub, sample_rate)
    except Exception:
        pass

    try:
        import numpy as np

        data = np.frombuffer(chunk, dtype=np.int16)
        if len(data) == 0:
            return False
        rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
        return rms >= rms_threshold
    except Exception:
        return True


def transcribe_groq_whisper(audio_bytes: bytes, api_key: str, sample_rate: int = 16000) -> str | None:
    """Опциональный гибридный распознаватель речи через быстрый Groq Whisper API."""
    if not api_key or not audio_bytes:
        return None
    try:
        import io
        import numpy as np
        import requests
        import soundfile as sf

        data = np.frombuffer(audio_bytes, dtype=np.int16)
        buf = io.BytesIO()
        sf.write(buf, data, sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)

        headers = {"Authorization": f"Bearer {api_key}"}
        files = {"file": ("speech.wav", buf, "audio/wav")}
        data_payload = {
            "model": "whisper-large-v3-turbo",
            "language": "ru",
            "response_format": "json",
        }
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data_payload,
            timeout=8.0,
        )
        if resp.status_code == 200:
            return (resp.json().get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("Groq Whisper error: %s", exc)
    return None


class VoiceInput:
    def __init__(self, config: AudioConfig, project_root: Path):
        self.config = config
        self.project_root = Path(project_root)
        self._model = None
        self._recognizer = None
        self._stream = None
        self._queue: queue.Queue[bytes] = queue.Queue()
        self.phrase_queue: queue.Queue[str] = queue.Queue()
        self._capture = threading.Event()
        self._capture.set()
        self._stop = threading.Event()
        self._status = "not_loaded"
        self._error: str | None = None
        self._device_name: str | None = None
        self._load_error: str | None = None
        self._is_muted = False
        self.vad_enabled = True
        self.groq_api_key: str | None = None

        # Push-to-talk
        self.push_to_talk_enabled = bool(getattr(config, "push_to_talk", True))
        self.ptt_key = str(getattr(config, "ptt_key", "v") or "v")
        self._ptt_active = threading.Event()
        self._ptt_listener: HotkeyListener | None = None
        self.on_ptt_status: Callable[[bool], None] | None = None
        self.on_partial: Callable[[str], None] | None = None
        self._recognizer_lock = threading.Lock()
        self._last_partial: str = ""

    # ------------------------------------------------------------------ #
    @property
    def status(self) -> str:
        return self._status

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def device_name(self) -> str | None:
        return self._device_name

    @property
    def ready(self) -> bool:
        return self._status == "ready"

    @property
    def ptt_key_label(self) -> str:
        return format_key_name(self.ptt_key)

    # ------------------------------------------------------------------ #
    def model_path(self) -> Path:
        raw = Path(self.config.model_path)
        if raw.is_absolute() and raw.is_dir() and (raw / "am" / "final.mdl").is_file():
            return raw

        candidates = [
            self.project_root / raw,
            self.project_root / "vosk-model-small-ru-0.22",
            self.project_root / "vosk-model-ru-0.22",
            self.project_root.parent / raw,
            self.project_root.parent / "vosk-model-small-ru-0.22",
            self.project_root.parent / "vosk-model-ru-0.22",
            Path.cwd() / raw,
            Path.cwd() / "vosk-model-small-ru-0.22",
            Path.cwd() / "vosk-model-ru-0.22",
            Path(__file__).resolve().parent.parent / raw,
            Path(__file__).resolve().parent.parent / "vosk-model-small-ru-0.22",
            Path(__file__).resolve().parent.parent / "vosk-model-ru-0.22",
        ]
        for candidate in candidates:
            if candidate.is_dir() and (candidate / "am" / "final.mdl").is_file():
                return candidate.resolve()
        return (self.project_root / raw).resolve()

    def check_model(self) -> tuple[bool, str]:
        path = self.model_path()
        if not path.is_dir():
            return False, (
                f"Модель Vosk не найдена: {path}. Скачайте vosk-model-ru-0.22 "
                "(https://alphacephei.com/vosk/models) и положите рядом с main.py "
                "или укажите audio.model_path в config.json."
            )
        required = (path / "am" / "final.mdl", path / "conf" / "model.conf")
        missing = [str(p.relative_to(path)) for p in required if not p.is_file()]
        if missing:
            return False, f"Модель Vosk повреждена: нет файлов {', '.join(missing)}."
        return True, str(path)

    def load_model(self, progress: Callable[[str], None] | None = None, grammar: str | None = None) -> None:
        """Загружает модель. Блокирующая операция (десятки секунд)."""
        if self._status == "ready":
            return
        ok, detail = self.check_model()
        if not ok:
            try:
                from .doctor import download_vosk_model

                if progress:
                    progress("Модель речи не найдена. Скачиваю компактную русскую модель (~45 МБ)...")
                dest = self.project_root
                target = download_vosk_model(
                    dest,
                    model_name="vosk-model-small-ru-0.22",
                    progress_callback=lambda p, msg: progress(msg) if progress else None,
                )
                self.config.model_path = target.name
                ok, detail = self.check_model()
            except Exception as exc:  # noqa: BLE001
                log.warning("Автоматическое скачивание модели Vosk не удалось: %s", exc)

        if not ok:
            self._status = "error"
            self._load_error = detail
            raise AudioError(detail)
        if progress:
            progress("Загружаю модель Vosk…")
        self._status = "loading"
        model_path_obj = self.model_path()
        model_safe_str = safe_model_path_str(model_path_obj)
        log.info("Загрузка модели Vosk: %s (исходный путь: %s)", model_safe_str, model_path_obj)
        try:
            import vosk  # локальный импорт: текстовый режим живёт без Vosk

            try:
                vosk.SetLogLevel(-1)
            except Exception:  # noqa: BLE001 — старые версии Vosk
                pass
            with _suppress_native_stderr():
                self._model = vosk.Model(model_safe_str)
            if grammar:
                self._recognizer = vosk.KaldiRecognizer(self._model, self.config.sample_rate, grammar)
            else:
                self._recognizer = vosk.KaldiRecognizer(self._model, self.config.sample_rate)
            self._status = "ready"
            self._load_error = None
            log.info("Модель Vosk успешно загружена: %s", model_path_obj)
        except Exception as exc:  # noqa: BLE001
            self._status = "error"
            self._load_error = f"Не удалось загрузить модель Vosk: {exc}"
            log.exception("Ошибка загрузки модели Vosk")
            raise AudioError(self._load_error) from exc

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Открывает микрофон и запускает перехват клавиши при Push-to-Talk."""
        if self._stream is not None:
            return
        if self._recognizer is None:
            raise AudioError("Сначала загрузите модель Vosk (load_model).")
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            raise AudioError(f"Модуль sounddevice недоступен: {exc}") from exc

        dev_id, dev_name = resolve_device(self.config.input_device, kind="input")
        self._device_name = dev_name

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("sounddevice status: %s", status)
            if self._capture.is_set():
                raw_bytes = bytes(indata)
                self._queue.put(raw_bytes)

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=self.config.block_size,
                device=dev_id,
                dtype="int16",
                channels=1,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            if dev_id is not None:
                log.warning("Не удалось открыть микрофон «%s» (id=%s): %s. Пробую микрофон по умолчанию...", self._device_name, dev_id, exc)
                try:
                    dev_id, dev_name = resolve_device(None, kind="input")
                    self._device_name = dev_name
                    self._stream = sd.RawInputStream(
                        samplerate=self.config.sample_rate,
                        blocksize=self.config.block_size,
                        device=dev_id,
                        dtype="int16",
                        channels=1,
                        callback=callback,
                    )
                    self._stream.start()
                    log.info("Успешно открыт микрофон по умолчанию: %s", dev_name)
                except Exception as fallback_exc:
                    self._stream = None
                    raise AudioError(
                        f"Не удалось открыть микрофон: {fallback_exc}. "
                        "Попробуйте выбрать другое устройство ввода в Настройках."
                    ) from fallback_exc
            else:
                self._stream = None
                raise AudioError(
                    f"Не удалось открыть микрофон «{self._device_name}»: {exc}. "
                    "Попробуйте выбрать другое устройство ввода в Настройках."
                ) from exc

        # Push-to-talk
        if self.push_to_talk_enabled:
            self._capture.clear()
            self._start_ptt_listener()
        else:
            self._capture.set()

        log.info("Микрофон открыт: %s (id=%s, PTT: %s)", self._device_name, dev_id, self.push_to_talk_enabled)

    def set_input_device(self, device: int | str | None) -> str:
        """Переключает микрофон на лету без повторной загрузки модели Vosk."""
        self.config.input_device = device
        was_running = self._stream is not None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._drain_queue()
        if was_running and self._recognizer is not None:
            self.start()
        else:
            _, name = resolve_device(device, kind="input")
            self._device_name = name
        log.info("Устройство ввода переключено: %s (id: %s)", self._device_name, device)
        return self._device_name or str(device)

    # ------------------------------------------------------------------ #
    def _start_ptt_listener(self) -> None:
        if self._ptt_listener is not None:
            self._ptt_listener.stop()
        try:
            self._ptt_listener = HotkeyListener(
                self.ptt_key,
                on_down=self._on_ptt_down,
                on_up=self._on_ptt_up,
            )
            self._ptt_listener.start()
            log.info("Push-to-talk активирован на клавишу [%s]", self.ptt_key_label)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось запустить хук клавиши PTT: %s", exc)
            self._ptt_listener = None

    def _on_ptt_down(self) -> None:
        if self._is_muted:
            return
        self._ptt_active.set()
        self._capture.set()
        self._drain_queue()
        with self._recognizer_lock:
            if self._recognizer is not None:
                try:
                    self._recognizer.Reset()
                except Exception:  # noqa: BLE001
                    pass
        self._last_partial = ""
        if self.on_ptt_status is not None:
            try:
                self.on_ptt_status(True)
            except Exception:  # noqa: BLE001
                pass

    def _on_ptt_up(self) -> None:
        self._ptt_active.clear()
        if self.push_to_talk_enabled:
            self._capture.clear()
        if self.on_ptt_status is not None:
            try:
                self.on_ptt_status(False)
            except Exception:  # noqa: BLE001
                pass

        if self._recognizer is None:
            return

        with self._recognizer_lock:
            # Прокачиваем накопившиеся порции звука из очереди
            while True:
                try:
                    chunk = self._queue.get_nowait()
                    self._recognizer.AcceptWaveform(chunk)
                except queue.Empty:
                    break
                except Exception:  # noqa: BLE001
                    break

            try:
                res = json.loads(self._recognizer.Result())
                text = (res.get("text") or "").strip().lower()
                if not text:
                    res_final = json.loads(self._recognizer.FinalResult())
                    text = (res_final.get("text") or "").strip().lower()
                if not text and self._last_partial:
                    text = self._last_partial
                if text:
                    log.info("Распознано по клавише PTT: %s", text)
                    self.phrase_queue.put(text)
                self._last_partial = ""
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка извлечения фразы PTT: %s", exc)

    def set_ptt_key(self, key: str) -> str:
        """Переключает клавишу push-to-talk и возвращает её форматированное имя."""
        parse_key(key)  # проверит корректность или бросит ValueError
        self.ptt_key = key
        self.config.ptt_key = key
        if self._ptt_listener is not None:
            self._ptt_listener.set_key(key)
        elif self.push_to_talk_enabled and self._stream is not None:
            self._start_ptt_listener()
        label = self.ptt_key_label
        log.info("Клавиша push-to-talk изменена на [%s]", label)
        return label

    def set_push_to_talk(self, enabled: bool) -> None:
        """Включает или выключает режим push-to-talk."""
        self.push_to_talk_enabled = bool(enabled)
        self.config.push_to_talk = bool(enabled)
        if self.push_to_talk_enabled:
            self._capture.clear()
            self._start_ptt_listener()
        else:
            if self._ptt_listener is not None:
                self._ptt_listener.stop()
                self._ptt_listener = None
            if not self._is_muted:
                self._capture.set()
        log.info("Режим push-to-talk: %s", "включён" if enabled else "выключен")

    # ------------------------------------------------------------------ #
    def set_muted(self, muted: bool) -> None:
        self._is_muted = bool(muted)
        if muted:
            self._capture.clear()
            self._drain_queue()
            if self._recognizer is not None:
                try:
                    self._recognizer.Reset()
                except Exception:  # noqa: BLE001
                    pass
        else:
            self._drain_queue()
            if not self.push_to_talk_enabled or self._ptt_active.is_set():
                self._capture.set()

    def discard_pending(self) -> None:
        """Сбрасывает накопленное аудио и недособранную фразу."""
        self._drain_queue()
        while True:
            try:
                self.phrase_queue.get_nowait()
            except queue.Empty:
                break
        with self._recognizer_lock:
            if self._recognizer is not None:
                try:
                    self._recognizer.Reset()
                except Exception:  # noqa: BLE001
                    pass
        self._last_partial = ""
        if self.on_partial is not None:
            try:
                self.on_partial("")
            except Exception:  # noqa: BLE001
                pass

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def listen(self, external_queue: queue.Queue | None = None) -> str | None:
        """Блокирующе ждёт распознанную фразу. None — если ввод остановлен."""
        if self._recognizer is None:
            return None

        last_partial = ""
        last_change_time = 0.0
        silence_timeout = getattr(self.config, "listen_timeout", 0.0)
        if silence_timeout <= 0.1:
            silence_timeout = 1.6

        while not self._stop.is_set():
            if external_queue is not None:
                try:
                    ext_text = external_queue.get_nowait()
                    if ext_text:
                        if self.on_partial is not None:
                            self.on_partial("")
                        return ext_text
                except queue.Empty:
                    pass

            # Если есть готовая фраза (из PTT on_up)
            try:
                text = self.phrase_queue.get_nowait()
                if self.on_partial is not None:
                    self.on_partial("")
                return text
            except queue.Empty:
                pass

            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                now = time.monotonic()
                if last_partial and last_change_time > 0 and (now - last_change_time) >= silence_timeout:
                    log.info("Таймаут тишины (очередь пуста, %.2fs), финализирую: %s", now - last_change_time, last_partial)
                    text = ""
                    with self._recognizer_lock:
                        try:
                            res = json.loads(self._recognizer.Result())
                            text = (res.get("text") or "").strip().lower()
                        except Exception:  # noqa: BLE001
                            pass
                        if not text:
                            text = last_partial
                        try:
                            self._recognizer.Reset()
                        except Exception:  # noqa: BLE001
                            pass
                    self._last_partial = ""
                    if self.on_partial is not None:
                        self.on_partial("")
                    return text
                continue

            if self._is_muted:
                continue

            if not self._capture.is_set() and not self._ptt_active.is_set():
                continue

            try:
                with self._recognizer_lock:
                    if self._recognizer.AcceptWaveform(data):
                        result = json.loads(self._recognizer.Result())
                        text = (result.get("text") or "").strip().lower()
                        last_partial = ""
                        self._last_partial = ""
                        last_change_time = 0.0
                        if self.on_partial is not None:
                            self.on_partial("")
                        if text:
                            log.info("Распознано: %s", text)
                            return text
                    else:
                        partial = (json.loads(self._recognizer.PartialResult()).get("partial") or "").strip().lower()
                        if partial:
                            self._last_partial = partial
                            now = time.monotonic()
                            if partial != last_partial:
                                last_partial = partial
                                last_change_time = now
                                if self.on_partial is not None:
                                    self.on_partial(partial)
                                else:
                                    print(f"\r🎤 Слушаю: {partial[:70]:<70}", end="", flush=True)
                            else:
                                # Фраза не меняется: пользователь сделал паузу
                                if last_change_time > 0 and (now - last_change_time) >= silence_timeout:
                                    log.info("Таймаут тишины (%.2fs), финализирую: %s", now - last_change_time, last_partial)
                                    text = ""
                                    try:
                                        res = json.loads(self._recognizer.Result())
                                        text = (res.get("text") or "").strip().lower()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    if not text:
                                        text = last_partial
                                    try:
                                        self._recognizer.Reset()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    if self.on_partial is not None:
                                        self.on_partial("")
                                    last_partial = ""
                                    self._last_partial = ""
                                    last_change_time = 0.0
                                    if text:
                                        return text
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка распознавания: %s", exc)
        return None

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._stop.set()
        if self._ptt_listener is not None:
            self._ptt_listener.stop()
            self._ptt_listener = None
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._is_muted = False
        log.info("Микрофон закрыт")

    def model_info(self) -> dict:
        return {
            "path": str(self.model_path()),
            "exists": self.model_path().is_dir(),
            "status": self._status,
            "error": self._load_error or self._error,
            "device": self._device_name,
            "push_to_talk": self.push_to_talk_enabled,
            "ptt_key": self.ptt_key,
            "ptt_key_label": self.ptt_key_label,
        }
