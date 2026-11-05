"""Модульные тесты для проверки таймеров мидера, макро-оповещений и автономного агента.

Проверяет:
- Тайминги водных рун на миду (1:40 / 100с и 3:40 / 220с).
- Тайминги активных рун (5:40 / 340с с напоминанием про ночную катапульту, 7:40 / 460с).
- Детекцию 6-го уровня вражеского мидера (явную по GSI и предиктивную по таймингу/опыту).
- Оповещения по рунам мудрости, лотосам, стакам (:53) и Терзателю (19:40).
- Автономный агент (Power Spikes и анти-тилт).
"""

from __future__ import annotations

import unittest

from aidota.context import (
    AutonomousCoachAgent,
    MidTimersTracker,
)
from aidota.heroes import HeroCatalog


class TestMidTimersAndMacroAlerts(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = MidTimersTracker(enabled=True)
        self.catalog = HeroCatalog([
            {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
            {"id": 74, "name": "npc_dota_hero_invoker", "localized_name": "Invoker"},
            {"id": 11, "name": "npc_dota_hero_shadow_fiend", "localized_name": "Shadow Fiend"},
        ])

    def test_water_runes_140_and_340(self) -> None:
        """Водные руны на миде срабатывают за 20 секунд (на 1:40 = 100с и 3:40 = 220с)."""
        # До тайминга — ничего
        self.assertIsNone(self.tracker.check_mid_runes(95))

        # 1:40 (100 сек)
        alert_140 = self.tracker.check_mid_runes(100)
        self.assertIsNotNone(alert_140)
        self.assertIn("водные руны", alert_140.lower())
        self.assertIn("мид", alert_140.lower())

        # Защита от спама (повторный опрос в ту же секунду)
        self.assertIsNone(self.tracker.check_mid_runes(101))

        # 3:40 (220 сек)
        alert_340 = self.tracker.check_mid_runes(220)
        self.assertIsNotNone(alert_340)
        self.assertIn("водные руны", alert_340.lower())

    def test_active_runes_540_with_catapult(self) -> None:
        """Активная руна на 5:40 (340с) содержит предупреждение о ночной катапульте."""
        # 5:40 (340 сек)
        alert_540 = self.tracker.check_mid_runes(340)
        self.assertIsNotNone(alert_540)
        self.assertIn("активная руна", alert_540.lower())
        self.assertIn("катапульта", alert_540.lower())

        # 7:40 (460 сек)
        alert_740 = self.tracker.check_mid_runes(460)
        self.assertIsNotNone(alert_740)
        self.assertIn("активная руна", alert_740.lower())

    def test_enemy_mid_level_6_explicit(self) -> None:
        """Явная детекция 6 уровня вражеского мидера через данные GSI."""
        payload = {
            "player": {"team_name": "radiant"},
            "allplayers": {
                "player0": {
                    "team_name": "dire",
                    "hero_name": "npc_dota_hero_invoker",
                    "level": 6,
                }
            },
        }
        alert = self.tracker.check_mid_level_6(payload, clock_sec=310)
        self.assertIsNotNone(alert)
        self.assertIn("шестого уровня", alert.lower())
        self.assertIn("ганг", alert.lower())

    def test_enemy_mid_level_6_predictive(self) -> None:
        """Предиктивное предупреждение о приближении 6 уровня мидера по таймингу."""
        tracker = MidTimersTracker(enabled=True)
        # Окно приближения 5:15 (315 сек)
        alert = tracker.check_mid_level_6({}, clock_sec=320)
        self.assertIsNotNone(alert)
        self.assertIn("шестой", alert.lower())

    def test_macro_timers_wisdom_and_tormentor(self) -> None:
        """Макро-таймеры: руны мудрости на 6:40 (400с) и Терзатель на 19:40 (1180с)."""
        # Руны мудрости: 6:40 (400 сек)
        wisdom = self.tracker.check_macro_timers(400, position=5)
        self.assertIsNotNone(wisdom)
        self.assertIn("мудрости", wisdom.lower())
        self.assertIn("саппорт", wisdom.lower())

        # Терзатель: 19:40 (1180 сек)
        tormentor = self.tracker.check_macro_timers(1180, position=1)
        self.assertIsNotNone(tormentor)
        self.assertIn("терзатель", tormentor.lower())

    def test_support_pulls_pos5_and_pos4(self) -> None:
        """Отводы крипов для саппортов 4 и 5 во время стадии лайнинга."""
        # Позиция 5 (легкая линия, малый кемп):
        tracker5 = MidTimersTracker(enabled=True)
        pull17_pos5 = tracker5.check_macro_timers(73, position=5)  # 1:13
        self.assertIsNotNone(pull17_pos5)
        self.assertIn("отвод", pull17_pos5.lower())
        self.assertIn("малый", pull17_pos5.lower())

        pull47_pos5 = tracker5.check_macro_timers(103, position=5)  # 1:43
        self.assertIsNotNone(pull47_pos5)
        self.assertIn("отвод", pull47_pos5.lower())
        self.assertIn("47", pull47_pos5)

        # Позиция 4 (сложная линия, большой кемп):
        tracker4 = MidTimersTracker(enabled=True)
        pull17_pos4 = tracker4.check_macro_timers(73, position=4)  # 1:13
        self.assertIsNotNone(pull17_pos4)
        self.assertIn("большого", pull17_pos4.lower())

        # Позиция 1 (керри) — не получает уведомлений про отводы, чтобы не отвлекаться
        tracker1 = MidTimersTracker(enabled=True)
        self.assertIsNone(tracker1.check_macro_timers(73, position=1))

    def test_support_stacks_pos4_and_pos5(self) -> None:
        """Стаки нейтралов для саппортов на 48-53 секундах каждой минуты."""
        tracker = MidTimersTracker(enabled=True)
        # На 50 секунде (1:50 = 110с) для саппорта 5 позиции
        stack_alert = tracker.check_macro_timers(110, position=5)
        self.assertIsNotNone(stack_alert)
        self.assertIn("стакать нейтралов", stack_alert.lower())
        self.assertIn("53–55", stack_alert)

        # Защита от спама в ту же минуту
        self.assertIsNone(tracker.check_macro_timers(111, position=5))

        # Следующая минута (2:51 = 171с) для саппорта 4 позиции
        stack_next = tracker.check_macro_timers(171, position=4)
        self.assertIsNotNone(stack_next)
        self.assertIn("стакать", stack_next.lower())

    def test_support_runes_assistance(self) -> None:
        """Саппорт 4 позиции получает подсказки о контроле рун на реке."""
        tracker = MidTimersTracker(enabled=True)
        # Водные руны (1:40 = 100с)
        water_pos4 = tracker.check_mid_runes(100, position=4)
        self.assertIsNotNone(water_pos4)
        self.assertIn("четвёрка", water_pos4.lower())
        self.assertIn("помоги мидеру", water_pos4.lower())

        # Активная руна (5:40 = 340с)
        active_pos4 = tracker.check_mid_runes(340, position=4)
        self.assertIsNotNone(active_pos4)
        self.assertIn("саппорт", active_pos4.lower())
        self.assertIn("катапульта", active_pos4.lower())

    def test_neutral_items_and_day_night(self) -> None:
        """Проверка таймингов нейтральных предметов и смены дня/ночи."""
        tracker = MidTimersTracker(enabled=True)

        # 7:00 (420с) — Тир 1 нейтралок
        t1 = tracker.check_macro_timers(420, position=1)
        self.assertIsNotNone(t1)
        self.assertIn("нейтральные предметы 1-го разряда", t1.lower())

        # 17:00 (1020с) — Тир 2 нейтралок
        t2 = tracker.check_macro_timers(1020, position=1)
        self.assertIsNotNone(t2)
        self.assertIn("2-й разряд", t2.lower())

        # Ночь (5:00 = 300с)
        night = tracker.check_macro_timers(300, position=1)
        self.assertIsNotNone(night)
        self.assertIn("наступила ночь", night.lower())

    def test_autonomous_agent_power_spikes(self) -> None:
        """Автономный агент детектирует покупку ключевого предмета (Power Spike)."""
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        payload1 = {
            "map": {"clock_time": 800},
            "hero": {"items": {"slot0": {"name": "item_boots"}}},
        }
        agent.process_tick(payload1, position=1)

        # Игрок купил Blink Dagger
        payload2 = {
            "map": {"clock_time": 805},
            "hero": {
                "items": {
                    "slot0": {"name": "item_boots"},
                    "slot1": {"name": "item_blink"},
                }
            },
        }
        advice = agent.process_tick(payload2, position=1)
        self.assertIsNotNone(advice)
        self.assertIn("Blink Dagger", advice)
        self.assertTrue("power spike" in advice.lower() or "пик силы" in advice.lower())

    def test_autonomous_agent_anti_tilt(self) -> None:
        """Автономный агент выдаёт поддержку и совет при серии смертей (анти-тилт)."""
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)
        payload1 = {
            "map": {"clock_time": 900},
            "player": {"deaths": 1},
        }
        agent.process_tick(payload1, position=1)

        # 3 смерти подряд
        payload2 = {
            "map": {"clock_time": 950},
            "player": {"deaths": 3},
        }
        advice = agent.process_tick(payload2, position=1)
        self.assertIsNotNone(advice)
        self.assertTrue("позицию" in advice.lower() or "не тильтуй" in advice.lower() or "смертей" in advice.lower())


if __name__ == "__main__":
    unittest.main()
