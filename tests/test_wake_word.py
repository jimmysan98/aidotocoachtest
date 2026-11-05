"""Модульные тесты для проверки строгого фильтра имени тренера (Астра / Джарвис).

Проверяет:
- Изолированность тренеров (Астра игнорирует Джарвиса, Джарвис игнорирует Астру).
- Отсечение ложных срабатываний и фраз без упоминания активного тренера.
- Все склонения и формы имён («Астра», «Астру», «Астре», «Астрой», «Остра», «Астрочка», «Astra»,
  «Джарвис», «Джарвиса», «Джарвису», «Жарвис», «Ярвис», «Jarvis»).
- Маршрутизацию произвольных вопросов к ИИ-тренеру (Intent.CHAT).
"""

from __future__ import annotations

import unittest

from aidota import commands
from aidota.heroes import HeroCatalog


class TestWakeWordStrictFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = HeroCatalog([
            {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
            {"id": 14, "name": "npc_dota_hero_pudge", "localized_name": "Pudge"},
            {"id": 35, "name": "npc_dota_hero_sniper", "localized_name": "Sniper"},
            {"id": 111, "name": "npc_dota_hero_oracle", "localized_name": "Oracle"},
        ])

    def test_astra_ignores_unaddressed_speech(self) -> None:
        """Если активна Астра, речь без обращения игнорируется (None)."""
        unaddressed_phrases = [
            "что купить дальше",
            "какой скилл качать",
            "почему они пушат",
            "я иду в мид",
            "рошан упал",
        ]
        for phrase in unaddressed_phrases:
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Астра")
            self.assertIsNone(intent, f"Фраза без имени «{phrase}» не должна активировать Астру")

    def test_astra_ignores_jarvis(self) -> None:
        """Если активна Астра, любые обращения к Джарвису строго игнорируются."""
        jarvis_phrases = [
            "джарвис что купить",
            "джарвис, какой скилл качать",
            "жарвис помоги",
            "джарвису привет",
            "джарвиса спроси",
            "jarvis what to buy",
        ]
        for phrase in jarvis_phrases:
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Астра")
            self.assertIsNone(intent, f"Астра не должна реагировать на обращение к Джарвису: «{phrase}»")

    def test_jarvis_ignores_astra(self) -> None:
        """Если активен Джарвис, любые обращения к Астре строго игнорируются."""
        astra_phrases = [
            "астра что купить",
            "астру попроси",
            "астре скажи",
            "астрой горжусь",
            "остра помоги",
            "астрочка привет",
            "astra status",
        ]
        for phrase in astra_phrases:
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Джарвис")
            self.assertIsNone(intent, f"Джарвис не должен реагировать на обращение к Астре: «{phrase}»")

    def test_astra_declensions_and_forms(self) -> None:
        """Поддержка всех форм и падежей имени Астра."""
        test_cases = [
            ("астра что купить на пуджа", commands.ITEMS),
            ("астру спроси что купить на пуджа", commands.ITEMS),
            ("астре скажи что купить на пуджа", commands.ITEMS),
            ("астрой что купить на пуджа", commands.ITEMS),
            ("остра что купить на пуджа", commands.ITEMS),
            ("астрочка что купить на пуджа", commands.ITEMS),
            ("Astra что купить на пуджа", commands.ITEMS),
        ]
        for phrase, expected_kind in test_cases:
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Астра")
            self.assertIsNotNone(intent, f"Астра должна активироваться от фразы «{phrase}»")
            self.assertEqual(intent.kind, expected_kind)

    def test_jarvis_declensions_and_forms(self) -> None:
        """Поддержка всех форм и падежей имени Джарвис."""
        test_cases = [
            ("джарвис что купить на снайпера", commands.ITEMS),
            ("джарвиса спроси что купить на снайпера", commands.ITEMS),
            ("джарвису скажи что купить на снайпера", commands.ITEMS),
            ("жарвис что купить на снайпера", commands.ITEMS),
            ("ярвис что купить на снайпера", commands.ITEMS),
            ("Jarvis что купить на снайпера", commands.ITEMS),
        ]
        for phrase, expected_kind in test_cases:
            intent = commands.parse_command(phrase, self.catalog, active_trainer="Джарвис")
            self.assertIsNotNone(intent, f"Джарвис должен активироваться от фразы «{phrase}»")
            self.assertEqual(intent.kind, expected_kind)

    def test_arbitrary_questions_routed_to_chat(self) -> None:
        """Свободные вопросы с именем тренера не отбрасываются, а направляются в Intent.CHAT."""
        arbitrary_queries = [
            "астра почему мидер не гангает",
            "астра что ты думаешь про рапиру на саппорта",
            "астра как мне переиграть вражеский смок",
            "джарвис рассчитай шанс победы в драке",
            "джарвис как ты оцениваешь шансы",
        ]
        for phrase in arbitrary_queries:
            active = "Астра" if "астра" in phrase else "Джарвис"
            intent = commands.parse_command(phrase, self.catalog, active_trainer=active)
            self.assertIsNotNone(intent, f"Фраза «{phrase}» должна быть распознана")
            self.assertEqual(intent.kind, commands.CHAT, f"Фраза «{phrase}» должна быть Intent.CHAT")
            self.assertTrue(bool(intent.arg), "Аргумент запроса должен содержать очищенный вопрос")


if __name__ == "__main__":
    unittest.main()
