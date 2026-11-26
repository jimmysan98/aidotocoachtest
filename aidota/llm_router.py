"""Маршрутизация LLM (Tier 0 / Tier 1 / Tier 2), Circuit Breaker и прерывание по generation_id."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

OperatingMode = Literal[
    "deepseek",
    "openai_compatible",
    "ollama",
    "pollinations",
    "deterministic",
]
TierName = Literal["tier0", "tier1", "tier2"]
FailureKind = Literal["auth", "billing", "rate_limit", "transient"]
BreakerState = Literal["closed", "open", "half_open"]

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_CHAT_MODEL = "deepseek-chat"
DEEPSEEK_REASONER_MODEL = "deepseek-reasoner"

# DeepSeek Reasoner делит max_tokens между reasoning_content и финальным content.
# Минимум 4096 исключает пустой content из-за исчерпания бюджета на рассуждения (Fork C).
REASONER_MIN_API_MAX_TOKENS = 4096

TIER1_VOICE_DEADLINE_SEC = 3.0
TIER1_PICK_DEADLINE_SEC = 6.0
TIER2_REASONER_DEADLINE_SEC = 20.0
TIER2_FALLBACK_DEADLINE_SEC = 6.0

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)

_DEEP_STRATEGIC_KINDS = frozenset({"draft", "game_analysis", "match", "matchup"})
_CHAT_DEEP_KINDS = frozenset({"why", "explain"})
_TAVERN_STATES = frozenset(
    {
        "DOTA_GAMERULES_STATE_HERO_SELECTION",
        "DOTA_GAMERULES_STATE_STRATEGY_TIME",
        "DOTA_GAMERULES_STATE_TEAM_SHOWCASE",
        "DOTA_GAMERULES_STATE_PRE_GAME",
        "DOTA_GAMERULES_STATE_POST_GAME",
    }
)


def detect_provider_by_key(api_key: str) -> tuple[str, str, str] | None:
    """Определяет (provider, base_url, model) по префиксу API-ключа."""
    key = (api_key or "").strip()
    if not key:
        return None
    if key.startswith("AIzaSy"):
        return (
            "openai_compatible",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-flash",
        )
    if key.startswith("gsk_"):
        return (
            "openai_compatible",
            "https://api.groq.com/openai/v1",
            "llama-3.3-70b-versatile",
        )
    if key.startswith("sk-or-"):
        return (
            "openai_compatible",
            "https://openrouter.ai/api/v1",
            "deepseek/deepseek-chat",
        )
    if key.startswith("sk-"):
        return (
            "openai_compatible",
            DEEPSEEK_BASE_URL,
            DEEPSEEK_CHAT_MODEL,
        )
    return None


def is_deepseek_endpoint(base_url: str) -> bool:
    return "api.deepseek.com" in (base_url or "").lower()


def is_pollinations_endpoint(base_url: str) -> bool:
    return "pollinations" in (base_url or "").lower()


def is_reasoning_model(model: str) -> bool:
    low = (model or "").strip().lower()
    return (
        "reasoner" in low
        or low.endswith("-r1")
        or "/deepseek-r1" in low
        or low.startswith(("o1", "o3"))
    )


def compute_operating_mode(llm: Any) -> OperatingMode:
    """Определяет активный режим работы на основе конфигурации (без иллюзорных фолбэков)."""
    provider = str(getattr(llm, "provider", "ollama") or "ollama").strip().lower()
    base_url = str(getattr(llm, "base_url", "") or "").strip()
    api_key = str(getattr(llm, "api_key", "") or "").strip()
    if provider == "ollama":
        return "ollama"
    if provider == "openai_compatible":
        if is_pollinations_endpoint(base_url):
            return "pollinations"
        if not api_key:
            detected = detect_provider_by_key(api_key)
            if detected is None:
                return "deterministic"
        if is_deepseek_endpoint(base_url) or (api_key.startswith("sk-") and not api_key.startswith("sk-or-")):
            return "deepseek"
        return "openai_compatible"
    return "deterministic"


def classify_http_failure(status_code: int, detail: str = "") -> FailureKind:
    """Разделяет постоянные ошибки ключа/баланса (без failover) и временные сбои."""
    low = (detail or "").lower()
    if status_code in (401, 403) or any(
        marker in low
        for marker in ("invalid api", "unauthorized", "expired", "api-ключ", "недействителен")
    ):
        return "auth"
    if status_code == 402 or "insufficient balance" in low or "quota exceeded" in low:
        return "billing"
    if status_code == 429 or "rate limit" in low:
        return "rate_limit"
    return "transient"


def extract_openai_message(data: dict[str, Any]) -> tuple[str, list[Any], str, dict[str, int]]:
    """Безопасно извлекает (content, tool_calls, reasoning_content, usage).

    ``reasoning_content`` (и <think>-теги) всегда отделяются от финального текста.
    """
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "", [], "", {}
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        return "", [], "", {}

    raw_content = str(msg.get("content") or "")
    reasoning = str(msg.get("reasoning_content") or "")
    if "<think>" in raw_content.lower():
        thinks = _THINK_BLOCK_RE.findall(raw_content)
        if thinks and not reasoning:
            reasoning = "\n".join(thinks)
        raw_content = _THINK_BLOCK_RE.sub("", raw_content)

    tool_calls = list(msg.get("tool_calls") or [])
    usage_raw = data.get("usage") or {}
    usage: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        details = usage_raw.get("completion_tokens_details") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
            "cache_hit_tokens": int(
                usage_raw.get("prompt_cache_hit_tokens")
                or (prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else 0)
                or 0
            ),
            "cache_miss_tokens": int(usage_raw.get("prompt_cache_miss_tokens") or 0),
            "reasoning_tokens": int(
                (details.get("reasoning_tokens") if isinstance(details, dict) else 0) or 0
            ),
        }
    return raw_content.strip(), tool_calls, reasoning.strip(), usage


class PreemptionController:
    """Счётчик поколений запросов (generation_id) для отмены устаревших вызовов."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._events: dict[int, threading.Event] = {}

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def start_turn(self) -> tuple[int, threading.Event]:
        """Открывает новое поколение и отменяет предыдущие активные вызовы."""
        with self._lock:
            for gen, ev in self._events.items():
                if gen <= self._generation:
                    ev.set()
            self._generation += 1
            gen_id = self._generation
            ev = threading.Event()
            self._events[gen_id] = ev
            # Храним только последние 16 событий отмен.
            stale = [k for k in self._events if k < gen_id - 16]
            for k in stale:
                self._events.pop(k, None)
            return gen_id, ev

    def cancel_current(self) -> int:
        """Принудительно отменяет текущее поколение (например, при Barge-In или новом PTT)."""
        with self._lock:
            for ev in self._events.values():
                ev.set()
            self._generation += 1
            return self._generation

    def is_current(self, generation_id: int) -> bool:
        with self._lock:
            ev = self._events.get(generation_id)
            if ev is not None and ev.is_set():
                return False
            return generation_id == self._generation


