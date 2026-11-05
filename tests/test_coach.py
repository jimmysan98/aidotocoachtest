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

    def test_analyze_match_llm(self):
        raw_match = {
            "match_id": 77777,
            "radiant_win": True,
            "duration": 2100,
            "radiant_score": 30,
            "dire_score": 15,
            "players": [
                {
                    "account_id": 123456,
                    "player_slot": 0,
                    "hero_id": 1,
                    "kills": 10,
                    "deaths": 1,
                    "assists": 7,
                    "gold_per_min": 700,
                    "xp_per_min": 750,
                    "item_0": 0,
                    "benchmarks": {"gpm_pct": 0.9, "xp_per_min": {"pct": 0.88}},
                }
            ],
        }
        self.session.routes["/matches/77777"] = raw_match
        self.session.routes["/api/chat"] = {"message": {"content": "Отличная игра на Антимаге."}}
        coach = self.make_coach()
        res = coach.analyze_match(match_id=77777, account_id=123456)
        self.assertIn("РАЗБОР МАТЧА #77777", res)
        self.assertIn("Anti-Mage", res)
        self.assertIn("Отличная игра на Антимаге", res)

    def test_analyze_match_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        raw_match = {
            "match_id": 88888,
            "radiant_win": False,
            "duration": 2400,
            "players": [
                {
                    "account_id": 123456,
                    "player_slot": 0,
                    "hero_id": 1,
                    "kills": 2,
                    "deaths": 9,
                    "assists": 3,
                    "gold_per_min": 350,
                    "xp_per_min": 400,
                    "tower_damage": 300,
                    "benchmarks": {"gold_per_min": {"pct": 0.20}},
                }
            ],
        }
        self.session.routes["/matches/88888"] = raw_match
        coach = self.make_coach()
        res = coach.analyze_match(match_id=88888, account_id=123456)
        self.assertIn("РАЗБОР МАТЧА #88888", res)
        self.assertIn("LLM недоступна", res)
        self.assertIn("Статистический разбор от AIDOTA", res)

    def test_analyze_match_no_match_specified(self):
        coach = Coach(self.api, HeroCatalog(FAKE_HEROES), None, LlmConfig(), PlayerProfile(), session=self.session)
        res = coach.analyze_match()
        self.assertIn("Не удалось определить матч для разбора", res)

    def test_coach_chat_success(self):
        coach = self.make_coach()
        answer = coach.coach_chat("я купил стики на второй минуте это нормально?")
        self.assertIn("Blink Dagger", answer)
        self.assertEqual(len(coach._memory), 1)

    def test_coach_chat_error_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        coach = self.make_coach()
        answer = coach.coach_chat("что делать?")
        self.assertIn("Не удалось связаться с ИИ", answer)

    def test_start_items_success(self):
        coach = self.make_coach()
        hero = self.api.hero_by_id(1)
        answer = coach.start_items(hero)
        self.assertIn("Blink Dagger", answer)

    def test_start_items_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        coach = self.make_coach()
        hero = self.api.hero_by_id(1)
        answer = coach.start_items(hero)
        self.assertIn("На старте для", answer)
        self.assertNotIn("лейт", answer.lower())

    def test_build_messages_with_history(self):
        coach = self.make_coach()
        coach.remember("что купить на старте?", "Купи танго и цирклет.")
        coach.remember("какой скилл качать?", "Качай первую способность.")
        msgs = coach._build_messages("а против кого играть?", "system prompt")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "что купить на старте?")
        self.assertEqual(msgs[2]["role"], "assistant")
        self.assertEqual(msgs[2]["content"], "Купи танго и цирклет.")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "а против кого играть?")

    def test_counter_faceless_void_no_bkb(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        coach = self.make_coach()
        void_hero = {"id": 41, "name": "npc_dota_hero_faceless_void", "localized_name": "Faceless Void"}
        answer = coach.counter_advice(void_hero)
        self.assertIn("Aeon Disk", answer)
        self.assertIn("Ghost Scepter", answer)
        self.assertIn("БКБ от хроносферы не спасает", answer)

    def test_skills_order_oracle_fallback(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        coach = self.make_coach()
        oracle_hero = {"id": 111, "name": "npc_dota_hero_oracle", "localized_name": "Oracle"}
        answer = coach.skills_order(oracle_hero)
        self.assertIn("Oracle", answer)
        self.assertIn("Purifying Flames", answer)

    def test_coach_chat_expired_api_key(self):
        from aidota.coach import OllamaError
        coach = self.make_coach()
        coach.chat = lambda *args, **kwargs: (_ for _ in ()).throw(OllamaError("API-ключ недействителен или истёк (HTTP 401)"))
        answer = coach.coach_chat("что делать?")
        self.assertIn("HTTP 401", answer)
        self.assertIn("Настройках", answer)

    def test_hero_intro_advice_chen(self):
        self.session.routes["/api/tags"] = RuntimeError("LLM offline")
        coach = self.make_coach(payload={})
        chen_hero = {"id": 66, "name": "npc_dota_hero_chen", "localized_name": "Chen"}
        answer = coach.hero_intro_advice(chen_hero)
        self.assertIn("Chen", answer)
        self.assertIn("Holy Persuasion", answer)
        # Verify manual hero is now set in coach when GSI is empty
        self.assertEqual(coach.current_hero(), chen_hero)

    def test_hero_intro_advice_llm(self):
        coach = self.make_coach()
        chen_hero = {"id": 66, "name": "npc_dota_hero_chen", "localized_name": "Chen"}
        answer = coach.hero_intro_advice(chen_hero)
        self.assertIn("Blink Dagger", answer)
        self.assertEqual(len(coach._memory), 1)

    def test_tool_recommend_draft_picks(self):
        coach = self.make_coach()
        res = coach.execute_tool("recommend_draft_picks", {"enemy_heroes": ["medusa", "storm_spirit"], "role": 1})
        self.assertIn("Антимаг", res)

    def test_tool_get_hero_item_build(self):
        coach = self.make_coach()
        res = coach.execute_tool("get_hero_item_build", {"hero_name": "axe", "role": 3})
        self.assertIn("Акс", res)
        self.assertIn("Blink Dagger", res)


if __name__ == "__main__":
    unittest.main()

