"""Тесты строгой активации по имени активного тренера (Астра / Джарвис)."""

from __future__ import annotations

import unittest

from aidota import commands
from aidota.heroes import HeroCatalog


class TestStrictWakeWord(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = HeroCatalog([
            {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
            {"id": 29, "name": "npc_dota_hero_tidehunter", "localized_name": "Tidehunter"},
        ])

    def test_astra_strict_activation(self) -> None:
        """Если активна Астра: фразы без имени или к Джарвису игнорируются (None)."""
        # Без имени — игнор
        self.assertIsNone(commands.parse_command("что покупать", self.catalog, active_trainer="Астра"))
        self.assertIsNone(commands.parse_command("план на игру", self.catalog, active_trainer="Астра"))

        # Обращение к другому тренеру (Джарвис) — игнор
        self.assertIsNone(commands.parse_command("джарвис что покупать", self.catalog, active_trainer="Астра"))
        self.assertIsNone(commands.parse_command("жарвис разбор", self.catalog, active_trainer="Астра"))

        # Точные и фонетические совпадения имени «Астра»
        for phrase in (
            "астра что покупать",
            "астру что покупать",
            "астре что покупать",
            "остра что покупать",
            "астра, что купить на антимага",
        ):
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Астра")
            self.assertIsNotNone(intent, f"Фраза «{phrase}» должна активировать Астру")
            self.assertEqual(intent.kind, commands.ITEMS)

    def test_jarvis_strict_activation(self) -> None:
        """Если активен Джарвис: фразы без имени или к Астре игнорируются (None)."""
        # Без имени — игнор
        self.assertIsNone(commands.parse_command("что покупать", self.catalog, active_trainer="Джарвис"))
        self.assertIsNone(commands.parse_command("рошан упал", self.catalog, active_trainer="Джарвис"))

        # Обращение к Астре — игнор
        self.assertIsNone(commands.parse_command("астра что покупать", self.catalog, active_trainer="Джарвис"))
        self.assertIsNone(commands.parse_command("остра рошан упал", self.catalog, active_trainer="Джарвис"))

        # Точные и фонетические совпадения имени «Джарвис»
        for phrase in (
            "джарвис что покупать",
            "жарвис что покупать",
            "джарвес что покупать",
            "ярвис что покупать",
            "джарвис, что купить на антимага",
        ):
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Джарвис")
            self.assertIsNotNone(intent, f"Фраза «{phrase}» должна активировать Джарвиса")
            self.assertEqual(intent.kind, commands.ITEMS)

    def test_backward_compatibility_no_active_trainer(self) -> None:
        """Если active_trainer не передан (None), сохраняется поведение по умолчанию."""
        intent = commands.parse_command("что покупать", self.catalog, active_trainer=None)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.kind, commands.ITEMS)

    def test_new_skill_intents(self) -> None:
        """Проверка распознавания новых интентов скиллов."""
        # Рошан
        intent_rosh = commands.parse_command("астра рошан упал", self.catalog, active_trainer="Астра")
        self.assertIsNotNone(intent_rosh)
        self.assertEqual(intent_rosh.kind, commands.ROSHAN_DOWN)

        # Ультимейт врага
        intent_ult = commands.parse_command("джарвис ульт тайда", self.catalog, active_trainer="Джарвис")
        self.assertIsNotNone(intent_ult)
        self.assertEqual(intent_ult.kind, commands.ENEMY_ULT)
        self.assertIsNotNone(intent_ult.hero)
        self.assertEqual(intent_ult.hero.get("localized_name"), "Tidehunter")

        # План на линию
        intent_lane = commands.parse_command("астра план на линию", self.catalog, active_trainer="Астра")
        self.assertIsNotNone(intent_lane)
        self.assertEqual(intent_lane.kind, commands.LANE_PLAN)


if __name__ == "__main__":
    unittest.main()
