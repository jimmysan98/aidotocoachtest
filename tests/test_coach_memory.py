"""Модульные тесты для проверки контекстной памяти тренера, инъекции GSI и Tool Calling.

Проверяет:
- Скользящее окно диалога (до 20 реплик, без срезания до 3).
- Динамическую инъекцию GSI-снимка и профиля игрока в системный промпт.
- Раздельные системные промпты для Астры и Джарвиса.
- Корректность исполнения зарегистрированных инструментов (Tool Calling).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from aidota.coach import Coach, make_system_prompt, COACH_TOOLS
from aidota.config import LlmConfig, PlayerProfile
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog


class TestCoachMemoryAndContext(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = HeroCatalog([
            {
                "id": 1,
                "name": "npc_dota_hero_antimage",
                "localized_name": "Anti-Mage",
                "roles": ["Carry", "Escape", "Nuker"],
            },
            {
                "id": 14,
                "name": "npc_dota_hero_pudge",
                "localized_name": "Pudge",
                "roles": ["Disabler", "Initiator", "Durable", "Nuker"],
            },
            {
                "id": 111,
                "name": "npc_dota_hero_oracle",
                "localized_name": "Oracle",
                "roles": ["Support", "Nuker", "Disabler", "Escape"],
            },
        ])
        self.api = DotaApi("data")
        self.mock_gsi = MagicMock()
        self.mock_gsi.snapshot.return_value = {
            "map": {
                "clock_time": 660,
                "game_time": 750,
                "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                "radiant_score": 12,
                "dire_score": 9,
            },
            "player": {
                "team_name": "radiant",
                "kills": 3,
                "deaths": 1,
                "assists": 5,
                "gold": 1450,
                "gpm": 520,
                "xpm": 580,
                "net_worth": 6200,
            },
            "hero": {
                "name": "npc_dota_hero_oracle",
                "level": 7,
                "alive": True,
                "items": {
                    "slot0": {"name": "item_arcane_boots"},
                    "slot1": {"name": "item_urn_of_shadows"},
                    "slot2": {"name": "item_magic_wand"},
                },
            },
        }
        self.mock_gsi.age.return_value = 1.0

        self.profile = PlayerProfile(
            account_id=12345678,
            default_position=5,
            autonomy_level="high",
            signature_heroes=["Oracle", "Crystal Maiden"],
            common_mistakes=["Позиционирование в замесе"],
        )
        self.llm_cfg = LlmConfig(
            provider="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test_key",
            model="gpt-4o-mini",
        )
        self.coach = Coach(
            self.api,
            self.catalog,
            self.mock_gsi,
            self.llm_cfg,
            player_profile=self.profile,
            coach_name="Астра",
            memory_size=20,
        )

    def test_sliding_memory_window_up_to_20_turns(self) -> None:
        """Проверка, что история диалога хранит и передает до 20 реплик (а не только 3)."""
        for i in range(15):
            self.coach.remember(f"Вопрос {i}", f"Ответ {i}")

        messages = self.coach._build_messages("Текущий вопрос", system="Тестовый системный промпт")

        # 1 системное + 15 пар (user + assistant = 30) + 1 финальный user = 32 сообщения
        self.assertEqual(len(messages), 1 + 15 * 2 + 1)
        self.assertEqual(messages[1]["content"], "Вопрос 0")
        self.assertEqual(messages[2]["content"], "Ответ 0")
        self.assertEqual(messages[-2]["content"], "Ответ 14")
        self.assertEqual(messages[-1]["content"], "Текущий вопрос")

    def test_dynamic_gsi_snapshot_injected_into_system_prompt(self) -> None:
        """В каждый системный запрос динамически инжектируется снимок GSI и профиль игрока."""
        messages = self.coach._build_messages("Что мне собрать?", system="Ты тренер Астра.")
        system_content = messages[0]["content"]

        # Проверяем наличие ключевых полей GSI в системном сообщении
        self.assertIn("GSI Live", system_content)
        self.assertIn("Oracle", system_content)
        self.assertIn("11:00", system_content)
        self.assertIn("12:9", system_content)
        self.assertIn("KDA 3/1/5", system_content)
        self.assertIn("ПРОФИЛЬ ИГРОКА", system_content)
        self.assertIn("Pos 5", system_content)

    def test_personas_differentiation(self) -> None:
        """Раздельные системные промпты для Астры (аналитик 7000+ MMR) и Джарвиса (тактический ассистент)."""
        prompt_astra = make_system_prompt("Астра")
        prompt_jarvis = make_system_prompt("Джарвис")

        self.assertIn("Астра", prompt_astra)
        self.assertIn("7000+ MMR", prompt_astra)
        self.assertNotIn("J.A.R.V.I.S.", prompt_astra)

        self.assertIn("Джарвис", prompt_jarvis)
        self.assertIn("J.A.R.V.I.S.", prompt_jarvis)
        self.assertIn("боевой тактический ИИ-ассистент", prompt_jarvis)

    def test_tool_calling_definitions(self) -> None:
        """Проверка наличия всех требуемых инструментов в схеме Tool Calling."""
        tool_names = [t["function"]["name"] for t in COACH_TOOLS]
        self.assertIn("get_live_gsi_state", tool_names)
        self.assertIn("get_opendota_meta", tool_names)
        self.assertIn("get_hero_counters", tool_names)
        self.assertIn("analyze_last_match", tool_names)
        self.assertIn("set_game_timer", tool_names)

    def test_execute_tool_live_gsi_state(self) -> None:
        """Выполнение инструмента get_live_gsi_state возвращает актуальный снимок матча."""
        res = self.coach.execute_tool("get_live_gsi_state", {})
        self.assertIn("GSI Снимок", res)
        self.assertIn("Oracle", res)
        self.assertIn("11:00", res)

    def test_execute_tool_opendota_meta(self) -> None:
        """Выполнение инструмента get_opendota_meta возвращает метаданные героя."""
        res = self.coach.execute_tool("get_opendota_meta", {"hero_name": "Anti-Mage"})
        self.assertIn("Anti-Mage", res)
        self.assertIn("Роли", res)

    def test_execute_tool_hero_counters(self) -> None:
        """Выполнение инструмента get_hero_counters возвращает контр-предметы."""
        res = self.coach.execute_tool("get_hero_counters", {"hero_name": "Pudge"})
        self.assertTrue(len(res) > 10)
        self.assertTrue("Force Staff" in res or "Linken" in res or "Pudge" in res)

    def test_execute_tool_set_game_timer(self) -> None:
        """Выполнение инструмента set_game_timer регистрирует таймер."""
        res = self.coach.execute_tool("set_game_timer", {"seconds": 30, "label": "проверить лотосы"})
        self.assertIn("30 сек", res)
        self.assertIn("проверить лотосы", res)


if __name__ == "__main__":
    unittest.main()
