# -*- coding: utf-8 -*-
"""Тесты Фазы 4 (Fork F2 + Fork A1): SSOT таймеры, устранение дублей и стабильность KV-префикса."""

from __future__ import annotations

import copy
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import aidota.coach as coach_mod
import aidota.context as context_mod
from aidota import commands
from aidota.app import Assistant
from aidota.coach import Coach
from aidota.config import Config, LlmConfig, PlayerProfile
from aidota.context import AutonomousCoachAgent, MidTimersTracker, analyze_enemy_threats
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.map_timers import (
    NEUTRAL_TIERS_NORMAL,
    NEUTRAL_TIERS_TURBO,
    CanonicalMapTimers,
    compute_map_timers,
    describe_neutral_schedule,
    detect_enemy_mid_level,
    extract_match_id,
)
from aidota.skills.cooldown_tracker import CooldownTrackerSkill
from aidota.skills.macro_timer import MacroTimerSkill
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import FakeSession, make_temp_dir, remove_temp_dir

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage"},
    {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut"},
    {"id": 41, "name": "npc_dota_hero_faceless_void", "localized_name": "Faceless Void"},
    {"id": 44, "name": "npc_dota_hero_phantom_assassin", "localized_name": "Phantom Assassin"},
    {"id": 74, "name": "npc_dota_hero_invoker", "localized_name": "Invoker"},
    {"id": 86, "name": "npc_dota_hero_rubick", "localized_name": "Rubick"},
    {"id": 99, "name": "npc_dota_hero_bristleback", "localized_name": "Bristleback"},
]


class _FakeGsi:
    def __init__(self, payload: dict | None = None):
        self.payload = payload

    def snapshot(self):
        return self.payload

    def age(self):
        return 0.5 if self.payload else None


class _Resp:
    def __init__(self, content: str):
        self.status_code = 200
        self.headers = {}
        self.text = content
        self._payload = {"choices": [{"message": {"content": content}}]}

    def json(self):
        return self._payload


class _CaptureOpenAISession:
    def __init__(self):
        self.requests: list[dict] = []

    def post(self, url, **kwargs):
        self.requests.append({"url": url, "json": copy.deepcopy(kwargs.get("json") or {})})
        return _Resp("Держи дистанцию и бери Force Staff для сейва.")

    def get(self, url, **kwargs):
        return _Resp("")

    def close(self):
        pass


def _make_coach(tmp: Path, *, payload: dict | None = None, session=None) -> Coach:
    api = DotaApi(tmp, session=FakeSession({}))
    llm = LlmConfig(
        provider="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        api_key="sk-test-phase4-key",
        model="deepseek-chat",
        routing_mode="auto",
    )
    profile = PlayerProfile(
        default_position=5,
        mmr_bracket="Legend",
        signature_heroes=["Rubick", "Invoker"],
        hero_pool={5: ["Rubick", "Lion"]},
        custom_focus="стакать на :53",
    )
    return Coach(
        api,
        HeroCatalog(HEROES),
        _FakeGsi(payload),
        llm,
        profile,
        session=session or FakeSession({}),
        state_dir=tmp,
    )


def _gsi_payload(
    *,
    clock_time: int = 120,
    matchid: str = "700001",
    game_mode: int = 22,
    gold: int = 1400,
    enemy_players: dict | None = None,
) -> dict:
    return {
        "map": {
            "matchid": matchid,
            "clock_time": clock_time,
            "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
            "game_mode": game_mode,
            "daytime": True,
            "radiant_score": 8,
            "dire_score": 6,
        },
        "player": {
            "team_name": "radiant",
            "kills": 2,
            "deaths": 1,
            "assists": 4,
            "gold": gold,
            "last_hits": 25,
            "denies": 3,
            "gpm": 380,
            "xpm": 430,
        },
        "hero": {
            "name": "npc_dota_hero_rubick",
            "level": 5,
            "health_percent": 90,
            "mana_percent": 80,
            "alive": True,
            "experience_percent": 30,
        },
        "items": {
            "slot0": {"name": "item_tranquil_boots"},
            "slot1": {"name": "item_magic_wand"},
            "teleport0": {"name": "item_tpscroll"},
        },
        "draft": {
            "activeteam": 2,
            "team2": {"pick0_class": "npc_dota_hero_rubick", "pick1_class": "npc_dota_hero_juggernaut"},
            "team3": {"pick0_class": "npc_dota_hero_invoker", "pick1_class": "npc_dota_hero_faceless_void"},
        },
        "allplayers": enemy_players
        or {
            "player5": {"team_name": "dire", "hero": {"name": "npc_dota_hero_invoker", "level": 4}, "lane": 2},
            "player6": {
                "team_name": "dire",
                "hero": {"name": "npc_dota_hero_faceless_void", "level": 5},
                "lane": 1,
            },
        },
    }


