"""Логирование в консоль и файл (с ротацией)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Настраивает корневой логгер. Повторный вызов безопасен."""
    global _configured
    logger = logging.getLogger("aidota")
    if _configured:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "aidota.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
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
