# -*- coding: utf-8 -*-
"""Регрессионные тесты скорости, KV-кэша, Edge->Piper failover и фактологической точности Dota 2."""

from __future__ import annotations

import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from aidota.app import Assistant
from aidota.coach import DEFAULT_LIVE_TTL_SEC, Coach
from aidota.coach_brain import SKIP_TOKEN, StrategyCard, build_brief, make_system_prompt, render_brief
from aidota.coach_memory import CoachMemory
from aidota.commands import ADVICE, Intent
from aidota.config import Config, LlmConfig, PlayerProfile, TtsConfig
from aidota.context import is_hero_in_combat
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.llm_router import DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL, LlmRouter
from aidota.map_timers import CanonicalMapTimers, format_objectives_window, upcoming_objectives
from aidota.telemetry import TelemetrySink
from aidota.tts import HumanTTSManager, SpeechPriority, normalize_for_speech
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


class TestStage1Through4Pipeline(unittest.TestCase):
    def test_replica_trace_jsonl_written_with_latency_and_stt_breakdown(self) -> None:
        tmp = make_temp_dir()
        logs_dir = tmp / "logs"
        sess = _CaptureSession()
        payload = {
            "map": {"clock_time": 600, "matchid": "tr_100", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 1800, "deaths": 0},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "health_percent": 92},
            "items": {"slot0": {"name": "item_power_treads"}, "teleport0": {"name": "item_tpscroll"}},
        }
        try:
            sink = TelemetrySink(logs_dir)
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                _FakeGsi(payload),
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="sk-test-trace",
                    model=DEEPSEEK_CHAT_MODEL,
                ),
                PlayerProfile(default_position=1),
                session=sess,
                state_dir=tmp,
                telemetry=sink,
            )
            answer = coach.coach_chat(
                "что делать сейчас?",
                voice=True,
                stt_raw="что делать сейчас на анти маге",
                stt_normalized="что делать сейчас на антимаге",
                ms_stt=18,
            )
            self.assertTrue(answer)
            traces = sink.recent_traces(limit=10)
            self.assertEqual(len(traces), 1)
            rec = traces[0]
            self.assertEqual(rec["match_id"], "tr_100")
            self.assertEqual(rec["trigger_type"], "voice")
            self.assertEqual(rec["stt_raw"], "что делать сейчас на анти маге")
            self.assertEqual(rec["stt_normalized"], "что делать сейчас на антимаге")
            self.assertEqual(rec["ms_stt"], 18)
            self.assertEqual(rec["route_tier"], "tier1")
            self.assertGreater(rec["prompt_chars"], 50)
            self.assertGreater(rec["prompt_tokens_est"], 10)
            self.assertTrue(sink.traces_path.is_file())
            summary = sink.trace_summary()
            self.assertEqual(summary["total_traces"], 1)
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_contour0_hero_reflexes_and_tts_local_prewarm(self) -> None:
        tmp = make_temp_dir()
        try:
            timers = CanonicalMapTimers()
            low_hp = timers.check_hero_reflexes(
                {
                    "map": {"clock_time": 120, "matchid": "rf_1"},
                    "hero": {"alive": True, "health_percent": 18.0, "level": 4, "mana_percent": 60},
                },
                proactive=True,
            )
            self.assertIsNotNone(low_hp)
            assert low_hp is not None
            self.assertEqual(low_hp[1], 1)

            no_tp = timers.check_hero_reflexes(
                {
                    "map": {"clock_time": 360, "matchid": "rf_1"},
                    "hero": {"alive": True, "health_percent": 90.0, "level": 6, "mana_percent": 80},
                    "items": {"slot0": {"name": "item_power_treads"}},
                },
                proactive=True,
            )
            self.assertIsNotNone(no_tp)
            assert no_tp is not None
            self.assertEqual(no_tp[1], 2)
            self.assertIn("ТП", no_tp[0])

            no_mana = timers.check_hero_reflexes(
                {
                    "map": {"clock_time": 420, "matchid": "rf_1"},
                    "hero": {"alive": True, "health_percent": 90.0, "level": 7, "mana_percent": 12.0},
                    "items": {"slot0": {"name": "item_power_treads"}, "teleport0": {"name": "item_tpscroll"}},
                },
                proactive=True,
            )
            self.assertIsNotNone(no_mana)
            assert no_mana is not None
            self.assertIn("маны", no_mana[0])

            mgr = HumanTTSManager(TtsConfig(enabled=True, provider="local", cache_audio=True), cache_dir=tmp)
            synth_calls: list[str] = []

            def fake_local(txt: str) -> tuple[np.ndarray, int]:
                synth_calls.append(txt)
                return np.ones(160, dtype=np.float32) * 0.05, 22050

            mgr.register_local_backend("silero_stub", fake_local)
            queued = mgr.prewarm_phrase_cache(["Забыт ТП-свиток."], background=False)
            self.assertEqual(queued, 1)
            self.assertEqual(len(synth_calls), 1)
            with patch.object(mgr, "_play_if_timely", return_value=True):
                mgr._say(normalize_for_speech("Забыт ТП-свиток."))
            self.assertEqual(len(synth_calls), 1)
            self.assertIn("Smart Cache", mgr.backend or "")
            mgr.close()
        finally:
            remove_temp_dir(tmp)

    def test_proactive_skip_token_and_ollama_zero_tools_fast_route(self) -> None:
        tmp = make_temp_dir()
        ollama_requests: list[dict] = []

        class _OllamaSkipSession:
            def get(self, url, **kwargs):
                return FakeSession({"/api/tags": {"models": [{"name": "qwen2.5:7b"}]}}).get(url, **kwargs)

            def post(self, url, **kwargs):
                ollama_requests.append(copy.deepcopy(kwargs.get("json") or {}))

                class _Ok:
                    status_code = 200
                    headers = {}

                    def json(self):
                        return {"message": {"content": SKIP_TOKEN}}

                return _Ok()

            def close(self):
                pass

        payload = {
            "map": {"clock_time": 540, "matchid": "ol_1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 1200},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "health_percent": 95},
        }
        try:
            sink = TelemetrySink(tmp / "logs")
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                _FakeGsi(payload),
                LlmConfig(provider="ollama", base_url="http://localhost:11434", model="qwen2.5:7b"),
                PlayerProfile(default_position=1),
                session=_OllamaSkipSession(),
                state_dir=tmp,
                telemetry=sink,
            )
            res = coach.proactive_call("минута 9 без событий", payload)
            self.assertEqual(res, "")
            self.assertEqual(len(ollama_requests), 1)
            self.assertNotIn("tools", ollama_requests[0])
            traces = sink.recent_traces(limit=5)
            self.assertEqual(traces[-1]["drop_reason"], "skip")
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_contour2_strategy_card_lifecycle_and_kv_prefix_stability(self) -> None:
        tmp = make_temp_dir()

        class _StrategistSession:
            def __init__(self):
                self.requests: list[dict] = []

            def get(self, url, **kwargs):
                return _Resp("")

            def post(self, url, **kwargs):
                body = copy.deepcopy(kwargs.get("json") or {})
                self.requests.append(body)
                content = str(body["messages"][-1]["content"])
                if "Повод обновления плана" in content:
                    return _Resp(
                        json.dumps(
                            {
                                "win_condition": "дофармить Battle Fury к 13 минуте и сплитить",
                                "main_threat": "хроносфера Войда",
                                "playstyle_now": "split",
                                "next_items_plan": [{"item": "Manta Style", "why": "сбросить сайленс"}],
                            },
                            ensure_ascii=False,
                        )
                    )
                return _Resp("Толкай топ, враги далеко.")

            def close(self):
                pass

        sess = _StrategistSession()
        payload = {
            "map": {"clock_time": 720, "matchid": "st_1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 1900},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "health_percent": 90},
            "items": {"slot0": {"name": "item_bfury"}},
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
                    api_key="sk-test-strat",
                    model=DEEPSEEK_CHAT_MODEL,
                ),
                PlayerProfile(default_position=1),
                session=sess,
                state_dir=tmp,
            )
            r1 = coach.coach_chat("что делать?", voice=True)
            self.assertIn("Толкай", r1)

            card = coach.update_strategy_card(payload, force_llm=True, reason="key_item:bfury")
            self.assertIsInstance(card, StrategyCard)
            assert card is not None
            self.assertEqual(card.playstyle_now, "split")
            self.assertIn("Manta Style", str(card.next_items_plan))

            r2 = coach.coach_chat("куда сместиться?", voice=True)
            self.assertIn("Толкай", r2)
            chat_reqs = [r for r in sess.requests if r["messages"][0]["content"] != "Ты фоновый макро-стратег Dota 2. Отвечай строго одним JSON-объектом."]
            self.assertEqual(len(chat_reqs), 2)
            self.assertEqual(chat_reqs[0]["messages"][0]["content"], chat_reqs[1]["messages"][0]["content"])
            self.assertIn("ПЛАН МАТЧА (СТРАТЕГ):", chat_reqs[1]["messages"][-1]["content"])
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_ttl_and_hero_died_and_phase_changed_drop_reasons(self) -> None:
        tmp = make_temp_dir()
        payload = {
            "map": {"clock_time": 800, "matchid": "dr_1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 1200},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "health_percent": 80},
        }
        gsi = _FakeGsi(copy.deepcopy(payload))

        class _SlowOrMutatingSession:
            def __init__(self):
                self.delay = 0.0
                self.mutate = None

            def post(self, url, **kwargs):
                if self.delay:
                    time.sleep(self.delay)
                if self.mutate:
                    self.mutate()
                return _Resp("Выходи на врага под ультом.")

            def get(self, url, **kwargs):
                return _Resp("")

            def close(self):
                pass

        sess = _SlowOrMutatingSession()
        try:
            sink = TelemetrySink(tmp / "logs")
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                gsi,
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="sk-test-drop",
                    model=DEEPSEEK_CHAT_MODEL,
                ),
                PlayerProfile(default_position=1),
                session=sess,
                state_dir=tmp,
                telemetry=sink,
            )
            sess.delay = 0.05
            res_ttl = coach.proactive_call("оконце", payload, ttl_seconds=0.01)
            self.assertEqual(res_ttl, "")
            self.assertEqual(sink.recent_traces(limit=1)[0]["drop_reason"], "stale_ttl")

            sess.delay = 0.0
            gsi.payload = copy.deepcopy(payload)
            sess.mutate = lambda: gsi.payload["hero"].__setitem__("alive", False)
            res_died = coach.proactive_call("драка", gsi.payload, ttl_seconds=DEFAULT_LIVE_TTL_SEC)
            self.assertEqual(res_died, "")
            self.assertEqual(sink.recent_traces(limit=1)[0]["drop_reason"], "hero_died")

            gsi.payload = copy.deepcopy(payload)
            sess.mutate = lambda: gsi.payload["map"].__setitem__("game_state", "DOTA_GAMERULES_STATE_POST_GAME")
            res_phase = coach.coach_chat("идем на хг?", voice=True)
            self.assertEqual(res_phase, "")
            self.assertEqual(sink.recent_traces(limit=1)[0]["drop_reason"], "phase_changed")
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_sse_streaming_sentences_and_skip_guard_and_barge_in_combat(self) -> None:
        tmp = make_temp_dir()

        class _SseResp:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def __init__(self, deltas: list[str]):
                self._lines = [
                    f'data: {json.dumps({"choices": [{"delta": {"content": d}}]})}'
                    for d in deltas
                ] + ["data: [DONE]"]

            def iter_lines(self, decode_unicode: bool = True):
                for line in self._lines:
                    time.sleep(0.01)
                    yield line

        class _SseSession:
            def __init__(self, deltas: list[str]):
                self.deltas = deltas

            def post(self, url, **kwargs):
                return _SseResp(self.deltas)

            def get(self, url, **kwargs):
                return _Resp("")

            def close(self):
                pass

        streamed: list[str] = []
        payload = {
            "map": {"clock_time": 420, "matchid": "sse_1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "gold": 900},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "health_percent": 90},
        }
        try:
            sink = TelemetrySink(tmp / "logs")
            api = DotaApi(tmp, session=FakeSession({}))
            sess = _SseSession(["Отступи ", "под вышку. ", "Жди ротацию ", "саппорта."])
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                _FakeGsi(payload),
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="sk-test-sse",
                    model=DEEPSEEK_CHAT_MODEL,
                ),
                PlayerProfile(default_position=1),
                session=sess,
                state_dir=tmp,
                telemetry=sink,
            )
            ans = coach.coach_chat("что делать?", voice=True, on_sentence=streamed.append)
            self.assertEqual(len(streamed), 2)
            self.assertIn("вышку", streamed[0])
            self.assertIn("саппорта", streamed[1])
            self.assertIn("Отступи", ans)
            trace = sink.recent_traces(limit=1)[0]
            self.assertGreaterEqual(trace["ms_llm_ttft"], 0)
            self.assertLessEqual(trace["ms_llm_ttft"], trace["ms_llm_total"] + 5)

            skip_streamed: list[str] = []
            sess.deltas = ["SK", "IP"]
            res_skip = coach.proactive_call("рутина", payload, on_sentence=skip_streamed.append)
            self.assertEqual(res_skip, "")
            self.assertEqual(skip_streamed, [])

            self.assertTrue(
                is_hero_in_combat(
                    {"hero": {"alive": True, "health_percent": 75.0}},
                    prev_hp_pct=92.0,
                )
            )
            cfg = Config()
            cfg.gsi.enabled = False
            cfg.tts.enabled = False
            assistant = Assistant(cfg, text_mode=True)
            try:
                with patch.object(assistant._tts, "barge_in") as mock_barge:
                    assistant._on_ptt_status(True)
                    mock_barge.assert_called_once_with(preserve_critical=True)
            finally:
                assistant.stop()
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_coach_memory_rejects_untrusted_noise_and_keeps_verified(self) -> None:
        tmp = make_temp_dir()
        try:
            mem = CoachMemory(tmp / "coach_memory.json")
            mem.remember("вопрос", "SKIP")
            mem.remember("вопрос", "Купи БКБ (Поправка: Black King Bar уже куплен)")
            mem.remember("вопрос", "API-ключ недействителен или истёк (HTTP 401)")
            mem.remember("вопрос", "Случайный бред", validated=False)
            mem.remember_decision("слот1", "Собираем Manta Style до 20 минуты")
            mem.remember_verified_fact("враг_бкб", "У Войда уже есть BKB")
            mem.remember("что делать на линии?", "Отводи крипов на 17 секунде.")

            summary = mem.summary()
            self.assertNotIn("SKIP", summary)
            self.assertNotIn("Поправка:", summary)
            self.assertNotIn("HTTP 401", summary)
            self.assertNotIn("Случайный бред", summary)
            self.assertIn("Manta Style", summary)
            self.assertIn("Войда уже есть BKB", summary)
            self.assertIn("17 секунде", summary)
        finally:
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
