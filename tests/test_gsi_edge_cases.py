"""Тесты краевых случаев GSI-пакетов, парсинга и переходов состояний (Area 1)."""

from __future__ import annotations

import json
import socket
import time
import unittest
from unittest.mock import patch

import requests

from aidota.context import (
    AutonomousCoachAgent,
    extract_own_abilities,
    extract_own_items,
    format_clock,
    get_live_gsi_snapshot,
    objectives_context,
    roshan_pit_location,
)
from aidota.gsi import GsiServer, deep_merge
from tests.helpers import catalog, gsi_payload


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestGsiMalformedJsonAndHeaders(unittest.TestCase):
    def setUp(self):
        self.token = "test-token"
        self.port = free_port()
        self.server = GsiServer("127.0.0.1", self.port, self.token)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.port}/"

    def tearDown(self):
        self.server.stop()

    def test_truncated_json_returns_400(self):
        truncated_body = b'{"map": {"clock_time": 100}, "auth":'
        resp = requests.post(
            self.url,
            data=truncated_body,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.server.snapshot())

    def test_binary_garbage_body_returns_400(self):
        garbage = b"\xff\xfe\x00\x01\x80\xde\xad\xbe\xef"
        resp = requests.post(
            self.url,
            data=garbage,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 400)
        # Server should still be healthy and process subsequent valid requests
        valid_payload = {"map": {"clock_time": 42}, "auth": {"token": self.token}}
        resp2 = requests.post(self.url, json=valid_payload, timeout=5)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(self.server.snapshot()["map"]["clock_time"], 42)

    def test_non_dict_json_types_return_400(self):
        for raw in (b"[1, 2, 3]", b"42", b"true", b"null"):
            resp = requests.post(
                self.url,
                data=raw,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            self.assertEqual(resp.status_code, 400)

    def test_invalid_and_boundary_content_length_headers(self):
        # Raw socket tests for boundary and invalid Content-Length
        for bad_cl in ("-5", "abc", "0"):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", self.port))
            req = f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {bad_cl}\r\n\r\n".encode("ascii")
            s.sendall(req)
            resp_data = s.recv(1024).decode("utf-8", errors="ignore")
            s.close()
            self.assertIn("400 Bad Request", resp_data)

        # Oversized (> 4MB MAX_BODY)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", self.port))
        req = b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5000000\r\n\r\n"
        s.sendall(req)
        resp_data = s.recv(1024).decode("utf-8", errors="ignore")
        s.close()
        self.assertIn("400 Bad Request", resp_data)
        self.assertIn("Connection: close", resp_data)


class TestGsiBrokenDataTypes(unittest.TestCase):
    def test_payload_with_corrupted_field_types_does_not_crash_context(self):
        payload = {
            "map": {
                "clock_time": "invalid_str",
                "radiant_score": None,
                "dire_score": True,
            },
            "hero": {
                "name": "npc_dota_hero_antimage",
                "alive": "maybe",
                "level": [1, 2],
                "health_percent": "100%",
            },
            "player": {
                "gold": "lots",
                "kills": None,
                "deaths": "ten",
            },
        }
        snap = get_live_gsi_snapshot(payload, catalog(), items={})
        self.assertTrue(snap["available"])
        self.assertIsNone(snap["clock_seconds"])
        self.assertEqual(snap["clock_formatted"], "?")
        self.assertIsNone(snap["hero_level"])
        self.assertEqual(snap["gold"], 0)
        self.assertEqual(snap["kills"], 0)
        self.assertEqual(snap["deaths"], 0)
        self.assertTrue(snap["alive"])

    def test_deep_merge_with_conflicting_types_overwrites_safely(self):
        base = {"hero": {"level": 5, "items": {"slot0": "item_tango"}}}
        update = {"hero": {"items": "corrupted_string"}}
        merged = deep_merge(base, update)
        self.assertEqual(merged["hero"]["items"], "corrupted_string")
        # Base dict unmodified
        self.assertIsInstance(base["hero"]["items"], dict)
        # Subsequent merge with a valid dict recovers dictionary structure
        recovery = {"hero": {"items": {"slot0": "item_quelling_blade"}}}
        recovered = deep_merge(merged, recovery)
        self.assertEqual(recovered["hero"]["items"]["slot0"], "item_quelling_blade")


class TestGsiMatchTimings(unittest.TestCase):
    def test_negative_pregame_clock_timings(self):
        for sec in (-120, -90, -1):
            formatted = format_clock(sec)
            self.assertTrue(formatted.startswith("-"))
            self.assertEqual(objectives_context(sec), "")
            pit = roshan_pit_location(sec, is_daytime=True)
            self.assertIn("нижнем", pit)

        payload = {
            "map": {"clock_time": -90, "game_state": "DOTA_GAMERULES_STATE_PRE_GAME"},
            "hero": {"name": "npc_dota_hero_antimage"},
        }
        snap = get_live_gsi_snapshot(payload, catalog(), items={})
        self.assertEqual(snap["game_stage"], "pre_game")
        self.assertEqual(snap["clock_formatted"], "-1:30")

    def test_zero_and_extreme_lategame_clock_timings(self):
        self.assertEqual(format_clock(0), "0:00")
        self.assertEqual(format_clock(7200), "120:00")
        self.assertEqual(format_clock(99999), "1666:39")

        # Objectives at 2 hours (7200s) should format properly without error
        obj = objectives_context(7200)
        self.assertIn("Ближайшие цели", obj)
        self.assertIn("руны мудрости", obj)
        self.assertIn("Рошан", obj)

        # Roshan cycles by 300s
        pit_day = roshan_pit_location(600, is_daytime=True)
        self.assertIn("нижнем", pit_day)


class TestGsiHeroStateTransitions(unittest.TestCase):
    def test_hero_instant_death_streak_transition(self):
        cat = catalog()
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)

        # First tick at t=100s: 1 death
        t1 = 100.0
        payload1 = {
            "map": {"clock_time": 600, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"deaths": 1, "team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_antimage", "alive": False},
        }
        with patch("time.time", return_value=t1):
            agent.process_tick(payload1, cat, items={}, position=1)
        self.assertEqual(agent._death_streak, 1)

        # Second tick at t=130s: 2 deaths in quick succession
        t2 = 130.0
        payload2 = {
            "map": {"clock_time": 630, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"deaths": 2, "team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_antimage", "alive": False},
        }
        with patch("time.time", return_value=t2):
            advice = agent.process_tick(payload2, cat, items={}, position=1)

        self.assertIsNotNone(advice)
        self.assertIn("Не тильтуй: 2 смерти подряд на Anti-Mage", advice)
        self.assertEqual(agent._death_streak, 0)

    def test_buyback_availability_state_matrix(self):
        cat = catalog()
        # Case 1: dead, ready, has gold -> buyback available
        p1 = {"hero": {"alive": False, "buyback_cost": 500, "buyback_cooldown": 0}, "player": {"gold": 1000}}
        s1 = get_live_gsi_snapshot(p1, cat, items={})
        self.assertTrue(s1["buyback_available"])

        # Case 2: dead, cooldown active -> not available
        p2 = {"hero": {"alive": False, "buyback_cost": 500, "buyback_cooldown": 480}, "player": {"gold": 1000}}
        s2 = get_live_gsi_snapshot(p2, cat, items={})
        self.assertFalse(s2["buyback_available"])

        # Case 3: dead, insufficient gold -> not available
        p3 = {"hero": {"alive": False, "buyback_cost": 500, "buyback_cooldown": 0}, "player": {"gold": 200}}
        s3 = get_live_gsi_snapshot(p3, cat, items={})
        self.assertFalse(s3["buyback_available"])

        # Case 4: dead, cost is 0 or None -> not available
        p4 = {"hero": {"alive": False, "buyback_cost": 0, "buyback_cooldown": 0}, "player": {"gold": 1000}}
        s4 = get_live_gsi_snapshot(p4, cat, items={})
        self.assertFalse(s4["buyback_available"])

        # Case 5: alive, ready, has gold -> available (ready if dies)
        p5 = {"hero": {"alive": True, "buyback_cost": 500, "buyback_cooldown": 0}, "player": {"gold": 1000}}
        s5 = get_live_gsi_snapshot(p5, cat, items={})
        self.assertTrue(s5["buyback_available"])

    def test_hero_natural_respawn_transition(self):
        cat = catalog()
        agent = AutonomousCoachAgent(autonomy_level="high", cooldown_seconds=0.0)

        # Dead state
        dead_payload = {
            "map": {"clock_time": 600, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"deaths": 1, "team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_antimage", "alive": False, "respawn_seconds": 30},
        }
        snap_dead = get_live_gsi_snapshot(dead_payload, cat, items={})
        self.assertFalse(snap_dead["alive"])

        with patch("time.time", return_value=100.0):
            agent.process_tick(dead_payload, cat, items={}, position=1)

        # Respawn state
        alive_payload = {
            "map": {"clock_time": 630, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"deaths": 1, "team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_antimage", "alive": True, "respawn_seconds": 0, "health_percent": 100.0},
        }
        snap_alive = get_live_gsi_snapshot(alive_payload, cat, items={})
        self.assertTrue(snap_alive["alive"])

        with patch("time.time", return_value=130.0):
            agent.process_tick(alive_payload, cat, items={}, position=1)

        # Deaths did not increase, so death streak stays 1 without triggering tilt
        self.assertEqual(agent._death_streak, 1)


