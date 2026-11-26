"""Долговременная память ИИ-тренера AIDOTA.

Хранит между запусками:
- последние реплики диалога (чтобы тренер не повторялся и держал контекст);
- уроки из разобранных матчей;
- небольшие факты о игроке (предпочтения, герои, типичные ошибки).

Файл — обычный JSON в ``data/coach_memory.json``. Запись атомарная
(tmp-файл + ``os.replace``), ошибки ввода-вывода не должны ронять приложение.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .paths import atomic_write_json, read_json_safe

log = logging.getLogger("aidota.coach_memory")

_MAX_TURNS = 30
_MAX_LESSONS = 12
_MAX_Q = 240
_MAX_A = 700
_MAX_FACT_VALUE = 200


_UNTRUSTED_MEMORY_MARKERS: tuple[str, ...] = (
    "skip",
    "(поправка:",
    "не удалось",
    "ошибка llm",
    "ошибка api",
    "http 401",
    "http 402",
    "http 403",
    "http 429",
    "превышен лимит",
    "отменён",
    "бесплатный ии",
    "pollinations",
    "temporarily unavailable",
)


def _is_trustworthy_memory_text(text: str) -> bool:
    """Фильтр против снежного кома галлюцинаций и ошибок в CoachMemory."""
    clean = " ".join(str(text or "").split()).strip()
    if not clean or len(clean) < 4:
        return False
    low = clean.lower()
    if low == "skip":
        return False
    if any(marker in low for marker in _UNTRUSTED_MEMORY_MARKERS):
        return False
    return True


class CoachMemory:
    """Потокобезопасная персистентная память тренера.

    ``path=None`` отключает дисковое хранение: объект работает как чистая
    оперативная память (полезно в тестах и при отсутствии data-каталога).
    """

    def __init__(self, path: Path | str | None = None, *, max_turns: int = _MAX_TURNS):
        self.path: Path | None = Path(path) if path else None
        self.max_turns = max(1, int(max_turns))
        self._lock = threading.RLock()
        self._turns: list[dict[str, Any]] = []
        self._lessons: list[str] = []
        self._facts: dict[str, str] = {}
        self._loaded = False
        if self.path is not None:
            self._load()

    # ------------------------------------------------------------------ #
    # Загрузка / сохранение
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self._loaded = True
            data = read_json_safe(self.path)
            if not isinstance(data, dict):
                self._turns, self._lessons, self._facts = [], [], {}
                return
            turns = data.get("turns")
            if isinstance(turns, list):
                self._turns = [
                    t for t in turns
                    if isinstance(t, dict) and isinstance(t.get("q"), str) and isinstance(t.get("a"), str)
                ][-self.max_turns:]
            lessons = data.get("lessons")
            if isinstance(lessons, list):
                self._lessons = [str(x).strip() for x in lessons if str(x).strip()][-_MAX_LESSONS:]
            facts = data.get("facts")
            if isinstance(facts, dict):
                self._facts = {
                    str(k): str(v) for k, v in facts.items()
                    if k and v is not None and str(v).strip()
                }

    def _save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            data = {
                "version": 1,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "turns": self._turns[-self.max_turns:],
                "lessons": self._lessons[-_MAX_LESSONS:],
                "facts": self._facts,
            }
            try:
                atomic_write_json(self.path, data, indent=1)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось сохранить память тренера %s: %s", self.path, exc)

    # ------------------------------------------------------------------ #
    # Диалог
    # ------------------------------------------------------------------ #
    def remember(
        self,
        question: str,
        answer: str,
        *,
        validated: bool = True,
        source: str = "dialog",
    ) -> None:
        q = " ".join(str(question or "").split())[:_MAX_Q]
        a = " ".join(str(answer or "").split())[:_MAX_A]
        if not q or not a or not validated or not _is_trustworthy_memory_text(a):
            return
        with self._lock:
            self._turns.append(
                {
                    "q": q,
                    "a": a,
                    "ts": int(time.time()),
                    "validated": True,
                    "source": str(source or "dialog")[:32],
                }
            )
            if len(self._turns) > self.max_turns:
                self._turns = self._turns[-self.max_turns:]
        self._save()

    def recall(self, limit: int = 5) -> str:
        """Компактная выжимка последних проверенных реплик для промпта."""
        limit = max(1, int(limit))
        with self._lock:
            valid_turns = [
                t for t in self._turns
                if isinstance(t, dict)
                and bool(t.get("validated", True))
                and _is_trustworthy_memory_text(str(t.get("a") or ""))
            ]
            turns = valid_turns[-limit:]
        if not turns:
            return ""
        lines = []
        for turn in turns:
            lines.append(f"Игрок: {turn['q']}")
            lines.append(f"Тренер: {turn['a']}")
        return "\n".join(lines)

    def turns(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._turns]

    # ------------------------------------------------------------------ #
    # Уроки из матчей
    # ------------------------------------------------------------------ #
    def remember_lesson(self, text: str, match_id: Any = None) -> None:
        clean = " ".join(str(text or "").split())
        if not clean or not _is_trustworthy_memory_text(clean):
            return
        if match_id:
            clean = f"Матч #{match_id}: {clean}"
        clean = clean[:400]
        with self._lock:
            if clean not in self._lessons:
                self._lessons.append(clean)
                self._lessons = self._lessons[-_MAX_LESSONS:]
        self._save()

    def lessons(self, limit: int = 5) -> list[str]:
        with self._lock:
            return list(self._lessons)[-max(1, int(limit)):]

    # ------------------------------------------------------------------ #
    # Факты и решения игрока
    # ------------------------------------------------------------------ #
    def remember_fact(self, key: str, value: Any) -> None:
        k = str(key or "").strip().lower()[:60]
        v = " ".join(str(value or "").split())[:_MAX_FACT_VALUE]
        if not k or not v or not _is_trustworthy_memory_text(v):
            return
        with self._lock:
            if self._facts.get(k) == v:
                return
            self._facts[k] = v
        self._save()

    def remember_verified_fact(self, key: str, value: Any) -> None:
        """Алиас явной записи проверенного игрового факта."""
        self.remember_fact(key, value)

    def remember_decision(self, decision_key: str, decision_text: str) -> None:
        """Сохраняет принятое игроком решение (предмет, роль, план), отфильтровывая шум."""
        self.remember_fact(f"решение_{decision_key}", decision_text)

    def facts(self) -> dict[str, str]:
        with self._lock:
            return dict(self._facts)

    # ------------------------------------------------------------------ #
    # Сводка для системного промпта
    # ------------------------------------------------------------------ #
    def summary(self, *, lessons_limit: int = 3, turns_limit: int = 2) -> str:
        parts: list[str] = []
        lessons = self.lessons(lessons_limit)
        if lessons:
            parts.append("Уроки прошлых матчей: " + "; ".join(lessons))
        facts = self.facts()
        if facts:
            pairs = ", ".join(f"{k}: {v}" for k, v in list(facts.items())[:6])
            parts.append("Память об игроке: " + pairs)
        recent = self.recall(turns_limit)
        if recent:
            parts.append("Последние реплики:\n" + recent)
        return "\n".join(parts)

    def clear(self) -> None:
        with self._lock:
            self._turns, self._lessons, self._facts = [], [], {}
        self._save()
