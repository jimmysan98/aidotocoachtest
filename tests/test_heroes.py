"""Тесты каталога героев, транслитерации и русских прозвищ."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from aidota.heroes import RU_ALIASES, HeroCatalog, normalize, translit
from tests.helpers import FAKE_HEROES


class HeroMatchingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = HeroCatalog(FAKE_HEROES)

    def test_exact_english(self):
        match = self.catalog.find("Anti-Mage")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Anti-Mage")

    def test_transliterated_name(self):
        match = self.catalog.find("антимаг")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Anti-Mage")

    def test_russian_alias(self):
        match = self.catalog.find("пудж")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Pudge")

    def test_alias_in_sentence(self):
        match = self.catalog.find("что купить на антимаге")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Anti-Mage")

    def test_alias_with_case_ending(self):
        match = self.catalog.find_in_tokens("сборка врки")
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "Windranger")

    def test_npc_name_lookup(self):
        self.assertEqual(self.catalog.by_npc("npc_dota_hero_antimage")["localized_name"], "Anti-Mage")
        self.assertEqual(self.catalog.by_npc("npc_dota_hero_nevermore")["localized_name"], "Shadow Fiend")

    def test_id_lookup(self):
        self.assertEqual(self.catalog.by_id(7)["localized_name"], "Windranger")

    def test_unknown_text(self):
        self.assertIsNone(self.catalog.find("абракадабра"))

    def test_common_word_not_a_hero(self):
        match = self.catalog.find_in_tokens("что мне купить в игре")
        self.assertIsNone(match)

    def test_question_words_do_not_match_chen(self):
        self.assertIsNone(self.catalog.find("чем играть"))
        self.assertIsNone(self.catalog.find_in_tokens("чем играть"))
        self.assertIsNone(self.catalog.find("как играть"))
        self.assertIsNone(self.catalog.find_in_tokens("как играть"))
        self.assertIsNone(self.catalog.find("с чем играть"))
        self.assertIsNone(self.catalog.find_in_tokens("чем контрить"))
        self.assertIsNone(self.catalog.find("чем"))

    def test_chen_exact_matches(self):
        m1 = self.catalog.find("чен")
        self.assertIsNotNone(m1)
        self.assertEqual(m1.name, "Chen")

        m2 = self.catalog.find_in_tokens("я на чене")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.name, "Chen")

    def test_empty_catalog(self):
        catalog = HeroCatalog([])
        self.assertFalse(catalog.available)
        self.assertIsNone(catalog.find("пудж"))

    def test_display_name(self):
        self.assertEqual(self.catalog.display_name("npc_dota_hero_pudge"), "Pudge")
        self.assertIsNone(self.catalog.display_name("npc_dota_hero_unknown"))


class TranslitTests(unittest.TestCase):
    def test_translit(self):
        self.assertEqual(translit("пудж"), "pudzh")
        self.assertEqual(translit("антимаг"), "antimag")

    def test_normalize_strips_punctuation(self):
        self.assertEqual(normalize("Anti-Mage!"), "antimage")

    def test_aliases_point_to_known_heroes(self):
        """Каждое русское прозвище должно ссылаться на существующего героя."""
        data_path = Path(__file__).resolve().parent.parent / "data" / "heroes.json"
        if not data_path.is_file():
            self.skipTest("нет data/heroes.json")
        heroes = json.loads(data_path.read_text(encoding="utf-8"))
        known = {h["name"].replace("npc_dota_hero_", "") for h in heroes}
        unknown = sorted(set(RU_ALIASES) - known)
        self.assertEqual(unknown, [], f"Прозвища ссылаются на неизвестных героев: {unknown}")


if __name__ == "__main__":
    unittest.main()