class TestGsiConnectionLifecycle(unittest.TestCase):
    def test_gsi_server_connection_drop_and_recovery(self):
        port = free_port()
        token = "secret-token"
        server = GsiServer("127.0.0.1", port, token)
        server.start()
        url = f"http://127.0.0.1:{port}/"

        try:
            # Packet 1 at t0
            t0 = 1000.0
            with patch("time.time", return_value=t0):
                resp1 = requests.post(
                    url,
                    json={"auth": {"token": token}, "map": {"clock_time": 100}},
                    timeout=5,
                )
                self.assertEqual(resp1.status_code, 200)
                self.assertEqual(server.count, 1)
                self.assertTrue(server.is_fresh(45.0))
                self.assertAlmostEqual(server.age(), 0.0, places=1)

            # Advance time by 50 seconds: stale
            t_stale = t0 + 50.0
            with patch("time.time", return_value=t_stale):
                self.assertFalse(server.is_fresh(45.0))
                self.assertGreaterEqual(server.age(), 50.0)

            # Reconnection: Packet 2 at t_stale + 5s
            t_reconnect = t_stale + 5.0
            with patch("time.time", return_value=t_reconnect):
                resp2 = requests.post(
                    url,
                    json={"auth": {"token": token}, "hero": {"level": 6}},
                    timeout=5,
                )
                self.assertEqual(resp2.status_code, 200)
                self.assertEqual(server.count, 2)
                self.assertTrue(server.is_fresh(45.0))
                self.assertAlmostEqual(server.age(), 0.0, places=1)

                snap = server.snapshot()
                self.assertEqual(snap["map"]["clock_time"], 100)
                self.assertEqual(snap["hero"]["level"], 6)
        finally:
            server.stop()


