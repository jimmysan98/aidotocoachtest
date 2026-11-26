"""MacroTimerSkill: проактивные голосовые напоминания по игровым часам из GSI.

Тонкая обёртка над каноническим единым источником истины ``aidota.map_timers``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..map_timers import CanonicalMapTimers


class MacroTimerSkill:
    def __init__(
        self,
        enabled: bool = True,
        *,
        core: CanonicalMapTimers | None = None,
        proactive_enabled: bool = True,
    ) -> None:
        self._core = core if core is not None else CanonicalMapTimers(
            enabled=bool(enabled),
            proactive_enabled=bool(proactive_enabled),
        )
        if core is None:
            self._core.enabled = bool(enabled)
            self._core.proactive_enabled = bool(proactive_enabled)

    @property
    def enabled(self) -> bool:
        return self._core.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._core.enabled = bool(value)

    @property
    def proactive_enabled(self) -> bool:
        return self._core.proactive_enabled

    @proactive_enabled.setter
    def proactive_enabled(self, value: bool) -> None:
        self._core.proactive_enabled = bool(value)

    @property
    def core(self) -> CanonicalMapTimers:
        return self._core

    def sync_match_id(self, match_id: Any) -> bool:
        """Синхронизирует ``match_id`` и сбрасывает таймеры при начале нового матча."""
        return self._core.sync_match_id(match_id)

    def add_custom_timer(self, target_seconds: int, label: str) -> None:
        """Добавляет пользовательский голосовой таймер."""
        self._core.add_custom_timer(target_seconds, label)

    def reset(self) -> None:
        """Сбрасывает историю оповещений (например, при начале нового матча)."""
        self._core.reset()

    def check(
        self,
        clock_seconds: Any,
        position: int = 1,
        team_name: str | None = None,
        enemy_mid_level: int | None = None,
        *,
        payload: Mapping[str, Any] | None = None,
        turbo: bool = False,
        match_id: Any = None,
    ) -> str | None:
        """Проверяет текущее игровое время через канонический движок ``CanonicalMapTimers``."""
        return self._core.check_all(
            clock_seconds,
            position=position,
            team_name=team_name,
            enemy_mid_level=enemy_mid_level,
            payload=payload,
            turbo=turbo,
            match_id=match_id,
        )
