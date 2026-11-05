# -*- coding: utf-8 -*-
"""Тесты «Тренера для новичков» и уровней игрока (контракт E + расширение до 4 уровней).

Проверяет:
- глоссарий Dota 2 (50+ терминов, падежи, алиасы, мусор);
- нормализацию уровней beginner/amateur/advanced/expert и их названия;
- правила новичка и глубокий тактический стиль для опытных в промптах;
- формат и лимиты ответов (новичок — коротко, опытный чат — 3–5 предложений);
- ``Coach.coach_chat``: объяснение термина из глоссария без LLM, приветствие-онбординг;
- разборы игры/матча в формате «Что получилось / Главная ошибка / Упражнение»;
- новичковые подсказки ``AutonomousCoachAgent`` («почему это важно»);
- прокидку ``skill_level`` из профиля в ``Assistant`` и сохранение настроек.

Сеть не используется: только локальные ``data/*.json``, fake-сессии и подмена
``Coach.chat``. Временные каталоги — ``tests.helpers.make_temp_dir``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

from aidota import beginner
from aidota.beginner import (
    GLOSSARY,
    SKILL_LEVELS,
    explain_term,
    freshman_greeting,
    level_label,
    next_drill,
    normalize_skill_level,
    resolve_term,
    role_training_plan,
)
from aidota.coach import Coach, OllamaError
from aidota.coach_brain import (
    detect_query_kind,
    make_system_prompt,
    render_system_prompt,
    sanitize_answer,
)
from aidota.config import Config, LlmConfig, PlayerProfile
from aidota.context import AutonomousCoachAgent
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from tests.helpers import (
    FakeResponse,
    gsi_payload,
    make_temp_dir,
    remove_temp_dir,
)

# --------------------------------------------------------------------------- #
# Фикстуры (локальные, без сети)
# --------------------------------------------------------------------------- #

COACH_HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage",
     "roles": ["Carry"], "pro_pick": 50, "pro_win": 25, "pub_pick": 1000, "pub_win": 520},
    {"id": 2, "name": "npc_dota_hero_pudge", "localized_name": "Pudge",
     "roles": ["Initiator"], "pub_pick": 1000, "pub_win": 500},
    {"id": 11, "name": "npc_dota_hero_nevermore", "localized_name": "Shadow Fiend",
     "roles": ["Carry"], "pub_pick": 600, "pub_win": 300},
    {"id": 44, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin",
     "roles": ["Carry"], "pub_pick": 500, "pub_win": 250},
]

COACH_ITEMS = {
    "blink": "Blink Dagger",
    "tango": "Tango",
    "branches": "Iron Branch",
    "boots": "Boots of Speed",
    "bfury": "Battle Fury",
    "tpscroll": "Town Portal Scroll",
}
ITEM_POPULARITY = {
    "start_game_items": {"tango": 100, "branches": 80},
    "early_game_items": {"boots": 50},
    "mid_game_items": {},
    "late_game_items": {"blink": 10},
}

REQUIRED_TERMS = (
    "БКБ", "байбек", "вижн", "ластхит", "денай", "харасс", "ротация", "ганк",
    "инициация", "кайт", "сейв", "диспел", "сайленс", "контроль", "тайминг",
    "паверспайк", "стак", "отвод", "смок", "руна", "аегис", "Рошан", "Терзатель",
    "нейтралка", "аганим", "шард", "GPM", "XPM", "net worth", "KDA",
    "позиция 1", "позиция 2", "позиция 3", "позиция 4", "позиция 5",
    "керри", "мид", "оффлейн", "саппорт", "драфт", "контрпик", "бан", "пуш",
    "сплитпуш", "хайграунд", "таверна", "броня", "магия", "физический урон",
    "ультимейт", "кулдаун", "мана-берн", "дебафф", "бафф",
)


class FakeGsi:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 1.5


class SequenceSession:
    """Fake requests.Session: GET по подстрокам URL, POST — из очереди."""

    def __init__(self, get_routes: dict | None = None, post_responses: list | None = None):
        self.get_routes = dict(get_routes or {})
        self.post_responses = list(post_responses or [])
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    def get(self, url, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        for fragment, data in self.get_routes.items():
            if fragment in url:
                if isinstance(data, Exception):
                    raise data
                return FakeResponse(data)
        raise AssertionError(f"Неожиданный GET в тесте: {url}")

    def post(self, url, json=None, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        self.payloads.append(json)
        if not self.post_responses:
            raise AssertionError(f"Нет заготовленного ответа на POST {url}")
        nxt = self.post_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)


class CoachCase(unittest.TestCase):
    """Общая фикстура: временный data-каталог, GSI-снимок и fake LLM."""

    def setUp(self):
        self.tmp = make_temp_dir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "heroes.json").write_text(
            json.dumps(COACH_HEROES, ensure_ascii=False), encoding="utf-8"
        )
        (self.data / "items.json").write_text(
            json.dumps(COACH_ITEMS, ensure_ascii=False), encoding="utf-8"
        )
        self.session = SequenceSession(
            get_routes={
                "/heroStats": COACH_HEROES,
                "/constants/items": {key: {"dname": name} for key, name in COACH_ITEMS.items()},
                "/itemPopularity": ITEM_POPULARITY,
                "/api/tags": {"models": [{"name": "gemma3:latest"}]},
            }
        )
        self.api = DotaApi(self.data, session=self.session)
        self.api.load_snapshots()
        self.catalog = HeroCatalog(COACH_HEROES)
        self.profile = PlayerProfile(default_position=5)
        self.llm = LlmConfig(model="gemma3")
        self.memory_path = self.data / "coach_memory.json"

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def make_coach(self, *, payload=None, profile=None) -> Coach:
        if payload is None:
            payload = gsi_payload()
        return Coach(
            self.api,
            self.catalog,
            FakeGsi(payload),
            self.llm,
            profile if profile is not None else self.profile,
            coach_name="Астра",
            session=self.session,
            memory_path=self.memory_path,
        )

    def install_chat(self, coach: Coach, answer: str) -> dict:
        captured: dict = {"calls": []}

        def fake_chat(prompt, system=None, **kwargs):
            captured["calls"].append(prompt)
            captured["prompt"] = prompt
            captured["system"] = system
            captured["kwargs"] = kwargs
            return answer

        coach.chat = fake_chat  # type: ignore[method-assign]
        return captured

    def install_failing_chat(self, coach: Coach) -> None:
        def failing_chat(*args, **kwargs):
            raise OllamaError("LLM offline")

        coach.chat = failing_chat  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# 1. Глоссарий и поиск терминов
# --------------------------------------------------------------------------- #
class GlossaryTests(unittest.TestCase):
    def test_glossary_has_at_least_50_terms(self):
        self.assertGreaterEqual(len(GLOSSARY), 50)

    def test_glossary_schema_and_no_markdown(self):
        for term, entry in GLOSSARY.items():
            with self.subTest(term=term):
                self.assertIsInstance(entry, dict)
                for key in ("aliases", "simple", "example"):
                    self.assertIn(key, entry)
                self.assertIsInstance(entry["aliases"], tuple)
                self.assertTrue(entry["aliases"], f"{term}: пустые алиасы")
                for text in (entry["simple"], entry["example"]):
                    self.assertIsInstance(text, str)
                    self.assertTrue(text.strip(), term)
                    self.assertLessEqual(len(text), 260, f"{term}: слишком длинно")
                    for marker in ("**", "`", "#", "•", "\n"):
                        self.assertNotIn(marker, text, f"{term}: markdown/markup")

    def test_required_terms_present_and_resolvable(self):
        for term in REQUIRED_TERMS:
            with self.subTest(term=term):
                self.assertIn(term, GLOSSARY)
                resolved = resolve_term(f"что такое {term}")
                self.assertIsNotNone(resolved, f"термин не находится: {term}")
                self.assertEqual(resolved[0], term)  # type: ignore[index]

    def test_resolve_term_understands_cases_and_aliases(self):
        cases = {
            "что такое БКБ": "БКБ",
            "объясни, что значит байбека": "байбек",
            "как работает вижн": "вижн",
            "что такое руны мудрости": "руна мудрости",
            "что такое аегиса": "аегис",
            "что такое Рошана": "Рошан",
            "что такое нет ворт": "net worth",
            "что такое КДА": "KDA",
            "что такое позиции 4": "позиция 4",
            "что такое крипов": "крипы",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                resolved = resolve_term(phrase)
                self.assertIsNotNone(resolved, phrase)
                self.assertEqual(resolved[0], expected)  # type: ignore[index]

    def test_resolve_term_ignores_garbage_and_empty(self):
        for junk in ("", "   ", None, 42, "привет как дела", "что такое", "купить хлеб в магазине"):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(resolve_term(junk))

    def test_explain_term_is_short_clean_and_has_example(self):
        for phrase in ("что такое бкб", "объясни байбек", "как работает вижн", "что такое ластхит"):
            with self.subTest(phrase=phrase):
                text = explain_term(phrase)
                self.assertIsNotNone(text)
                text = str(text)
                self.assertLessEqual(len(text), 400)
                self.assertIn("Пример:", text)
                self.assertTrue(text[:1].isupper(), text)
                self.assertTrue(text.endswith("."), text)
                self.assertNotIn("*", text)
                self.assertNotIn("#", text)
                self.assertNotIn("\n", text)
                self.assertLessEqual(len([s for s in text.split(".") if s.strip()]), 3)

    def test_explain_term_returns_none_for_unknown(self):
        self.assertIsNone(explain_term("привет"))
        self.assertIsNone(explain_term("что такое квантовая хромодинамика"))


class SkillLevelTests(unittest.TestCase):
    def test_four_levels_and_labels(self):
        self.assertEqual(SKILL_LEVELS, ("beginner", "amateur", "advanced", "expert"))
        self.assertEqual(level_label("beginner"), "Новичок")
        self.assertEqual(level_label("amateur"), "Любитель")
        self.assertEqual(level_label("advanced"), "Опытный")
        self.assertEqual(level_label("expert"), "Профи")

    def test_normalize_aliases(self):
        cases = {
            "новичок": "beginner",
            "Нуб": "beginner",
            "beginner": "beginner",
            "любитель": "amateur",
            "средний": "amateur",
            "amateur": "amateur",
            "опытный": "advanced",
            "Продвинутый": "advanced",
            "advanced": "advanced",
            "профи": "expert",
            "ПРО": "expert",
            "эксперт": "expert",
            "expert": "expert",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_skill_level(raw), expected)

    def test_normalize_garbage_defaults_to_beginner(self):
        for junk in (None, "", "   ", "неизвестно", 42, ["expert"]):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(normalize_skill_level(junk), "beginner")


class OnboardingTests(unittest.TestCase):
    def test_freshman_greeting_has_three_steps(self):
        text = freshman_greeting("Астра")
        self.assertIn("Астра", text)
        self.assertIn("Спроси", text)
        self.assertIn("Пользуйся", text)
        self.assertIn("Начать", text)
        self.assertLessEqual(len(text), 600)
        for marker in ("**", "#", "•", "\n"):
            self.assertNotIn(marker, text)
        self.assertIn("Джарвис", freshman_greeting("Джарвис"))

    def test_role_training_plan_has_steps_for_every_role(self):
        for position in range(1, 6):
            with self.subTest(position=position):
                plan = role_training_plan(position)
                self.assertIn(f"поз. {position}", plan)
                for step in ("1)", "2)", "3)"):
                    self.assertIn(step, plan)
                self.assertLessEqual(len(plan), 600)
                self.assertEqual(plan, role_training_plan(str(position)))

    def test_next_drill_is_deterministic_and_role_based(self):
        for position in range(1, 6):
            with self.subTest(position=position):
                first = next_drill(position, "матч #1")
                second = next_drill(position, "матч #1")
                self.assertEqual(first, second)
                self.assertTrue(first.startswith("Упражнение:"), first)
                self.assertGreater(len(first), 20)
        # Упражнения покрывают базовые навыки: ластхит, вижн, руны и карту.
        all_drills = " ".join(next_drill(position, f"seed-{i}") for position in range(1, 6) for i in range(12))
        for keyword in ("ластхит", "вижн", "рун", "карт"):
            self.assertIn(keyword, all_drills)


# --------------------------------------------------------------------------- #
# 2. Промпты и санитайзер: стиль под уровень
# --------------------------------------------------------------------------- #
class PromptStyleTests(unittest.TestCase):
    def test_beginner_rules_in_prompt(self):
        prompt = make_system_prompt("Астра", skill_level="beginner")
        self.assertIn("7000+ MMR", prompt)  # персона не сломана
        self.assertIn("ПРАВИЛА ДЛЯ НОВИЧКА", prompt)
        self.assertIn("в скобках", prompt)
        self.assertIn("зачем", prompt)
        self.assertIn("Не ругай", prompt)
        self.assertIn("упражнение", prompt.lower())

    def test_expert_prompt_deep_tactical_style(self):
        prompt = make_system_prompt("Астра", skill_level="expert")
        self.assertIn("7000+ MMR", prompt)
        self.assertIn("базовые термины", prompt)
        self.assertNotIn("ПРАВИЛА ДЛЯ НОВИЧКА", prompt)
        for needle in (
            "паверспайков", "темпа карты", "окна драфта", "лейн-равновесия",
            "вижна и смоков", "байбека", "контроль объектов",
        ):
            self.assertIn(needle, prompt)

    def test_amateur_prompt_without_extreme_blocks(self):
        prompt = make_system_prompt("Астра", skill_level="amateur")
        self.assertNotIn("ПРАВИЛА ДЛЯ НОВИЧКА", prompt)
        self.assertNotIn("базовые термины", prompt)

    def test_render_format_depends_on_level(self):
        beginner_chat = render_system_prompt("Астра", kind="chat", skill_level="beginner")
        self.assertIn("1–2 предложения", beginner_chat)
        expert_chat = render_system_prompt("Астра", kind="chat", skill_level="expert")
        self.assertIn("3–5", expert_chat)
        self.assertIn("500", expert_chat)
        # В остальных режимах опытный игрок получает короткий формат.
        self.assertIn("1–2 предложения", render_system_prompt("Астра", kind="build", skill_level="expert"))
        beginner_explain = render_system_prompt("Астра", kind="explain", skill_level="beginner")
        self.assertIn("пример", beginner_explain.lower())

    def test_detect_explain_queries(self):
        for phrase in ("что такое бкб", "объясни, что значит байбек", "как работает вижн", "что значит паверспайк"):
            with self.subTest(phrase=phrase):
                self.assertEqual(detect_query_kind(phrase), "explain")
        self.assertEqual(detect_query_kind("почему бкб не спасает"), "why")
        self.assertEqual(detect_query_kind("что купить на старте"), "build")
        self.assertEqual(detect_query_kind("привет"), "chat")

    def test_sanitize_limits_by_level(self):
        five = "Держи темп. Следи за таймингами. Покупай BKB. Двигайся к Рошану. Ставь вижн."
        self.assertEqual(sanitize_answer(five).count("."), 3)  # новичок: до 3 предложений
        self.assertEqual(
            sanitize_answer(five, skill_level="expert", kind="chat").count("."), 5
        )
        self.assertEqual(
            sanitize_answer(five, max_sentences=2, skill_level="expert", kind="chat").count("."), 2
        )
        # В обычном режиме (build) лимиты прежние даже у профи.
        self.assertEqual(sanitize_answer(five, skill_level="expert", kind="build").count("."), 2)
        long_text = "а" * 600 + "."
        self.assertLessEqual(len(sanitize_answer(long_text, skill_level="beginner")), 322)
        self.assertLessEqual(len(sanitize_answer(long_text, skill_level="expert")), 502)


# --------------------------------------------------------------------------- #
# 3. Coach: уровень, глоссарий без LLM, приветствие, fallback-разборы
# --------------------------------------------------------------------------- #
class CoachBeginnerTests(CoachCase):
    def test_skill_level_property_and_setter(self):
        coach = self.make_coach()
        self.assertEqual(coach.skill_level, "beginner")  # дефолт через getattr
        self.assertEqual(coach.set_skill_level("про"), "expert")
        self.assertEqual(coach.skill_level, "expert")
        self.assertEqual(getattr(coach.profile, "skill_level"), "expert")
        self.assertEqual(coach.set_skill_level("нуб"), "beginner")
        self.assertEqual(coach.skill_level, "beginner")
        self.assertEqual(coach.set_skill_level("мусор"), "beginner")

    def test_coach_sees_level_from_profile(self):
        profile = PlayerProfile(default_position=1)
        profile.skill_level = "advanced"  # type: ignore[attr-defined]
        coach = self.make_coach(profile=profile)
        self.assertEqual(coach.skill_level, "advanced")
        self.assertEqual(coach.level_label, "Опытный")

    def test_explain_uses_glossary_without_llm(self):
        coach = self.make_coach()
        answer = coach.coach_chat("что такое бкб")
        self.assertIn("БКБ", answer)
        self.assertIn("Black King Bar", answer)
        self.assertLessEqual(len(answer), 400)
        self.assertNotIn("**", answer)
        self.assertEqual(self.session.calls, [], "глоссарий не должен ходить в LLM")
        self.assertEqual(len(coach._memory), 1)

    def test_empty_query_returns_onboarding_greeting(self):
        coach = self.make_coach()
        self.assertIn("Привет", coach.coach_chat(""))
        self.assertIn("Астра", coach.coach_chat("   "))

    def test_beginner_hero_intro_fallback_has_training_step(self):
        coach = self.make_coach()
        self.install_failing_chat(coach)
        answer = coach.hero_intro_advice(self.api.hero_by_id(1))
        self.assertIn("Anti-Mage", answer)
        self.assertIn("Тренировк", answer)
        self.assertTrue(
            any(word in answer.lower() for word in ("вижн", "ластхит", "рун", "карт")), answer
        )

    def test_beginner_game_analysis_fallback_format(self):
        coach = self.make_coach()
        self.install_failing_chat(coach)
        text = coach.game_analysis()
        self.assertIn("LLM недоступна", text)
        self.assertIn("Что получилось", text)
        self.assertIn("Главная ошибка", text)
        self.assertIn("Упражнение", text)

    def test_beginner_match_analysis_fallback_format(self):
        raw_match = {
            "match_id": 99999,
            "radiant_win": False,
            "duration": 2400,
            "radiant_score": 15,
            "dire_score": 25,
            "players": [{
                "account_id": 123456,
                "player_slot": 0,
                "hero_id": 1,
                "kills": 2,
                "deaths": 9,
                "assists": 3,
                "gold_per_min": 350,
                "xp_per_min": 400,
                "tower_damage": 300,
                "benchmarks": {"gpm_pct": 0.2},
            }],
        }
        self.session.get_routes["/matches/99999"] = raw_match
        coach = self.make_coach()
        self.install_failing_chat(coach)
        result = coach.analyze_match(match_id=99999, account_id=123456)
        self.assertIn("РАЗБОР МАТЧА #99999", result)
        self.assertIn("Статистический разбор от AIDOTA", result)
        self.assertIn("LLM недоступна", result)
        self.assertIn("Что получилось", result)
        self.assertIn("Главная ошибка", result)
        self.assertIn("Упражнение", result)

    def test_beginner_advice_guidance_explains_actions(self):
        coach = self.make_coach()
        captured = self.install_chat(coach, "Купи Tango и держи линию.")
        hero = self.api.hero_by_id(1)
        coach.advice(hero, user_query="что купить дальше")
        prompt = captured["prompt"]
        self.assertIn("новичок", prompt.lower())
        self.assertIn("простыми словами", prompt.lower())
        self.assertIn("зачем", prompt.lower())

    def test_expert_chat_is_longer_but_voice_stays_short(self):
        profile = PlayerProfile(default_position=2)
        profile.skill_level = "expert"  # type: ignore[attr-defined]
        coach = self.make_coach(profile=profile)
        five = "Держи темп карты. Следи за окнами драфта. Покупай BKB до драки. Двигайся к Рошану с командой. Ставь вижн заранее."
        captured = self.install_chat(coach, five)
        answer = coach.coach_chat("что дальше делать по карте")
        self.assertGreaterEqual(answer.count("."), 3)
        self.assertIn("базовые термины", str(captured["system"]))
        voice_answer = coach.coach_chat("что дальше делать по карте", voice=True)
        self.assertLessEqual(voice_answer.count("."), 2)

    def test_expert_match_analysis_prompt_asks_for_decisions(self):
        raw_match = {
            "match_id": 77777,
            "radiant_win": True,
            "duration": 2100,
            "radiant_score": 30,
            "dire_score": 15,
            "players": [{
                "account_id": 123456,
                "player_slot": 0,
                "hero_id": 1,
                "kills": 10,
                "deaths": 1,
                "assists": 7,
                "gold_per_min": 700,
                "xp_per_min": 750,
                "benchmarks": {"gpm_pct": 0.9},
            }],
        }
        self.session.get_routes["/matches/77777"] = raw_match
        profile = PlayerProfile(default_position=1)
        profile.skill_level = "advanced"  # type: ignore[attr-defined]
        coach = self.make_coach(profile=profile)
        captured = self.install_chat(coach, "Разбор решений: темп карты и тайминги вижна.")
        coach.analyze_match(match_id=77777, account_id=123456)
        system = str(captured["system"])
        self.assertIn("решения", system)
        self.assertIn("темп карты", system)
        self.assertIn("тайминги паверспайков", system)


# --------------------------------------------------------------------------- #
# 4. AutonomousCoachAgent: новичковые объяснения
# --------------------------------------------------------------------------- #
class AutonomousAgentLevelTests(unittest.TestCase):
    def _agent(self, level: str = "beginner") -> AutonomousCoachAgent:
        return AutonomousCoachAgent(
            autonomy_level="high", cooldown_seconds=0.0, skill_level=level
        )

    def test_beginner_gets_why_notes(self):
        agent = self._agent()
        # Серия смертей.
        agent.process_tick({"map": {"clock_time": 830}, "player": {"deaths": 0, "team_name": "radiant"}}, position=1)
        deaths = agent.process_tick(
            {"map": {"clock_time": 850}, "player": {"deaths": 2, "team_name": "radiant"}},
            position=1,
        )
        self.assertIsNotNone(deaths)
        self.assertIn("Не тильтуй", str(deaths))
        self.assertIn("Почему это важно", str(deaths))

        # Накопленное золото.
        gold_agent = self._agent()
        gold = gold_agent.process_tick(
            {
                "map": {"clock_time": 800},
                "player": {"gold": 5000, "team_name": "radiant"},
                "hero": {"alive": True},
            },
            position=1,
        )
        self.assertIsNotNone(gold)
        self.assertIn("золот", str(gold))
        self.assertIn("Почему это важно", str(gold))

        # Аегис.
        aegis_agent = self._agent()
        aegis = aegis_agent.process_tick(
            {"map": {"clock_time": 1000, "aegis": "dire"}, "player": {"team_name": "radiant"}},
            position=1,
        )
        self.assertIn("аегис", str(aegis))
        self.assertIn("Почему это важно", str(aegis))

        # Паверспайк по новому предмету.
        spike_agent = self._agent()
        spike_agent.process_tick(
            {"map": {"clock_time": 800}, "hero": {"items": {"slot0": {"name": "item_boots"}}}},
            position=1,
        )
        spike = spike_agent.process_tick(
            {
                "map": {"clock_time": 805},
                "hero": {"items": {"slot0": {"name": "item_boots"}, "slot1": {"name": "item_blink"}}},
            },
            position=1,
        )
        self.assertIn("Blink Dagger", str(spike))
        self.assertIn("Почему это важно", str(spike))

    def test_expert_advice_stays_short_and_unexplained(self):
        agent = self._agent("expert")
        agent.process_tick({"map": {"clock_time": 830}, "player": {"deaths": 0, "team_name": "radiant"}}, position=1)
        deaths = agent.process_tick(
            {"map": {"clock_time": 850}, "player": {"deaths": 2, "team_name": "radiant"}},
            position=1,
        )
        self.assertIsNotNone(deaths)
        self.assertNotIn("Почему это важно", str(deaths))

        aegis_agent = self._agent("advanced")
        aegis = aegis_agent.process_tick(
            {"map": {"clock_time": 1000, "aegis": "dire"}, "player": {"team_name": "radiant"}},
            position=1,
        )
        self.assertNotIn("Почему это важно", str(aegis))

    def test_agent_set_skill_level_normalizes(self):
        agent = self._agent()
        self.assertEqual(agent.set_skill_level("про"), "expert")
        self.assertEqual(agent.skill_level, "expert")
        self.assertEqual(agent.set_skill_level("нуб"), "beginner")
        self.assertEqual(agent.skill_level, "beginner")


# --------------------------------------------------------------------------- #
# 5. app.py: прокидка уровня в Agent и сохранение настроек
# --------------------------------------------------------------------------- #
class AppWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def test_assistant_passes_profile_level_to_agent(self):
        from aidota.app import Assistant

        config = Config()
        config.overlay.enabled = False
        config.gsi.enabled = False
        config.player_profile.skill_level = "expert"  # type: ignore[attr-defined]
        assistant = Assistant(config, text_mode=True, config_path=self.tmp / "config.json")
        try:
            self.assertEqual(assistant._agent.skill_level, "expert")
            self.assertEqual(assistant._coach.skill_level, "expert")
        finally:
            try:
                assistant._voice.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                assistant._tts.close()
            except Exception:  # noqa: BLE001
                pass

    def test_settings_save_applies_skill_level_to_coach_and_agent(self):
        from aidota.app import Assistant

        class _CoachStub:
            def __init__(self):
                self.llm = LlmConfig()
                self.profile = PlayerProfile()
                self._models_cache = (0.0, [], None)
                self._level: str | None = None
                self.coach_name = "Астра"

            def set_skill_level(self, level):
                self._level = normalize_skill_level(level)
                setattr(self.profile, "skill_level", self._level)
                return self._level

            def set_coach_name(self, name):
                self.coach_name = name

        assistant = Assistant.__new__(Assistant)
        assistant.config = Config()
        assistant.config_path = self.tmp / "config.json"
        assistant.log = logging.getLogger("tests.beginner")
        assistant._coach = _CoachStub()  # type: ignore[assignment]
        assistant._agent = SimpleNamespace(
            set_skill_level=lambda level: setattr(
                assistant._agent, "skill_level", normalize_skill_level(level)
            )
        )
        assistant._overlay = None
        assistant._voice = SimpleNamespace()  # type: ignore[assignment]
        assistant._tts = SimpleNamespace(config=SimpleNamespace())  # type: ignore[assignment]

        assistant._on_settings_save({"skill_level": "продвинутый"})

        self.assertEqual(assistant._coach._level, "advanced")  # type: ignore[attr-defined]
        self.assertEqual(assistant._agent.skill_level, "advanced")
        self.assertEqual(getattr(assistant.config.player_profile, "skill_level"), "advanced")
        saved_path = Path(assistant.config_path)
        self.assertTrue(saved_path.is_file())
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        field_names = {field.name for field in dataclasses.fields(PlayerProfile)}
        if "skill_level" in field_names:  # поле добавляет settings-ui
            self.assertEqual(saved["player_profile"]["skill_level"], "advanced")


if __name__ == "__main__":
    unittest.main()
