# -*- coding: utf-8 -*-
"""Регрессионные тесты скорости, KV-кэша, Edge->Piper failover и фактологической точности Dota 2."""

from __future__ import annotations

import copy
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from aidota.app import Assistant
from aidota.coach import Coach
from aidota.coach_brain import build_brief, make_system_prompt, render_brief
from aidota.commands import ADVICE, Intent
from aidota.config import Config, LlmConfig, PlayerProfile, TtsConfig
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.llm_router import DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL, LlmRouter
from aidota.map_timers import CanonicalMapTimers, format_objectives_window, upcoming_objectives
from aidota.tts import HumanTTSManager
from tests.helpers import FakeSession, make_temp_dir, remove_temp_dir

HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
    {"id": 86, "name": "npc_dota_hero_rubick", "localized_name": "Rubick"},
    {"id": 104, "name": "npc_dota_hero_legion_commander", "localized_name": "Legion Commander"},
]


class _FakeGsi:
    def __init__(self, payload: dict | None = None):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 0.4 if self.payload else None


class _Resp:
    def __init__(self, content: str):
        self.status_code = 200
        self.headers = {}
        self.text = content
        self._payload = {"choices": [{"message": {"content": content}}]}

    def json(self):
        return self._payload


class _CaptureSession:
    def __init__(self):
        self.requests: list[dict] = []

    def post(self, url, **kwargs):
        self.requests.append({"url": url, "json": copy.deepcopy(kwargs.get("json") or {})})
        return _Resp("Купи Magic Stick и варды.")

    def get(self, url, **kwargs):
        return _Resp("")

    def close(self):
        pass


class TestEdgeTtsCooldownFailover(unittest.TestCase):
    def test_edge_failure_threshold_activates_cooldown_to_piper(self) -> None:
        tmp = make_temp_dir()
        try:
            mgr = HumanTTSManager(TtsConfig(enabled=True, provider="edge", cache_audio=False), cache_dir=tmp)
            mgr._edge_failure_threshold = 2
            mgr._edge_cooldown_seconds = 60.0
            edge_calls = {"n": 0}
            piper_calls = {"n": 0}

            def fail_edge(_text: str):
                edge_calls["n"] += 1
                raise RuntimeError("NoAudioReceived")

            def ok_piper(_text: str):
                piper_calls["n"] += 1
                return np.ones(400, dtype=np.float32) * 0.1, 22050

            with (
                patch.object(mgr, "_find_piper_binary_and_model", return_value=(Path("piper.exe"), Path("m.onnx"))),
                patch.object(mgr, "_synthesize_edge_audio", side_effect=fail_edge),
                patch.object(mgr, "_synthesize_piper_audio", side_effect=ok_piper),
                patch.object(mgr, "_play_if_timely", return_value=True),
            ):
                mgr._say("первая фраза")
                mgr._say("вторая фраза")
                # После 2 подряд отказов Edge должен уйти на cooldown и сразу идти в Piper.
                mgr._say("третья фраза")

            self.assertEqual(edge_calls["n"], 2)
            self.assertEqual(piper_calls["n"], 3)
            self.assertGreater(mgr._edge_cooldown_until, time.monotonic())
        finally:
            remove_temp_dir(tmp)


