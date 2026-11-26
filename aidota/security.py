"""Утилиты редактирования секретов в логах, ошибках и диагностике."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from typing import Any

_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._\-+/=]{6,})")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api_key|apikey|key|token|access_token)=)([^&\s'\"]+)"
)
_KV_SECRET_RE = re.compile(
    r"""(?i)(["']?(?:api_key|llm_api_key|auth_token|gsi_token|x-api-key|secret|password)["']?\s*[:=]\s*["']?)([^"'\s,;}\]]+)"""
)
_TOKEN_KEY_RE = re.compile(r"""(?i)(["']token["']\s*[:=]\s*["']?)([^"'\s,;}\]]+)""")
_PREFIX_SECRET_RE = re.compile(
    r"\b(?:sk-or-v1-[A-Za-z0-9\-_]{8,}|sk-[A-Za-z0-9\-_]{10,}|AIzaSy[A-Za-z0-9\-_]{8,}|gsk_[A-Za-z0-9\-_]{8,}|enc:[A-Za-z0-9+/=]{12,})"
)


class SecretRegistry:
    """Потокобезопасный реестр актуальных секретов для фильтрации логов."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._secrets: set[str] = set()

    def register(self, secret: str | None) -> None:
        if not secret:
            return
        val = str(secret).strip()
        if len(val) < 6 or val == "[REDACTED]":
            return
        with self._lock:
            self._secrets.add(val)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._secrets, key=len, reverse=True))


_REGISTRY = SecretRegistry()


def register_secret(secret: str | None) -> None:
    """Регистрирует значение секрета для последующего редактирования в логах."""
    _REGISTRY.register(secret)


def clear_registered_secrets() -> None:
    """Очищает зарегистрированные секреты (используется в изоляции тестов)."""
    _REGISTRY.clear()


def register_config_secrets(config: Any) -> None:
    """Извлекает известные ключи из конфигурации и регистрирует их."""
    if config is None:
        return
    llm = getattr(config, "llm", None)
    if llm is not None:
        register_secret(getattr(llm, "api_key", None))
        presets = getattr(llm, "presets", None)
        if isinstance(presets, dict):
            for preset in presets.values():
                if isinstance(preset, dict):
                    register_secret(preset.get("api_key"))
    gsi = getattr(config, "gsi", None)
    if gsi is not None:
        tok = getattr(gsi, "token", None)
        # Стандартный локальный токен GSI не является облачным секретом,
        # но пользовательский нестандартный токен защищаем.
        if isinstance(tok, str) and tok and tok != "aidota-local-token":
            register_secret(tok)


def mask_api_key_for_display(plain_key: str | None) -> str:
    """Возвращает безопасную маску ключа для отображения в UI (без открытого секрета)."""
    val = str(plain_key or "").strip()
    if not val:
        return ""
    if len(val) <= 8:
        return "****"
    return f"{val[:4]}...{val[-4:]}"


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Скрывает API-ключи, Bearer-токены, URL-параметры ключей и известные секреты."""
    if not text:
        return ""
    result = str(text)
    known = list(_REGISTRY.snapshot())
    for item in extra_secrets:
        if item and len(str(item).strip()) >= 6:
            known.append(str(item).strip())
    for secret in sorted(set(known), key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, "[REDACTED]")
    result = _BEARER_RE.sub(r"\1[REDACTED]", result)
    result = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", result)
    result = _KV_SECRET_RE.sub(r"\1[REDACTED]", result)
    result = _TOKEN_KEY_RE.sub(r"\1[REDACTED]", result)
    result = _PREFIX_SECRET_RE.sub("[REDACTED]", result)
    return result


class SecretRedactingFilter(logging.Filter):
    """Фильтр логирования, редактирующий секреты до форматирования записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            redacted = redact_secrets(rendered)
            if redacted != rendered or record.args:
                record.msg = redacted
                record.args = ()
            if record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
        except Exception:
            if isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
        return True


class RedactingFormatter(logging.Formatter):
    """Обёртка над Formatter, гарантирующая редактирование секретов в трейсбеках."""

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self._inner.format(record))
