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

from .paths import (
    app_install_root,
    atomic_write_json,
    bundled_data_dir,
    default_config_path,
    meta_cache_dir,
    resolve_writable_dir,
    writable_root,
)
from .security import register_config_secrets

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

PROJECT_ROOT = app_install_root()
DEFAULT_CONFIG_PATH = default_config_path()


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

COACH_TONES: tuple[str, ...] = ("balanced", "tactical", "supportive", "brief")
DETAIL_MODES: tuple[str, ...] = ("auto", "concise", "detailed")


def normalize_coach_tone(value: typing.Any) -> str:
    """Приводит тон/стиль тренера к одному из допустимых значений."""
    text = str(value or "").strip().lower()
    aliases = {
        "balanced": "balanced", "сбалансированный": "balanced", "обычный": "balanced",
        "tactical": "tactical", "тактический": "tactical", "строгий": "tactical", "профи": "tactical",
        "supportive": "supportive", "поддерживающий": "supportive", "дружелюбный": "supportive",
        "brief": "brief", "краткий": "brief", "лаконичный": "brief", "короткий": "brief",
    }
    return aliases.get(text, "balanced")


def normalize_detail_mode(value: typing.Any) -> str:
    """Приводит режим детализации ответов к auto | concise | detailed."""
    text = str(value or "").strip().lower()
    aliases = {
        "auto": "auto", "авто": "auto",
        "concise": "concise", "кратко": "concise", "коротко": "concise",
        "detailed": "detailed", "подробно": "detailed", "глубокий": "detailed",
    }
    return aliases.get(text, "auto")

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
    provider: str = "edge"  # "edge" (нейросеть Dmitry/Svetlana), "piper" или "sapi"
    edge_voice: str = "ru-RU-DmitryNeural"  # "ru-RU-DmitryNeural" или "ru-RU-SvetlanaNeural"
    # Настройки разговорной просодии edge-tts (живая речь, без монотонности)
    rate_str: str = "+10%"
    pitch_str: str = "-3Hz"
    # Настройки Piper TTS (офлайн/fallback)
    piper_noise_scale: float = 0.75
    piper_length_scale: float = 0.92
    # RVC Voice Conversion hook
    rvc_enabled: bool = False
    rvc_model_path: str = ""
    # Локальное кэширование статических фраз
    cache_audio: bool = True
    # Диапазон SAPI: -10 (медленно) .. 10 (быстро), 0 — норма.
    rate: int = 0
    volume: int = 100
    voice_hint: str = "Dmitry"
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
    "pollinations_deepseek": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key": "",
        "model": "deepseek",
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
    "google_gemini_lite": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "",
        "model": "gemini-2.5-flash-lite",
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
    "deepseek": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "deepseek_r1": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-reasoner",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 4096,
    },
    "groq": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "groq_fast": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "model": "llama-3.1-8b-instant",
        "temperature": 0.4,
        "timeout": 45.0,
        "max_tokens": 300,
    },
    "openrouter": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "model": "deepseek/deepseek-chat",
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
    "openai": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.4,
        "timeout": 60.0,
        "max_tokens": 350,
    },
    "ollama": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "api_key": "",
        "model": "gemma3",
        "temperature": 0.4,
        "timeout": 120.0,
        "max_tokens": 350,
    },
}


