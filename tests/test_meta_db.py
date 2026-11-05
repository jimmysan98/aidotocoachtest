"""Модульные тесты для базы знаний Dota 2 Meta DB (meta_db.py)."""

import unittest

from aidota.meta_db import (
    META_HEROES_DB,
    SITUATIONAL_COUNTER_RULES,
    generate_fallback_build,
    get_hero_build,
    get_hero_meta_entry,
    recommend_picks,
)


class TestMetaDb(unittest.TestCase):
    def test_meta_heroes_db_not_empty(self):
        """База данных мета-героев должна содержать ключевых героев Dota 2."""
        self.assertGreaterEqual(len(META_HEROES_DB), 10)
        self.assertIn("antimage", META_HEROES_DB)
        self.assertIn("axe", META_HEROES_DB)
        self.assertIn("juggernaut", META_HEROES_DB)
        self.assertIn("crystal_maiden", META_HEROES_DB)
        self.assertIn("witch_doctor", META_HEROES_DB)

    def test_get_hero_meta_entry_various_keys(self):
        """Поиск героя по ID, английскому и русскому имени."""
        am1 = get_hero_meta_entry("antimage")
        self.assertIsNotNone(am1)
        self.assertEqual(am1["name"], "Anti-Mage")

        am2 = get_hero_meta_entry("Anti-Mage")
        self.assertIsNotNone(am2)
        self.assertEqual(am2["id"], "antimage")

        am3 = get_hero_meta_entry("Антимаг")
        self.assertIsNotNone(am3)
        self.assertEqual(am3["id"], "antimage")

        axe = get_hero_meta_entry("Акс")
        self.assertIsNotNone(axe)
        self.assertEqual(axe["id"], "axe")

    def test_get_hero_build_specific_hero(self):
        """Проверка получения детального билда на героя и позицию."""
        build = get_hero_build("antimage", role=1)
        self.assertEqual(build["hero_name"], "Anti-Mage")
        self.assertEqual(build["role"], 1)
        self.assertIsInstance(build.get("starting"), list)
        self.assertGreater(len(build["starting"]), 2)
        self.assertIsInstance(build.get("core"), list)
        self.assertGreater(len(build["core"]), 2)
        self.assertTrue(any("Battle Fury" in str(c) for c in build["core"]))
        self.assertIn("shard", build)
        self.assertIn("lane_tips", build)

    def test_get_hero_build_adaptive_counter_items(self):
        """Ситуативные предметы динамически адаптируются под врагов."""
        # Против Фантомки должен рекомендовать MKB
        build_vs_pa = get_hero_build("antimage", role=1, enemy_heroes=["Phantom Assassin", "Lion"])
        situational = build_vs_pa.get("situational", [])
        self.assertTrue(any("Monkey King Bar" in s.get("item", "") for s in situational))

        # Против Морфа должен рекомендовать Skadi / Spirit Vessel
        build_vs_morph = get_hero_build("juggernaut", role=1, enemy_heroes=["Morphling", "Axe"])
        sit_morph = build_vs_morph.get("situational", [])
        self.assertTrue(any("Skadi" in s.get("item", "") or "Vessel" in s.get("item", "") for s in sit_morph))

    def test_generate_fallback_build(self):
        """Генератор fallback-билда для любого неизвестного героя."""
        fb = generate_fallback_build("UnknownHero", role=2, primary_attr="int")
        self.assertIn("starting", fb)
        self.assertIn("core", fb)
        self.assertIn("situational", fb)
        self.assertGreater(len(fb["core"]), 0)

    def test_recommend_picks_counter_scoring(self):
        """Рекомендатор контрпиков должен выдавать обоснованные предложения с высоким скором."""
        # Против Медузы и Зевса Антимаг (поз. 1) должен быть в топе
        picks = recommend_picks(enemy_heroes=["medusa", "zuus"], role=1, top_k=3)
        self.assertIsInstance(picks, list)
        self.assertGreater(len(picks), 0)

        # Проверяем структуру рекомендации
        top = picks[0]
        self.assertIn("hero_id", top)
        self.assertIn("name", top)
        self.assertIn("ru_name", top)
        self.assertIn("score", top)
        self.assertGreaterEqual(top["score"], 50)
        self.assertIn("strong_against", top)
        self.assertIn("core_items", top)

    def test_recommend_picks_different_roles(self):
        """Рекомендации фильтруются по роли игрока (1..5)."""
        picks_carry = recommend_picks(enemy_heroes=["sniper"], role=1)
        picks_offlane = recommend_picks(enemy_heroes=["phantom_lancer"], role=3)
        picks_support = recommend_picks(enemy_heroes=["enigma"], role=4)

        for p in picks_carry:
            self.assertEqual(p["role"], 1)
        for p in picks_offlane:
            self.assertEqual(p["role"], 3)
        for p in picks_support:
            self.assertEqual(p["role"], 4)

    def test_situational_counter_rules(self):
        """Правила контр-предметов покрывают основные типы угроз."""
        self.assertGreaterEqual(len(SITUATIONAL_COUNTER_RULES), 5)
        threat_keys = {r["threat"] for r in SITUATIONAL_COUNTER_RULES}
        self.assertIn("evasion", threat_keys)
        self.assertIn("heal_regen", threat_keys)
        self.assertIn("passives", threat_keys)
        self.assertIn("saves_buffs", threat_keys)
        self.assertIn("magic_burst_cc", threat_keys)


if __name__ == "__main__":
    unittest.main()
