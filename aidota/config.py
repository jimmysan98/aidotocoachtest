"""Конфигурация AIDOTA: значения по умолчанию, чтение/запись config.json."""

from __future__ import annotations

import dataclasses
import json
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path

def _base_dir() -> Path:
    """Корень приложения: рядом с exe при заморозке, иначе корень проекта.

    В замороженном виде ``__file__`` указывает внутрь PyInstaller-архива,
    поэтому для поиска config.json / data / hero_icons / модели берём каталог
    самого исполняемого файла.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _base_dir()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


class ConfigError(RuntimeError):
    """Конфигурационный файл повреждён или содержит недопустимые значения."""


# --------------------------------------------------------------------------- #
# Секции конфигурации
# --------------------------------------------------------------------------- #


@dataclass
class GsiConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 3000
    token: str = "aidota-local-token"
    stale_seconds: float = 45.0


@dataclass
class AudioConfig:
    model_path: str = "vosk-model-ru-0.22"
    sample_rate: int = 16000
    block_size: int = 8000
    input_device: int | None = None
    # Сколько секунд ждать тишины перед тем, как считать фразу законченной.
    listen_timeout: float = 0.0
    # Push-to-talk: слушать только пока зажата клавиша.
    push_to_talk: bool = True
    ptt_key: str = "v"


@dataclass
class TtsConfig:
    enabled: bool = True
    # Диапазон SAPI: -10 (медленно) .. 10 (быстро), 0 — норма.
    rate: int = 0
    volume: int = 100
    voice_hint: str = "Irina"
    max_chars: int = 700


@dataclass
class OverlayConfig:
    enabled: bool = True
    width: int = 520
    height: int = 300
    alpha: float = 0.9
    topmost: bool = True
    x: int | None = None
    y: int | None = None


@dataclass
class VisionConfig:
    enabled: bool = True
    icons_dir: str = "hero_icons"
    auto_download: bool = True
    threshold: float = 0.78
    search_radius: int = 90


@dataclass
class OllamaConfig:
    """Оставлена для обратной совместимости; новый код использует LlmConfig."""

    host: str = "http://127.0.0.1:11434"
    model: str = "gemma3"
    timeout: float = 120.0
    temperature: float = 0.4


@dataclass
class LlmConfig:
    """Единый интерфейс для локальных (Ollama) и облачных LLM.

    provider: "ollama" | "openai_compatible"
    Для OpenAI-совместимых провайдеров (DeepSeek, OpenRouter, Groq, ...)
    задаются base_url, api_key и model.
    presets — именованные наборы настроек для быстрого переключения голосом.
    """

    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    model: str = "gemma3"
    temperature: float = 0.4
    timeout: float = 120.0
    presets: dict[str, dict] = field(default_factory=dict)


@dataclass
class PlayerProfile:
    """Личный профиль игрока: основная роль и пул героев по позициям.

    hero_pool: ключи "1".."5" (строки), значения — списки имён героев
    (как в OpenDota localized_name, напр. "Queen of Pain").
    """

    default_position: int = 1
    mmr_bracket: str = ""
    hero_pool: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class DotaConfig:
    # Если None — путь определяется автоматически по библиотекам Steam.
    cfg_dir: str | None = None
    gsi_config_name: str = "gamestate_integration_aidota.cfg"


@dataclass
class Config:
    gsi: GsiConfig = field(default_factory=GsiConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    player_profile: PlayerProfile = field(default_factory=PlayerProfile)
    dota: DotaConfig = field(default_factory=DotaConfig)

    data_dir: str = "data"
    cache_dir: str = ".cache"
    logs_dir: str = "logs"
    memory_size: int = 6
    log_level: str = "INFO"

    # ----- пути ----- #
    def path(self, value: str | Path) -> Path:
        """Разрешает путь относительно корня проекта."""
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def data_path(self) -> Path:
        return self.path(self.data_dir)

    @property
    def cache_path(self) -> Path:
        return self.path(self.cache_dir)

    @property
    def logs_path(self) -> Path:
        return self.path(self.logs_dir)


# --------------------------------------------------------------------------- #
# Чтение конфигурации
# --------------------------------------------------------------------------- #


def _defaults_dict() -> dict:
    return dataclasses.asdict(Config())


def _coerce(value: typing.Any, target: typing.Any, path: str) -> typing.Any:
    """Приводит значение из JSON к типу поля, понятно ругаясь на ошибки."""
    origin = typing.get_origin(target)
    if origin is typing.Union or origin is types.UnionType:  # X | None
        args = [a for a in typing.get_args(target) if a is not type(None)]
        if value is None:
            return None
        return _coerce(value, args[0], path) if args else value
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "да"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off", "нет"}:
            return False
        raise ConfigError(f"{path}: ожидалось true/false, получено {value!r}")
    if target is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{path}: ожидалось целое число, получено {value!r}") from None
    if target is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{path}: ожидалось число, получено {value!r}") from None
    if target is str:
        if isinstance(value, str):
            return value
        raise ConfigError(f"{path}: ожидалась строка, получено {value!r}")
    return value


def _from_dict(cls: type, data: dict, path: str = "") -> typing.Any:
    """Собирает dataclass из словаря, игнорируя неизвестные ключи."""
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, typing.Any] = {}
    for f in dataclasses.fields(cls):
        key = f.name
        if key not in data:
            continue
        value = data[key]
        field_path = f"{path}.{key}" if path else key
        hint = hints.get(key, typing.Any)
        if isinstance(value, dict) and dataclasses.is_dataclass(hint):
            kwargs[key] = _from_dict(hint, value, field_path)
        else:
            kwargs[key] = _coerce(value, hint, field_path)
    unknown = sorted(set(data) - {f.name for f in dataclasses.fields(cls)})
    for key in unknown:
        print(
            f"Предупреждение: неизвестный ключ конфигурации "
            f"'{path + '.' if path else ''}{key}' — пропущен.",
            file=sys.stderr,
        )
    return cls(**kwargs)


def load_config(path: str | Path | None = None, *, create_if_missing: bool = True) -> Config:
    """Загружает config.json, дополняя его значениями по умолчанию."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    if not config_path.is_file():
        if create_if_missing:
            config = Config()
            save_config(config, config_path)
            print(f"Создан файл конфигурации по умолчанию: {config_path}")
            return config
        raise ConfigError(f"Файл конфигурации не найден: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path}: некорректный JSON ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: корневой элемент должен быть объектом")

    merged = _merge(_defaults_dict(), raw)
    config = _from_dict(Config, merged)
    _apply_backward_compat(config, raw)
    _validate_profile(config)
    return config


