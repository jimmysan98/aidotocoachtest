"""Конфигурация AIDOTA: значения по умолчанию, чтение/запись config.json."""

from __future__ import annotations

import base64
import dataclasses
import json
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path

_OBFUSCATION_KEY = b"AIDOTA_KEY_SALT_2026_XOR"


def obfuscate_api_key(plain_key: str) -> str:
    """Обфусцирует API-ключ через XOR + Base64 с префиксом enc:."""
    if not plain_key:
        return ""
    b_key = str(plain_key).strip().encode("utf-8")
    xor_bytes = bytes([b ^ _OBFUSCATION_KEY[i % len(_OBFUSCATION_KEY)] for i, b in enumerate(b_key)])
    return "enc:" + base64.b64encode(xor_bytes).decode("ascii")


def deobfuscate_api_key(encoded_key: str) -> str:
    """Деобфусцирует API-ключ. Если ключ не обфусцирован, возвращает как есть."""
    if not encoded_key:
        return ""
    val = str(encoded_key).strip()
    if not val.startswith("enc:"):
        return val
    try:
        raw_b64 = val[4:]
        xor_bytes = base64.b64decode(raw_b64)
        plain_bytes = bytes([b ^ _OBFUSCATION_KEY[i % len(_OBFUSCATION_KEY)] for i, b in enumerate(xor_bytes)])
        return plain_bytes.decode("utf-8", errors="replace").strip()
    except Exception:
        return val

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
# Уровень игры (skill_level)
# --------------------------------------------------------------------------- #

#: Допустимые коды уровня игрока, от самого простого к самому сильному.
SKILL_LEVELS: tuple[str, ...] = ("beginner", "amateur", "advanced", "expert")

#: Русские подписи уровня для интерфейсов (Tk, веб, трей).
SKILL_LEVEL_LABELS: dict[str, str] = {
    "beginner": "Новичок",
    "amateur": "Любитель",
    "advanced": "Опытный",
    "expert": "Профи",
}

#: Алиасы (в нижнем регистре) -> канонический код уровня.
_SKILL_LEVEL_ALIASES: dict[str, str] = {
    # beginner / новичок
    "новичок": "beginner",
    "новичек": "beginner",
    "начинающий": "beginner",
    "нуб": "beginner",
    "нубский": "beginner",
    "лёгкий": "beginner",
    "легкий": "beginner",
    "beginner": "beginner",
    "newbie": "beginner",
    "noob": "beginner",
    "easy": "beginner",
    # amateur / любитель
    "любитель": "amateur",
    "любительский": "amateur",
    "средний": "amateur",
    "середняк": "amateur",
    "среднячок": "amateur",
    "amateur": "amateur",
    "medium": "amateur",
    "intermediate": "amateur",
    "average": "amateur",
    # advanced / опытный
    "опытный": "advanced",
    "продвинутый": "advanced",
    "продвинут": "advanced",
    "высокий": "advanced",
    "advanced": "advanced",
    "high": "advanced",
    "strong": "advanced",
    # expert / профи
    "профи": "expert",
    "про": "expert",
    "эксперт": "expert",
    "мастер": "expert",
    "профессионал": "expert",
    "expert": "expert",
    "professional": "expert",
    "pro": "expert",
}

#: Числовые обозначения 1..4 (из старых конфигов/UI) -> код уровня.
_SKILL_LEVEL_NUMERIC: dict[str, str] = {
    "1": "beginner",
    "2": "amateur",
    "3": "advanced",
    "4": "expert",
}


