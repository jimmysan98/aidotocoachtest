"""Самодиагностика: `python main.py --doctor`.

Проверяет всё, что нужно ассистенту, и печатает понятные подсказки.
Запускать можно без микрофона/интернета — часть проверок будет WARN.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config, PROJECT_ROOT
from .dota_api import DotaApi
from .dota_install import find_gsi_dirs

STATUS_ORDER = {"OK": 0, "WARN": 1, "FAIL": 2}
STATUS_LABEL = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _check_python(config: Config) -> Check:
    import sys

    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 10):
        return Check("Python", "FAIL", f"версия {version}", "Нужен Python 3.10+")
    return Check("Python", "OK", version)


def _check_dependencies(config: Config) -> Check:
    modules = {
        "vosk": "vosk",
        "sounddevice": "sounddevice",
        "cv2": "opencv-python",
        "numpy": "numpy",
        "mss": "mss",
        "requests": "requests",
        "win32com.client": "pywin32",
        "tkinter": "tkinter (входит в Python)",
    }
    missing = []
    for module, package in modules.items():
        try:
            __import__(module)
        except Exception:  # noqa: BLE001
            missing.append(package)
    if missing:
        return Check(
            "Зависимости",
            "FAIL",
            "нет: " + ", ".join(missing),
            "Активируйте .venv и выполните: pip install -r requirements.txt",
        )
    return Check("Зависимости", "OK", "все модули импортируются")


def _check_model(config: Config) -> Check:
    from .audio import VoiceInput

    vi = VoiceInput(config.audio, PROJECT_ROOT)
    model = vi.model_path()
    required = (model / "am" / "final.mdl", model / "conf" / "model.conf")
    if not model.is_dir():
        return Check(
            "Модель Vosk",
            "WARN",
            f"нет каталога {model}",
            "Скачайте vosk-model-ru-0.22 с https://alphacephei.com/vosk/models "
            "и положите рядом с main.py (или укажите путь в config.json).",
        )
    missing = [str(p.relative_to(model)) for p in required if not p.is_file()]
    if missing:
        return Check("Модель Vosk", "FAIL", "нет файлов: " + ", ".join(missing), "Перекачайте модель.")
    size_gb = sum(p.stat().st_size for p in model.rglob("*") if p.is_file()) / 1024**3
    return Check("Модель Vosk", "OK", f"{model.name} ({size_gb:.1f} ГБ)")


def _check_microphone(config: Config) -> Check:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        return Check("Микрофон", "FAIL", f"sounddevice недоступен: {exc}", "pip install sounddevice")
    try:
        default = sd.query_devices(kind="input")
    except Exception as exc:  # noqa: BLE001
        return Check(
            "Микрофон",
            "WARN",
            f"нет устройства ввода по умолчанию: {exc}",
            "Подключите микрофон или укажите audio.input_device в config.json.",
        )
    try:
        with sd.RawInputStream(
            samplerate=config.audio.sample_rate,
            blocksize=config.audio.block_size,
            dtype="int16",
            channels=1,
            device=config.audio.input_device,
        ):
            pass
        opened = "открывается"
    except Exception as exc:  # noqa: BLE001
        return Check(
            "Микрофон",
            "WARN",
            f"«{default.get('name')}» найден, но не открывается: {exc}",
            "Проверьте доступ к микрофону в параметрах Windows.",
        )
    return Check("Микрофон", "OK", f"{default.get('name')} ({opened})")


def _check_tts(config: Config) -> Check:
    from .tts import Tts

    tts = Tts(config.tts, config.cache_path)
    try:
        tts.probe(timeout=8)
        status = tts.status()
    finally:
        tts.close()
    if status.get("error"):
        return Check(
            "Озвучка",
            "WARN",
            str(status["error"]),
            "Проверьте устройства вывода звука в Windows. Без озвучки ассистент работает текстом.",
        )
    backend = status.get("backend") or "?"
    voices = len(status.get("voices") or [])
    return Check("Озвучка", "OK", f"{backend}, голосов: {voices}")


def _check_llm(config: Config) -> Check:
    import requests

    llm = config.llm
    provider = (llm.provider or "ollama").strip().lower()
    base = llm.base_url.rstrip("/")

    if provider == "openai_compatible":
        if not (llm.api_key or "").strip():
            return Check(
                "LLM",
                "WARN",
                f"модель {llm.model}, но API-ключ не указан",
                "Откройте Настройки (⚙) в окне AIDOTA или укажите llm.api_key в config.json.",
            )
        headers = {"Authorization": f"Bearer {llm.api_key}"}
        try:
            response = requests.get(f"{base}/models", headers=headers, timeout=6)
            response.raise_for_status()
            response.json()
        except Exception as exc:  # noqa: BLE001
            return Check(
                "LLM",
                "WARN",
                f"{base} не отвечает: {exc}",
                "Проверьте llm.base_url и llm.api_key в config.json или окне Настроек.",
            )
        if not llm.model:
            return Check("LLM", "WARN", "модель не задана", "Укажите llm.model в config.json.")
        return Check("LLM", "OK", f"openai_compatible / {llm.model}")

    try:
        response = requests.get(f"{base}/api/tags", timeout=4)
        response.raise_for_status()
        models = [m.get("name", "") for m in response.json().get("models", []) if m.get("name")]
    except Exception as exc:  # noqa: BLE001
        return Check(
            "LLM",
            "WARN",
            f"{base} не отвечает: {exc}",
            "Запустите Ollama (ollama serve) или настройте облачную модель через llm в config.json.",
        )
    if not models:
        return Check(
            "LLM",
            "WARN",
            "сервер работает, но моделей нет",
            f"Выполните: ollama pull {llm.model}",
        )
    wanted = llm.model.split(":")[0].lower()
    has_wanted = any(m.split(":")[0].lower() == wanted for m in models)
    if not has_wanted:
        return Check(
            "LLM",
            "WARN",
            f"модели «{llm.model}» нет (есть: {', '.join(models[:4])})",
            f"Выполните: ollama pull {llm.model}",
        )
    return Check("LLM", "OK", f"ollama / {llm.model}")


def _check_gsi_port(config: Config) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.gsi.host, config.gsi.port))
    except OSError as exc:
        return Check(
            "Порт GSI",
            "WARN",
            f"{config.gsi.host}:{config.gsi.port} занят ({exc})",
            "Закройте другой экземпляр помощника или поменяйте gsi.port.",
        )
    finally:
        sock.close()
    return Check("Порт GSI", "OK", f"{config.gsi.host}:{config.gsi.port} свободен")


def _check_dota(config: Config) -> Check:
    if config.dota.cfg_dir:
        candidates = [Path(config.dota.cfg_dir)]
        if not candidates[0].is_absolute():
            candidates[0] = PROJECT_ROOT / candidates[0]
    else:
        candidates = find_gsi_dirs()
    if not candidates:
        return Check(
            "Dota 2 / GSI",
            "WARN",
            "установка Dota 2 не найдена",
            "Установите игру через Steam или укажите dota.cfg_dir в config.json.",
        )
    existing = candidates[0] / config.dota.gsi_config_name
    if existing.is_file():
        return Check("Dota 2 / GSI", "OK", f"конфиг на месте: {existing}")
    return Check(
        "Dota 2 / GSI",
        "WARN",
        f"каталог найден, конфига нет: {candidates[0]}",
        "Выполните: python main.py --install-gsi, затем добавьте в параметры запуска Dota 2 "
        "флаг -gamestateintegration.",
    )


def _check_snapshots(config: Config) -> Check:
    api = DotaApi(config.data_path)
    api.load_snapshots()
    if not api.heroes:
        return Check(
            "Данные героев",
            "WARN",
            "data/heroes.json отсутствует или пуст",
            "Выполните: python main.py --fetch-icons (скачает и данные, и иконки).",
        )
    return Check("Данные героев", "OK", f"{len(api.heroes)} героев, {len(api.items())} предметов")


def _check_opendota(config: Config, quick: bool = True) -> Check:
    if quick:
        return Check("OpenDota", "OK", "проверка при необходимости: python main.py --doctor --online")
    api = DotaApi(config.data_path, timeout=8)
    if api.online():
        return Check("OpenDota", "OK", "API доступен")
    return Check(
        "OpenDota",
        "WARN",
        "API недоступен",
        "Советы будут без свежей меты; проверьте интернет/файрвол.",
    )


def _check_icons(config: Config) -> Check:
    from .vision import count_icon_files

    icons_dir = config.path(config.vision.icons_dir)
    count = count_icon_files(icons_dir)
    if count < 50:
        return Check(
            "Иконки героев",
            "WARN",
            f"найдено {count} портретов в {icons_dir}",
            "Выполните: python main.py --fetch-icons",
        )
    return Check("Иконки героев", "OK", f"{count} портретов")


def _check_overlay(config: Config) -> Check:
    if not config.overlay.enabled:
        return Check("Оверлей", "OK", "выключен в config.json")
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "Оверлей",
            "WARN",
            f"Tkinter не открывает окно: {exc}",
            "Запустите из обычного сеанса Windows (не из службы/RDP без рабочего стола).",
        )
    return Check("Оверлей", "OK", "Tkinter работает")


def get_checks(config: Config, *, online: bool = False) -> list[Check]:
    return [
        _check_python(config),
        _check_dependencies(config),
        _check_model(config),
        _check_microphone(config),
        _check_tts(config),
        _check_llm(config),
        _check_gsi_port(config),
        _check_dota(config),
        _check_snapshots(config),
        _check_icons(config),
        _check_overlay(config),
        _check_opendota(config, quick=not online),
    ]


def run_doctor(config: Config, *, online: bool = False) -> int:
    checks = get_checks(config, online=online)
    print("AIDOTA — самодиагностика\n" + "=" * 64)
    for check in checks:
        label = STATUS_LABEL.get(check.status, check.status)
        print(f"{label} {check.name:<16} {check.detail}")
        if check.hint and check.status != "OK":
            for line in _wrap(check.hint, 60):
                print(f"       {line}")
    print("=" * 64)
    failed = [c for c in checks if c.status == "FAIL"]
    warned = [c for c in checks if c.status == "WARN"]
    print(f"Итог: OK — {len(checks) - len(failed) - len(warned)}, предупреждений — {len(warned)}, ошибок — {len(failed)}")
    if failed:
        print("Сначала исправьте ошибки — без них помощник не запустится.")
        return 1
    if warned:
        print("Ассистент запустится, но часть функций будет недоступна.")
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def download_vosk_model(
    dest_dir: Path,
    model_name: str = "vosk-model-small-ru-0.22",
    progress_callback: Callable[[float, str], None] | None = None,
) -> Path:
    """Скачивает и распаковывает русскую модель Vosk."""
    import io
    import urllib.request
    import zipfile

    url = f"https://alphacephei.com/vosk/models/{model_name}.zip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_dir = dest_dir / model_name

    if (target_dir / "am" / "final.mdl").is_file():
        if progress_callback:
            progress_callback(1.0, f"Модель {model_name} уже установлена!")
        return target_dir

    if progress_callback:
        progress_callback(0.01, f"Подключение к серверу моделей Vosk...")

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AIDOTA"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        buf = io.BytesIO()
        chunk_size = 1024 * 64
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            buf.write(chunk)
            downloaded += len(chunk)
            if progress_callback and total > 0:
                pct = downloaded / total
                mb = downloaded / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                progress_callback(pct * 0.85, f"Скачивание: {mb:.1f} / {tot_mb:.1f} МБ ({int(pct*100)}%)")

    if progress_callback:
        progress_callback(0.90, "Распаковка архива модели...")

    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(dest_dir)

    if progress_callback:
        progress_callback(1.0, f"Модель {model_name} успешно установлена!")
    return target_dir

