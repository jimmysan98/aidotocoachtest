"""MacroTimerSkill: проактивные голосовые напоминания по игровым часам из GSI.

Напоминает:
- Руны мудрости: каждые 7 минут (предупреждение за 20 секунд)
- Лотусы в пруду: каждые 3 минуты (предупреждение за 20 секунд)
- Стаки нейтралов: каждую минуту на :52–:53 секунде
- Появление Терзателя: 20:00 (предупреждение за 20 секунд на 19:40)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aidota.skills.macro_timer")


class MacroTimerSkill:
    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._alerted_wisdom: set[int] = set()
        self._alerted_lotus: set[int] = set()
        self._alerted_stacks: set[int] = set()
        self._alerted_pulls: set[int] = set()
        self._alerted_tormentor = False
        self._alerted_mid_lvl6 = False
        self._custom_timers: list[tuple[int, str]] = []

    def add_custom_timer(self, target_seconds: int, label: str) -> None:
        """Добавляет пользовательский голосовой таймер."""
        self._custom_timers.append((int(target_seconds), str(label).strip()))

    def reset(self) -> None:
        """Сбрасывает историю оповещений (например, при начале нового матча)."""
        self._alerted_wisdom.clear()
        self._alerted_lotus.clear()
        self._alerted_stacks.clear()
        self._alerted_pulls.clear()
        self._alerted_tormentor = False
        self._alerted_mid_lvl6 = False
        self._custom_timers.clear()

    def check(
        self,
        clock_seconds: Any,
        position: int = 1,
        team_name: str | None = None,
        enemy_mid_level: int | None = None,
    ) -> str | None:
        """Проверяет текущее игровое время и возвращает текст голосового напоминания."""
        if not self.enabled:
            return None
        try:
            if clock_seconds is None:
                return None
            sec = int(clock_seconds)
        except (ValueError, TypeError):
            return None

        # Игра ещё не началась (разминка / пики)
        if sec < 0:
            if self._alerted_wisdom or self._alerted_lotus or self._alerted_pulls:
                self.reset()
            return None

        # Проверка пользовательских таймеров
        if self._custom_timers:
            for idx, (target_sec, label) in enumerate(list(self._custom_timers)):
                if sec >= target_sec:
                    self._custom_timers.pop(idx)
                    return f"Напоминание: {label}!"

        # 1. Предупреждение о 6 уровне вражеского мидера
        if enemy_mid_level and enemy_mid_level >= 6 and not self._alerted_mid_lvl6:
            self._alerted_mid_lvl6 = True
            log.info("Сработал детектор: вражеский мидер достиг 6 уровня (%d)", enemy_mid_level)
            return "Внимание! Вражеский мидер достиг шестого уровня! Возможен ганг на боковых линиях."

        if 329 <= sec <= 334 and not self._alerted_mid_lvl6:
            self._alerted_mid_lvl6 = True
            log.info("Сработал таймер: приближение 6-й минуты (мидер 6 lvl + руна)")
            return "Внимание: шестая минута игры! Вражеский мидер получает шестой уровень и идёт за руной. Играйте осторожно!"

        # 2. Появление Терзателя: 20:00 (1200 сек), предупреждение за 20 сек (1180 сек)
        if 1179 <= sec <= 1182 and not self._alerted_tormentor:
            self._alerted_tormentor = True
            log.info("Сработал таймер: появление Терзателя")
            return "Через 20 секунд появится Терзатель на двадцатой минуте!"

        # 3. Руны мудрости: каждые 7 минут (420 сек), предупреждение за 20 сек
        # 6:40 (400c), 13:40 (820c), 20:40 (1240c)
        if sec >= 400:
            rem = (sec + 20) % 420
            cycle = (sec + 20) // 420
            if rem in (0, 1) and cycle not in self._alerted_wisdom:
                self._alerted_wisdom.add(cycle)
                log.info("Сработал таймер: руны мудрости (цикл %d)", cycle)
                return "Через 20 секунд руны мудрости! Не забудьте забрать."

        # 4. Лотусы в пруду: каждые 3 минуты (180 сек), предупреждение за 20 сек
        # 2:40 (160c), 5:40 (340c), 8:40 (520c)
        if sec >= 160:
            rem = (sec + 20) % 180
            cycle = (sec + 20) // 180
            if rem in (0, 1) and cycle not in self._alerted_lotus:
                self._alerted_lotus.add(cycle)
                log.info("Сработал таймер: лотосы (цикл %d)", cycle)
                return "Через 20 секунд появятся лотосы в пруду."

        # 5. Отводы крипов для 4 и 5 позиций (с учётом Свет / Тьма)
        # Стадия линии: от 1:00 (60 сек) до 8:00 (480 сек)
        if 60 <= sec <= 480 and position in (4, 5):
            second_in_min = sec % 60
            minute_idx = sec // 60
            if second_in_min in (36, 37, 38) and minute_idx not in self._alerted_pulls:
                self._alerted_pulls.add(minute_idx)
                team_clean = (team_name or "").strip().lower()
                team_label = "Свет" if "radiant" in team_clean else ("Тьма" if "dire" in team_clean else "")

                if position == 5:
                    prefix = f"{team_label}, пятёрка" if team_label else "Пятёрка"
                    log.info("Сработал таймер: отвод малого лагеря для поз 5 (%s)", prefix)
                    return f"{prefix}: 38-я секунда — готовься к отводу на малый лагерь через 10 секунд!"
                elif position == 4:
                    prefix = f"{team_label}, четвёрка" if team_label else "Четвёрка"
                    log.info("Сработал таймер: отвод большого лагеря для поз 4 (%s)", prefix)
                    return f"{prefix}: 38-я секунда — готовься к отводу большого лагеря через 10 секунд!"

        # 6. Стаки нейтралов: на :52-:53 секунде каждой минуты (до 25-й минуты)
        if 50 <= sec <= 1500:
            second_in_min = sec % 60
            minute_idx = sec // 60
            if second_in_min in (52, 53) and minute_idx not in self._alerted_stacks:
                self._alerted_stacks.add(minute_idx)
                log.info("Сработал таймер: стаки нейтралов (минута %d)", minute_idx)
                return "53-я секунда — время стакать нейтралов."

        return None