def normalize_skill_level(value: typing.Any) -> str:
    """Приводит произвольное значение уровня к одному из ``SKILL_LEVELS``.

    Понимает канонические коды (``beginner``/``amateur``/``advanced``/``expert``),
    русские подписи и алиасы («новичок», «любитель», «средний», «опытный»,
    «продвинутый», «профи», «эксперт», «про»), числовые коды 1–4 и значения
    с лишними пробелами/в другом регистре/в падежах (общие правила живут в
    :func:`aidota.beginner.normalize_skill_level`). Любое невалидное значение
    превращается в ``"beginner"`` — тренер по умолчанию объясняет новичку.

    Импорт ``aidota.beginner`` ленивый и обёрнут в ``try/except``: конфиг
    должен загружаться даже если модуль новичкового тренера недоступен.
    """
    if value is None:
        return "beginner"
    if isinstance(value, bool):
        return "beginner"
    if isinstance(value, (int, float)):
        return _SKILL_LEVEL_NUMERIC.get(str(int(value)), "beginner")
    text = str(value).strip().lower()
    if not text:
        return "beginner"
    if text in SKILL_LEVELS:
        return text
    if text in _SKILL_LEVEL_ALIASES:
        return _SKILL_LEVEL_ALIASES[text]
    if text in _SKILL_LEVEL_NUMERIC:
        return _SKILL_LEVEL_NUMERIC[text]
    # Падежи и составные алиасы («новичкам», «про-игрок», «уровень профи»)
    # разбирает единый нормализатор новичкового тренера.
    try:
        from .beginner import normalize_skill_level as _beginner_normalize

        return _beginner_normalize(text)
    except Exception:  # noqa: BLE001 — конфиг важнее, чем расширенные алиасы
        return "beginner"


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
    model_path: str = "vosk-model-small-ru-0.22"
    sample_rate: int = 16000
    block_size: int = 8000
    input_device: int | str | None = None
    # Сколько секунд ждать тишины перед тем, как считать фразу законченной.
    listen_timeout: float = 0.0
    # Push-to-talk: слушать только пока зажата клавиша.
    push_to_talk: bool = True
    ptt_key: str = "v"


@dataclass
class TtsConfig:
    enabled: bool = True
    provider: str = "edge"  # "edge" (нейросеть Svetlana/Dmitry) или "sapi" (локальный Windows)
    edge_voice: str = "ru-RU-SvetlanaNeural"  # "ru-RU-SvetlanaNeural" или "ru-RU-DmitryNeural"
    # Диапазон SAPI: -10 (медленно) .. 10 (быстро), 0 — норма.
    rate: int = 0
    volume: int = 100
    voice_hint: str = "Irina"
    max_chars: int = 700
    output_device: int | str | None = None



@dataclass
class OverlayConfig:
    enabled: bool = True
    width: int = 520
    height: int = 300
    alpha: float = 0.9
    topmost: bool = True
    x: int | None = None
    y: int | None = None
    toggle_key: str = "alt+z"



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


DEFAULT_LLM_PRESETS: dict[str, dict] = {
    "pollinations": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key": "",
        "model": "openai",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "pollinations_gemini": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key": "",
        "model": "gemini",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "pollinations_gemini_thinking": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key": "",
        "model": "gemini-thinking",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "google_gemini": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "",
        "model": "gemini-2.5-flash",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "gemini_pro": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "",
        "model": "gemini-2.5-pro",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "openrouter_free": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "model": "google/gemini-2.0-flash-exp:free",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "deepseek": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.4,
        "timeout": 60.0,
    },
    "openrouter": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "model": "openai/gpt-4o-mini",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "groq": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.4,
        "timeout": 60.0,
    },
    "openai": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.4,
        "timeout": 60.0,
    },
    "ollama": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "api_key": "",
        "model": "gemma3",
        "temperature": 0.4,
        "timeout": 120.0,
    },
}


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
    max_tokens: int = 350
    presets: dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_LLM_PRESETS))


@dataclass
class PlayerProfile:
    """Личный профиль игрока: основная роль и пул героев по позициям.

    hero_pool: ключи "1".."5" (строки), значения — списки имён героев
    (как в OpenDota localized_name, напр. "Queen of Pain").
    account_id: Dota 2 (Steam32) ID игрока для автоматического анализа матчей.
    """

    default_position: int = 1
    mmr_bracket: str = ""
    hero_pool: dict[str, list[str]] = field(default_factory=dict)
    account_id: int | None = None
    coach_name: str = "Астра"
    autonomy_level: str = "medium"
    #: Уровень игры: beginner | amateur | advanced | expert.
    #: Тренер подстраивает объяснения под новичка (см. normalize_skill_level).
    skill_level: str = "beginner"
    signature_heroes: list[str] = field(default_factory=list)
    common_mistakes: list[str] = field(default_factory=list)


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

    minimize_to_tray: bool = True
    autostart: bool = False

    data_dir: str = "data"
    cache_dir: str = ".cache"
    logs_dir: str = "logs"
    memory_size: int = 20
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
        last_exc: Exception | None = None
        for arg in args:
            try:
                return _coerce(value, arg, path)
            except ConfigError as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        return value
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
        if key == "skill_level" and hint is str:
            # Невалидный тип уровня (число/список/None) не должен ломать загрузку
            # конфига: нормализуем в "beginner" (см. _validate_profile).
            kwargs[key] = value if isinstance(value, str) else normalize_skill_level(value)
            continue
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


