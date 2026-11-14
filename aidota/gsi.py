"""Приёмник Dota 2 Game State Integration.

Свой минимальный HTTP-сервер вместо сторонней библиотеки: GSI шлёт POST
с JSON на локальный порт, мы храним последний снимок состояния под замком.
Сервер переживает недоступный порт (тогда приложение работает без GSI).
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("aidota.gsi")

MAX_BODY = 4 * 1024 * 1024  # защита от мусорных запросов


class GsiError(RuntimeError):
    pass


def deep_merge(base: dict, update: dict, max_depth: int = 15) -> dict:
    """Рекурсивно сливает новые данные GSI с предыдущим снимком.

    Dota присылает не все секции в каждом пакете (например, драфт
    перестаёт приходить после начала игры), поэтому объединяем.
    Не мутирует исходный base, копирует вложенные структуры без дорогого copy.deepcopy.
    """
    if max_depth <= 0:
        return copy.deepcopy(update)
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value, max_depth=max_depth - 1)
        elif value is not None:
            if isinstance(value, dict):
                result[key] = deep_merge({}, value, max_depth=max_depth - 1)
            elif isinstance(value, list):
                result[key] = [
                    deep_merge({}, item, max_depth=max_depth - 1) if isinstance(item, dict)
                    else (list(item) if isinstance(item, list) else item)
                    for item in value
                ]
            else:
                result[key] = value
    return result


class _Handler(BaseHTTPRequestHandler):
    server_version = "AIDOTA-GSI/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 10.0

    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802 — HTTP-метод
        server: "GsiServer" = self.server.gsi_server  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            self._respond(400, close=True)
            return
        try:
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
        except (ConnectionError, BrokenPipeError):
            return
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.debug("Плохой JSON от Dota 2: %s", exc)
            self._respond(400)
            return
        if not isinstance(payload, dict):
            self._respond(400)
            return

        auth = payload.get("auth")
        token = auth.get("token") if isinstance(auth, dict) else None
        if not server.check_token(token):
            log.warning("GSI: неверный токен, запрос отклонён")
            self._respond(401)
            return

        server._store(payload)  # noqa: SLF001 — намеренно внутренний API
        try:
            self._respond(200)
        except (ConnectionError, BrokenPipeError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        # Dota 2 использует только POST; GET отвечаем для отладки.
        self._respond(405)

    def _respond(self, code: int, close: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        if close or getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        else:
            self.send_header("Connection", "keep-alive")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("GSI http: " + fmt, *args)


class GsiServer:
    def __init__(self, host: str, port: int, token: str):
        self.host = host
        self.port = int(port)
        self.token = token
        self._lock = threading.RLock()
        self._payload: dict = {}
        self._cached_snapshot: dict | None = None
        self._received_at: float | None = None
        self._count = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._httpd is not None:
            return
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as exc:
            raise GsiError(
                f"Не удалось занять {self.host}:{self.port} для GSI: {exc}. "
                "Возможно, порт уже занят другим приложением."
            ) from exc
        httpd.daemon_threads = True
        httpd.gsi_server = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="aidota-gsi", daemon=True
        )
        self._thread.start()
        log.info("GSI-сервер слушает http://%s:%s/", self.host, self.port)

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ------------------------------------------------------------------ #
    def check_token(self, token: Any) -> bool:
        if not isinstance(token, str):
            return False
        return hmac.compare_digest(token, self.token)

    def _store(self, payload: dict) -> None:
        with self._lock:
            self._payload = deep_merge(self._payload, payload)
            self._cached_snapshot = None
            self._received_at = time.time()
            self._count += 1
        provider_data = payload.get("provider")
        provider = provider_data.get("name", "?") if isinstance(provider_data, dict) else "?"
        log.debug("GSI #%d от %s", self._count, provider)

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict | None:
        """Копия последнего состояния или None, если данных ещё не было."""
        with self._lock:
            if not self._payload:
                return None
            if self._cached_snapshot is None:
                self._cached_snapshot = copy.deepcopy(self._payload)
            return self._cached_snapshot

    @property
    def received_at(self) -> float | None:
        with self._lock:
            return self._received_at

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def age(self) -> float | None:
        with self._lock:
            if self._received_at is None:
                return None
            return max(0.0, time.time() - self._received_at)

    def is_fresh(self, stale_seconds: float = 45.0) -> bool:
        age = self.age()
        return age is not None and age <= stale_seconds
