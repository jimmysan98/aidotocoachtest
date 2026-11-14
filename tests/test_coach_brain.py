"""Модульные тесты тактического ядра тренера aidota.coach_brain."""

import unittest
from aidota.coach_brain import (
    build_brief,
    detect_query_kind,
    is_voice_safe,
    make_system_prompt,
    render_brief,
    render_system_prompt,
    sanitize_answer,
)


class TestCoachBrainQueryDetection(unittest.TestCase):
    def test_detect_query_kind_variants(self):
        cases = [
            ("кого пикнуть на керри", "pick"),
            ("что взять против фантомки", "counter"),
            ("что собрать на акса", "build"),
            ("что качать первым на пудже", "skill"),
            ("как стоять линию в харде", "lane"),
            ("когда появится рошан", "timing"),
            ("разбери мои ошибки за этот матч", "analysis"),
            ("что по игре", "status"),
            ("что такое байбек", "explain"),
            ("почему нужно покупать бкб", "why"),
            ("как дела", "status"),
            ("сравни героев снайпер и тракса", "matchup"),
            ("привет я готов играть", "chat"),
            ("", "chat"),
        ]
        for query, expected_kind in cases:
            kind = detect_query_kind(query)
            self.assertEqual(kind, expected_kind, f"Failed for query '{query}': got {kind}")


class TestCoachBrainSanitizer(unittest.TestCase):
    def test_strip_markdown_and_code(self):
        text = "Купи **Blink Dagger** и `Black King Bar` для врыва. ```console\ncmd\n```"
        clean = sanitize_answer(text)
        self.assertNotIn("**", clean)
        self.assertNotIn("`", clean)
        self.assertNotIn("console", clean)
        self.assertIn("Blink Dagger", clean)
        self.assertIn("Black King Bar", clean)

    def test_strip_markdown_links(self):
        text = "Собирай [Black King Bar](https://dota2.fandom.com/BKB) против магии."
        clean = sanitize_answer(text)
        self.assertNotIn("https://", clean)
        self.assertNotIn("[", clean)
        self.assertNotIn("]", clean)
        self.assertIn("Black King Bar против магии.", clean)

    def test_strip_html_tags(self):
        text = "Тебе нужен <b>BKB</b> и <span class='item'>Manta Style</span> срочно."
        clean = sanitize_answer(text)
        self.assertNotIn("<b>", clean)
        self.assertNotIn("</span>", clean)
        self.assertIn("BKB и Manta Style срочно.", clean)

    def test_strip_markdown_tables(self):
        text = (
            "Вот твоя сборка:\n"
            "| Предмет | Стоимость |\n"
            "|:---:|:---:|\n"
            "| Blink Dagger | 2250 |\n"
            "| BKB | 4050 |\n"
            "Иди пушить мид."
        )
        clean = sanitize_answer(text)
        self.assertNotIn("|", clean)
        self.assertNotIn("---", clean)
        self.assertTrue(clean.startswith("Вот твоя сборка:"))
        self.assertTrue(is_voice_safe(clean))

    def test_strip_filler_prefixes(self):
        fillers = [
            "Конечно, покупай BKB.",
            "Разумеется, держи позицию.",
            "Отличный вопрос! Качай первый скилл.",
            "Как языковая модель, ...",
        ]
        self.assertEqual(sanitize_answer(fillers[0]), "Покупай BKB.")
        self.assertEqual(sanitize_answer(fillers[1]), "Держи позицию.")
        self.assertEqual(sanitize_answer(fillers[2]), "Качай первый скилл.")
        # Refusal marker should return empty string
        self.assertEqual(sanitize_answer(fillers[3]), "")

    def test_strip_emojis_and_bullets(self):
        text = "- 🔥 Покупай Даггер.\n* 🛡️ Ставь варды."
        clean = sanitize_answer(text)
        self.assertNotIn("🔥", clean)
        self.assertNotIn("🛡️", clean)
        self.assertNotIn("-", clean)
        self.assertNotIn("*", clean)
        self.assertIn("Покупай Даггер", clean)

    def test_is_voice_safe_checks(self):
        self.assertTrue(is_voice_safe("Покупай BKB и иди с командой на Рошана."))
        self.assertFalse(is_voice_safe("Покупай **BKB**."))
        self.assertFalse(is_voice_safe("```python\nprint(1)```"))
        self.assertFalse(is_voice_safe("1. Покупай BKB."))
        self.assertFalse(is_voice_safe("Покупай [BKB](url)."))
        self.assertFalse(is_voice_safe("Покупай <b>BKB</b>."))
        self.assertFalse(is_voice_safe("Предмет | Цена"))
        self.assertFalse(is_voice_safe("Привет 😊"))
        self.assertFalse(is_voice_safe("А" * 300))


class TestCoachBrainBrief(unittest.TestCase):
    def test_build_brief_offline(self):
        brief = build_brief(None)
        self.assertFalse(brief["available"])
        self.assertEqual(brief["stage"], "offline")
        rendered = render_brief(brief)
        self.assertIn("Матч не запущен", rendered)

    def test_build_brief_with_facets_and_cc_statuses(self):
        payload = {
            "map": {
                "clock_time": 450,
                "radiant_score": 5,
                "dire_score": 8,
                "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
            },
            "player": {
                "team_name": "radiant",
                "gold": 1200,
                "kills": 3,
                "deaths": 1,
                "assists": 4,
            },
            "hero": {
                "name": "npc_dota_hero_antimage",
                "level": 12,
                "health_percent": 80,
                "mana_percent": 60,
                "alive": True,
                "facet": 2,
                "stunned": False,
                "rooted": True,
                "muted": True,
                "taunted": False,
            },
            "items": {
                "slot0": {"name": "item_power_treads"},
                "slot1": {"name": "item_bfury"},
            },
        }
        brief = build_brief(payload, position=1)
        self.assertTrue(brief["available"])
        self.assertEqual(brief["facet"], 2)
        self.assertIn("rooted", brief["statuses"])
        self.assertIn("muted", brief["statuses"])
        self.assertNotIn("stunned", brief["statuses"])
        self.assertNotIn("taunted", brief["statuses"])

        rendered = render_brief(brief)
        self.assertIn("аспект 2", rendered)
        self.assertIn("контроль: rooted, muted", rendered)

    def test_render_system_prompt_structure(self):
        payload = {
            "map": {"clock_time": 600},
            "player": {"gold": 500},
            "hero": {"name": "npc_dota_hero_axe", "level": 6, "health_percent": 100},
        }
        brief = build_brief(payload, position=3)
        brief_text = render_brief(brief)
        prompt = render_system_prompt(coach_name="Астра", brief_text=brief_text, kind="build", skill_level="amateur")
        self.assertIn("Dota 2", prompt)
        self.assertIn("ТАКТИЧЕСКИЙ БРИФ", prompt)
        self.assertIn("ФОРМАТ ОТВЕТА", prompt)


if __name__ == "__main__":
    unittest.main()
