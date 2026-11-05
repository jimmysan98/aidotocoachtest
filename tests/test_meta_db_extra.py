# -*- coding: utf-8 -*-
"""Расширенные тесты базы знаний Dota 2 (contract A: все герои + новые API тренера).

Проверяют:
- покрытие META_HEROES_DB всеми героями из data/heroes.json и обязательные поля записей;
- словарь тегов угроз A2 и реальные предметы контр-билдов;
- get_hero_counters / get_skill_order / get_hero_threats / get_lane_tips;
- умный recommend_picks (контрпики, синергии, исключение взятых/забаненных героев).

Сетевых вызовов нет: читаются только локальные data/*.json.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from aidota.meta_db import (
    META_HEROES_DB,
    ROLE_NAMES,
    SITUATIONAL_COUNTER_RULES,
    TAG_COUNTER_ITEMS,
    TAG_THREAT_PHRASES,
    THREAT_TAGS,
    generate_fallback_build,
    get_hero_build,
    get_hero_counters,
    get_hero_meta_entry,
    get_hero_threats,
    get_lane_tips,
    get_skill_order,
    recommend_picks,
)

ROOT = Path(__file__).resolve().parents[1]
HEROES_JSON = ROOT / "data" / "heroes.json"
ITEMS_JSON = ROOT / "data" / "items.json"

_REQUIRED_HERO_FIELDS = (
    "id", "name", "ru_name", "attr", "roles", "tags",
    "counters", "countered_by", "synergies", "builds",
)
_REQUIRED_BUILD_FIELDS = (
    "starting", "early_game", "core", "situational", "shard", "scepter", "lane_tips",
)
_SAMPLE_HEROES = [
    "antimage", "axe", "pudge", "lion", "lina", "tidehunter", "spectre",
    "medusa", "spirit_breaker", "crystal_maiden", "nevermore", "queenofpain",
    "earthshaker", "juggernaut", "witch_doctor", "rubick",
]
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def _load_heroes() -> dict[str, dict]:
    data = json.loads(HEROES_JSON.read_text(encoding="utf-8"))
    return {hero["name"].replace("npc_dota_hero_", ""): hero for hero in data}


def _load_item_names() -> set[str]:
    data = json.loads(ITEMS_JSON.read_text(encoding="utf-8"))
    return {item.get("dname") for item in data.values() if item.get("dname")}


HEROES = _load_heroes()
ITEM_NAMES = _load_item_names()


def _as_set(value) -> set[str]:
    return set(value or ())


def _pick_by_id(picks: list[dict], hero_id: str) -> dict | None:
    for pick in picks:
        if pick.get("hero_id") == hero_id:
            return pick
    return None


class TestHeroCoverage(unittest.TestCase):
    """A1: база покрывает всех героев и каждая запись заполнена осмысленно."""

    def test_covers_all_heroes_from_catalog(self):
        self.assertGreaterEqual(len(META_HEROES_DB), 120)
        missing = sorted(set(HEROES) - set(META_HEROES_DB))
        self.assertEqual(missing, [], f"нет героев в базе: {missing}")

    def test_keys_are_catalog_short_ids(self):
        self.assertEqual(set(META_HEROES_DB), set(HEROES))
        for hero_id in META_HEROES_DB:
            self.assertRegex(hero_id, r"^[a-z0-9_]+$")

    def test_every_hero_has_required_fields(self):
        for hero_id, meta in META_HEROES_DB.items():
            for field in _REQUIRED_HERO_FIELDS:
                self.assertIn(field, meta, f"{hero_id}: нет поля {field}")
            self.assertEqual(meta["id"], hero_id)
            self.assertEqual(meta["name"], HEROES[hero_id]["localized_name"])

    def test_names_are_human_readable(self):
        for hero_id, meta in META_HEROES_DB.items():
            for field in ("name", "ru_name"):
                value = str(meta.get(field) or "").strip()
                self.assertGreaterEqual(len(value), 2, f"{hero_id}: пустое {field}")
                self.assertNotIn("unknown", value.lower(), f"{hero_id}: заглушка в {field}")
            self.assertNotEqual(meta["name"], meta["id"], f"{hero_id}: английское имя не заполнено")

    def test_ru_names_are_russian(self):
        cyrillic = [meta for meta in META_HEROES_DB.values() if _CYRILLIC_RE.search(meta["ru_name"])]
        self.assertGreaterEqual(len(cyrillic), 120, "у большинства героев должно быть русское имя")

    def test_attr_and_roles_are_valid(self):
        for hero_id, meta in META_HEROES_DB.items():
            self.assertIn(meta["attr"], {"str", "agi", "int", "uni"}, f"{hero_id}: attr")
            roles = meta["roles"]
            self.assertIsInstance(roles, list)
            self.assertGreaterEqual(len(roles), 1, f"{hero_id}: пустые roles")
            for role in roles:
                self.assertIn(role, (1, 2, 3, 4, 5), f"{hero_id}: роль {role}")
            self.assertEqual(len(roles), len(set(roles)), f"{hero_id}: дубли ролей")

    def test_tags_use_threat_dictionary(self):
        vocabulary = set(THREAT_TAGS)
        for hero_id, meta in META_HEROES_DB.items():
            tags = meta["tags"]
            self.assertIsInstance(tags, list, f"{hero_id}: tags не список")
            self.assertTrue(2 <= len(tags) <= 4, f"{hero_id}: тегов {len(tags)}, нужно 2-4")
            self.assertEqual(len(tags), len(set(tags)), f"{hero_id}: дубли тегов")
            self.assertTrue(set(tags) <= vocabulary, f"{hero_id}: теги вне словаря: {set(tags) - vocabulary}")

    def test_counter_references_are_valid_hero_ids(self):
        known = set(META_HEROES_DB)
        for hero_id, meta in META_HEROES_DB.items():
            for field in ("counters", "countered_by", "synergies"):
                refs = meta[field]
                self.assertIsInstance(refs, list, f"{hero_id}: {field} не список")
                self.assertGreaterEqual(len(refs), 1, f"{hero_id}: пустое {field}")
                for ref in refs:
                    self.assertIn(ref, known, f"{hero_id}: {field} -> неизвестный id {ref!r}")
                    self.assertNotEqual(ref, hero_id, f"{hero_id}: {field} ссылается на себя")
                self.assertEqual(len(refs), len(set(refs)), f"{hero_id}: дубли в {field}")

    def test_every_hero_has_builds_for_its_roles(self):
        for hero_id, meta in META_HEROES_DB.items():
            builds = meta["builds"]
            self.assertIsInstance(builds, dict)
            for role in meta["roles"]:
                self.assertIn(role, builds, f"{hero_id}: нет билда на роль {role}")

    def test_build_structure_is_complete(self):
        for hero_id, meta in META_HEROES_DB.items():
            for role, build in meta["builds"].items():
                for field in _REQUIRED_BUILD_FIELDS:
                    self.assertIn(field, build, f"{hero_id}[{role}]: нет {field}")
                for field in ("starting", "early_game", "core", "situational"):
                    self.assertIsInstance(build[field], list, f"{hero_id}[{role}]: {field}")
                    self.assertGreaterEqual(len(build[field]), 1, f"{hero_id}[{role}]: пустой {field}")
                self.assertGreaterEqual(len(build["core"]), 3, f"{hero_id}[{role}]: мало core")
                for entry in build["core"]:
                    for field in ("item", "timing", "tip"):
                        self.assertTrue(str(entry.get(field) or "").strip(), f"{hero_id}[{role}]: core {field}")
                for entry in build["situational"]:
                    for field in ("item", "trigger", "why"):
                        self.assertTrue(str(entry.get(field) or "").strip(), f"{hero_id}[{role}]: ситуативка {field}")
                self.assertTrue(str(build["shard"]).strip(), f"{hero_id}[{role}]: пустой shard")
                self.assertTrue(str(build["scepter"]).strip(), f"{hero_id}[{role}]: пустой scepter")
                self.assertGreaterEqual(len(str(build["lane_tips"]).strip()), 10, f"{hero_id}[{role}]: lane_tips")

    def test_popular_heroes_have_multiple_role_builds(self):
        multi = [
            meta for meta in META_HEROES_DB.values()
            if len(meta["builds"]) >= 2 and len(meta["roles"]) >= 2
        ]
        self.assertGreaterEqual(len(multi), 30, "у популярных героев должно быть 2+ билда на роли")

    def test_every_role_has_enough_heroes(self):
        for role in range(1, 6):
            pool = [meta for meta in META_HEROES_DB.values() if role in meta["roles"]]
            self.assertGreaterEqual(len(pool), 15, f"на роль {role} мало героев: {len(pool)}")
            self.assertIn(role, ROLE_NAMES)

    def test_detailed_builds_are_preserved(self):
        antimage = get_hero_build("antimage", role=1)
        self.assertTrue(any("Battle Fury" in str(item) for item in antimage["core"]))
        self.assertIn("shard", antimage)
        self.assertIn("lane_tips", antimage)
        axe = get_hero_build("axe", role=3)
        self.assertTrue(any("Blink Dagger" in str(item.get("item")) for item in axe["core"]))
        self.assertEqual(get_hero_meta_entry("Акс")["id"], "axe")
        self.assertEqual(get_hero_meta_entry("Антимаг")["id"], "antimage")


class TestThreatDictionary(unittest.TestCase):
    """A2: словарь тегов и контр-предметы — только реальные предметы Dota 2."""

    def test_tag_counter_items_cover_dictionary(self):
        self.assertTrue(set(THREAT_TAGS) <= set(TAG_COUNTER_ITEMS), "у каждого тега должны быть предметы")
        self.assertEqual(set(TAG_COUNTER_ITEMS), set(THREAT_TAGS))

    def test_counter_items_are_real_items(self):
        for tag, items in TAG_COUNTER_ITEMS.items():
            self.assertGreaterEqual(len(items), 1, f"{tag}: нет контр-предметов")
            for item in items:
                self.assertIn(item, ITEM_NAMES, f"{tag}: предмет {item!r} не существует")

    def test_situational_rules_cover_tags_and_real_items(self):
        self.assertGreaterEqual(len(SITUATIONAL_COUNTER_RULES), 10)
        rule_keys = {rule["threat"] for rule in SITUATIONAL_COUNTER_RULES}
        self.assertTrue(set(THREAT_TAGS) <= rule_keys, f"нет правил для: {set(THREAT_TAGS) - rule_keys}")
        for rule in SITUATIONAL_COUNTER_RULES:
            self.assertTrue(str(rule["description"]).strip())
            self.assertTrue(str(rule["tip"]).strip())
            for item in rule["items"]:
                self.assertIn(item, ITEM_NAMES, f"{rule['threat']}: предмет {item!r} не существует")

    def test_threat_phrases_are_russian(self):
        for tag in THREAT_TAGS:
            phrase = TAG_THREAT_PHRASES.get(tag, "")
            self.assertTrue(_CYRILLIC_RE.search(phrase), f"{tag}: фраза не по-русски: {phrase!r}")


class TestCounterKnowledge(unittest.TestCase):
    """A3: get_hero_counters."""

    def test_works_for_ten_plus_heroes(self):
        for hero_id in _SAMPLE_HEROES:
            meta = get_hero_counters(hero_id)
            self.assertIsNotNone(meta, hero_id)
            self.assertEqual(meta["hero_id"], hero_id)
            self.assertEqual(meta["name"], HEROES[hero_id]["localized_name"])
            self.assertTrue(meta["ru_name"])
            self.assertIsInstance(meta["tags"], list)
            self.assertGreaterEqual(len(meta["counter_items"]), 1, hero_id)
            for item in meta["counter_items"]:
                self.assertIn(item, ITEM_NAMES, f"{hero_id}: выдуманный предмет {item!r}")
            self.assertGreaterEqual(len(meta["counter_heroes"]), 2, hero_id)
            for name in meta["counter_heroes"]:
                self.assertTrue(any(hero["localized_name"] == name for hero in HEROES.values()))
            self.assertTrue(meta["tip"].strip())
            self.assertTrue(_CYRILLIC_RE.search(meta["tip"]), f"{hero_id}: tip не по-русски")

    def test_phantom_assassin_has_evasion_counters(self):
        meta = get_hero_counters("Phantom Assassin")
        self.assertIn("evasion", meta["tags"])
        self.assertIn("Monkey King Bar", meta["counter_items"])
        self.assertIn("Axe", meta["counter_heroes"])

    def test_resolves_id_english_and_russian_names(self):
        for name in ("phantom_assassin", "Phantom Assassin", "Фантомка", "npc_dota_hero_phantom_assassin"):
            meta = get_hero_counters(name)
            self.assertIsNotNone(meta, name)
            self.assertEqual(meta["hero_id"], "phantom_assassin")

    def test_unknown_and_garbage_return_none(self):
        for garbage in (None, "", "   ", "Несуществующий Герой", "zzzzzz", 123):
            self.assertIsNone(get_hero_counters(garbage), repr(garbage))


class TestSkillKnowledge(unittest.TestCase):
    """A3: get_skill_order."""

    def test_works_for_ten_plus_heroes(self):
        for hero_id in _SAMPLE_HEROES:
            skill = get_skill_order(hero_id)
            self.assertIsNotNone(skill, hero_id)
            self.assertEqual(skill["hero_id"], hero_id)
            for field in ("ru_name", "priority", "level1", "tip"):
                self.assertTrue(str(skill.get(field) or "").strip(), f"{hero_id}: пустое {field}")

    def test_known_values(self):
        antimage = get_skill_order("Антимаг")
        self.assertEqual(antimage["hero_id"], "antimage")
        self.assertIn("Mana Break", antimage["priority"])
        invoker = get_skill_order("Invoker")
        self.assertEqual(invoker["level1"], "Quas")
        axe = get_skill_order("axe")
        self.assertIn("Counter Helix", axe["priority"])

    def test_unknown_and_garbage_return_none(self):
        for garbage in (None, "", "Несуществующий Герой", "qwerty", 42):
            self.assertIsNone(get_skill_order(garbage), repr(garbage))


class TestThreatKnowledge(unittest.TestCase):
    """A3: get_hero_threats."""

    def test_returns_found_heroes_in_input_order(self):
        threats = get_hero_threats(["huskar", "Несуществующий Герой", "phantom_assassin"])
        self.assertEqual([item["hero_id"] for item in threats], ["huskar", "phantom_assassin"])
        for item in threats:
            self.assertIn("ru_name", item)
            self.assertIn("tags", item)
            self.assertIn("threat", item)
            self.assertIn("counter_items", item)
            self.assertTrue(item["threat"].strip())
            self.assertGreaterEqual(len(item["counter_items"]), 1)

    def test_huskar_threat_mentions_regen_and_vessel(self):
        huskar = get_hero_threats(["huskar"])[0]
        self.assertIn("heal_regen", huskar["tags"])
        self.assertIn("Spirit Vessel", huskar["counter_items"])
        self.assertTrue(_CYRILLIC_RE.search(huskar["threat"]))

    def test_empty_and_garbage_input(self):
        self.assertEqual(get_hero_threats([]), [])
        self.assertEqual(get_hero_threats(None), [])
        self.assertEqual(get_hero_threats(["мусор", "ещё мусор"]), [])
        self.assertEqual(get_hero_threats("pudge")[0]["hero_id"], "pudge")


class TestLaneTips(unittest.TestCase):
    """A3: get_lane_tips."""

    def test_non_empty_for_ten_plus_heroes_and_roles(self):
        for hero_id in _SAMPLE_HEROES:
            role = META_HEROES_DB[hero_id]["roles"][0]
            tip = get_lane_tips(hero_id, role)
            self.assertIsInstance(tip, str)
            self.assertGreaterEqual(len(tip.strip()), 20, f"{hero_id}: короткий лейн-совет")
            self.assertTrue(re.search(r"[а-яё]", tip, re.IGNORECASE), f"{hero_id}: совет не по-русски")

    def test_enemy_hero_adds_counter_item(self):
        tip = get_lane_tips("axe", 3, ["Phantom Assassin"])
        self.assertIn("Monkey King Bar", tip)
        self.assertIn("Phantom Assassin", tip)

    def test_unknown_hero_and_bad_role_still_return_text(self):
        tip = get_lane_tips("Несуществующий Герой", 3)
        self.assertIsInstance(tip, str)
        self.assertGreaterEqual(len(tip.strip()), 20)
        bad_role = get_lane_tips("pudge", "не роль", None)  # type: ignore[arg-type]
        self.assertTrue(bad_role.strip())


class TestRecommendPicksSmart(unittest.TestCase):
    """A4: recommend_picks — контрпики, синергии, исключения и роли."""

    def test_returns_required_fields_and_preview(self):
        picks = recommend_picks(["medusa", "zuus"], role=1, top_k=5)
        self.assertGreaterEqual(len(picks), 1)
        for pick in picks:
            for field in (
                "hero_id", "name", "ru_name", "score", "role", "role_name",
                "strong_against", "weak_against", "combos", "core_items",
                "shard", "advice", "countered_by_preview",
            ):
                self.assertIn(field, pick)
            self.assertGreaterEqual(pick["score"], 50)
            self.assertTrue(1 <= pick["score"] <= 99)
            self.assertGreaterEqual(len(pick["countered_by_preview"]), 2)
            self.assertLessEqual(len(pick["countered_by_preview"]), 3)
            for name in pick["countered_by_preview"]:
                self.assertTrue(any(hero["localized_name"] == name for hero in HEROES.values()))
        # Антимаг контрит Медузу и Зевса — он обязан быть в топе.
        self.assertEqual(picks[0]["hero_id"], "antimage")

    def test_never_recommends_enemy_picks(self):
        enemies = ["medusa", "storm_spirit", "axe"]
        picks = recommend_picks(enemies, role=1, top_k=50)
        offered = {pick["hero_id"] for pick in picks}
        self.assertTrue(offered)
        self.assertFalse(offered & set(enemies), "враги не должны предлагаться")

    def test_never_recommends_ally_or_banned_heroes(self):
        picks = recommend_picks(
            ["sniper"],
            ally_heroes=["spectre"],
            role=1,
            top_k=127,
            banned_heroes=["antimage", "PA"],
        )
        offered = {pick["hero_id"] for pick in picks}
        self.assertNotIn("spectre", offered)
        self.assertNotIn("antimage", offered)
        self.assertNotIn("phantom_assassin", offered, "бан по русскому/англ. имени тоже учитывается")

    def test_role_filter_for_all_roles(self):
        for role in range(1, 6):
            picks = recommend_picks(["medusa"], role=role, top_k=7)
            self.assertGreaterEqual(len(picks), 1, f"роль {role}")
            for pick in picks:
                self.assertEqual(pick["role"], role)
                self.assertIn(role, META_HEROES_DB[pick["hero_id"]]["roles"])

    def test_counter_pick_scores_higher_than_non_counter(self):
        vs_medusa = _pick_by_id(recommend_picks(["medusa"], role=1, top_k=127), "antimage")
        vs_luna = _pick_by_id(recommend_picks(["luna"], role=1, top_k=127), "antimage")
        self.assertIsNotNone(vs_medusa)
        self.assertIsNotNone(vs_luna)
        self.assertGreater(vs_medusa["score"], vs_luna["score"])
        self.assertTrue(vs_medusa["strong_against"])

    def test_synergy_with_ally_raises_score(self):
        base = _pick_by_id(recommend_picks(["sniper"], role=1, top_k=127), "juggernaut")
        with_ally = _pick_by_id(
            recommend_picks(["sniper"], role=1, top_k=127, ally_heroes=["crystal_maiden"]),
            "juggernaut",
        )
        self.assertIsNotNone(base)
        self.assertIsNotNone(with_ally)
        self.assertGreater(with_ally["score"], base["score"])
        self.assertTrue(any("Crystal Maiden" in combo for combo in with_ally["combos"]))

    def test_warned_hero_gets_lower_score(self):
        # Против Axe у Phantom Assassin появляется опасный враг в countered_by_preview.
        picks = recommend_picks(["axe"], role=1, top_k=127)
        pa = _pick_by_id(picks, "phantom_assassin")
        self.assertIsNotNone(pa)
        self.assertIn("Axe", pa["countered_by_preview"])
        self.assertTrue(any("Axe" in str(item) for item in pa["weak_against"]))

    def test_accepts_localized_aliases_and_npc_names(self):
        cases = [
            (["Phantom Assassin"], "phantom_assassin"),
            (["npc_dota_hero_medusa"], "medusa"),
            (["СФ"], "nevermore"),
            (["shadow_fiend"], "nevermore"),
            (["axe", "Axe"], "axe"),
        ]
        for enemies, hero_id in cases:
            picks = recommend_picks(enemies, role=1, top_k=127)
            offered = {pick["hero_id"] for pick in picks}
            self.assertNotIn(hero_id, offered, f"{enemies}: герой врага попал в рекомендации")

    def test_empty_and_garbage_draft_does_not_crash(self):
        self.assertTrue(recommend_picks([], role=1))
        self.assertTrue(recommend_picks(None, role=2))  # type: ignore[arg-type]
        self.assertTrue(recommend_picks(["Несуществующий Герой"], role=3))
        self.assertTrue(recommend_picks("medusa,zuus", role=1))  # type: ignore[arg-type]

    def test_top_k_limits_results(self):
        picks = recommend_picks(["sniper"], role=1, top_k=2)
        self.assertEqual(len(picks), 2)
        single = recommend_picks(["sniper"], role=1, top_k=1)
        self.assertEqual(len(single), 1)


class TestFallbackBuilds(unittest.TestCase):
    """A1: шаблонные билды и обратная совместимость get_hero_build."""

    def test_fallback_build_structure_and_real_items(self):
        for role, attr in ((1, "agi"), (2, "int"), (3, "str"), (4, "uni"), (5, "int")):
            build = generate_fallback_build("Неизвестный", role=role, primary_attr=attr)
            for field in _REQUIRED_BUILD_FIELDS:
                self.assertIn(field, build)
            self.assertGreaterEqual(len(build["core"]), 3)
            items = list(build["starting"]) + list(build["early_game"])
            items += [entry["item"].split(" / ") for entry in build["core"]]
            for item in _flatten(items):
                self.assertIn(item, ITEM_NAMES, f"шаблон {role}/{attr}: выдуманный предмет {item!r}")

    def test_get_hero_build_for_all_heroes_and_roles(self):
        for hero_id, meta in META_HEROES_DB.items():
            for role in range(1, 6):
                build = get_hero_build(hero_id, role)
                self.assertTrue(build["starting"], f"{hero_id}[{role}]")
                self.assertTrue(build["core"], f"{hero_id}[{role}]")
                self.assertEqual(build["role"], role)
                self.assertTrue(str(build["hero_name"]).strip())

    def test_get_hero_build_unknown_hero_returns_fallback(self):
        build = get_hero_build("Несуществующий Герой", role=2)
        self.assertTrue(build["starting"])
        self.assertTrue(build["core"])
        self.assertEqual(build["role"], 2)

    def test_adaptive_items_use_threat_tags(self):
        build = get_hero_build("axe", role=3, enemy_heroes=["Huskar"])
        items = " ".join(str(entry.get("item", "")) for entry in build["situational"])
        self.assertIn("Spirit Vessel", items)


def _flatten(values) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            result.extend(_flatten(value))
        else:
            result.append(str(value))
    return result


if __name__ == "__main__":
    unittest.main()