class TestGsiSpectatorMode(unittest.TestCase):
    def test_spectator_nested_items_and_abilities_extraction(self):
        payload = {
            "player": {"team_name": "dire", "player_slot": 128, "accountid": 111222},
            "hero": {"name": "npc_dota_hero_rubick"},
            "items": {
                "team3": {
                    "player0": {
                        "accountid": 111222,
                        "slot0": {"name": "item_parasma"},
                        "slot1": {"name": "item_disperser"},
                    }
                }
            },
            "abilities": {
                "team3": {
                    "player0": {
                        "accountid": 111222,
                        "ability0": {"name": "rubick_telekinesis"},
                    }
                }
            },
        }

        extracted_items = extract_own_items(payload)
        self.assertIn("slot0", extracted_items)
        self.assertEqual(extracted_items["slot0"]["name"], "item_parasma")
        self.assertEqual(extracted_items["slot1"]["name"], "item_disperser")

        extracted_abilities = extract_own_abilities(payload)
        self.assertIn("ability0", extracted_abilities)
        self.assertEqual(extracted_abilities["ability0"]["name"], "rubick_telekinesis")

        snap = get_live_gsi_snapshot(payload, catalog(), items={"item_parasma": {"cost": 5575}})
        self.assertTrue(snap["available"])
        self.assertIn("Parasma", snap["inventory"])


if __name__ == "__main__":
    unittest.main()
