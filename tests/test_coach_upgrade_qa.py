# -*- coding: utf-8 -*-
"""Независимые сквозные QA-тесты «умного» ИИ-тренера AIDOTA (контракт D).

Проверяют тренера глазами пользователя, а не внутренности модулей:

- конкретные контр-предметы из базы знаний по 10+ героям (и через ``execute_tool``);
- ``recommend_picks`` на 8+ драфтах: роль, границы score, структура ответа,
  исключение взятых/забаненных героев;
- ``Coach.coach_chat``: в системный промпт попадают «ТАКТИЧЕСКИЙ БРИФ»,
  «ЗНАНИЯ ПО ВОПРОСУ» и «ПРОФИЛЬ ИГРОКА»; знания идут именно по упомянутому
  в вопросе врагу; ответ санитизирован для озвучки;
- умные fallback без LLM (ответ из базы знаний, а не общая фраза);
- долговременная память: переживает пересоздание Coach, уроки матча из
  ``analyze_match`` сохраняются между запусками;
- GSI: полный/пустой/кривой payload не роняет снимок, новые ключи на месте,
  ``enemy_threats`` осмысленны для Huskar / Phantom Assassin / Doom;
- голосовую дисциплину: «холодная» температура фактических вопросов и
  увеличенный бюджет токенов для разбора матча;
- README: заявленные возможности умного тренера подтверждаются кодом.

Сеть не используется: только локальные ``data/*.json``, fake-сессии и
подмена ``Coach.chat``. Временные каталоги — ``tests.helpers.make_temp_dir``.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from aidota.coach import Coach, OllamaError
from aidota.coach_brain import detect_query_kind, is_voice_safe, sanitize_answer
from aidota.coach_memory import CoachMemory
from aidota.config import LlmConfig, PlayerProfile
from aidota.context import (
    analyze_enemy_threats,
    format_gsi_summary,
    get_live_gsi_snapshot,
)
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.meta_db import (
    META_HEROES_DB,
    get_hero_counters,
    get_hero_meta_entry,
    recommend_picks,
)
from tests.helpers import (
    FakeResponse,
    gsi_payload,
    make_temp_dir,
    remove_temp_dir,
)

ROOT = Path(__file__).resolve().parents[1]
HEROES_JSON = ROOT / "data" / "heroes.json"
ITEMS_JSON = ROOT / "data" / "items.json"

# --------------------------------------------------------------------------- #
# Локальные данные проекта (сеть не нужна): проверяем, что коды предметов и
# имена героев из базы знаний существуют в снапшотах Dota 2.
# --------------------------------------------------------------------------- #
CATALOG_HEROES: list[dict] = json.loads(HEROES_JSON.read_text(encoding="utf-8"))
KNOWN_HERO_NAMES = {str(hero["localized_name"]) for hero in CATALOG_HEROES}
# Имя героя -> короткий id: используется для проверки, что знания находятся по имени.
KNOWN_HERO_IDS_ID = {
    str(meta["name"]): hero_id for hero_id, meta in META_HEROES_DB.items()
}
KNOWN_ITEM_NAMES = {
    str(value.get("dname"))
    for value in json.loads(ITEMS_JSON.read_text(encoding="utf-8")).values()
    if isinstance(value, dict) and value.get("dname")
}

# «Умные» контр-предметы, которые пользователь ожидает услышать про героя.
# Для двух последних героев допускаем несколько вариантов: в тегах базы знаний
# предметы сгенерированы по тегам угроз, а не вручную, и QA фиксирует, что
# пользователь в любом случае получает осмысленный контр-предмет.
EXPECTED_COUNTER_ITEMS: dict[str, tuple[str, ...]] = {
    "Phantom Assassin": ("Monkey King Bar",),
    "Huskar": ("Spirit Vessel",),
    "Doom": ("Linken's Sphere",),
    "Medusa": ("Eye of Skadi", "Diffusal Blade"),
    "Storm Spirit": ("Orchid Malevolence",),
    "Phantom Lancer": ("Mjollnir", "Shiva's Guard"),
    "Bristleback": ("Silver Edge",),
    "Zeus": ("Pipe of Insight",),
    "Sniper": ("Silver Edge", "Blade Mail", "Blink Dagger"),
    "Axe": ("Spirit Vessel", "Eul's Scepter of Divinity", "Aeon Disk", "Linken's Sphere"),
}

# Инструмент ``get_hero_counters`` отдаёт первые 4 предмета из базы (для голоса).
# Здесь фиксируем то, что пользователь реально услышит в ответе инструмента.
TOOL_COUNTER_EXPECTED: dict[str, str] = {
    "Phantom Assassin": "Monkey King Bar",
    "Huskar": "Spirit Vessel",
    "Doom": "Linken's Sphere",
    "Medusa": "Eye of Skadi",
    "Storm Spirit": "Orchid Malevolence",
    "Phantom Lancer": "Mjollnir",
    "Bristleback": "Spirit Vessel",
    "Zeus": "Pipe of Insight",
    "Sniper": "Heaven's Halberd",
    "Axe": "Spirit Vessel",
}

# Мини-каталог героев для Coach и GSI (без сети).
COACH_HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage",
     "roles": ["Carry"], "pro_pick": 50, "pro_win": 25, "pub_pick": 1000, "pub_win": 520},
    {"id": 2, "name": "npc_dota_hero_pudge", "localized_name": "Pudge",
     "roles": ["Initiator"], "pub_pick": 1000, "pub_win": 500},
    {"id": 11, "name": "npc_dota_hero_nevermore", "localized_name": "Shadow Fiend",
     "roles": ["Carry"], "pub_pick": 600, "pub_win": 300},
    {"id": 15, "name": "npc_dota_hero_razor", "localized_name": "Razor",
     "roles": ["Carry"], "pub_pick": 500, "pub_win": 250},
    {"id": 44, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin",
     "roles": ["Carry"], "pub_pick": 500, "pub_win": 250},
    {"id": 59, "name": "npc_dota_hero_huskar", "localized_name": "Huskar",
     "roles": ["Carry"], "pub_pick": 500, "pub_win": 250},
    {"id": 69, "name": "npc_dota_hero_doom_bringer", "localized_name": "Doom",
     "roles": ["Carry"], "pub_pick": 500, "pub_win": 250},
]

COACH_ITEMS = {
    "blink": "Blink Dagger",
    "tango": "Tango",
    "branches": "Iron Branch",
    "boots": "Boots of Speed",
    "bfury": "Battle Fury",
    "manta": "Manta Style",
    "power_treads": "Power Treads",
    "tpscroll": "Town Portal Scroll",
    "occult_bracelet": "Occult Bracelet",
}
ITEM_POPULARITY = {
    "start_game_items": {"tango": 100, "branches": 80},
    "early_game_items": {"boots": 50},
    "mid_game_items": {},
    "late_game_items": {"blink": 10},
}

# GSI-каталог для радара угроз: свои + враги, которых проверяем.
THREAT_CATALOG_HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
    {"id": 44, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin"},
    {"id": 59, "name": "npc_dota_hero_huskar", "localized_name": "Huskar"},
    {"id": 69, "name": "npc_dota_hero_doom_bringer", "localized_name": "Doom"},
]

THREAT_ITEMS = {
    "power_treads": "Power Treads",
    "bfury": "Battle Fury",
    "manta": "Manta Style",
    "blink": "Blink Dagger",
    "black_king_bar": "BKB",
    "occult_bracelet": "Occult Bracelet",
    "tpscroll": "Town Portal Scroll",
}

_DEFAULT = object()


class FakeGsi:
    """Минимальный GSI-провайдер: отдаёт заданный payload или None."""

    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 1.5


class SequenceSession:
    """Fake ``requests.Session``: GET отвечает по подстрокам URL, POST — из очереди."""

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

    def make_coach(self, *, payload=_DEFAULT, memory_path=_DEFAULT) -> Coach:
        gsi = FakeGsi(gsi_payload() if payload is _DEFAULT else payload)
        memory = self.memory_path if memory_path is _DEFAULT else memory_path
        return Coach(
            self.api,
            self.catalog,
            gsi,
            self.llm,
            self.profile,
            coach_name="Астра",
            session=self.session,
            memory_path=memory,
        )

    def install_chat(self, coach: Coach, answer: str) -> dict:
        """Подменяет ``coach.chat`` и перехватывает промпт/систему/kwargs."""
        captured: dict = {"calls": []}

        def fake_chat(prompt, system=None, **kwargs):
            captured["calls"].append({"prompt": prompt, "system": system, "kwargs": kwargs})
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
# 1. База знаний: конкретные контр-предметы по 10+ героям
# --------------------------------------------------------------------------- #
class TestHeroCounterKnowledge(unittest.TestCase):
    def test_ten_heroes_have_concrete_counter_items(self):
        for hero_name, expected in EXPECTED_COUNTER_ITEMS.items():
            with self.subTest(hero=hero_name):
                counters = get_hero_counters(hero_name)
                self.assertIsNotNone(counters, f"нет знаний по {hero_name}")
                self.assertEqual(counters["hero_id"], KNOWN_HERO_IDS_ID[hero_name])

                items = [str(item) for item in counters["counter_items"]]
                self.assertTrue(items, f"{hero_name}: пустой список контр-предметов")
                self.assertTrue(
                    any(item in items for item in expected),
                    f"{hero_name}: ждали один из {expected}, получили {items}",
                )
                for item in items:
                    self.assertIn(item, KNOWN_ITEM_NAMES, f"{hero_name}: выдуманный предмет {item!r}")

                self.assertGreaterEqual(len(counters["counter_heroes"]), 2, hero_name)
                for name in counters["counter_heroes"]:
                    self.assertIn(name, KNOWN_HERO_NAMES, f"{hero_name}: неизвестный герой {name!r}")

                self.assertTrue(str(counters["tip"]).strip(), f"{hero_name}: пустой tip")
                self.assertRegex(counters["tip"], r"[а-яё]", msg=f"{hero_name}: tip не по-русски")


# --------------------------------------------------------------------------- #
# 2. recommend_picks: 8+ драфтов, роли, score, структура, исключения
# --------------------------------------------------------------------------- #
DRAFT_CASES: list[dict] = [
    {"enemies": ["medusa", "zuus"], "role": 1},
    {"enemies": ["phantom_assassin"], "role": 2, "allies": ["axe"], "banned": ["juggernaut"]},
    {"enemies": ["huskar"], "role": 1, "allies": ["crystal_maiden"], "banned": ["PA"]},
    {"enemies": ["storm_spirit", "axe"], "role": 3, "allies": ["crystal_maiden"]},
    {"enemies": ["sniper"], "role": 4, "banned": ["lion"]},
    {"enemies": ["bristleback", "doom_bringer"], "role": 2, "allies": ["shadow_shaman"],
     "banned": ["medusa"]},
    {"enemies": ["weaver"], "role": 5},
    {"enemies": ["tinker"], "role": 3, "allies": ["lion"], "banned": ["phantom_assassin"]},
]

PICK_REQUIRED_FIELDS = (
    "hero_id", "name", "ru_name", "score", "role", "role_name",
    "strong_against", "weak_against", "combos", "core_items",
    "shard", "advice", "countered_by_preview",
)


def _resolve_known_id(name: str) -> str | None:
    meta = get_hero_meta_entry(name)
    return str(meta["id"]) if meta else None


class TestRecommendPicksDrafts(unittest.TestCase):
    def test_eight_drafts_respect_role_and_structure(self):
        for case in DRAFT_CASES:
            enemies = case["enemies"]
            role = case["role"]
            picks = recommend_picks(
                enemies,
                ally_heroes=case.get("allies"),
                role=role,
                top_k=5,
                banned_heroes=case.get("banned"),
            )
            with self.subTest(enemies=enemies, role=role):
                self.assertTrue(picks, "нет рекомендаций")
                self.assertLessEqual(len(picks), 5)
                scores = [pick["score"] for pick in picks]
                self.assertEqual(scores, sorted(scores, reverse=True), "пики не отсортированы")
                for pick in picks:
                    for field in PICK_REQUIRED_FIELDS:
                        self.assertIn(field, pick, f"{pick.get('hero_id')}: нет поля {field}")
                    self.assertEqual(pick["role"], role)
                    self.assertIn(role, META_HEROES_DB[pick["hero_id"]]["roles"])
                    self.assertIsInstance(pick["score"], int)
                    self.assertGreater(pick["score"], 0)
                    self.assertLessEqual(pick["score"], 100)
                    self.assertTrue(pick["strong_against"])
                    self.assertTrue(pick["core_items"])
                    self.assertGreaterEqual(len(pick["countered_by_preview"]), 2)
                    self.assertLessEqual(len(pick["countered_by_preview"]), 3)
                    for name in pick["countered_by_preview"]:
                        self.assertIn(name, KNOWN_HERO_NAMES)

    def test_drafts_exclude_taken_and_banned_heroes(self):
        for case in DRAFT_CASES:
            offered = {
                pick["hero_id"]
                for pick in recommend_picks(
                    case["enemies"],
                    ally_heroes=case.get("allies"),
                    role=case["role"],
                    top_k=50,
                    banned_heroes=case.get("banned"),
                )
            }
            with self.subTest(enemies=case["enemies"], role=case["role"]):
                for name in case["enemies"]:
                    hero_id = _resolve_known_id(name)
                    self.assertIsNotNone(hero_id, name)
                    self.assertNotIn(hero_id, offered, f"враг {name} предложен в пики")
                for name in case.get("allies") or []:
                    hero_id = _resolve_known_id(name)
                    self.assertIsNotNone(hero_id, name)
                    self.assertNotIn(hero_id, offered, f"союзник {name} предложен повторно")
                for name in case.get("banned") or []:
                    hero_id = _resolve_known_id(name)
                    self.assertIsNotNone(hero_id, name)
                    self.assertNotIn(hero_id, offered, f"забаненный {name} предложен в пики")

    def test_counterpick_of_medusa_zeus_is_on_top_and_bad_drafts_do_not_crash(self):
        picks = recommend_picks(["medusa", "zuus"], role=1, top_k=5)
        self.assertEqual(picks[0]["hero_id"], "antimage", "Антимаг обязан контрить Медузу и Зевса")

        for enemies in ([], None, "medusa,zuus", ["Несуществующий Герой"]):
            with self.subTest(enemies=repr(enemies)):
                picks = recommend_picks(enemies, role=1, top_k=3)  # type: ignore[arg-type]
                self.assertEqual(len(picks), 3)
                for pick in picks:
                    self.assertEqual(pick["role"], 1)
                    self.assertGreater(pick["score"], 0)
                    self.assertLessEqual(pick["score"], 100)


# --------------------------------------------------------------------------- #
# 3. coach_chat: промпт, знания по упомянутому герою, санитайзер ответа
# --------------------------------------------------------------------------- #
class TestCoachChatIntelligence(CoachCase):
    def test_system_prompt_contains_brief_knowledge_and_profile(self):
        coach = self.make_coach()
        captured = self.install_chat(coach, "Купи Battle Fury и держи темп фарма.")
        answer = coach.coach_chat("что купить на старте")

        system = captured["system"]
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", system)
        self.assertIn("ЗНАНИЯ ПО ВОПРОСУ:", system)
        self.assertIn("ПРОФИЛЬ ИГРОКА", system)
        # Знания под текущего героя (Anti-Mage из GSI) — не пустая заготовка.
        self.assertIn("Эталонный билд", system)
        self.assertIn("старт", system)
        self.assertIn("что купить на старте", captured["prompt"].lower())
        self.assertEqual(answer, "Купи Battle Fury и держи темп фарма.")

    def test_system_prompt_contains_grounded_item_advice(self):
        """Живой совет по закупке с причиной и врагом попадает в системный промпт."""
        payload = gsi_payload()
        payload["items"] = payload["hero"].pop("items")  # top-level, как в реальном GSI
        payload["draft"]["team3"]["pick_ids"] = [44]  # Phantom Assassin — враг
        coach = self.make_coach(payload=payload)
        # Эмулируем DotaApi.item_costs() из контракта v1.2.
        coach.api.item_costs = lambda: {  # type: ignore[method-assign]
            "Monkey King Bar": 4050,
            "Silver Edge": 5100,
            "Heaven's Halberd": 3400,
        }
        captured = self.install_chat(coach, "Бери Monkey King Bar против Фантомки.")
        coach.coach_chat("что купить против фантомки")

        system = captured["system"]
        self.assertIn("Закупка", system, "в промпте должен быть живой совет по закупке")
        self.assertIn("Phantom Assassin", system, "причина совета привязана к врагу")
        self.assertIn("4050", system, "цена предмета из api.item_costs()")

    def test_voice_answer_is_sanitized(self):
        coach = self.make_coach()
        raw = "**Конечно**, отличный вопрос!\n1. Купи BKB 😊\n2. Собери Manta Style"
        self.install_chat(coach, raw)
        answer = coach.coach_chat("что купить против пуджа")

        self.assertNotIn("**", answer)
        self.assertNotIn("Конечно", answer)
        self.assertNotIn("отличный вопрос", answer.lower())
        self.assertNotIn("\n", answer)
        self.assertNotIn("😊", answer)
        self.assertNotRegex(answer, r"\d+[.)]\s")
        self.assertLessEqual(len(answer), 240)
        self.assertLessEqual(len(re.findall(r"[.!?…]+", answer)), 2)
        self.assertTrue(answer[:1].isupper(), answer)
        self.assertTrue(is_voice_safe(answer), answer)
        self.assertIn("BKB", answer)

    def test_mentioned_enemy_routes_counter_knowledge(self):
        coach = self.make_coach()
        captured = self.install_chat(coach, "Бери Monkey King Bar и Silver Edge против Фантомки.")
        coach.coach_chat("что собрать против фантомки")

        system = captured["system"]
        self.assertIn("Фантомка", system, "знания должны идти по упомянутой Фантомке")
        self.assertIn("Monkey King Bar", system)
        # Врагов из текущего драфта тоже держим в контексте.
        self.assertIn("Угрозы", system)

    def test_mentioned_hero_routes_skill_knowledge(self):
        coach = self.make_coach()
        captured = self.install_chat(coach, "Макси Meat Hook и качай Rot вторым.")
        coach.coach_chat("что качать на пудже")
        self.assertIn("Прокачка", captured["system"])
        self.assertIn("Meat Hook", captured["system"])

    def test_llm_refusal_falls_back_to_knowledge_base(self):
        coach = self.make_coach()
        captured = self.install_chat(coach, "Извините, но я не могу помочь с этим вопросом.")
        answer = coach.coach_chat("что купить против хускара")

        self.assertIn("Spirit Vessel", answer, "fallback обязан назвать контр-предмет из базы")
        self.assertIn("Хускар", answer)
        self.assertNotIn("Извините", answer)
        self.assertNotIn("Нет данных для точного совета", answer)
        self.assertEqual(len(captured["calls"]), 2, "после «пустого» ответа должен быть retry")

    def test_unknown_hero_gives_honest_fallback(self):
        coach = self.make_coach(payload=None)
        captured = self.install_chat(coach, "Как языковая модель, я не могу ответить.")
        answer = coach.coach_chat("что собрать против несуществующего героя")

        self.assertIn("Нет данных для точного совета", answer)
        self.assertNotIn("Monkey King Bar", answer)
        self.assertNotIn("Spirit Vessel", answer)
        self.assertEqual(len(captured["calls"]), 2)
        # Даже без GSI бриф и профиль остаются в промпте.
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", captured["system"])
        self.assertIn("Матч не запущен", captured["system"])

    def test_works_without_gsi(self):
        coach = self.make_coach(payload=None)
        captured = self.install_chat(coach, "Купи BKB и держи линию.")
        answer = coach.coach_chat("что купить")
        self.assertEqual(answer, "Купи BKB и держи линию.")
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", captured["system"])
        self.assertIn("ПРОФИЛЬ ИГРОКА", captured["system"])


# --------------------------------------------------------------------------- #
# 4. Голосовая дисциплина: холодная температура и бюджет токенов
# --------------------------------------------------------------------------- #
class TestVoiceDiscipline(CoachCase):
    FACTUAL_QUESTIONS = [
        ("что купить на пуджа", "build"),
        ("что собрать против пуджа", "counter"),
        ("как качать на пудже", "skill"),
        ("кого пикнуть против пуджа", "pick"),
        ("что делать на линии", "lane"),
        ("когда идти на рошана", "timing"),
        ("почему бкб не спасает", "why"),
        ("разбери мою игру", "analysis"),
    ]

    def test_factual_questions_use_cold_temperature(self):
        coach = self.make_coach()
        for query, expected_kind in self.FACTUAL_QUESTIONS:
            with self.subTest(query=query):
                self.assertEqual(detect_query_kind(query), expected_kind)
                captured = self.install_chat(coach, "Купи BKB и играй от вижна.")
                coach.coach_chat(query)
                temperature = float(captured["kwargs"]["temperature"])
                self.assertLessEqual(temperature, 0.35, query)
                self.assertGreaterEqual(temperature, 0.15, query)

    def test_creative_chat_temperature_is_bounded(self):
        coach = self.make_coach()
        self.assertEqual(detect_query_kind("привет"), "chat")
        captured = self.install_chat(coach, "Привет! Готов помочь.")
        coach.coach_chat("привет")
        temperature = float(captured["kwargs"]["temperature"])
        self.assertGreaterEqual(temperature, 0.35)
        self.assertLessEqual(temperature, 0.6)

    def test_sanitizer_cuts_length_sentences_and_refusals(self):
        long_text = (
            "Против Фантомки бери Monkey King Bar, Ghost Scepter или Solar Crest, "
            "чтобы пробивать уворот и не умирать от критов на линии и в драке, "
            "а потом докупи Silver Edge и выходи на драки через вижн."
        )
        clean = sanitize_answer(long_text, max_chars=120, max_sentences=2)
        self.assertTrue(clean)
        self.assertLessEqual(len(clean), 122, clean)
        self.assertLessEqual(len(re.findall(r"[.!?…]+", clean)), 2, clean)
        self.assertTrue(clean[:1].isupper(), clean)

        # Отказ LLM и пустота превращаются в пустую строку -> Coach берёт fallback.
        self.assertEqual(sanitize_answer("Извините, но я не могу помочь с этим вопросом."), "")
        self.assertEqual(sanitize_answer(""), "")
        self.assertEqual(sanitize_answer(None), "")  # type: ignore[arg-type]

    def test_match_analysis_uses_long_answer_budget(self):
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
                "deaths": 12,
                "assists": 7,
                "gold_per_min": 400,
                "xp_per_min": 500,
                "hero_damage": 5000,
                "tower_damage": 300,
                "benchmarks": {
                    "gpm_pct": 0.2, "xpm_pct": 0.3, "hero_damage_pct": 0.2,
                    "tower_damage_pct": 0.1, "last_hits_pct": 0.2, "deaths_pct": 0.1,
                },
            }],
        }
        self.session.get_routes["/matches/99999"] = raw_match
        coach = self.make_coach()
        captured = self.install_chat(coach, "Много смертей. Купи BKB раньше и играй от вижна.")
        result = coach.analyze_match(match_id=99999, account_id=123456)

        self.assertIn("РАЗБОР МАТЧА #99999", result)
        self.assertIn("Anti-Mage", result)
        self.assertGreaterEqual(int(captured["kwargs"]["max_tokens"]), 550)
        self.assertLessEqual(float(captured["kwargs"]["temperature"]), 0.35)


# --------------------------------------------------------------------------- #
# 5. Долговременная память: переживает пересоздание Coach, уроки матчей
# --------------------------------------------------------------------------- #
class TestCoachMemoryPersistence(CoachCase):
    def test_dialog_memory_survives_coach_recreation(self):
        coach = self.make_coach()
        self.install_chat(coach, "Купи Tango и ветки.")
        coach.coach_chat("что купить на старте")

        self.assertTrue(self.memory_path.is_file(), "память должна лежать на диске")
        saved = json.loads(self.memory_path.read_text(encoding="utf-8"))
        self.assertTrue(saved.get("turns"), saved)

        restored = self.make_coach()
        self.assertIn("Tango", restored.long_memory.recall())
        self.assertIsInstance(restored.long_memory, CoachMemory)

        captured = self.install_chat(restored, "Держи линию и фарми.")
        restored.coach_chat("что дальше")
        self.assertIn("Купи Tango и ветки", captured["system"], "память должна попасть в промпт")

    def test_match_lesson_and_profile_survive_restart(self):
        raw_match = {
            "match_id": 55555,
            "radiant_win": False,
            "duration": 2100,
            "radiant_score": 12,
            "dire_score": 30,
            "players": [{
                "account_id": 777,
                "player_slot": 0,
                "hero_id": 1,
                "kills": 2,
                "deaths": 11,
                "assists": 3,
                "gold_per_min": 350,
                "xp_per_min": 400,
                "hero_damage": 3000,
                "tower_damage": 200,
                "benchmarks": {
                    "gpm_pct": 0.15, "xpm_pct": 0.2, "hero_damage_pct": 0.15,
                    "tower_damage_pct": 0.1, "last_hits_pct": 0.2, "deaths_pct": 0.05,
                },
            }],
        }
        self.session.get_routes["/matches/55555"] = raw_match
        coach = self.make_coach()
        self.install_chat(coach, "Слишком много смертей. Купи BKB и играй от вижна.")
        coach.analyze_match(match_id=55555, account_id=777)

        lessons = coach.long_memory.lessons()
        self.assertTrue(lessons, "analyze_match должен запомнить урок")
        self.assertTrue(any("55555" in lesson for lesson in lessons), lessons)

        profile_path = self.data / "player_profile.json"
        self.assertTrue(profile_path.is_file(), "профиль игрока должен сохраниться")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertTrue(profile.get("common_mistakes"), profile)
        self.assertIn("Anti-Mage", profile.get("signature_heroes") or [])

        restored = self.make_coach()
        self.assertTrue(
            any("55555" in lesson for lesson in restored.long_memory.lessons()),
            "урок матча должен пережить пересоздание Coach",
        )


# --------------------------------------------------------------------------- #
# 6. Инструменты тренера и fallback без LLM
# --------------------------------------------------------------------------- #
class TestCoachToolsAndFallbacks(CoachCase):
    def test_counter_tool_knows_ten_heroes(self):
        coach = self.make_coach()
        for hero_name, expected_item in TOOL_COUNTER_EXPECTED.items():
            with self.subTest(hero=hero_name):
                answer = coach.execute_tool("get_hero_counters", {"hero_name": hero_name})
                self.assertNotIn("не найден", answer)
                self.assertIn(
                    expected_item, answer,
                    f"{hero_name}: в озвученном ответе инструмента нет {expected_item!r}: {answer}",
                )

    def test_tools_return_structured_knowledge_and_honest_misses(self):
        coach = self.make_coach()

        counters = coach.execute_tool("get_hero_counters", {"hero_name": "Phantom Assassin"})
        self.assertIn("Monkey King Bar", counters)
        self.assertIn("Прот", counters)

        unknown = coach.execute_tool("get_hero_counters", {"hero_name": "Несуществующий Герой"})
        self.assertIn("не найден", unknown)

        skill = coach.execute_tool("get_hero_skill_order", {"hero_name": "Pudge"})
        self.assertIn("Прокачка", skill)
        self.assertIn("Meat Hook", skill)

        build = coach.execute_tool("get_hero_item_build", {"hero_name": "Anti-Mage"})
        self.assertIn("Battle Fury", build)
        self.assertIn("Старт", build)

        picks = coach.execute_tool(
            "recommend_draft_picks", {"enemy_heroes": ["Phantom Assassin"], "role": 1}
        )
        self.assertIn("%", picks)
        self.assertIn("\n", picks)

    def test_fallback_answers_come_from_database(self):
        coach = self.make_coach()
        self.install_failing_chat(coach)
        advice = coach.hero_intro_advice(self.api.hero_by_id(1))
        self.assertIn("Anti-Mage", advice)
        self.assertIn("Tango", advice)
        self.assertNotIn("LLM offline", advice)

        # Стартовый закуп тоже обязан идти из базы знаний, а не общей фразой.
        start = coach.start_items(self.api.hero_by_id(1))
        self.assertIn("Anti-Mage", start)
        self.assertIn("Tango", start)


# --------------------------------------------------------------------------- #
# 7. GSI: полный/пустой/кривой payload и осмысленные угрозы
# --------------------------------------------------------------------------- #
def threat_catalog() -> HeroCatalog:
    return HeroCatalog(THREAT_CATALOG_HEROES)


def full_gsi_payload() -> dict:
    payload = gsi_payload()
    payload["draft"] = {
        "team2": {"pick_ids": [1]},
        "team3": {"pick_ids": [59, 44, 69]},  # Huskar, Phantom Assassin, Doom
    }
    payload["allplayers"] = {
        "player0": {
            "name": "Player",
            "team_name": "radiant",
            "hero": {
                "name": "npc_dota_hero_antimage",
                "level": 12,
                "kills": 4,
                "deaths": 1,
                "assists": 2,
                "alive": True,
                "respawn_seconds": 0,
                "net_worth": 9000,
                "items": {
                    "slot0": {"name": "item_power_treads"},
                    "slot1": {"name": "item_bfury"},
                },
            },
        },
        "player5": {
            "name": "Enemy",
            "team_name": "dire",
            "hero": {
                "name": "npc_dota_hero_phantom_assassin",
                "level": 11,
                "kills": 3,
                "deaths": 2,
                "assists": 1,
                "alive": False,
                "respawn_seconds": 20,
                "net_worth": 8000,
                "items": {
                    "slot0": {"name": "item_blink"},
                    "slot1": {"name": "item_black_king_bar"},
                },
            },
        },
    }
    return payload


class TestGsiSnapshotQA(unittest.TestCase):
    NEW_KEYS = (
        "hero_items_display", "neutral_item", "aghanims_scepter", "aghanims_shard",
        "buyback_cost", "buyback_available", "has_tp", "my_team_players",
        "enemy_players", "enemy_threats", "objectives",
    )

    def test_full_payload_new_keys_and_meaningful_threats(self):
        payload = full_gsi_payload()
        catalog = threat_catalog()
        snapshot = get_live_gsi_snapshot(payload, catalog, THREAT_ITEMS, position=1)

        self.assertTrue(snapshot["available"])
        for key in self.NEW_KEYS:
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["hero_items_display"], ["Power Treads", "Battle Fury", "Manta Style"])
        self.assertEqual(snapshot["inventory"], snapshot["hero_items_display"])
        self.assertEqual(snapshot["neutral_item"], "Occult Bracelet")
        self.assertEqual(snapshot["buyback_cost"], 300)
        self.assertTrue(snapshot["buyback_available"])
        self.assertFalse(snapshot["has_tp"])
        self.assertEqual(snapshot["enemy_players"][0]["items"], ["Blink Dagger", "BKB"])
        self.assertTrue(snapshot["objectives"].startswith("Ближайшие цели"))

        threats = snapshot["enemy_threats"]
        self.assertGreaterEqual(len(threats), 3)
        self.assertLessEqual(len(threats), 5)
        joined = " ".join(threats)
        # Huskar -> срез регена, PA -> пробитие уворота, Doom -> Linken/Lotus.
        self.assertIn("Huskar", joined)
        self.assertIn("Spirit Vessel", joined)
        self.assertIn("Phantom Assassin", joined)
        self.assertIn("Monkey King Bar", joined)
        self.assertIn("Doom", joined)
        self.assertIn("Linken", joined)
        self.assertIn("BKB", joined, "видимые предметы врага тоже должны попасть в радар")
        self.assertIn("GSI Снимок", format_gsi_summary(snapshot))

    def test_empty_and_broken_payloads_never_crash(self):
        catalog = threat_catalog()
        empty = get_live_gsi_snapshot(None, catalog, THREAT_ITEMS)
        self.assertFalse(empty["available"])
        self.assertEqual(empty["enemy_threats"], [])
        self.assertEqual(empty["my_team_players"], [])
        self.assertEqual(empty["hero_items_display"], [])
        self.assertIsNone(empty["neutral_item"])
        self.assertIsNone(empty["buyback_cost"])
        self.assertEqual(empty["objectives"], "")

        garbage_payloads = [
            "junk",
            [1, 2, 3],
            {},
            {"map": "oops", "hero": [1, 2], "player": 5},
            {"hero": {"items": {"slot0": 5}}, "map": {"clock_time": "abc"}},
            {"draft": "junk", "allplayers": "junk", "players": [1, 2]},
            {"draft": {"team3": {"pick_ids": "junk"}}, "allplayers": {"player0": {"hero": "junk"}}},
        ]
        for payload in garbage_payloads:
            with self.subTest(payload=repr(payload)):
                snapshot = get_live_gsi_snapshot(payload, catalog, THREAT_ITEMS)  # type: ignore[arg-type]
                self.assertIsInstance(snapshot, dict)
                for key in self.NEW_KEYS:
                    self.assertIn(key, snapshot)
                self.assertIsInstance(snapshot["enemy_threats"], list)
                self.assertLessEqual(len(snapshot["enemy_threats"]), 5)
                self.assertIsInstance(format_gsi_summary(snapshot), str)

        # Сам анализатор угроз на мусоре обязан вернуть пустой список.
        self.assertEqual(analyze_enemy_threats(None, catalog, THREAT_ITEMS), [])
        self.assertEqual(analyze_enemy_threats("junk", catalog, THREAT_ITEMS), [])
        self.assertEqual(analyze_enemy_threats([], catalog, THREAT_ITEMS), [])
        self.assertEqual(analyze_enemy_threats({}, catalog, THREAT_ITEMS), [])


# --------------------------------------------------------------------------- #
# 8. README: заявленные возможности умного тренера подтверждаются кодом
# --------------------------------------------------------------------------- #
class TestReadmeClaims(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_smart_coach_section_documented(self):
        self.assertIn("Умный ИИ-тренер", self.readme)
        readme_lower = self.readme.lower()
        for needle in ("coach_brain.py", "coach_memory.py", "127", "data/coach_memory.json",
                       "санитайзер", "tool-loop", "fallback", "резерв"):
            self.assertIn(needle.lower(), readme_lower, f"README не описывает {needle!r}")
        self.assertIn("COACH_UPGRADE.md", self.readme)

    def test_readme_claims_match_code(self):
        # «База знаний на всех 127 героев» — реально покрываем всех героев снапшота.
        self.assertEqual(len(CATALOG_HEROES), 127)
        self.assertEqual(
            set(META_HEROES_DB),
            {str(hero["name"]).replace("npc_dota_hero_", "") for hero in CATALOG_HEROES},
        )
        self.assertIn("127", self.readme)

        # Модули и документ из README существуют.
        for relative in ("aidota/coach_brain.py", "aidota/coach_memory.py", "docs/COACH_UPGRADE.md"):
            self.assertTrue((ROOT / relative).is_file(), f"README ссылается на несуществующий {relative}")

        # Память по умолчанию — <data_dir>/coach_memory.json, файл создаётся при живой сессии.
        tmp = make_temp_dir()
        try:
            api = DotaApi(tmp, session=SequenceSession())
            coach = Coach(
                api, HeroCatalog(COACH_HEROES), None, LlmConfig(model="gemma3"),
                session=SequenceSession(),
            )
            self.assertEqual(coach.long_memory.path, tmp / "coach_memory.json")
            self.assertFalse((tmp / "coach_memory.json").exists())
            coach.remember("вопрос", "ответ")
            self.assertTrue((tmp / "coach_memory.json").is_file())
        finally:
            remove_temp_dir(tmp)

        # Умные fallback/failover-хуки из README реально существуют.
        for name in ("_knowledge_fallback", "_fallback_match_analysis",
                     "_free_pollinations_chat", "execute_tool"):
            self.assertTrue(callable(getattr(Coach, name, None)), name)

        # Число тестов в README — из фактического прогона (не «древняя» цифра).
        match = re.search(r"Набор из (\d+) автоматических тестов", self.readme)
        self.assertIsNotNone(match, "README не указывает число тестов в структуре проекта")
        self.assertGreaterEqual(int(match.group(1)), 300)


if __name__ == "__main__":
    unittest.main()
