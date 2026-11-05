"""Тесты «мозга» ИИ-тренера: распознавание вопросов, бриф, знания, промпты,
санитайзер ответов, долговременная память и интеграция с Coach.
"""

from __future__ import annotations

import copy
import json
import unittest

from aidota.coach import Coach
from aidota.coach_brain import (
    build_brief,
    detect_query_kind,
    make_system_prompt,
    render_brief,
    render_system_prompt,
    resolve_mentioned_hero,
    retrieve_knowledge,
    sanitize_answer,
)
from aidota.coach_memory import CoachMemory
from aidota.config import LlmConfig, PlayerProfile
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from tests.helpers import (
    FAKE_HEROES,
    FakeResponse,
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


class SequenceSession:
    """Возвращает из post() заранее заданную последовательность ответов."""

    def __init__(self, get_routes: dict | None = None, post_responses: list | None = None):
        self.get_routes = get_routes or {}
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
        raise AssertionError(f"Неожиданный GET: {url}")

    def post(self, url, json=None, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        self.payloads.append(json)
        if not self.post_responses:
            raise AssertionError(f"Нет заготовленного ответа на POST {url}")
        nxt = self.post_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)


class TestQueryKind(unittest.TestCase):
    def test_build_questions(self):
        for phrase in ("что купить на старте", "что собрать на сфа", "какой билд на пуджа",
                       "какой закуп у антимага"):
            self.assertEqual(detect_query_kind(phrase), "build", phrase)

    def test_counter_questions(self):
        for phrase in ("что купить против пуджа", "как играть против хускара",
                       "чем контрить фантомку"):
            self.assertEqual(detect_query_kind(phrase), "counter", phrase)

    def test_pick_questions(self):
        for phrase in ("кого пикнуть против морфа", "кого банить", "кого взять из моих"):
            self.assertEqual(detect_query_kind(phrase), "pick", phrase)

    def test_skill_questions(self):
        for phrase in ("как качать на пудже", "что максить у оракла", "порядок прокачки скиллов"):
            self.assertEqual(detect_query_kind(phrase), "skill", phrase)

    def test_other_kinds(self):
        self.assertEqual(detect_query_kind("что делать на линии"), "lane")
        self.assertEqual(detect_query_kind("когда идти на рошана"), "timing")
        self.assertEqual(detect_query_kind("почему бкб не спасает"), "why")
        self.assertEqual(detect_query_kind("как дела"), "status")
        self.assertEqual(detect_query_kind("разбери мою игру"), "analysis")
        self.assertEqual(detect_query_kind("привет"), "chat")
        self.assertEqual(detect_query_kind(""), "chat")


class TestSanitizer(unittest.TestCase):
    def test_removes_markdown_and_lists(self):
        raw = "**Конечно**, отличный вопрос! 1. Купи BKB\n2. Собери Manta 😊"
        clean = sanitize_answer(raw)
        self.assertNotIn("**", clean)
        self.assertNotIn("1.", clean)
        self.assertNotIn("2.", clean)
        self.assertNotIn("Конечно", clean)
        self.assertNotIn("отличный вопрос", clean.lower())
        self.assertNotIn("😊", clean)
        self.assertTrue(clean.startswith("Купи"))

    def test_trims_to_sentences(self):
        raw = "Купи BKB. Потом собери Manta. Ещё возьми Pipe. И Shiva тоже."
        clean = sanitize_answer(raw, max_sentences=2)
        self.assertEqual(clean, "Купи BKB. Потом собери Manta.")

    def test_trims_to_chars_and_keeps_sentence_boundary(self):
        raw = "Против Фантомки бери Monkey King Bar, Ghost Scepter или Solar Crest, " * 2
        clean = sanitize_answer(raw, max_chars=80, max_sentences=3)
        self.assertLessEqual(len(clean), 82)
        self.assertTrue(clean.endswith("."))

    def test_refusal_returns_empty(self):
        self.assertEqual(sanitize_answer("Извините, но я не могу помочь с этим вопросом."), "")

    def test_empty_and_none(self):
        self.assertEqual(sanitize_answer(""), "")
        self.assertEqual(sanitize_answer(None), "")  # type: ignore[arg-type]

    def test_capitalizes_and_adds_period(self):
        self.assertEqual(sanitize_answer("купи bkb"), "Купи bkb.")


class TestBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.api = DotaApi(self.tmp, session=FakeSession({}))
        heroes_payload = json.loads(
            (__import__("pathlib").Path(__file__).parent / "fixtures" / "gsi_sample.json")
            .read_text(encoding="utf-8")
        )
        self.heroes = HeroCatalog(FAKE_HEROES + [
            {"id": 11, "name": "npc_dota_hero_nevermore", "localized_name": "Shadow Fiend"},
            {"id": 15, "name": "npc_dota_hero_razor", "localized_name": "Razor"},
        ])
        self.payload = heroes_payload

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def test_brief_from_live_payload(self):
        brief = build_brief(self.payload, self.heroes, ITEMS, position=1, profile=PlayerProfile())
        self.assertTrue(brief["available"])
        self.assertIn(brief["stage"], ("pre_game", "laning", "midgame", "lategame"))
        self.assertEqual(brief["hero_name"], "Anti-Mage")
        self.assertIsInstance(brief["inventory"], list)
        self.assertIsInstance(brief["threats"], list)
        text = render_brief(brief)
        self.assertIn("Роль:", text)
        self.assertIn("Anti-Mage", text)

    def test_brief_without_gsi(self):
        brief = build_brief(None, self.heroes, ITEMS, position=5)
        self.assertFalse(brief["available"])
        self.assertIn("Матч не запущен", render_brief(brief))

    def test_brief_does_not_crash_on_garbage(self):
        for payload in ({}, {"map": "oops", "hero": [1, 2], "player": 5}, {"map": {"clock_time": "abc"}}):
            brief = build_brief(payload, None, None, position=3)  # type: ignore[arg-type]
            self.assertIsInstance(brief, dict)
            render_brief(brief)


class TestLiveItemsBrief(unittest.TestCase):
    """Живые данные v1.2: верхнеуровневые items/abilities и советы по закупкам."""

    def setUp(self):
        self.heroes = HeroCatalog(FAKE_HEROES)
        self.items = {
            "power_treads": "Power Treads",
            "bfury": "Battle Fury",
            "manta": "Manta Style",
            "occult_bracelet": "Occult Bracelet",
            "tpscroll": "Town Portal Scroll",
        }
        self.item_costs = {
            "Ghost Scepter": 1500,
            "Eul's Scepter of Divinity": 2475,
            "Aeon Disk": 3000,
            "Black King Bar": 4050,
            "Monkey King Bar": 4050,
            "Magic Wand": 460,
        }

    def make_payload(self) -> dict:
        payload = copy.deepcopy(gsi_payload())
        # Реальный GSI: items/abilities — верхнеуровневые секции, а не hero.items.
        payload["items"] = payload["hero"].pop("items")
        payload["abilities"] = {
            "ability3": {
                "name": "antimage_mana_void",
                "ultimate": True,
                "cooldown": 0,
                "can_cast": True,
            }
        }
        # Враги игрока (dire -> team3): Juggernaut и Shadow Fiend.
        payload["draft"]["team3"]["pick_ids"] = [8, 11]
        return payload

    def test_brief_reads_top_level_items_and_ability(self):
        brief = build_brief(
            self.make_payload(), self.heroes, self.items, position=5,
            item_costs=self.item_costs,
        )
        self.assertEqual(
            brief["inventory"], ["Power Treads", "Battle Fury", "Manta Style"]
        )
        self.assertEqual(brief["ult_name"], "Mana Void")
        self.assertTrue(brief["ult_ready"])
        # player.net_worth в фикстуре нет -> оценка по золоту и предметам.
        self.assertTrue(brief["net_worth_estimated"])
        self.assertIsNotNone(brief["net_worth"])

    def test_brief_has_grounded_advice_and_render(self):
        brief = build_brief(
            self.make_payload(), self.heroes, self.items, position=5,
            item_costs=self.item_costs,
        )
        self.assertTrue(brief["item_advice"])
        self.assertIsNotNone(brief["next_item"])
        joined = " ".join(entry["reason"] for entry in brief["item_advice"])
        self.assertIn("Juggernaut", joined)
        self.assertIn("Omnislash", joined)
        text = render_brief(brief)
        self.assertIn("Закупка", text)
        self.assertIn("Juggernaut", text)
        self.assertIn("Omnislash", text)


class TestKnowledgeRetrieval(unittest.TestCase):
    def setUp(self):
        self.catalog = HeroCatalog(FAKE_HEROES)
        self.hero = self.catalog.find("Anti-Mage").hero  # type: ignore[union-attr]

    def test_build_knowledge(self):
        snippets = retrieve_knowledge(
            "что купить на антимага",
            hero=self.hero,
            position=1,
            enemy_heroes=["Pudge"],
        )
        joined = " ".join(snippets).lower()
        self.assertIn("старт", joined)
        self.assertIn("кор", joined)

    def test_counter_knowledge_uses_legacy_hints(self):
        snippets = retrieve_knowledge(
            "что купить против пуджа",
            hero=self.hero,
            position=1,
            enemy_heroes=["Pudge"],
            legacy_counter_hints={"pudge": "Linken's Sphere / Lotus Orb — от хука."},
        )
        joined = " ".join(snippets)
        self.assertIn("Linken", joined)

    def test_skill_knowledge_uses_legacy_tips(self):
        snippets = retrieve_knowledge(
            "что качать на оракуле",
            hero={"name": "npc_dota_hero_oracle", "localized_name": "Oracle"},
            position=5,
            legacy_skill_tips={"oracle": "Макси Purifying Flames."},
        )
        self.assertTrue(any("Purifying" in s for s in snippets), snippets)

    def test_pick_knowledge(self):
        snippets = retrieve_knowledge(
            "кого пикнуть против медузы",
            position=1,
            enemy_heroes=["Medusa", "Zeus"],
        )
        joined = " ".join(snippets)
        self.assertIn("Контрпики", joined)

    def test_unknown_hero_gives_empty_or_generic(self):
        snippets = retrieve_knowledge("что купить", hero={"localized_name": "NoSuchHero"}, position=1)
        self.assertIsInstance(snippets, list)


class TestPromptRendering(unittest.TestCase):
    def test_personas(self):
        astra = make_system_prompt("Астра")
        jarvis = make_system_prompt("Джарвис")
        self.assertIn("7000+ MMR", astra)
        self.assertIn("J.A.R.V.I.S.", jarvis)
        self.assertIn("боевой тактический ИИ-ассистент", jarvis)

    def test_render_system_prompt_blocks(self):
        prompt = render_system_prompt(
            "Астра",
            "Роль: поз. 1. Минута 12:00.",
            ["Эталонный билд Anti-Mage: старт Tango."],
            kind="build",
        )
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", prompt)
        self.assertIn("ЗНАНИЯ ПО ВОПРОСУ", prompt)
        self.assertIn("ФОРМАТ ОТВЕТА", prompt)
        self.assertIn("1–2 предложения", prompt)


class TestCoachMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.path = self.tmp / "data" / "coach_memory.json"

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def test_roundtrip_turns_and_persistence(self):
        memory = CoachMemory(self.path)
        memory.remember("что купить на старте", "Купи танго и ветки.")
        self.assertIn("танго", memory.recall(limit=3))

        restored = CoachMemory(self.path)
        self.assertIn("что купить на старте", restored.recall(limit=3))
        self.assertIn("танго", restored.summary())

    def test_lessons_and_facts(self):
        memory = CoachMemory(self.path)
        memory.remember_lesson("Матч #1: не хватало BKB", match_id=1)
        memory.remember_fact("любимая роль", "поз. 5")
        self.assertTrue(any("BKB" in lesson for lesson in memory.lessons()))
        self.assertEqual(memory.facts()["любимая роль"], "поз. 5")
        restored = CoachMemory(self.path)
        self.assertTrue(any("BKB" in lesson for lesson in restored.lessons()))

    def test_corrupted_file_is_tolerated(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{это не json", encoding="utf-8")
        memory = CoachMemory(self.path)
        self.assertEqual(memory.recall(), "")
        memory.remember("вопрос", "ответ")
        self.assertIn("ответ", memory.recall())

    def test_disabled_path(self):
        memory = CoachMemory(None)
        memory.remember("вопрос", "ответ")
        self.assertIn("ответ", memory.recall())


class CoachIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "heroes.json").write_text(json.dumps(FAKE_HEROES, ensure_ascii=False), encoding="utf-8")
        (self.data / "items.json").write_text(
            json.dumps({"blink": "Blink Dagger", "tango": "Tango"}), encoding="utf-8"
        )
        self.routes = {
            "/heroStats": FAKE_HEROES,
            "/constants/items": ITEMS,
            "/itemPopularity": POPULARITY,
            "/api/tags": {"models": [{"name": "gemma3:latest"}]},
            "/api/chat": {"message": {"content": "Покупай Blink Dagger и дави линии."}},
        }
        self.session = SequenceSession(get_routes=self.routes)
        self.api = DotaApi(self.data, session=self.session)
        self.api.load_snapshots()
        self.profile = PlayerProfile(default_position=5)
        self.llm = LlmConfig(model="gemma3")
        self.catalog = HeroCatalog(FAKE_HEROES)

    def tearDown(self):
        remove_temp_dir(self.tmp)

    def make_coach(self, session=None, llm=None, memory_path=None):
        return Coach(
            self.api,
            self.catalog,
            FakeGsi(gsi_payload()),
            llm or self.llm,
            self.profile,
            coach_name="Астра",
            session=session or self.session,
            memory_path=memory_path,
        )

    def test_dynamic_context_has_brief_and_profile(self):
        coach = self.make_coach()
        context = coach.get_dynamic_context()
        self.assertIn("GSI Live", context)
        self.assertIn("ПРОФИЛЬ ИГРОКА", context)
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", context)
        self.assertIn("Pos 5", context)

    def test_coach_chat_uses_brain_and_cold_temperature(self):
        coach = self.make_coach()
        captured: dict = {}

        def fake_chat(prompt, system=None, **kwargs):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["kwargs"] = kwargs
            return "Купи Linken's Sphere против Пуджа."

        coach.chat = fake_chat  # type: ignore[method-assign]
        answer = coach.coach_chat("что купить против пуджа")
        self.assertIn("Linken", answer)
        self.assertLessEqual(float(captured["kwargs"]["temperature"]), 0.35)
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", captured["system"])
        self.assertIn("ЗНАНИЯ ПО ВОПРОСУ", captured["system"])
        self.assertIn("пуджа", captured["prompt"].lower())

    def test_mentioned_enemy_hero_routes_counter_knowledge(self):
        coach = self.make_coach()
        captured: dict = {}

        def fake_chat(prompt, system=None, **kwargs):
            captured["system"] = system
            return "Бери Linken's Sphere и Lotus Orb против Пуджа."

        coach.chat = fake_chat  # type: ignore[method-assign]
        coach.coach_chat("что собрать против пуджа")
        self.assertIn("Пудж", captured["system"])
        self.assertIn("Linken", captured["system"])

    def test_mentioned_hero_routes_skill_knowledge(self):
        coach = self.make_coach()
        captured: dict = {}

        def fake_chat(prompt, system=None, **kwargs):
            captured["system"] = system
            return "Максим Meat Hook."

        coach.chat = fake_chat  # type: ignore[method-assign]
        coach.coach_chat("что качать на пудже")
        self.assertIn("Прокачка", captured["system"])

    def test_coach_chat_sanitizes_answer(self):
        coach = self.make_coach()
        coach.chat = lambda *a, **k: "**Конечно!**\n1. Купи BKB\n2. Собери Manta"  # type: ignore[method-assign]
        answer = coach.coach_chat("что купить")
        self.assertNotIn("**", answer)
        self.assertNotIn("Конечно", answer)
        self.assertNotIn("\n", answer)

    def test_coach_chat_persists_memory(self):
        memory_path = self.data / "coach_memory.json"
        coach = self.make_coach(memory_path=memory_path)
        coach.chat = lambda *a, **k: "Купи Tango и ветки."  # type: ignore[method-assign]
        coach.coach_chat("что купить на старте")
        self.assertTrue(memory_path.is_file())
        restored = CoachMemory(memory_path)
        self.assertIn("старте", restored.recall())

    def test_start_items_fallback_uses_knowledge_base(self):
        self.routes["/api/tags"] = RuntimeError("LLM offline")
        session = SequenceSession(get_routes=self.routes)
        coach = self.make_coach(session=session)
        coach.chat = lambda *a, **k: (_ for _ in ()).throw(__import__("aidota.coach", fromlist=["OllamaError"]).OllamaError("offline"))  # type: ignore[method-assign]
        answer = coach.start_items(self.api.hero_by_id(1))
        self.assertIn("На старте для", answer)
        self.assertNotIn("лейт", answer.lower())

    def test_execute_new_tools(self):
        coach = self.make_coach()
        skill_answer = coach.execute_tool("get_hero_skill_order", {"hero_name": "Oracle"})
        self.assertTrue("Прокачка" in skill_answer or "Purifying" in skill_answer)
        knowledge = coach.execute_tool("search_coach_knowledge", {"query": "что купить против пуджа"})
        self.assertTrue(len(knowledge) > 10)

    def test_tool_loop_multi_round_openai(self):
        responses = [
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call_1",
                "function": {"name": "get_hero_counters", "arguments": "{\"hero_name\": \"Pudge\"}"},
            }]}}]},
            {"choices": [{"message": {"content": "Бери Linken's Sphere против Пуджа."}}]},
        ]
        session = SequenceSession(post_responses=responses)
        llm = LlmConfig(
            provider="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            model="deepseek-chat",
        )
        coach = self.make_coach(session=session, llm=llm)
        answer = coach.chat("что купить против пуджа")
        self.assertIn("Linken", answer)
        self.assertGreaterEqual(len(session.payloads), 2)
        self.assertIn("tools", session.payloads[0])
        second_messages = session.payloads[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in second_messages))

    def test_failover_to_free_ai(self):
        routes = {"/api/tags": RuntimeError("Ollama offline")}
        session = SequenceSession(
            get_routes=routes,
            post_responses=[{"choices": [{"message": {"content": "Резервный ответ."}}]}],
        )
        coach = self.make_coach(session=session)
        self.assertEqual(coach.chat("привет"), "Резервный ответ.")

    def test_analyze_match_saves_lesson(self):
        raw_match = {
            "match_id": 99999,
            "radiant_win": True,
            "duration": 2400,
            "radiant_score": 30,
            "dire_score": 20,
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
        session = SequenceSession(
            get_routes={"/matches/99999": raw_match},
            post_responses=[{"message": {"content": "Хорошая игра."}}],
        )
        self.api._session = session
        coach = self.make_coach(session=session)
        res = coach.analyze_match(match_id=99999, account_id=123456)
        self.assertIn("РАЗБОР МАТЧА #99999", res)
        self.assertTrue(coach.long_memory.lessons())
        self.assertIn("99999", coach.long_memory.lessons()[-1])


if __name__ == "__main__":
    unittest.main()