@dataclass
class _BreakerSlot:
    state: BreakerState = "closed"
    failures: int = 0
    opened_at: float = 0.0
    half_open_in_flight: bool = False


class EndpointBreaker:
    """Потокобезопасный Circuit Breaker по ключу (provider, base_url, model)."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = max(1.0, float(cooldown_seconds))
        self._monotonic = monotonic_fn or time.monotonic
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str, str], _BreakerSlot] = {}

    @staticmethod
    def _key(provider: str, base_url: str, model: str) -> tuple[str, str, str]:
        return (
            (provider or "").strip().lower(),
            (base_url or "").strip().rstrip("/").lower(),
            (model or "").strip().lower(),
        )

    def allow_request(self, provider: str, base_url: str, model: str) -> bool:
        key = self._key(provider, base_url, model)
        now = self._monotonic()
        with self._lock:
            slot = self._slots.setdefault(key, _BreakerSlot())
            if slot.state == "closed":
                return True
            if slot.state == "open":
                if now - slot.opened_at >= self._cooldown:
                    slot.state = "half_open"
                    slot.half_open_in_flight = True
                    return True
                return False
            # half_open: пускаем строго один пробный запрос
            if not slot.half_open_in_flight:
                slot.half_open_in_flight = True
                return True
            return False

    def record_success(self, provider: str, base_url: str, model: str) -> None:
        key = self._key(provider, base_url, model)
        with self._lock:
            slot = self._slots.setdefault(key, _BreakerSlot())
            slot.state = "closed"
            slot.failures = 0
            slot.opened_at = 0.0
            slot.half_open_in_flight = False

    def record_failure(self, provider: str, base_url: str, model: str) -> BreakerState:
        key = self._key(provider, base_url, model)
        now = self._monotonic()
        with self._lock:
            slot = self._slots.setdefault(key, _BreakerSlot())
            slot.failures += 1
            slot.half_open_in_flight = False
            if slot.state == "half_open" or slot.failures >= self._threshold:
                slot.state = "open"
                slot.opened_at = now
            return slot.state

    def reset_all(self) -> None:
        with self._lock:
            self._slots.clear()

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                f"{prov}:{model}": slot.state
                for (prov, _url, model), slot in self._slots.items()
                if slot.state != "closed"
            }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Выбранный маршрут выполнения конкретного запроса."""

    tier: TierName
    model: str
    fallback_model: str | None
    deadline_seconds: float
    fallback_deadline_seconds: float
    allow_tools: bool
    allow_pollinations_failover: bool
    api_max_tokens: int
    send_temperature: bool