@dataclass
class LlmConfig:
    """Единый интерфейс для локальных (Ollama) и облачных LLM.

    provider: "ollama" | "openai_compatible"
    Для OpenAI-совместимых провайдеров (DeepSeek, OpenRouter, Groq, Gemini...)
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
    routing_mode: str = "auto"
    tier1_model: str = ""
    tier2_model: str = ""
    tier1_timeout: float = 3.0
    tier2_timeout: float = 20.0
    tier2_max_tokens: int = 4096
    allow_pollinations_fallback: bool = True
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
    #: Стиль подачи тренера: balanced | tactical | supportive | brief
    coach_tone: str = "balanced"
    #: Режим детализации: auto | concise | detailed
    detail_mode: str = "auto"
    #: Личная цель или фокус игрока (подмешивается в промпт)
    custom_focus: str = ""
    #: Автоматически озвучивать ответы в текстовом чате
    speak_chat: bool = True
    signature_heroes: list[str] = field(default_factory=list)
    common_mistakes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.coach_tone = normalize_coach_tone(self.coach_tone)
        self.detail_mode = normalize_detail_mode(self.detail_mode)
        self.custom_focus = " ".join(str(self.custom_focus or "").split())[:200]
        self.speak_chat = bool(self.speak_chat)


@dataclass
class DotaConfig:
    # Если None — путь определяется автоматически по библиотекам Steam.
    cfg_dir: str | None = None
    gsi_config_name: str = "gamestate_integration_aidota.cfg"


@dataclass
class Config:
    schema_version: int = 2
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
    def bundled_data_path(self) -> Path:
        """Read-only каталог офлайн-снапшотов (``data/heroes.json``, ``data/items.json``)."""
        raw = Path(self.data_dir)
        if raw.is_absolute() and (raw / "heroes.json").is_file():
            return raw
        return bundled_data_dir(app_install_root())

    @property
    def state_data_path(self) -> Path:
        """Записываемый каталог пользовательского состояния (%LOCALAPPDATA%\\AIDOTA\\data в frozen)."""
        return resolve_writable_dir(self.data_dir)

    @property
    def data_path(self) -> Path:
        return self.state_data_path

    @property
    def cache_path(self) -> Path:
        return resolve_writable_dir(self.cache_dir)

    @property
    def meta_cache_path(self) -> Path:
        return meta_cache_dir(self.cache_path)

    @property
    def logs_path(self) -> Path:
        return resolve_writable_dir(self.logs_dir)


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


def _resolve_config_path(path: str | Path | None) -> Path:
    if not path:
        return default_config_path()
    p = Path(path)
    return p if p.is_absolute() else (writable_root() / p)


def reset_to_defaults(path: str | Path | None = None) -> Config:
    """Сбрасывает конфигурацию к эталонным настройкам из default_preset.json."""
    config_path = _resolve_config_path(path)

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
    config_path = _resolve_config_path(path)

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
    for preset in (config.llm.presets or {}).values():
        if isinstance(preset, dict):
            pkey = preset.get("api_key")
            if isinstance(pkey, str) and pkey.startswith("enc:"):
                preset["api_key"] = deobfuscate_api_key(pkey)

    # Защита от пустых настроек LLM: если в конфиге был пустой блок llm
    if "llm" in raw:
        if not (config.llm.base_url or "").strip():
            config.llm.base_url = "https://text.pollinations.ai/openai"
        if not (config.llm.model or "").strip():
            config.llm.model = "openai"
        if not (config.llm.provider or "").strip():
            config.llm.provider = "openai_compatible"

    _validate_profile(config)
    validate_audio_devices(config)
    register_config_secrets(config)
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
    config.player_profile.coach_tone = normalize_coach_tone(
        getattr(config.player_profile, "coach_tone", "balanced")
    )
    config.player_profile.detail_mode = normalize_detail_mode(
        getattr(config.player_profile, "detail_mode", "auto")
    )
    config.player_profile.custom_focus = str(
        getattr(config.player_profile, "custom_focus", "") or ""
    ).strip()[:200]
    config.player_profile.speak_chat = bool(
        getattr(config.player_profile, "speak_chat", True)
    )


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _prepare_llm_dict_for_disk(llm_dict: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Обфусцирует API-ключи перед записью config.json на диск (не меняя объект в RAM)."""
    out = dict(llm_dict)
    raw_key = str(out.get("api_key") or "").strip()
    if raw_key and not raw_key.startswith("enc:"):
        out["api_key"] = obfuscate_api_key(raw_key)
    presets = out.get("presets")
    if isinstance(presets, dict):
        clean_presets: dict[str, typing.Any] = {}
        for name, preset in presets.items():
            if isinstance(preset, dict):
                p_copy = dict(preset)
                pkey = str(p_copy.get("api_key") or "").strip()
                if pkey and not pkey.startswith("enc:"):
                    p_copy["api_key"] = obfuscate_api_key(pkey)
                clean_presets[name] = p_copy
            else:
                clean_presets[name] = preset
        out["presets"] = clean_presets
    return out


def save_config(config: Config, path: str | Path | None = None) -> Path:
    config_path = _resolve_config_path(path)
    register_config_secrets(config)
    data = dataclasses.asdict(config)
    if isinstance(data.get("llm"), dict):
        data["llm"] = _prepare_llm_dict_for_disk(data["llm"])
    # None-значения (например, input_device) оставляем — это осознанный выбор.
    atomic_write_json(config_path, data)
    return config_path


def save_llm_only(config: Config, path: str | Path | None = None) -> Path:
    """Сохраняет только секцию ``llm``, не трогая остальной config.json.

    Нужно, чтобы смена модели/пресета голосом не затирала другие настройки
    (и не сохраняла временные флаги командной строки вроде ``--no-tts``).
    """
    config_path = _resolve_config_path(path)
    register_config_secrets(config)
    raw: dict = {}
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["llm"] = _prepare_llm_dict_for_disk(dataclasses.asdict(config.llm))
    atomic_write_json(config_path, raw)
    return config_path
