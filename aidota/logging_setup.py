"""Логирование в консоль и файл (с ротацией и обязательным редактированием секретов)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .security import RedactingFormatter, SecretRedactingFilter

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def reset_logging() -> None:
    """Сбрасывает обработчики и фильтры логгера ``aidota`` (для изоляции тестов)."""
    global _configured
    logger = logging.getLogger("aidota")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    for flt in list(logger.filters):
        logger.removeFilter(flt)
    _configured = False


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Настраивает корневой логгер ``aidota``. Повторный вызов безопасен."""
    global _configured
    logger = logging.getLogger("aidota")
    if _configured:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    secret_filter = SecretRedactingFilter()
    logger.addFilter(secret_filter)

    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter(logging.Formatter("%(message)s")))
    console.addFilter(secret_filter)
    logger.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "aidota.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            RedactingFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
        )
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)
    except OSError as exc:  # например, каталог только для чтения
        logger.warning("Не удалось открыть лог-файл: %s", exc)

    # Сторонние библиотеки не должны засорять консоль.
    for noisy in ("urllib3", "requests", "httpx", "PIL", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"aidota.{name}" if name else "aidota")
