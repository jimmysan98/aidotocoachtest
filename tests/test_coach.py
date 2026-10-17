"""Тесты тренера: промпты, запасные ответы, выбор модели, память."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aidota.coach import Coach
from aidota.config import LlmConfig, OllamaConfig, PlayerProfile
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from tests.helpers import (
    FAKE_HEROES,
    FakeSession,
    gsi_payload,
    make_temp_dir,
    remove_temp_dir,
)

ITEMS = {"blink": {"dname": "Blink Dagger"}, "tango": {"dname": "Tango"},
         "branches": {"dname": "Iron Branch"}, "boots": {"dname": "Boots of Speed"}}
POPULARITY = {
    "start_game_items": {"tango": 100, "branches": 80},
    "early_game_items": {"boots": 50},
    "mid_game_items": {},
    "late_game_items": {"blink": 10},
}


class FakeGsi:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 1.5


class CoachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        data = self.tmp / "data"
        data.mkdir()
        (data / "heroes.json").write_text(json.dumps(FAKE_HEROES, ensure_ascii=False),
                                          encoding="utf-8")
        (data / "items.json").write_text(json.dumps({"blink": "Blink Dagger", "tango": "Tango"}),
                                         encoding="utf-8")
        self.routes = {
            "/heroStats": FAKE_HEROES,
            "/constants/items": ITEMS,
            "/itemPopularity": POPULARITY,
            "/api/tags": {"models": [{"name": "gemma3:latest"}, {"name": "llama3:8b"}]},
            "/api/chat": {"message": {"content": "Покупай Blink Dagger и дави линии."}},
        }
        self.session = FakeSession(self.routes)
        self.api = DotaApi(data, session=self.session)
        self.api.load_snapshots()
        self.config = LlmConfig(model="gemma3")

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def make_coach(self, payload=None, *, profile=None):
        payload = gsi_payload() if payload is None else payload
        config = LlmConfig(**self.config.__dict__)
        profile = profile if profile is not None else PlayerProfile(hero_pool={"1": ["Anti-Mage", "Pudge"]})
        return Coach(self.api, HeroCatalog(FAKE_HEROES), FakeGsi(payload),
                     config, profile, session=self.session)

    def test_resolve_configured_model(self):
        coach = self.make_coach()
        self.assertEqual(coach.resolve_model(), "gemma3:latest")

    def test_resolve_fallback_model(self):
        coach = self.make_coach()
        coach.llm.model = "nonexistent"
        self.assertEqual(coach.resolve_model(), "gemma3:latest")
        self.assertIn("не установлена", coach.last_error or "")

    def test_coerces_legacy_ollama_config(self):
        coach = Coach(self.api, HeroCatalog(FAKE_HEROES), FakeGsi(gsi_payload()),
                      OllamaConfig(model="gemma3"), PlayerProfile(), session=self.session)
        self.assertEqual(coach.provider, "ollama")
        self.assertEqual(coach.llm.model, "gemma3")

    def test_advice_uses_llm(self):
        coach = self.make_coach()
        hero = self.api.hero_by_id(1)
        answer = coach.advice(hero)
        self.assertIn("Blink Dagger", answer)
        self.assertEqual(len(coach._memory), 1)

    def test_advice_fallback_without_ollama(self):
        self.session.routes["/api/tags"] = RuntimeError("нет сервера")
        coach = self.make_coach()
        hero = self.api.hero_by_id(1)
        answer = coach.advice(hero)
        self.assertIn("LLM недоступна", answer)
        self.assertIn("Tango", answer)  # предметы из OpenDota всё равно попали
        self.assertIn("Anti-Mage", answer)

    def test_advice_chat_error_falls_back(self):
        self.session.routes["/api/chat"] = RuntimeError("timeout")
        coach = self.make_coach()
        answer = coach.advice(self.api.hero_by_id(1))
        self.assertIn("LLM недоступна", answer)

    def test_status_contains_game_data(self):
        coach = self.make_coach()
        text = coach.status()
        self.assertIn("Anti-Mage", text)
        self.assertIn("12:34", text)

    def test_status_without_gsi(self):
        coach = self.make_coach(payload={})
        text = coach.status()
        self.assertIn("GSI не получает данных", text)

    def test_current_hero_from_gsi(self):
        coach = self.make_coach()
        self.assertEqual(coach.current_hero()["localized_name"], "Anti-Mage")

    def test_meta(self):
        coach = self.make_coach()
        text = coach.meta()
        self.assertIn("Shadow Fiend", text)
        self.assertIn("%", text)

    def test_draft_advice_uses_llm(self):
        coach = self.make_coach()
        text = coach.draft_advice()
        self.assertIn("Покупай Blink", text)

    def test_draft_advice_without_gsi(self):
        coach = self.make_coach(payload={})
        text = coach.draft_advice()
        self.assertTrue("Драфт" in text or "драфт" in text)
        self.assertIn("GSI", text)

    def test_draft_advice_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("нет сервера")
        coach = self.make_coach()
        text = coach.draft_advice()
        self.assertIn("LLM недоступна", text)
        self.assertIn("Anti-Mage", text)

    def test_memory_limit(self):
        coach = Coach(self.api, HeroCatalog(FAKE_HEROES), FakeGsi(gsi_payload()),
                      self.config, memory_size=2, session=self.session)
        for i in range(5):
            coach.remember(f"вопрос {i}", f"ответ {i}")
        context = coach.memory_context(limit=10)
        self.assertIn("вопрос 4", context)
        self.assertNotIn("вопрос 2", context)

    def test_describe_hero_from_text(self):
        coach = self.make_coach()
        self.assertEqual(coach.describe_hero_from_text("пудж")["localized_name"], "Pudge")

    # ------------------------------------------------------------------ #
    def test_position_default_and_set(self):
        coach = self.make_coach(profile=PlayerProfile(default_position=3))
        self.assertEqual(coach.position, 3)
        self.assertEqual(coach.set_position("саппорт"), 5)
        self.assertEqual(coach.reset_position(), 3)

    def test_pick_advice_uses_llm(self):
        coach = self.make_coach()
        self.assertIn("Blink", coach.pick_advice())

    def test_pick_advice_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("нет сервера")
        silencer = {"id": 999, "name": "npc_dota_hero_silencer", "localized_name": "Silencer",
                    "roles": ["Support"], "pro_pick": 1, "pro_win": 1}
        catalog = HeroCatalog(FAKE_HEROES + [silencer])
        coach = Coach(self.api, catalog, FakeGsi(gsi_payload()), LlmConfig(model="gemma3"),
                      PlayerProfile(hero_pool={"1": ["Silencer"]}), session=self.session)
        text = coach.pick_advice()
        self.assertIn("LLM недоступна", text)
        self.assertIn("Silencer", text)

    def test_game_analysis_uses_llm(self):
        coach = self.make_coach()
        self.assertIn("Blink", coach.game_analysis())

    def test_game_analysis_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("нет сервера")
        coach = self.make_coach()
        text = coach.game_analysis()
        self.assertIn("LLM недоступна", text)
        self.assertIn("детектор", text)

    def test_buy_next_uses_llm(self):
        coach = self.make_coach()
        self.assertIn("Blink", coach.buy_next())

    def test_buy_next_fallback_with_enemy_hint(self):
        self.session.routes["/api/tags"] = RuntimeError("нет сервера")
        silencer = {"id": 999, "name": "npc_dota_hero_silencer", "localized_name": "Silencer",
                    "roles": ["Support"], "pro_pick": 1, "pro_win": 1}
        catalog = HeroCatalog(FAKE_HEROES + [silencer])
        payload = gsi_payload()
        payload["draft"]["team3"]["pick_ids"] = [11, 999]
        coach = Coach(self.api, catalog, FakeGsi(payload), LlmConfig(model="gemma3"),
                      PlayerProfile(), session=self.session)
        text = coach.buy_next()
        self.assertIn("LLM недоступна", text)
        self.assertIn("Silencer", text)
        self.assertIn("BKB", text)

    def test_switch_model_by_name(self):
        coach = self.make_coach()
        changed, message = coach.switch_model("deepseek-chat")
        self.assertTrue(changed)
        self.assertEqual(coach.llm.model, "deepseek-chat")
        self.assertIn("deepseek-chat", message)

    def test_switch_model_by_preset(self):
        config = LlmConfig(model="gemma3", presets={
            "deepseek": {"provider": "openai_compatible", "base_url": "https://api.deepseek.com/v1",
                         "api_key": "k", "model": "deepseek-chat"},
        })
        coach = Coach(self.api, HeroCatalog(FAKE_HEROES), FakeGsi(gsi_payload()), config,
                      PlayerProfile(), session=self.session)
        changed, _ = coach.switch_model("deepseek")
        self.assertTrue(changed)
        self.assertEqual(coach.provider, "openai_compatible")
        self.assertEqual(coach.llm.model, "deepseek-chat")

    def test_switch_model_no_target(self):
        coach = self.make_coach()
        changed, message = coach.switch_model(None)
        self.assertFalse(changed)
        self.assertIn("gemma3", message)

    def test_openai_compatible_chat(self):
        self.session.routes["/chat/completions"] = {
            "choices": [{"message": {"content": "Ответ от DeepSeek"}}]
        }
        config = LlmConfig(provider="openai_compatible", base_url="https://api.deepseek.com/v1",
                           api_key="k", model="deepseek-chat")
        coach = Coach(self.api, HeroCatalog(FAKE_HEROES), FakeGsi(gsi_payload()), config,
                      PlayerProfile(), session=self.session)
        self.assertEqual(coach.resolve_model(), "deepseek-chat")
        self.assertEqual(coach.chat("привет"), "Ответ от DeepSeek")
        self.assertTrue(any("chat/completions" in call for call in self.session.calls))


if __name__ == "__main__":
    unittest.main()