def get_preset_path() -> Path | None:
    """Ищет файл default_preset.json в каталогах проекта и дистрибутива."""
    candidates = [
        Path(__file__).resolve().parent / "default_preset.json",
        PROJECT_ROOT / "aidota" / "default_preset.json",
        PROJECT_ROOT / "default_preset.json",
    ]
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend([
                exe_dir / "aidota" / "default_preset.json",
                exe_dir / "_internal" / "aidota" / "default_preset.json",
                exe_dir / "default_preset.json",
            ])
        except Exception:
            pass
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def load_default_preset() -> dict:
    """Загружает эталонный пресет разработчика default_preset.json."""
    preset_path = get_preset_path()
    if preset_path and preset_path.is_file():
        try:
            data = json.loads(preset_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            print(f"Предупреждение: ошибка чтения {preset_path}: {exc}", file=sys.stderr)
    return _defaults_dict()


def validate_audio_devices(config: Config) -> bool:
    """Проверяет валидность настроенных аудиоустройств; если устройства нет на ПК — сбрасывает на None."""
    changed = False
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        input_indices = {i for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0}
        output_indices = {i for i, d in enumerate(devices) if d.get("max_output_channels", 0) > 0}

        if config.audio.input_device is not None:
            dev = config.audio.input_device
            valid = False
            if isinstance(dev, int) and dev in input_indices:
                valid = True
            elif isinstance(dev, str):
                valid = any(
                    str(dev).lower() in d.get("name", "").lower()
                    for i, d in enumerate(devices)
                    if i in input_indices
                )
            if not valid:
                config.audio.input_device = None
                changed = True

        if config.tts.output_device is not None:
            dev = config.tts.output_device
            valid = False
            if isinstance(dev, int) and dev in output_indices:
                valid = True
            elif isinstance(dev, str):
                valid = any(
                    str(dev).lower() in d.get("name", "").lower()
                    for i, d in enumerate(devices)
                    if i in output_indices
                )
            if not valid:
                config.tts.output_device = None
                changed = True
    except Exception:
        pass
    return changed


def reset_to_defaults(path: str | Path | None = None) -> Config:
    """Сбрасывает конфигурацию к эталонным настройкам из default_preset.json."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    raw_preset = load_default_preset()
    # Деобфусцируем ключ для живой конфигурации
    if isinstance(raw_preset.get("llm"), dict) and "api_key" in raw_preset["llm"]:
        raw_preset["llm"]["api_key"] = deobfuscate_api_key(raw_preset["llm"]["api_key"])

    merged = _merge(_defaults_dict(), raw_preset)
    config = _from_dict(Config, merged)
    _apply_backward_compat(config, raw_preset)
    _validate_profile(config)
    validate_audio_devices(config)

    save_config(config, config_path)
    return config


def load_config(path: str | Path | None = None, *, create_if_missing: bool = True) -> Config:
    """Загружает config.json, дополняя его значениями по умолчанию."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    if not config_path.is_file():
        if create_if_missing:
            config = reset_to_defaults(config_path)
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

    # Деобфусцируем ключ, если он сохранён с префиксом enc:
    if config.llm.api_key and config.llm.api_key.startswith("enc:"):
        config.llm.api_key = deobfuscate_api_key(config.llm.api_key)

    _validate_profile(config)
    validate_audio_devices(config)
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

    acc = config.player_profile.account_id
    if acc is not None:
        try:
            config.player_profile.account_id = int(acc) if int(acc) > 0 else None
        except (ValueError, TypeError):
            config.player_profile.account_id = None

    # Уровень игры: любые алиасы/мусор нормализуем, невалидное -> "beginner".
    config.player_profile.skill_level = normalize_skill_level(
        getattr(config.player_profile, "skill_level", "beginner")
    )


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