def _apply_backward_compat(config: Config, raw: dict) -> None:
    """Старые конфиги с блоком ``ollama`` превращаем в новую настройку ``llm``."""
    if "llm" in raw:
        return
    llm = config.llm
    ollama = config.ollama
    llm.provider = "ollama"
    if not llm.base_url or llm.base_url == "http://127.0.0.1:11434":
        llm.base_url = ollama.host
    if not llm.model or llm.model == "gemma3":
        llm.model = ollama.model
    llm.temperature = ollama.temperature
    llm.timeout = ollama.timeout


def _validate_profile(config: Config) -> None:
    position = config.player_profile.default_position
    if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= 5:
        raise ConfigError("player_profile.default_position должен быть числом от 1 до 5")
    pool: dict[str, list[str]] = {}
    for key, value in (config.player_profile.hero_pool or {}).items():
        if isinstance(value, list):
            pool[str(key)] = [str(item) for item in value]
        elif value:
            pool[str(key)] = [str(value)]
    config.player_profile.hero_pool = pool


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def save_config(config: Config, path: str | Path | None = None) -> Path:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(config)
    # None-значения (например, input_device) оставляем — это осознанный выбор.
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return config_path


def save_llm_only(config: Config, path: str | Path | None = None) -> Path:
    """Сохраняет только секцию ``llm``, не трогая остальной config.json.

    Нужно, чтобы смена модели/пресета голосом не затирала другие настройки
    (и не сохраняла временные флаги командной строки вроде ``--no-tts``).
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    raw: dict = {}
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["llm"] = dataclasses.asdict(config.llm)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return config_path
