"""Тесты игровых навыков (MacroTimerSkill, CooldownTrackerSkill, LaneMatchupSkill)."""

from __future__ import annotations

import unittest

from aidota.skills import CooldownTrackerSkill, LaneMatchupSkill, MacroTimerSkill


class TestMacroTimerSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.timer = MacroTimerSkill()

    def test_wisdom_runes_alert(self) -> None:
        """Руны мудрости каждые 7 минут, предупреждение за 20 секунд (6:40 = 400с)."""
        alert = self.timer.check(400)
        self.assertIsNotNone(alert)
        self.assertIn("руны мудрости", alert.lower())

        # Не должно спамить при повторном вызове в ту же минуту
        repeat = self.timer.check(405)
        self.assertIsNone(repeat)

    def test_lotus_pool_alert(self) -> None:
        """Лотосы каждые 3 минуты (напр. 2:40 = 160с)."""
        alert = self.timer.check(160)
        self.assertIsNotNone(alert)
        self.assertIn("лотос", alert.lower())

    def test_tormentor_alert(self) -> None:
        """Терзатель появляется на 20:00, предупреждение на 19:40 = 1180с."""
        alert = self.timer.check(1180)
        self.assertIsNotNone(alert)
        self.assertIn("терзател", alert.lower())

    def test_neutral_stack_alert(self) -> None:
        """Стак нейтралов на :52 секунде (напр. 1:52 = 112с)."""
        alert = self.timer.check(112)
        self.assertIsNotNone(alert)
        self.assertIn("стак", alert.lower())

    def test_pull_reminders_pos5_radiant(self) -> None:
        """Отвод малого лагеря для 5 позиции Света (напр. 1:37 = 97с)."""
        alert = self.timer.check(97, position=5, team_name="radiant")
        self.assertIsNotNone(alert)
        self.assertIn("свет", alert.lower())
        self.assertIn("пятёрка", alert.lower())
        self.assertIn("малый лагерь", alert.lower())

    def test_pull_reminders_pos4_dire(self) -> None:
        """Отвод большого лагеря для 4 позиции Тьмы (напр. 1:37 = 97с)."""
        alert = self.timer.check(97, position=4, team_name="dire")
        self.assertIsNotNone(alert)
        self.assertIn("тьма", alert.lower())
        self.assertIn("четвёрка", alert.lower())
        self.assertIn("большого лагеря", alert.lower())

    def test_mid_six_time_alert(self) -> None:
        """Предупреждение о 6 уровне вражеского мидера на 5:30 (330с)."""
        alert = self.timer.check(330)
        self.assertIsNotNone(alert)
        self.assertIn("мидер", alert.lower())
        self.assertIn("шестой", alert.lower())

    def test_mid_six_gsi_level_alert(self) -> None:
        """Предупреждение при обнаружении 6 уровня мидера в GSI."""
        alert = self.timer.check(250, enemy_mid_level=6)
        self.assertIsNotNone(alert)
        self.assertIn("мидер достиг шестого уровня", alert.lower())


class TestCooldownTrackerSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = CooldownTrackerSkill()

    def test_roshan_death_and_windows(self) -> None:
        """Фиксация смерти Рошана и расчёт окон Аегиса и респауна."""
        res = self.tracker.record_roshan(1000)  # 16:40
        self.assertIn("Рошан убит", res)
        self.assertIn("Аегис пропадёт", res)
        self.assertIn("Респаун", res)

        # Проверка предупреждения об окончании Аегиса (за 30с: 1000 + 300 - 30 = 1270)
        alert = self.tracker.check_alerts(1270)
        self.assertIsNotNone(alert)
        self.assertIn("аегис", alert.lower())

        # Проверка минимального окна респауна (1000 + 450 = 1450)
        alert_respawn = self.tracker.check_alerts(1455)
        self.assertIsNotNone(alert_respawn)
        self.assertIn("рошан", alert_respawn.lower())

    def test_enemy_ult_tracking(self) -> None:
        """Отслеживание вражеских ультимейтов (Tidehunter, Enigma, Faceless Void)."""
        res = self.tracker.record_enemy_ult("tidehunter", 600)
        self.assertIn("Равага", res)
        self.assertIn("150", res)

        # Предупреждение за 30 секунд до отката (600 + 150 - 30 = 720)
        alert = self.tracker.check_alerts(720)
        self.assertIsNotNone(alert)
        self.assertIn("откатится", alert.lower())



class TestLaneMatchupSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = LaneMatchupSkill()

    def test_lane_plan_pos_1(self) -> None:
        """План на линию для керри (Pos 1)."""
        plan = self.skill.get_lane_plan(1, hero_name="Anti-Mage")
        self.assertIn("Керри", plan)
        self.assertIn("Anti-Mage", plan)

    def test_lane_plan_with_enemy_matchup(self) -> None:
        """План с учётом опасных врагов на линии (Axe, Viper)."""
        plan = self.skill.get_lane_plan(1, hero_name="Phantom Assassin", enemy_heroes=["Axe"])
        self.assertIn("Axe", plan)
        self.assertIn("Battle Hunger", plan)


if __name__ == "__main__":
    unittest.main()