class LlmRouter:
    """Определяет тир, модель, бюджет токенов и дедлайн под режим игры (Forks B2, C2)."""

    def __init__(self, *, breaker: EndpointBreaker | None = None) -> None:
        self.breaker = breaker or EndpointBreaker()

    def plan_route(
        self,
        llm: Any,
        *,
        kind: str = "chat",
        voice: bool = False,
        detail_mode: str = "auto",
        game_state: str = "",
        hero_alive: bool | None = None,
        respawn_seconds: int = 0,
        requested_max_tokens: int | None = None,
    ) -> RouteDecision:
        """Выбирает Tier 1 vs Tier 2 строго по правилам боевых и стратегических путей."""
        provider = str(getattr(llm, "provider", "ollama") or "ollama").strip().lower()
        base_url = str(getattr(llm, "base_url", "") or "").strip()
        model = str(getattr(llm, "model", "") or "").strip()
        api_key = str(getattr(llm, "api_key", "") or "").strip()
        routing_mode = str(getattr(llm, "routing_mode", "auto") or "auto").strip().lower()
        # Pollinations допускается только при отсутствии облачного API-ключа
        # и вне горячего голосового пути (Fork B / Decision #1).
        cfg_allow_poll = getattr(llm, "allow_pollinations_fallback", None)
        allow_poll = (
            (not bool(api_key))
            and (not voice)
            and (True if cfg_allow_poll is None else bool(cfg_allow_poll))
        )
        tier1_override = str(getattr(llm, "tier1_model", "") or "").strip()
        tier2_override = str(getattr(llm, "tier2_model", "") or "").strip()

        is_deepseek = provider == "openai_compatible" and is_deepseek_endpoint(base_url)

        # Определяем стратегическое окно для Tier 2 (Fork C2).
        in_tavern = game_state in _TAVERN_STATES or (
            hero_alive is False and respawn_seconds >= 15
        )
        wants_tier2 = False
        if not voice and routing_mode != "fast_only":
            if kind in _DEEP_STRATEGIC_KINDS:
                wants_tier2 = True
            elif kind in _CHAT_DEEP_KINDS and (detail_mode == "detailed" or in_tavern):
                wants_tier2 = True
            elif kind == "chat" and (in_tavern or detail_mode == "detailed"):
                wants_tier2 = True

        # Срочные пики в лайв-драфте (kind == "pick") всегда идут в Tier 1 (<= 6s, Fork C2).
        if kind == "pick":
            wants_tier2 = False

        # В бою / при voice=True deepseek-reasoner строго запрещён.
        if voice:
            wants_tier2 = False

        if is_deepseek:
            fast_model = tier1_override or DEEPSEEK_CHAT_MODEL
            deep_model = tier2_override or DEEPSEEK_REASONER_MODEL
        else:
            fast_model = tier1_override or model
            deep_model = tier2_override or model

        # Если пользователь прямо выбрал deepseek-reasoner как базовую модель на DeepSeek API,
        # голосовой горячий путь всё равно обязан идти через deepseek-chat (Fork C2).
        if is_deepseek and is_reasoning_model(fast_model):
            fast_model = DEEPSEEK_CHAT_MODEL

        req_tokens = int(requested_max_tokens or getattr(llm, "max_tokens", 350) or 350)

        if wants_tier2 and (is_deepseek or is_reasoning_model(deep_model)):
            tier2_ceiling = max(
                REASONER_MIN_API_MAX_TOKENS,
                int(getattr(llm, "tier2_max_tokens", REASONER_MIN_API_MAX_TOKENS) or REASONER_MIN_API_MAX_TOKENS),
                req_tokens,
            )
            deadline = float(
                getattr(llm, "tier2_timeout", TIER2_REASONER_DEADLINE_SEC)
                or TIER2_REASONER_DEADLINE_SEC
            )
            return RouteDecision(
                tier="tier2",
                model=deep_model,
                fallback_model=fast_model if fast_model != deep_model else None,
                deadline_seconds=min(deadline, float(getattr(llm, "timeout", deadline) or deadline)),
                fallback_deadline_seconds=TIER2_FALLBACK_DEADLINE_SEC,
                allow_tools=False,
                allow_pollinations_failover=allow_poll,
                api_max_tokens=tier2_ceiling,
                send_temperature=not is_reasoning_model(deep_model),
            )

        # Tier 1: боевой/голосовой или срочный лайв-драфт.
        if voice:
            t1_deadline = float(
                getattr(llm, "tier1_timeout", TIER1_VOICE_DEADLINE_SEC)
                or TIER1_VOICE_DEADLINE_SEC
            )
            allow_tools = False
        elif kind == "pick":
            t1_deadline = TIER1_PICK_DEADLINE_SEC
            allow_tools = False
        else:
            # Текстовый чат без reasoner: допускаем стандартный таймаут провайдера.
            cfg_timeout = float(getattr(llm, "timeout", 60.0) or 60.0)
            t1_deadline = min(cfg_timeout, 20.0 if provider == "openai_compatible" else cfg_timeout)
            allow_tools = not is_pollinations_endpoint(base_url)

        chosen_t1 = fast_model
        if is_reasoning_model(chosen_t1) and is_deepseek:
            chosen_t1 = DEEPSEEK_CHAT_MODEL

        return RouteDecision(
            tier="tier1",
            model=chosen_t1,
            fallback_model=None,
            deadline_seconds=t1_deadline,
            fallback_deadline_seconds=0.0,
            allow_tools=allow_tools,
            allow_pollinations_failover=allow_poll,
            api_max_tokens=max(60, min(req_tokens, 900)),
            send_temperature=not is_reasoning_model(chosen_t1),
        )
