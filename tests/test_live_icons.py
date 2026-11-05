"""Иконки героев/предметов без сети: data URI, атомарное скачивание, API-мост.

Проверяют ``aidota/icons.py`` (чтение PNG → data URI, атомарная запись,
таймауты) и ``AidotaWebApi.get_hero_icon/get_item_icon`` на временных
каталогах. Сеть подменяется ``FakeSession``-подобным объектом.
"""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

from aidota import icons
from aidota.config import Config, save_config
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import make_temp_dir, remove_temp_dir

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload-for-tests"
CDN = "https://cdn.cloudflare.steamstatic.com"


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_BYTES)
    return path


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = PNG_BYTES):
        self.status_code = status_code
        self.content = content


class FakeIconSession:
    """Подменяет requests.Session: маршруты по подстроке URL, без сети."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append(url)
        for fragment, result in self.routes.items():
            if fragment in url:
                if isinstance(result, Exception):
                    raise result
                if isinstance(result, tuple):
                    status, content = result
                    return _FakeResponse(status, content)
                return result
        raise RuntimeError(f"нет маршрута для {url}")


class IconsHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def test_read_data_uri_from_png(self) -> None:
        path = _write_png(self.tmp / "rubick.png")
        uri = icons.read_image_data_uri(path)
        self.assertTrue(uri.startswith("data:image/png;base64,"), uri[:40])
        payload = base64.b64decode(uri.split(",", 1)[1])
        self.assertEqual(payload, PNG_BYTES)

    def test_read_data_uri_missing_or_empty(self) -> None:
        self.assertEqual(icons.read_image_data_uri(self.tmp / "nope.png"), "")
        empty = self.tmp / "empty.png"
        empty.write_bytes(b"")
        self.assertEqual(icons.read_image_data_uri(empty), "")
        directory = self.tmp / "dir.png"
        directory.mkdir()
        self.assertEqual(icons.read_image_data_uri(directory), "")

    def test_download_image_success_is_atomic(self) -> None:
        target = self.tmp / "cache" / "items" / "magic_wand.png"
        session = FakeIconSession({"magic_wand": _FakeResponse(200, PNG_BYTES)})
        self.assertTrue(icons.download_image(session, "https://cdn/x/magic_wand.png", target))
        self.assertEqual(target.read_bytes(), PNG_BYTES)
        self.assertEqual(list(target.parent.glob("*.part")), [])
        self.assertEqual(len(session.calls), 1)

    def test_download_image_skips_existing_file(self) -> None:
        target = _write_png(self.tmp / "blink.png")
        session = FakeIconSession({})
        self.assertTrue(icons.download_image(session, "https://cdn/blink.png", target))
        self.assertEqual(session.calls, [])

    def test_download_image_http_error(self) -> None:
        target = self.tmp / "broken.png"
        session = FakeIconSession({"broken": _FakeResponse(404, b"")})
        self.assertFalse(icons.download_image(session, "https://cdn/broken.png", target))
        self.assertFalse(target.exists())

    def test_download_image_empty_body(self) -> None:
        target = self.tmp / "empty.png"
        session = FakeIconSession({"empty.png": _FakeResponse(200, b"")})
        self.assertFalse(icons.download_image(session, "https://cdn/empty.png", target))
        self.assertFalse(target.exists())

    def test_download_image_exception_and_none_session(self) -> None:
        target = self.tmp / "err.png"
        session = FakeIconSession({"err.png": RuntimeError("offline")})
        self.assertFalse(icons.download_image(session, "https://cdn/err.png", target))
        self.assertFalse(icons.download_image(None, "https://cdn/err.png", target))

    def test_item_icon_url(self) -> None:
        self.assertEqual(
            icons.item_icon_url("blink"),
            f"{CDN}/apps/dota2/images/dota_react/items/blink.png",
        )
        self.assertEqual(icons.item_icon_url(""), "")

    def test_create_session_returns_client(self) -> None:
        session = icons.create_session()
        if session is None:  # requests отсутствует — допустимый фолбэк
            self.skipTest("requests недоступен")
        self.assertTrue(callable(session.get))
        self.assertIn("aidota", str(session.headers.get("User-Agent", "")).lower())


class _FakeCatalog:
    available = True

    def display_name(self, npc: str) -> str:
        return "Rubick" if "rubick" in str(npc) else ""

    def by_npc(self, npc: str):
        if "rubick" in str(npc):
            return {"id": 86, "name": "npc_dota_hero_rubick", "localized_name": "Rubick"}
        return None

    def by_id(self, hero_id):
        return None


class _FakeApi:
    ITEMS = {"magic_wand": "Magic Wand", "blink": "Blink Dagger"}
    COSTS = {"Magic Wand": 460, "Blink Dagger": 2250}

    def items(self):
        return dict(self.ITEMS)

    def item_costs(self):
        return dict(self.COSTS)

    def item_slug(self, name):
        candidate = str(name or "")
        if candidate.startswith("item_"):
            candidate = candidate[5:]
        if candidate in self.ITEMS:
            return candidate
        for slug, display in self.ITEMS.items():
            if str(display).casefold() == str(name).strip().casefold():
                return slug
        return None

    def item_cost(self, name):
        slug = self.item_slug(name)
        return self.COSTS.get(self.ITEMS.get(slug)) if slug else None

    def item_info(self, name):
        slug = self.item_slug(name)
        if slug is None:
            return None
        display = self.ITEMS.get(slug)
        return {"slug": slug, "display": display, "cost": self.COSTS.get(display)}


class WebIconApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.icons_dir = self.tmp / "icons"
        self.cache_dir = self.tmp / "cache"
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "heroes.json").write_text(
            json.dumps(
                [
                    {
                        "id": 86,
                        "name": "npc_dota_hero_rubick",
                        "localized_name": "Rubick",
                        "img": "/apps/dota2/images/dota_react/heroes/rubick.png?",
                    },
                    {
                        "id": 999,
                        "name": "npc_dota_hero_testhero",
                        "localized_name": "Testhero",
                        "img": "/apps/dota2/images/dota_react/heroes/testhero.png?",
                    },
                ]
            ),
            encoding="utf-8",
        )
        (self.data_dir / "items.json").write_text(
            json.dumps(
                {
                    "magic_wand": {"dname": "Magic Wand", "id": 36, "cost": 460},
                    "blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250},
                }
            ),
            encoding="utf-8",
        )
        self.config = Config()
        self.config.vision.icons_dir = str(self.icons_dir)
        self.config.cache_dir = str(self.cache_dir)
        self.config.data_dir = str(self.data_dir)
        self.config_path = self.tmp / "config.json"
        save_config(self.config, self.config_path)
        self.ctrl = WebGuiController(self.config, self.config_path)
        self.api = AidotaWebApi(self.ctrl)
        self.session = FakeIconSession()

    def tearDown(self) -> None:
        self.ctrl.tray.stop()
        self.ctrl.assistant = None
        remove_temp_dir(self.tmp)

    def _attach_coach(self) -> None:
        coach = type(
            "FakeCoach",
            (),
            {
                "api": _FakeApi(),
                "catalog": _FakeCatalog(),
                "gsi_payload": lambda self: None,
                "gsi_age": lambda self: None,
            },
        )()
        self.ctrl.assistant = type(
            "FakeAssistant",
            (),
            {"_coach": coach, "_gsi": None, "_catalog": _FakeCatalog(), "_api": None},
        )()

    def test_get_hero_icon_local_data_uri_and_cache(self) -> None:
        _write_png(self.icons_dir / "rubick.png")
        uri = self.api.get_hero_icon("rubick")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertIn("rubick", self.api._hero_icon_cache)
        # npc-имя нормализуется к тому же ключу и берётся из кэша.
        self.assertEqual(self.api.get_hero_icon("npc_dota_hero_rubick"), uri)
        self.assertEqual(self.api.get_hero_icon("Rubick"), uri)

    def test_get_hero_icon_cdn_fallback_from_heroes_json(self) -> None:
        self._attach_coach()
        uri = self.api.get_hero_icon("testhero")
        self.assertEqual(
            uri, f"{CDN}/apps/dota2/images/dota_react/heroes/testhero.png"
        )

    def test_get_hero_icon_unknown_is_empty(self) -> None:
        self._attach_coach()
        self.assertEqual(self.api.get_hero_icon("nobody"), "")
        for junk in (None, "", "!!!", 123):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(self.api.get_hero_icon(junk), "")

    def test_get_hero_icon_never_raises_on_broken_config(self) -> None:
        self.config.vision.icons_dir = "\x00bad"
        self.assertIsInstance(self.api.get_hero_icon("rubick"), str)

    def test_get_item_icon_from_cache_file(self) -> None:
        _write_png(self.cache_dir / "item_icons" / "blink.png")
        uri = self.api.get_item_icon("item_blink")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.api.get_item_icon("Blink Dagger"), uri)

    def test_get_item_icon_downloads_via_session(self) -> None:
        self._attach_coach()
        self.api._icon_session_obj = FakeIconSession(
            {"magic_wand.png": _FakeResponse(200, PNG_BYTES)}
        )
        uri = self.api.get_item_icon("item_magic_wand")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        cached_file = self.cache_dir / "item_icons" / "magic_wand.png"
        self.assertEqual(cached_file.read_bytes(), PNG_BYTES)
        self.assertEqual(
            self.api._icon_session_obj.calls,
            [f"{CDN}/apps/dota2/images/dota_react/items/magic_wand.png"],
        )

    def test_get_item_icon_failure_returns_cdn_url(self) -> None:
        self._attach_coach()
        self.api._icon_session_obj = FakeIconSession({"": RuntimeError("offline")})
        uri = self.api.get_item_icon("item_magic_wand")
        self.assertEqual(
            uri, f"{CDN}/apps/dota2/images/dota_react/items/magic_wand.png"
        )

    def test_get_item_icon_empty_key_is_empty(self) -> None:
        self.assertEqual(self.api.get_item_icon(""), "")
        self.assertEqual(self.api.get_item_icon(None), "")

    def test_get_item_icon_resolves_display_name(self) -> None:
        _write_png(self.cache_dir / "item_icons" / "magic_wand.png")
        self._attach_coach()
        self.api._icon_session_obj = self.session
        uri = self.api.get_item_icon("Magic Wand")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(self.session.calls, [])

    def test_get_item_icon_never_raises_on_broken_config(self) -> None:
        self.config.cache_dir = "\x00bad"
        self._attach_coach()
        self.api._icon_session_obj = FakeIconSession({"": RuntimeError("offline")})
        self.assertIsInstance(self.api.get_item_icon("item_blink"), str)


if __name__ == "__main__":
    unittest.main()
