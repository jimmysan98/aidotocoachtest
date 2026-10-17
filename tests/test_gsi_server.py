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


if __name__ == "__main__":
    unittest.main()