class CanonicalMapTimersTests(unittest.TestCase):
    def test_neutral_tiers_edge_triggered_normal_and_turbo(self):
        self.assertEqual([t[1] for t in NEUTRAL_TIERS_NORMAL], [420, 900, 1500, 2100, 3600])
        self.assertEqual([t[1] for t in NEUTRAL_TIERS_TURBO], [210, 450, 750, 1050, 1800])

        timers = CanonicalMapTimers()
        # Первый раз на 7:00 (420с) срабатывает, дальше на 421/422с не дублируется
        first = timers.check_macro_timers(420, position=1, turbo=False)
        self.assertIsNotNone(first)
        self.assertIn("1-го разряда", first or "")
        self.assertIsNone(timers.check_macro_timers(421, position=1, turbo=False))
        # Большой скачок в середину 18:00 не должен задним числом спамить пропущенный 15:00
        self.assertIsNone(timers.check_macro_timers(1080, position=1, turbo=False))

        turbo_timers = CanonicalMapTimers()
        turbo_msg = turbo_timers.check_macro_timers(210, position=1, turbo=True)
        self.assertIsNotNone(turbo_msg)
        self.assertIn("Турбо", turbo_msg or "")
        self.assertIsNone(turbo_timers.check_macro_timers(211, position=1, turbo=True))

        self.assertIn("15:00", describe_neutral_schedule(500, turbo=False))
        self.assertIn("7-й, 15-й, 25-й, 35-й и 60-й", describe_neutral_schedule(None, turbo=False))

    def test_matchid_reset_across_skills_and_agent(self):
        shared_core = CanonicalMapTimers()
        macro = MacroTimerSkill(core=shared_core)
        tracker = MidTimersTracker(core=shared_core)
        cd = CooldownTrackerSkill()
        agent = AutonomousCoachAgent(autonomy_level="medium", macro_timer=macro)

        # Матч 1: сработал Тир 1 нейтралок и зафиксирован Рошан
        self.assertIsNotNone(macro.check(420, position=1, match_id="match_A"))
        self.assertIsNone(tracker.check_macro_timers(421, position=1, match_id="match_A"))
        cd.record_roshan(1000, match_id="match_A")
        self.assertEqual(cd.roshan_death_sec, 1000.0)

        # Смена matchid в новом матче автоматически сбрасывает оба трекера и Рошана
        msg_new = macro.check(420, position=1, match_id="match_B")
        self.assertIsNotNone(msg_new)
        self.assertEqual(extract_match_id({"map": {"matchid": "match_B"}}), "match_B")
        self.assertIsNone(cd.check(420, match_id="match_B"))
        self.assertIsNone(cd.roshan_death_sec)

        # Смена matchid в AutonomousCoachAgent тоже сбрасывает состояние
        agent.process_tick(_gsi_payload(clock_time=420, matchid="match_B"))
        agent.process_tick(_gsi_payload(clock_time=420, matchid="match_C"))
        self.assertEqual(agent._active_match_id, "match_C")

    def test_no_double_firing_and_autonomy_off_guard(self):
        shared_core = CanonicalMapTimers()
        macro = MacroTimerSkill(core=shared_core)
        agent = AutonomousCoachAgent(autonomy_level="medium", macro_timer=macro)

        payload = _gsi_payload(clock_time=420, matchid="9001")
        # Coach/MacroTimerSkill срабатывает первым -> Agent в том же тике не дублирует
        first = macro.check(420, position=5, payload=payload, match_id="9001")
        second = agent.process_tick(payload, HeroCatalog(HEROES), position=5)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

        # При autonomy_level == "off" агент НЕ шлёт даже таймеры рун / Аегис / Рошана,
        # а ручной пользовательский таймер (add_custom_timer) всё равно работает.
        off_core = CanonicalMapTimers(proactive_enabled=False)
        off_macro = MacroTimerSkill(core=off_core)
        off_agent = AutonomousCoachAgent(autonomy_level="off", macro_timer=off_macro)

        off_payload = _gsi_payload(clock_time=100, matchid="9002")
        off_payload["map"]["aegis"] = "radiant"
        off_payload["map"]["roshan_respawn_timer"] = 480
        self.assertTrue(off_agent.is_autonomy_off())
        self.assertIsNone(off_agent.process_tick(off_payload, HeroCatalog(HEROES), position=5))
        self.assertIsNone(off_macro.check(100, position=5, payload=off_payload, match_id="9002"))

        off_macro.add_custom_timer(150, "забрать мудрость")
        custom_alert = off_macro.check(151, position=5, payload=_gsi_payload(clock_time=151, matchid="9002"))
        self.assertIsNotNone(custom_alert)
        self.assertIn("забрать мудрость", custom_alert or "")

    def test_enemy_mid_lvl6_does_not_fire_on_sidelaner_max_level(self):
        # Без указания линии: враг Faceless Void (керри) 7 ур., а мидер Invoker 4 ур.
        payload_roles = _gsi_payload(
            clock_time=240,
            matchid="mid_check_1",
            enemy_players={
                "player5": {"team_name": "dire", "hero": {"name": "npc_dota_hero_invoker", "level": 4}},
                "player6": {"team_name": "dire", "hero": {"name": "npc_dota_hero_faceless_void", "level": 7}},
            },
        )
        self.assertEqual(detect_enemy_mid_level(payload_roles), 4)
        timers = CanonicalMapTimers()
        self.assertIsNone(timers.check_mid_level_6(payload_roles, clock_seconds=240))

        # С явной разметкой линии: сайдлайнер 7 ур., мидер 6 ур. -> только при мидере 6+ срабатывает
        payload_mid6 = _gsi_payload(
            clock_time=270,
            matchid="mid_check_2",
            enemy_players={
                "player5": {"team_name": "dire", "hero": {"name": "npc_dota_hero_invoker", "level": 6}, "lane": 2},
                "player6": {"team_name": "dire", "hero": {"name": "npc_dota_hero_faceless_void", "level": 8}, "lane": 1},
            },
        )
        self.assertEqual(detect_enemy_mid_level(payload_mid6), 6)
        alert = timers.check_mid_level_6(payload_mid6, clock_seconds=270)
        self.assertIsNotNone(alert)
        self.assertIn("мидер достиг шестого уровня", alert or "")

    def test_assistant_syncs_macro_timer_with_autonomy_off_and_neutrals(self):
        cfg = Config()
        cfg.player_profile.autonomy_level = "medium"
        with (
            patch("aidota.app.DotaApi") as mock_api,
            patch("aidota.app.HeroCatalog"),
            patch("aidota.app.GsiServer"),
            patch("aidota.app.HumanTTSManager"),
            patch("aidota.app.VoiceInput"),
            patch("aidota.app.HeroVision"),
        ):
            mock_api.return_value.heroes = []
            mock_api.return_value.items.return_value = {}
            app = Assistant(cfg, text_mode=True, disable_overlay=True)
            self.assertIs(app._coach.macro_timer.core, app._agent.mid_timers.core)

            said: list[str] = []
            app._say = lambda text, **_kw: said.append(text)
            app.set_autonomy_level("off")
            self.assertFalse(app._coach.macro_timer.proactive_enabled)

            app._coach.gsi = _FakeGsi(_gsi_payload(clock_time=800, game_mode=22))
            app._handle(types.SimpleNamespace(kind=commands.NEUTRALS_ALERT, arg=None, position=None))
            self.assertTrue(said and "15:00" in said[-1])


