"""Телеметрия маршрутизации LLM, кэша префикса и задержек (metrics.jsonl)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .security import redact_secrets


@dataclass(frozen=True, slots=True)
class TurnMetric:
    """Структурированная запись об одном обращении к маршрутизатору/LLM."""

    ts: float
    turn_id: str
    tier: str
    route: str
    provider: str
    model: str
    kind: str
    ttft_ms: int
    total_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    outcome: str = "ok"
    generation_id: int = 0
    error: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("error"):
            data["error"] = redact_secrets(str(data["error"]))[:300]
        return data


def new_turn_id() -> str:
    """Генерирует короткий идентификатор корреляции запроса."""
    return uuid.uuid4().hex[:12]


class TelemetrySink:
    """Потокобезопасный буфер метрик и запись в metrics.jsonl без SQLite."""

    def __init__(
        self,
        log_dir: Path | None = None,
        *,
        max_bytes: int = 2_000_000,
        ring_size: int = 256,
    ) -> None:
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self._max_bytes = max(50_000, int(max_bytes))
        self._lock = threading.Lock()
        self._turns: deque[TurnMetric] = deque(maxlen=max(16, int(ring_size)))
        self._events: deque[dict[str, Any]] = deque(maxlen=max(32, int(ring_size)))

    @property
    def metrics_path(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / "metrics.jsonl"

    def set_log_dir(self, log_dir: Path | None) -> None:
        with self._lock:
            self._log_dir = Path(log_dir) if log_dir is not None else None

    def record_turn(self, metric: TurnMetric) -> None:
        payload = {"type": "turn", **metric.to_safe_dict()}
        with self._lock:
            self._turns.append(metric)
            self._write_line(payload)

    def record_event(self, event: str, **fields: Any) -> None:
        safe_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, str):
                safe_fields[key] = redact_secrets(value)[:300]
            else:
                safe_fields[key] = value
        payload: dict[str, Any] = {
            "type": "event",
            "ts": round(time.time(), 3),
            "event": event,
            **safe_fields,
        }
        with self._lock:
            self._events.append(payload)
            self._write_line(payload)

    def _write_line(self, payload: dict[str, Any]) -> None:
        path = self.metrics_path
        if path is None:
            return
        line = redact_secrets(json.dumps(payload, ensure_ascii=False)) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size >= self._max_bytes:
                backup = path.with_name(path.name + ".1")
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            # Каталог может быть доступен только для чтения; продолжаем работать в памяти.
            return

    def summary(self) -> dict[str, Any]:
        """Возвращает агрегированную сводку последних обращений для диагностики."""
        with self._lock:
            turns = list(self._turns)
            events = list(self._events)

        by_tier: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        ttfts: list[int] = []
        cache_hits = 0
        prompt_total = 0
        errors: list[dict[str, Any]] = []
        for t in turns:
            by_tier[t.tier] = by_tier.get(t.tier, 0) + 1
            by_outcome[t.outcome] = by_outcome.get(t.outcome, 0) + 1
            if t.ttft_ms > 0:
                ttfts.append(t.ttft_ms)
            cache_hits += max(0, t.cache_hit_tokens)
            prompt_total += max(0, t.prompt_tokens)
            if t.outcome != "ok" and t.error:
                errors.append({"ts": t.ts, "tier": t.tier, "outcome": t.outcome, "error": t.error})

        ttfts_sorted = sorted(ttfts)
        p95_ttft = ttfts_sorted[min(len(ttfts_sorted) - 1, int(len(ttfts_sorted) * 0.95))] if ttfts_sorted else 0
        avg_ttft = int(sum(ttfts) / len(ttfts)) if ttfts else 0
        hit_ratio = round(cache_hits / prompt_total, 3) if prompt_total > 0 else 0.0

        return {
            "total_turns": len(turns),
            "by_tier": by_tier,
            "by_outcome": by_outcome,
            "avg_ttft_ms": avg_ttft,
            "p95_ttft_ms": p95_ttft,
            "cache_hit_tokens": cache_hits,
            "prompt_tokens": prompt_total,
            "cache_hit_ratio": hit_ratio,
            "recent_errors": errors[-5:],
            "recent_events_count": len(events),
        }

    def recent_turns(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._turns)[-max(1, limit) :]
        return [t.to_safe_dict() for t in items]
