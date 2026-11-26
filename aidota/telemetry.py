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


@dataclass(frozen=True, slots=True)
class ReplicaTrace:
    """Полный диагностический след одной реплики (Этап 1) в JSONL."""

    ts: float
    trace_id: str
    match_id: str | None = None
    game_time_trigger: int | float | None = None
    game_time_spoken: int | float | None = None
    latency_total_ms: int = 0
    trigger_type: str = "voice"
    stt_raw: str | None = None
    stt_normalized: str | None = None
    model_used: str | None = None
    route_tier: str | None = None
    prompt_chars: int = 0
    prompt_tokens_est: int = 0
    ms_stt: int = 0
    ms_llm_ttft: int = 0
    ms_llm_total: int = 0
    ms_validator: int = 0
    ms_tts: int = 0
    llm_raw: str = ""
    after_validator: str = ""
    drop_reason: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for field_name, limit in (
            ("stt_raw", 300),
            ("stt_normalized", 300),
            ("llm_raw", 600),
            ("after_validator", 600),
            ("drop_reason", 120),
        ):
            val = data.get(field_name)
            if isinstance(val, str) and val:
                data[field_name] = redact_secrets(val)[:limit]
        return data


def new_turn_id() -> str:
    """Генерирует короткий идентификатор корреляции запроса."""
    return uuid.uuid4().hex[:12]


class TelemetrySink:
    """Потокобезопасный буфер метрик и запись в metrics.jsonl / coach_traces.jsonl без SQLite."""

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
        self._traces: deque[ReplicaTrace] = deque(maxlen=max(32, int(ring_size)))

    @property
    def metrics_path(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / "metrics.jsonl"

    @property
    def traces_path(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / "coach_traces.jsonl"

    def set_log_dir(self, log_dir: Path | None) -> None:
        with self._lock:
            self._log_dir = Path(log_dir) if log_dir is not None else None

    def record_turn(self, metric: TurnMetric) -> None:
        payload = {"type": "turn", **metric.to_safe_dict()}
        with self._lock:
            self._turns.append(metric)
            self._write_line(payload, path=self.metrics_path)

    def record_trace(self, trace: ReplicaTrace) -> None:
        """Записывает диагностический след одной реплики в coach_traces.jsonl (с ротацией)."""
        payload = {"type": "trace", **trace.to_safe_dict()}
        with self._lock:
            self._traces.append(trace)
            self._write_line(payload, path=self.traces_path)

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
            self._write_line(payload, path=self.metrics_path)

    def _write_line(self, payload: dict[str, Any], *, path: Path | None = None) -> None:
        target = self.metrics_path if path is None else path
        if target is None:
            return
        line = redact_secrets(json.dumps(payload, ensure_ascii=False)) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size >= self._max_bytes:
                backup = target.with_name(target.name + ".1")
                if backup.exists():
                    backup.unlink()
                target.replace(backup)
            with target.open("a", encoding="utf-8") as fh:
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

    def recent_traces(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._traces)[-max(1, limit) :]
        return [t.to_safe_dict() for t in items]

    def trace_summary(self) -> dict[str, Any]:
        """Агрегированная сводка диагностических следов реплик (Этап 1)."""
        with self._lock:
            traces = list(self._traces)
        by_trigger: dict[str, int] = {}
        by_drop: dict[str, int] = {}
        total_latencies: list[int] = []
        ttfts: list[int] = []
        for tr in traces:
            by_trigger[tr.trigger_type] = by_trigger.get(tr.trigger_type, 0) + 1
            if tr.drop_reason:
                by_drop[tr.drop_reason] = by_drop.get(tr.drop_reason, 0) + 1
            if tr.latency_total_ms > 0:
                total_latencies.append(tr.latency_total_ms)
            if tr.ms_llm_ttft > 0:
                ttfts.append(tr.ms_llm_ttft)
        return {
            "total_traces": len(traces),
            "by_trigger": by_trigger,
            "by_drop_reason": by_drop,
            "avg_latency_ms": int(sum(total_latencies) / len(total_latencies)) if total_latencies else 0,
            "avg_ttft_ms": int(sum(ttfts) / len(ttfts)) if ttfts else 0,
        }

    def estimate_tokens(self, text: str) -> int:
        clean = str(text or "")
        if not clean:
            return 0
        return max(1, (len(clean) + 2) // 3)