class SingleSourceCountersAndWebUiTests(unittest.TestCase):
    def test_duplicate_counter_tables_removed_and_ssot_preserved(self):
        self.assertFalse(hasattr(coach_mod, "_COUNTER_HINTS"))
        self.assertFalse(hasattr(coach_mod, "_ENEMY_ITEM_HINTS"))
        self.assertFalse(hasattr(context_mod, "_ENEMY_THREAT_HINTS"))
        self.assertFalse(hasattr(context_mod, "_HERO_THREAT_ALIASES"))

        threats = analyze_enemy_threats(
            _gsi_payload(clock_time=600),
            HeroCatalog(HEROES),
        )
        self.assertTrue(any("Faceless Void" in t and "Хроносфера" in t for t in threats))

        tmp = make_temp_dir()
        try:
            coach = _make_coach(tmp, payload=_gsi_payload(clock_time=600))
            hints = coach._enemy_item_hints_for(["Faceless Void", "Bristleback"], limit=2)
            self.assertEqual(len(hints), 2)
            self.assertIn("Aeon Disk", hints[0])
            void_hero = coach.catalog.find("Faceless Void").hero
            c_adv = coach.counter_advice(void_hero)
            self.assertIn("БКБ от хроносферы не спасает", c_adv)
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_web_api_macro_timers_roshan_and_clean_html_js(self):
        tmp = make_temp_dir()
        ctrl = None
        coach = None
        api = None
        try:
            cfg = Config()
            cfg.gsi.enabled = False
            ctrl = WebGuiController(cfg, PROJECT_ROOT, Path(tmp) / "cfg.json")
            api = AidotaWebApi(ctrl)
            coach = _make_coach(tmp, payload=_gsi_payload(clock_time=430, matchid="web_1"))
            ctrl.assistant = types.SimpleNamespace(_coach=coach, _catalog=HeroCatalog(HEROES), _api=coach.api)

            roshan_res = api.record_roshan()
            self.assertTrue(roshan_res["success"])
            self.assertEqual(roshan_res["roshan_death_sec"], 430.0)
            self.assertTrue(roshan_res["roshan"]["recorded"])

            state = api.get_state()
            self.assertFalse(state["rust_active"])
            mt = state["gsi"]["macro_timers"]
            self.assertEqual(mt["lotus_remaining_sec"], 110)  # 540 - 430
            self.assertEqual(mt["neutral_next_tier"], 2)  # следующая после 7:00 — Тир 2 на 15:00
            self.assertIn("Тир 2", mt["neutral_label"])
            self.assertTrue(mt["roshan"]["recorded"])

            html = (PROJECT_ROOT / "aidota" / "web" / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("Rust Core", html)
            self.assertNotIn("rust-status-pill", html)
            self.assertIn("7:00 / 15:00 / 25:00", html)
            self.assertNotIn("17:00", html)

            js = (PROJECT_ROOT / "aidota" / "web" / "app.js").read_text(encoding="utf-8")
            self.assertNotIn("rust-status-pill", js)
            self.assertNotIn("speakPhrase", js)
            self.assertIn('callApi("record_roshan")', js)
            self.assertIn("timer-val-lotus", js)
            self.assertIn("timer-val-neutral", js)
            self.assertIn("window.speakMessage", js)
        finally:
            if ctrl is not None and getattr(ctrl, "tray", None) is not None:
                ctrl.tray.stop()
            if api is not None and hasattr(api, "close"):
                api.close()
            if coach is not None:
                coach.close()
            remove_temp_dir(tmp)


class KvPrefixCachingAndReplayTests(unittest.TestCase):
    def test_kv_prefix_byte_identical_across_ticks_and_kinds(self):
        tmp = make_temp_dir()
        sess = _CaptureOpenAISession()
        gsi = _FakeGsi(_gsi_payload(clock_time=120, matchid="kv_100", gold=900))
        try:
            api = DotaApi(tmp, session=FakeSession({}))
            coach = Coach(
                api,
                HeroCatalog(HEROES),
                gsi,
                LlmConfig(
                    provider="openai_compatible",
                    base_url="https://api.deepseek.com/v1",
                    api_key="sk-test-phase4-key",
                    model="deepseek-chat",
                ),
                PlayerProfile(
                    default_position=5,
                    mmr_bracket="Legend",
                    signature_heroes=["Rubick", "Invoker"],
                    hero_pool={5: ["Rubick", "Lion"]},
                    custom_focus="не отдавать мид руны",
                ),
                session=sess,
                state_dir=tmp,
            )

            # Последовательные тики GSI и разные маршруты/intent-ы
            gsi.payload = _gsi_payload(clock_time=405, matchid="kv_100", gold=1250)
            coach.coach_chat("Что собрать против Войда?")

            gsi.payload = _gsi_payload(clock_time=425, matchid="kv_100", gold=1900)
            coach.coach_chat("почему нам тяжело в драке?")

            gsi.payload = _gsi_payload(clock_time=610, matchid="kv_100", gold=2450)
            coach.buy_next()

            gsi.payload = _gsi_payload(clock_time=910, matchid="kv_100", gold=3100)
            coach.game_analysis()

            self.assertGreaterEqual(len(sess.requests), 4)
            system_prefixes = [req["json"]["messages"][0]["content"] for req in sess.requests]
            self.assertEqual(len(set(system_prefixes)), 1, "system_prefix должен быть байтово-идентичным для DeepSeek KV Cache")
            prefix = system_prefixes[0]
            self.assertNotIn("1250", prefix)
            self.assertNotIn("3100", prefix)
            self.assertNotIn("ДЛЯ РАЗБОРА:", prefix)
            self.assertNotIn("не отдавать мид руны", prefix)

            # Компактное состояние и волатильные данные — только после префикса (в dynamic_suffix)
            joined_tails = [
                "\n".join(m["content"] for m in req["json"]["messages"][1:])
                for req in sess.requests
            ]
            self.assertTrue(all("СОСТОЯНИЕ МАТЧА (КРАТКО):" in tail for tail in joined_tails))
            self.assertTrue(any("Объективы (30–45с / Рошан):" in tail for tail in joined_tails))
        finally:
            if "coach" in locals():
                coach.close()
            remove_temp_dir(tmp)

    def test_deterministic_gsi_session_replay(self):
        tmp = make_temp_dir()
        coach = None
        try:
            coach = _make_coach(tmp, payload=_gsi_payload(clock_time=99, matchid="replay_1"))
            agent = AutonomousCoachAgent(autonomy_level="medium", macro_timer=coach.macro_timer)

            # На 101с отвод (:41) может сработать отдельно (не дублируясь с рунами на 100с).
            timeline_ticks = [
                (100, "replay_1"),   # водные руны за 20с -> ровно 1 алерт
                (101, "replay_1"),   # повтор водных рун не происходит; на 1:41 (:41) триггерится отвод pos 5
                (400, "replay_1"),   # руна мудрости на 7-й мин -> ровно 1 алерт
                (420, "replay_1"),   # нейтралки Тир 1 -> ровно 1 алерт
                (1180, "replay_1"),  # Торментор за 20с -> ровно 1 алерт
                (100, "replay_2"),   # новая игра (новый matchid) -> водные руны снова срабатывают
            ]
            alerts: list[tuple[int, str, str]] = []
            for sec, mid in timeline_ticks:
                payload = _gsi_payload(clock_time=sec, matchid=mid)
                coach.gsi.payload = payload
                a1 = coach.check_skills_alerts()
                a2 = agent.process_tick(payload, coach.catalog, position=5)
                self.assertFalse(a1 and a2, f"Двойное срабатывание на {sec}s / {mid}")
                triggered = a1 or a2
                if triggered:
                    alerts.append((sec, mid, triggered))

            self.assertEqual([(sec, mid) for sec, mid, _ in alerts], [
                (100, "replay_1"),
                (101, "replay_1"),
                (400, "replay_1"),
                (420, "replay_1"),
                (1180, "replay_1"),
                (100, "replay_2"),
            ])
            # Проверка сериализации JSON-среза карты в replay-точке
            mt = compute_map_timers(1180, roshan_death_sec=1000)
            encoded = json.dumps(mt, ensure_ascii=False)
            self.assertIn("upcoming_30_45s", encoded)
        finally:
            if coach is not None:
                coach.close()
            remove_temp_dir(tmp)


if __name__ == "__main__":
    unittest.main()
