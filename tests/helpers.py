"""Общие тестовые фикстуры: мини-каталог героев и тестовый GSI-payload."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from aidota.heroes import HeroCatalog

FIXTURES = Path(__file__).parent / "fixtures"
# Временные каталоги создаём внутри рабочей папки: системный TEMP под sandbox
# может быть доступен только на чтение после смены прав.
TMP_BASE = Path(__file__).resolve().parent.parent / ".cache" / "test-tmp"


def make_temp_dir() -> Path:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    path = TMP_BASE / uuid.uuid4().hex
    path.mkdir()
    return path


def remove_temp_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)

FAKE_HEROES = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage",
     "roles": ["Carry"], "pro_pick": 50, "pro_win": 25, "pub_pick": 1000, "pub_win": 520},
    {"id": 2, "name": "npc_dota_hero_pudge", "localized_name": "Pudge",
     "roles": ["Initiator"], "pro_pick": 40, "pro_win": 18, "pub_pick": 2000, "pub_win": 1000},
    {"id": 7, "name": "npc_dota_hero_windrunner", "localized_name": "Windranger",
     "roles": ["Support"], "pro_pick": 30, "pro_win": 15, "pub_pick": 900, "pub_win": 450},
    {"id": 11, "name": "npc_dota_hero_nevermore", "localized_name": "Shadow Fiend",
     "roles": ["Carry"], "pro_pick": 20, "pro_win": 12, "pub_pick": 800, "pub_win": 380},
    {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut",
     "roles": ["Carry"], "pro_pick": 10, "pro_win": 5, "pub_pick": 700, "pub_win": 350},
    {"id": 22, "name": "npc_dota_hero_zuus", "localized_name": "Zeus",
     "roles": ["Nuker"], "pro_pick": 10, "pro_win": 5, "pub_pick": 700, "pub_win": 350},
    {"id": 111, "name": "npc_dota_hero_oracle", "localized_name": "Oracle",
     "roles": ["Support"], "pro_pick": 10, "pro_win": 5, "pub_pick": 700, "pub_win": 350},
    {"id": 66, "name": "npc_dota_hero_chen", "localized_name": "Chen",
     "roles": ["Support"], "pro_pick": 10, "pro_win": 5, "pub_pick": 400, "pub_win": 200},
    {"id": 15, "name": "npc_dota_hero_razor", "localized_name": "Razor",
     "roles": ["Carry"], "pro_pick": 10, "pro_win": 5, "pub_pick": 700, "pub_win": 350},
]


def catalog() -> HeroCatalog:
    return HeroCatalog(FAKE_HEROES)


def gsi_payload() -> dict:
    return json.loads((FIXTURES / "gsi_sample.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Подменяет requests.Session: отдаёт данные по подстрокам URL."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        for fragment, data in self.routes.items():
            if fragment in url:
                if isinstance(data, Exception):
                    raise data
                return FakeResponse(data)
        raise AssertionError(f"Неожиданный URL в тесте: {url}")

    def post(self, url: str, json=None, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        for fragment, data in self.routes.items():
            if fragment in url:
                if isinstance(data, Exception):
                    raise data
                return FakeResponse(data)
        raise AssertionError(f"Неожиданный URL: {url}")