class TestLivePromptEfficiencyAndRouter(unittest.TestCase):
    def test_router_respects_allow_tools_override_and_direct_chat_keeps_tools(self) -> None:
        router = LlmRouter()
        llm = LlmConfig(
            provider="openai_compatible",
            base_url=DEEPSEEK_BASE_URL,
            api_key="sk-test-live-123",
            model=DEEPSEEK_CHAT_MODEL,
        )
        suppressed = router.plan_route(
            llm,
            kind="chat",
            voice=False,
            game_state="DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
            hero_alive=True,
            allow_tools=False,
        )
        self.assertFalse(suppressed.allow_tools)

        direct = router.plan_route(llm, kind="chat", voice=False, game_state="")
        self.assertTrue(direct.allow_tools)

    def test_compact_turn_assembly_avoids_duplicate_blocks_and_caps_history_budget(self) -> None:
        tmp = make_temp_dir()
        sess = _CaptureSession()
        payload = {
            "map": {
                "matchid": "9106395585",
                "clock_time": 420,
                "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                "game_mode": 22,
                "daytime": False,
                "radiant_score": 4,
                "dire_score": 5,
            },
            "player": {"team_name": "radiant", "gold": 1300, "gpm": 260, "xpm": 310, "last_hits": 12},
            "hero": {
                "name": "npc_dota_hero_rubick",
                "level": 5,
                "health_percent": 0,
                "mana_percent": 0,
                "alive": False,
                "respawn_seconds": 18,
                "buyback_cost": 350,
                "buyback_cooldown": 0,
            },
            "items": {
                "slot0": {"name": "item_magic_stick"},
                "slot1": {"name": "item_boots"},
                "teleport0": {"name": "item_tpscroll"},
            },
            "draft": {
                "activeteam": 2,
                "team2": {"pick0_class": "npc_dota_hero_rubick"},
                "team3": {"pick0_class": "npc_dota_hero_legion_commander"},
            },
        }
        try:
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                _FakeGsi(payload),
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="sk-test-live-123",
                    model=DEEPSEEK_CHAT_MODEL,
                ),
                PlayerProfile(default_position=5),
                session=sess,
                state_dir=tmp,
            )
            # Заполняем историю повторами для проверки компактного бюджета
            for i in range(12):
                coach.remember(f"вопрос {i}", f"копи на Блинк к четырнадцатой минуте номер {i} " * 5)

            coach.coach_chat("что делать дальше", voice=False)
            self.assertEqual(len(sess.requests), 1)
            sent_json = sess.requests[0]["json"]
            self.assertNotIn("tools", sent_json)
            msgs = sent_json["messages"]
            joined = "\n".join(m["content"] for m in msgs)
            self.assertIn("ТАКТИЧЕСКИЙ БРИФ", joined)
            self.assertIn("СОСТОЯНИЕ МАТЧА (КРАТКО):", joined)
            # Дублирующие заголовки GSI Снимок / Инвентарь: не должны повторяться рядом с match_snapshot
            self.assertNotIn("GSI Снимок (", joined)
            self.assertNotIn("Инвентарь: Magic Stick", joined)
            self.assertIn("респавн 18", joined)
            self.assertIn("байбек НЕ трать", joined)
            # История в _build_messages ограничена ~1800 символами (не 6000)
            history_chars = sum(len(m["content"]) for m in msgs[1:-1])
            self.assertLessEqual(history_chars, 2100)
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)


class TestFactualGroundingAndPreemption(unittest.TestCase):
    def test_map_timers_and_system_prompt_ground_water_lotus_neutral_and_buyback(self) -> None:
        events = upcoming_objectives(105, window_sec=45)
        water = next(e for e in events if e["key"] == "water")
        self.assertIn("для мидера", water["label"])

        events_lotus = upcoming_objectives(165, window_sec=45)
        lotus = next(e for e in events_lotus if e["key"] == "lotus")
        self.assertIn("хил", lotus["label"])

        events_neutral = upcoming_objectives(405, window_sec=45)
        neutral = next(e for e in events_neutral if e["key"] == "neutral_tier")
        self.assertIn("лесу", neutral["label"])

        fmt = format_objectives_window(105, window_sec=45)
        self.assertIn("для мидера", fmt)

        tracker = CanonicalMapTimers()
        pos5_alert = tracker.check_mid_runes(100, position=5)
        self.assertIsNotNone(pos5_alert)
        assert pos5_alert is not None
        self.assertIn("не уходи с лёгкой без нужды", pos5_alert)

        sys_prompt = make_system_prompt("Джарвис")
        self.assertIn("ФАКТИЧЕСКАЯ ТОЧНОСТЬ", sys_prompt)
        self.assertIn("не дают золота или опыта", sys_prompt)
        self.assertIn("байбек", sys_prompt)

    def test_render_brief_compact_keeps_respawn_and_early_buyback_guard(self) -> None:
        brief = build_brief(
            {
                "map": {"clock_time": 420, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                "player": {"gold": 1300},
                "hero": {"name": "npc_dota_hero_rubick", "alive": False, "buyback_cost": 350, "buyback_cooldown": 0},
                "items": {"slot0": {"name": "item_boots"}},
            },
            HeroCatalog(HEROES),
            position=5,
            hero=HEROES[1],
        )
        compact = render_brief(brief, compact=True, respawn_seconds=16)
        self.assertIn("респавн 16", compact)
        self.assertIn("байбек НЕ трать", compact)
        self.assertNotIn("Инвентарь:", compact)

    def test_assistant_handle_advice_forwards_generation_id(self) -> None:
        cfg = Config()
        cfg.gsi.enabled = False
        cfg.tts.enabled = False
        assistant = Assistant(cfg, text_mode=True)
        try:
            with (
                patch.object(assistant._coach, "coach_chat", return_value="Проверь слоты врага.") as mock_chat,
                patch.object(assistant, "_answer"),
            ):
                assistant._handle_advice(Intent(kind=ADVICE, raw="что делать дальше"))
                mock_chat.assert_called_once()
                self.assertIn("generation_id", mock_chat.call_args.kwargs)
                self.assertGreaterEqual(mock_chat.call_args.kwargs["generation_id"], 1)
        finally:
            assistant.stop()


if __name__ == "__main__":
    unittest.main()
