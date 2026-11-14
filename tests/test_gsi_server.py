"""Интеграционные тесты приёмника GSI (локальный HTTP-сервер)."""

from __future__ import annotations

import socket
import unittest

from aidota.gsi import GsiServer, deep_merge


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class GsiServerTests(unittest.TestCase):
    def setUp(self):
        import requests

        self.requests = requests
        self.token = "test-token"
        self.port = free_port()
        self.server = GsiServer("127.0.0.1", self.port, self.token)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.port}/"

    def tearDown(self):
        self.server.stop()

    def test_payload_accepted(self):
        payload = {"provider": {"name": "Dota 2"}, "map": {"clock_time": 100},
                   "auth": {"token": self.token}}
        response = self.requests.post(self.url, json=payload, timeout=5)
        self.assertEqual(response.status_code, 200)
        snapshot = self.server.snapshot()
        self.assertEqual(snapshot["map"]["clock_time"], 100)
        self.assertEqual(self.server.count, 1)
        self.assertIsNotNone(self.server.age())
        self.assertTrue(self.server.is_fresh())

    def test_wrong_token_rejected(self):
        response = self.requests.post(
            self.url, json={"auth": {"token": "wrong"}, "map": {}}, timeout=5
        )
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.server.snapshot())

    def test_missing_token_rejected(self):
        response = self.requests.post(self.url, json={"map": {}}, timeout=5)
        self.assertEqual(response.status_code, 401)

    def test_get_not_allowed(self):
        response = self.requests.get(self.url, timeout=5)
        self.assertEqual(response.status_code, 405)

    def test_bad_json(self):
        response = self.requests.post(self.url, data="не json", timeout=5)
        self.assertEqual(response.status_code, 400)

    def test_updates_are_merged(self):
        self.requests.post(self.url, json={"auth": {"token": self.token},
                                           "map": {"clock_time": 10}}, timeout=5)
        self.requests.post(self.url, json={"auth": {"token": self.token},
                                           "draft": {"team2": {"pick_ids": [1]}}}, timeout=5)
        snapshot = self.server.snapshot()
        self.assertEqual(snapshot["map"]["clock_time"], 10)
        self.assertEqual(snapshot["draft"]["team2"]["pick_ids"], [1])

    def test_non_dict_auth_and_provider_does_not_crash(self):
        # auth is a string instead of dict
        resp1 = self.requests.post(
            self.url, json={"auth": "invalid_string", "provider": "Dota2_string", "map": {}}, timeout=5
        )
        self.assertEqual(resp1.status_code, 401)

        # auth is valid dict, but provider is string
        resp2 = self.requests.post(
            self.url,
            json={"auth": {"token": self.token}, "provider": "Dota2_string", "map": {"clock_time": 42}},
            timeout=5,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(self.server.snapshot()["map"]["clock_time"], 42)

    def test_oversized_payload_returns_400_and_closes_connection(self):
        # Raw socket test to verify Connection: close header
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", self.port))
        # Request with oversized Content-Length
        req = b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5000000\r\n\r\n"
        s.sendall(req)
        resp_data = s.recv(1024).decode("utf-8", errors="ignore")
        s.close()
        self.assertIn("400 Bad Request", resp_data)
        self.assertIn("Connection: close", resp_data)

    def test_snapshot_caching_performance(self):
        self.requests.post(
            self.url,
            json={"auth": {"token": self.token}, "map": {"clock_time": 100}},
            timeout=5,
        )
        snap1 = self.server.snapshot()
        snap2 = self.server.snapshot()
        self.assertIs(snap1, snap2)


class DeepMergeTests(unittest.TestCase):
    def test_nested_merge(self):
        base = {"map": {"a": 1, "b": 2}, "draft": None}
        update = {"map": {"b": 3, "c": 4}, "draft": {"x": 1}}
        merged = deep_merge(base, update)
        self.assertEqual(merged["map"], {"a": 1, "b": 3, "c": 4})
        self.assertEqual(merged["draft"], {"x": 1})
        self.assertEqual(base["map"], {"a": 1, "b": 2})  # исходный не меняется

    def test_none_does_not_erase(self):
        merged = deep_merge({"a": 1}, {"a": None})
        self.assertEqual(merged["a"], 1)

    def test_max_depth_recursion_guard(self):
        # Create recursive or deeply nested dict
        nested = {"level": 0}
        curr = nested
        for i in range(1, 30):
            curr["child"] = {"level": i}
            curr = curr["child"]
        merged = deep_merge({}, nested, max_depth=10)
        self.assertIsNotNone(merged)


if __name__ == "__main__":
    unittest.main()
