"""Тесты улучшений интеллекта, гибкости и производительности ИИ-тренера + Apple HUD UI."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aidota.coach import COACH_TOOLS, Coach
from aidota.coach_brain import (
    build_brief,
    detect_query_intents,
    make_system_prompt,
    render_brief,
    render_system_prompt,
    resolve_mentioned_heroes,
    retrieve_knowledge,
    sanitize_answer,
)
from aidota.config import (
    Config,
    LlmConfig,
    PlayerProfile,
    normalize_coach_tone,
    normalize_detail_mode,
)
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.live_advice import _UPGRADE_EQUIVALENTS, build_item_advice
from aidota.meta_db import compare_hero_matchup
from aidota.tts import SpeechPriority
from aidota.web_app import AidotaWebApi, WebGuiController


def _make_catalog() -> HeroCatalog:
    return HeroCatalog([
        {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage", "roles": ["Carry"]},
        {"id": 2, "name": "npc_dota_hero_axe", "localized_name": "Axe", "roles": ["Initiator"]},
        {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut", "roles": ["Carry"]},
        {"id": 14, "name": "npc_dota_hero_pudge", "localized_name": "Pudge", "roles": ["Initiator"]},
        {"id": 17, "name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit", "roles": ["Carry", "Nuker"]},
        {"id": 93, "name": "npc_dota_hero_slark", "localized_name": "Slark", "roles": ["Carry"]},
        {"id": 99, "name": "npc_dota_hero_bristleback", "localized_name": "Bristleback", "roles": ["Durable"]},
        {"id": 36, "name": "npc_dota_hero_necrolyte", "localized_name": "Necrophos", "roles": ["Carry", "Nuker"]},
    ])


def _make_coach(tmp_dir: str, **profile_kwargs) -> Coach:
    api = DotaApi(Path(tmp_dir) / "api")
    profile = PlayerProfile(**profile_kwargs)
    return Coach(
        api,
        _make_catalog(),
        None,
        LlmConfig(base_url="http://127.0.0.1:11434", model="gemma3"),
        profile,
        coach_name="Астра",
    )


class TestConfigFlexibility(unittest.TestCase):
    def test_normalize_coach_tone_and_detail_mode(self):
        self.assertEqual(normalize_coach_tone("tactical"), "tactical")
        self.assertEqual(normalize_coach_tone("Тактический"), "tactical")
        self.assertEqual(normalize_coach_tone("поддерживающий"), "supportive")
        self.assertEqual(normalize_coach_tone("краткий"), "brief")
        self.assertEqual(normalize_coach_tone("unknown"), "balanced")
        self.assertEqual(normalize_coach_tone(None), "balanced")

        self.assertEqual(normalize_detail_mode("concise"), "concise")
        self.assertEqual(normalize_detail_mode("кратко"), "concise")
        self.assertEqual(normalize_detail_mode("detailed"), "detailed")
        self.assertEqual(normalize_detail_mode("подробно"), "detailed")
        self.assertEqual(normalize_detail_mode("weird"), "auto")

    def test_player_profile_defaults_and_focus_trim(self):
        profile = PlayerProfile(
            coach_tone="поддержка",
            detail_mode="подробно",
            custom_focus="  " + ("стаки " * 60),
            speak_chat=False,
        )
        self.assertEqual(profile.coach_tone, "balanced")
        self.assertEqual(profile.detail_mode, "detailed")
        self.assertLessEqual(len(profile.custom_focus), 200)
        self.assertFalse(profile.speak_chat)


class TestMetaAndLiveAdviceUpgrades(unittest.TestCase):
    def test_compare_hero_matchup(self):
        res = compare_hero_matchup("Juggernaut", "Axe", role_a=1)
        self.assertIsInstance(res, dict)
        self.assertIn("Джаггер", res.get("hero_a", ""))
        self.assertTrue(res.get("summary"))
        empty = compare_hero_matchup("Juggernaut", "")
        self.assertIsNone(empty)

    def test_upgrade_equivalents_and_new_enemy_notes(self):
        self.assertIn("force staff", _UPGRADE_EQUIVALENTS.get("hurricane pike", ()))
        self.assertIn("shadow blade", _UPGRADE_EQUIVALENTS.get("silver edge", ()))

        advice = build_item_advice(
            {
                "available": True,
                "gold": 2200,
                "clock_seconds": 1200,
                "inventory": ["Power Treads", "Dragon Lance", "Hurricane Pike"],
            },
            hero_name="Drow Ranger",
            position=1,
            enemy_heroes=["Slark", "Necrophos"],
            item_costs={"force staff": 2200, "black king bar": 4050, "spirit vessel": 2780},
        )
        names = [a["item"] for a in advice]
        self.assertNotIn("Force Staff", names)
        self.assertTrue(any("Spirit Vessel" in n or "Silver Edge" in n or "Black King Bar" in n for n in names))


class TestCoachBrainIntelligence(unittest.TestCase):
    def test_detect_query_intents_multi_and_matchup(self):
        intents = detect_query_intents("кого пикнуть и что собрать на линию")
        self.assertGreaterEqual(len(intents), 2)
        self.assertEqual(intents[0], "pick")
        self.assertTrue(any(k in intents for k in ("build", "lane")))

        matchup_intents = detect_query_intents("кто сильнее в матчапе juggernaut против axe?")
        self.assertEqual(matchup_intents[0], "matchup")

    def test_resolve_mentioned_heroes_multi(self):
        cat = _make_catalog()
        heroes = resolve_mentioned_heroes("стою на джаггере против акса и сларка", cat, max_heroes=3)
        names = [h["localized_name"] for h in heroes]
        self.assertIn("Juggernaut", names)
        self.assertIn("Axe", names)
        self.assertIn("Slark", names)

    def test_build_brief_extracts_enemy_items_from_allplayers(self):
        payload = {
            "map": {"clock_time": 900, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 1800, "last_hits": 70, "deaths": 1},
            "hero": {"name": "npc_dota_hero_juggernaut", "level": 11, "health": 1100, "max_health": 1100, "mana": 400, "max_mana": 400},
            "items": {"slot0": {"name": "item_phase_boots"}, "slot1": {"name": "item_bfury"}},
            "allplayers": {
                "player5": {
                    "team_name": "dire",
                    "hero_name": "npc_dota_hero_axe",
                    "items": {"slot0": {"name": "item_blink"}, "slot1": {"name": "item_blade_mail"}},
                }
            },
        }
        brief = build_brief(payload, _make_catalog(), {}, position=1)
        rendered = render_brief(brief)
        self.assertTrue(any("Axe" in entry for entry in brief.get("enemy_items", [])))
        self.assertIn("Blade Mail", rendered)

    def test_retrieve_knowledge_with_matchup_and_secondary_intents(self):
        snippets = retrieve_knowledge(
            "матчап джаггер против акса и что прокачать",
            hero={"localized_name": "Juggernaut", "name": "npc_dota_hero_juggernaut"},
            position=1,
            enemy_heroes=["Axe"],
            catalog=_make_catalog(),
            secondary_intents=["skill"],
            matchup_heroes=("Juggernaut", "Axe"),
        )
        joined = "\n".join(snippets)
        self.assertIn("Axe", joined)

    def test_tone_focus_and_detail_mode_in_prompt_and_sanitizer(self):
        sys_prompt = make_system_prompt(
            "Астра",
            skill_level="amateur",
            coach_tone="tactical",
            custom_focus="не отдавать позицию 1 на рошане",
        )
        self.assertIn("ТОН ТРЕНЕРА: говори максимально чётко", sys_prompt)
        self.assertIn("не отдавать позицию 1 на рошане", sys_prompt)

        rendered = render_system_prompt(
            "Астра",
            "",
            [],
            kind="chat",
            skill_level="beginner",
            coach_tone="brief",
            detail_mode="concise",
        )
        self.assertIn("180 символов", rendered)

        long_text = (
            "Первое важное предложение про линию и фарм крипов в лесу. "
            "Второе важное предложение про покупку БКБ перед выходом на Рошана. "
            "Третье предложение про расстановку вардов и контроль рун мудрости. "
            "Четвёртое предложение про темп карты и давление на вышки."
        )
        concise = sanitize_answer(long_text, detail_mode="concise", skill_level="expert", kind="chat")
        detailed = sanitize_answer(long_text, detail_mode="detailed", skill_level="amateur", kind="chat")
        self.assertLessEqual(len(concise), 205)
        self.assertGreater(len(detailed), len(concise))


class TestCoachRuntimeUpgrades(unittest.TestCase):
    def test_tools_and_execute_tool(self):
        tool_names = {t["function"]["name"] for t in COACH_TOOLS}
        self.assertIn("compare_hero_matchup", tool_names)
        self.assertIn("remember_player_preference", tool_names)

        with tempfile.TemporaryDirectory() as td:
            coach = _make_coach(td)
            cmp_res = coach.execute_tool("compare_hero_matchup", {"hero_a": "Juggernaut", "hero_b": "Axe"})
            self.assertIn("Джаггернаут", cmp_res)
            self.assertIn("Акс", cmp_res)

            mem_res = coach.execute_tool(
                "remember_player_preference",
                {"fact_key": "стиль", "fact_value": "агрессивная игра с blink"},
            )
            self.assertIn("Запомнил", mem_res)
            self.assertEqual(coach.long_memory.facts().get("стиль"), "агрессивная игра с blink")

    def test_preference_extraction_bypasses_llm(self):
        with tempfile.TemporaryDirectory() as td:
            coach = _make_coach(td)
            with patch.object(coach, "_ask_llm") as mock_llm:
                r1 = coach.coach_chat("запомни: я не люблю покупать мидас на керри")
                r2 = coach.coach_chat("моя цель на матч: 60 ластхитов к 10 минуте")
                mock_llm.assert_not_called()
            self.assertIn("Принял", r1)
            self.assertIn("предпочтение", coach.long_memory.facts())
            self.assertIn("60 ластхитов", r2)
            self.assertEqual(coach.custom_focus, "60 ластхитов к 10 минуте")

    def test_build_messages_trims_long_history_to_budget(self):
        with tempfile.TemporaryDirectory() as td:
            coach = _make_coach(td)
            with coach._lock:
                for i in range(20):
                    coach._memory.append({"q": f"вопрос {i} " + ("x" * 160), "a": f"ответ {i} " + ("y" * 280)})
            msgs = coach._build_messages("текущий вопрос", "SYSTEM", include_dynamic=False)
            # system + selected pairs + final user prompt
            self.assertEqual(msgs[-1]["content"], "текущий вопрос")
            self.assertLess(len(msgs), 34)  # существенно короче всех 20*2+2=42 сообщений

    def test_brief_cache_and_item_costs_cache(self):
        with tempfile.TemporaryDirectory() as td:
            coach = _make_coach(td)
            payload = {"map": {"clock_time": 300}, "player": {"gold": 500, "deaths": 0}, "hero": {"name": "npc_dota_hero_axe"}}
            with patch("aidota.coach.build_brief", return_value={"hero": "Axe"}) as mock_bb:
                _, t1 = coach._cached_brief(payload, None)
                _, t2 = coach._cached_brief(payload, None)
                self.assertEqual(mock_bb.call_count, 1)
                self.assertEqual(t1, t2)
                payload["player"]["gold"] = 900
                coach._cached_brief(payload, None)
                self.assertEqual(mock_bb.call_count, 2)

            calls = {"n": 0}

            def fake_costs():
                calls["n"] += 1
                return {"blink": 2250}

            coach.api.item_costs = fake_costs
            self.assertEqual(coach.item_costs()["blink"], 2250)
            self.assertEqual(coach.item_costs()["blink"], 2250)
            self.assertEqual(calls["n"], 1)

    def test_ollama_chat_sets_keep_alive(self):
        with tempfile.TemporaryDirectory() as td:
            coach = _make_coach(td)
            resp = MagicMock()
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {"message": {"content": "Покупай стики и держи линию."}}
            with patch.object(coach._session, "post", return_value=resp) as mock_post:
                out = coach._ollama_chat("gemma3", "что купить", "Ты Астра", temperature=0.3, max_tokens=120)
            self.assertIn("стики", out)
            posted = mock_post.call_args.kwargs["json"]
            self.assertEqual(posted.get("keep_alive"), "10m")


class TestWebBridgeAndAppleHud(unittest.TestCase):
    def test_web_api_settings_and_speak_chat(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config()
            cfg_path = Path(td) / "config.json"
            ctrl = WebGuiController(cfg, cfg_path, enable_tray=False)
            mock_coach = MagicMock()
            mock_coach.coach_chat.return_value = "Бери БКБ"
            mock_tts = MagicMock()
            ctrl.assistant = SimpleNamespace(
                _coach=mock_coach,
                _agent=MagicMock(),
                _tts=mock_tts,
            )
            api = AidotaWebApi(ctrl)
            try:
                res = api.save_settings({
                    "coach_tone": "tactical",
                    "detail_mode": "detailed",
                    "custom_focus": "контроль рошана",
                    "speak_chat": False,
                })
                self.assertTrue(res["success"])
                settings = api.get_settings()
                self.assertEqual(settings["coach_tone"], "tactical")
                self.assertEqual(settings["detail_mode"], "detailed")
                self.assertEqual(settings["custom_focus"], "контроль рошана")
                self.assertFalse(settings["speak_chat"])

                reply = api.send_chat("что собрать?")
                self.assertEqual(reply, "Бери БКБ")
                mock_tts.speak.assert_not_called()

                api.save_settings({"speak_chat": True})
                api.send_chat("а потом?")
                mock_tts.speak.assert_called_once_with(
                    "Бери БКБ",
                    wait=False,
                    priority=SpeechPriority.LLM_COACH,
                )
            finally:
                api.close()

    def test_apple_hud_html_css_js_standards(self):
        web_dir = Path(__file__).resolve().parent.parent / "aidota" / "web"
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        css = (web_dir / "style-modern.css").read_text(encoding="utf-8")
        js = (web_dir / "app.js").read_text(encoding="utf-8")

        for hook in ("cfg-coach-tone", "cfg-detail-mode", "cfg-custom-focus", "cfg-speak-chat", 'aria-selected="true"'):
            self.assertIn(hook, html)

        self.assertIn("color-scheme: dark;", css)
        self.assertIn("in oklab", css)
        self.assertNotIn("in oklch", css)
        self.assertIn("corner-shape: squircle", css)
        self.assertIn("forced-colors: active", css)
        self.assertIn('[aria-selected="true"]', css)
        self.assertIn("border-block-end", css)
        self.assertNotIn("animation-duration: 0.001ms", css)

        for hook in ("cfgCoachTone", "cfgDetailMode", "cfgCustomFocus", "cfgSpeakChat", "aria-selected", "prefers-reduced-motion"):
            self.assertIn(hook, js)


if __name__ == "__main__":
    unittest.main()
