"""Тесты Фазы 2: пути (%LOCALAPPDATA%), асинхронный SWR OpenDota, 429 backoff и cache_version."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import build_exe
from aidota import meta_db
from aidota.app import Assistant
from aidota.coach import Coach
from aidota.config import Config, LlmConfig, PlayerProfile, load_config, save_config
from aidota.context import get_player_profile_path, load_player_profile, save_player_profile
from aidota.doctor import get_checks, runtime_diagnostics
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.meta_sync import (
    MAX_MATCH_HEROES,
    MetaSyncService,
)
from aidota.paths import (
    BUNDLED_DATA_ALLOWLIST,
    atomic_write_json,
    bundled_data_dir,
    default_config_path,
    meta_cache_dir,
    read_json_safe,
    writable_cache_dir,
    writable_data_dir,
    writable_logs_dir,
)
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import FAKE_HEROES, FakeResponse, gsi_payload, make_temp_dir, remove_temp_dir


class _FakeHttpSession:
    """Подмена requests.Session с поддержкой заголовков и 429 ответов."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout=None, **kwargs):
        self.calls.append(url)
        return self._handler(url, timeout)

    def close(self) -> None:
        pass


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class PathsAndFrozenIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def test_frozen_mode_separates_read_only_install_and_localappdata(self) -> None:
        exe_dir = self.tmp / "ProgramFiles" / "AIDOTA"
        exe_path = exe_dir / "AIDOTA.exe"
        local_app = self.tmp / "LocalAppData"
        env = {"LOCALAPPDATA": str(local_app), "AIDOTA_DATA_DIR": ""}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(exe_path)),
        ):
            self.assertEqual(bundled_data_dir(), exe_dir / "data")
            self.assertEqual(default_config_path(), local_app / "AIDOTA" / "config.json")
            self.assertEqual(writable_data_dir(), local_app / "AIDOTA" / "data")
            self.assertEqual(writable_cache_dir(), local_app / "AIDOTA" / ".cache")
            self.assertEqual(meta_cache_dir(), local_app / "AIDOTA" / ".cache" / "meta")
            self.assertEqual(writable_logs_dir(), local_app / "AIDOTA" / "logs")

            cfg = Config()
            self.assertEqual(cfg.bundled_data_path, exe_dir / "data")
            self.assertEqual(cfg.state_data_path, local_app / "AIDOTA" / "data")
            self.assertEqual(cfg.cache_path, local_app / "AIDOTA" / ".cache")
            self.assertEqual(cfg.logs_path, local_app / "AIDOTA" / "logs")
            self.assertEqual(cfg.meta_cache_path, local_app / "AIDOTA" / ".cache" / "meta")

            # Даже при передаче data_dir=exe_dir/data профиль пишется в LocalAppData.
            profile_path = get_player_profile_path(exe_dir / "data")
            self.assertEqual(profile_path, local_app / "AIDOTA" / "data" / "player_profile.json")
            save_player_profile({"preferred_position": 2}, exe_dir / "data")
            self.assertFalse((exe_dir / "data" / "player_profile.json").exists())
            loaded = load_player_profile()
            self.assertEqual(loaded["preferred_position"], 2)

    def test_atomic_write_and_corrupt_json_protection(self) -> None:
        target = self.tmp / "nested" / "state.json"
        atomic_write_json(target, {"schema_version": 1, "ok": True})
        self.assertEqual(read_json_safe(target)["ok"], True)

        target.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(read_json_safe(target, default={"fallback": 1}), {"fallback": 1})
        self.assertIsNone(read_json_safe(self.tmp / "missing.json"))


class MetaSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.cache_dir = self.tmp / "cache" / "meta"

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def test_bulk_sync_rate_limits_and_survives_corrupt_cache(self) -> None:
        # Заранее портим файлы кэша, чтобы проверить восстановление без падения.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "hero_stats.json").write_text("{bad-json", encoding="utf-8")
        (self.cache_dir / "items.json").write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

        clock = _FakeClock(1000.0)
        request_times: list[float] = []

        def handler(url: str, _timeout):
            request_times.append(clock.time())
            if url.endswith("/heroStats"):
                return FakeResponse(FAKE_HEROES)
            if url.endswith("/constants/items"):
                return FakeResponse({"blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250}})
            if url.endswith("/constants/patch"):
                return FakeResponse([{"id": 56, "name": "7.38"}, {"id": 57, "name": "7.39"}])
            raise AssertionError(url)

        session = _FakeHttpSession(handler)
        sync = MetaSyncService(
            self.cache_dir,
            session=session,
            min_interval=1.05,
            bulk_ttl=24 * 3600,
            clock_fn=clock.time,
            sleep_fn=clock.sleep,
        )
        try:
            self.assertTrue(sync.should_refresh_bulk())
            self.assertTrue(sync.sync_bulk(force=False))
            self.assertEqual(sync.current_patch, "7.39")
            self.assertEqual(len(request_times), 3)
            self.assertGreaterEqual(request_times[1] - request_times[0], 1.05 - 1e-6)
            self.assertGreaterEqual(request_times[2] - request_times[1], 1.05 - 1e-6)
            self.assertFalse(sync.should_refresh_bulk())
            # По прошествии TTL кэш снова помечается к обновлению.
            clock.now += 24 * 3600 + 1
            self.assertTrue(sync.should_refresh_bulk())
        finally:
            sync.close()

    def test_429_retry_after_persisted_across_restart(self) -> None:
        clock = _FakeClock(5000.0)

        class _Resp429:
            status_code = 429
            headers = {"Retry-After": "120"}

            def json(self):
                return {"error": "rate limited"}

        session = _FakeHttpSession(lambda _u, _t: _Resp429())
        sync1 = MetaSyncService(
            self.cache_dir,
            session=session,
            min_interval=0.0,
            clock_fn=clock.time,
            sleep_fn=clock.sleep,
        )
        try:
            self.assertFalse(sync1.sync_bulk(force=True))
            self.assertTrue(sync1.is_backed_off())
        finally:
            sync1.close()

        # Повторный старт сервиса должен подхватить backoff из sync_state.json и не делать HTTP.
        session2 = _FakeHttpSession(lambda _u, _t: (_ for _ in ()).throw(AssertionError("HTTP forbidden during backoff")))
        sync2 = MetaSyncService(
            self.cache_dir,
            session=session2,
            min_interval=0.0,
            clock_fn=clock.time,
            sleep_fn=clock.sleep,
        )
        try:
            self.assertTrue(sync2.is_backed_off())
            self.assertFalse(sync2.sync_bulk(force=True))
            self.assertEqual(sync2.enqueue_match_heroes([1, 2]), 0)
            self.assertEqual(session2.calls, [])
            clock.now += 121.0
            self.assertFalse(sync2.is_backed_off())
        finally:
            sync2.close()

    def test_swr_hot_path_zero_sync_http_and_caps_10_match_heroes(self) -> None:
        clock = _FakeClock(2000.0)
        data_dir = self.tmp / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "heroes.json").write_text(json.dumps(FAKE_HEROES), encoding="utf-8")
        (data_dir / "items.json").write_text(
            json.dumps({"blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250}}),
            encoding="utf-8",
        )

        # Засеиваем устаревший (stale) кэш для героя 1 на диск.
        atomic_write_json(
            self.cache_dir / "hero_popularity" / "1.json",
            {
                "schema_version": 1,
                "fetched_at": 10.0,
                "patch": "7.37",
                "hero_id": 1,
                "data": {"start_game_items": {"1": 50}},
            },
        )

        def handler(url: str, _timeout):
            if "/itemPopularity" in url:
                return FakeResponse({"mid_game_items": {"1": 900}})
            if "/matchups" in url:
                return FakeResponse([{"hero_id": 2, "games_played": 100, "wins": 55}])
            return FakeResponse([])

        session = _FakeHttpSession(handler)
        api = DotaApi(
            data_dir,
            session=session,
            cache_dir=self.cache_dir,
            popularity_ttl=3600.0,
            auto_swr=True,
        )
        try:
            api.load_snapshots()
            # Ограничение в queue: максимум 10 уникальных героев.
            with patch.object(api._sync, "_ensure_worker_started_locked"):
                added = api.enqueue_match_heroes(list(range(1, 20)))
            self.assertEqual(added, MAX_MATCH_HEROES)
            self.assertEqual(session.calls, [])

            # Горячий вызов item_popularity возвращает stale без синхронного HTTP.
            with patch.object(api._sync, "_ensure_worker_started_locked"):
                pop = api.item_popularity(1)
            self.assertEqual(pop["start_game_items"]["1"], 50)
            self.assertEqual(session.calls, [])

            # Прямая фоновая синхронизация обновляет cache_version и свежие данные.
            prev_ver = api.cache_version
            api._sync.sync_match_hero(1, force=True)
            self.assertGreater(api.cache_version, prev_ver)
            fresh_pop = api.item_popularity(1)
            self.assertEqual(fresh_pop["mid_game_items"]["1"], 900)
            self.assertEqual(api.hero_matchups(1)[0]["hero_id"], 2)

            # Фоновый воркер aidota-meta-sync корректно обрабатывает очередь и останавливается.
            api._sync._limiter.min_interval = 0.0
            with api._sync._lock:
                api._sync._pending_heroes.clear()
                api._sync._queued_hero_set.clear()
                api._sync._popularity_cache.pop(2, None)
            self.assertTrue(api.start_background_sync(hero_ids=[2], sync_bulk_now=False))
            self.assertTrue(api._sync.wait_idle(timeout=3.0))
            self.assertEqual(api.item_popularity(2)["mid_game_items"]["1"], 900)
        finally:
            api.close()
            self.assertIsNone(api._sync._worker)


class ReadOnlyAndCacheInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.data_dir = self.tmp / "data"
        self.cache_dir = self.tmp / "cache" / "meta"
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "heroes.json").write_text(json.dumps(FAKE_HEROES), encoding="utf-8")
        (self.data_dir / "items.json").write_text(
            json.dumps({"blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        meta_db.set_live_meta_provider(None)
        remove_temp_dir(self.tmp)

    def test_bundled_data_remains_read_only_and_cache_version_invalidates_consumers(self) -> None:
        orig_heroes_bytes = (self.data_dir / "heroes.json").read_bytes()
        orig_items_bytes = (self.data_dir / "items.json").read_bytes()

        state = {"blink_cost": 2250, "wr": 52}

        def handler(url: str, _timeout):
            if url.endswith("/heroStats"):
                heroes = [dict(h) for h in FAKE_HEROES]
                heroes[0]["pro_pick"] = 100
                heroes[0]["pro_win"] = state["wr"]
                return FakeResponse(heroes)
            if url.endswith("/constants/items"):
                return FakeResponse({"blink": {"dname": "Blink Dagger", "id": 1, "cost": state["blink_cost"]}})
            if url.endswith("/constants/patch"):
                return FakeResponse([{"id": 58, "name": "7.40"}])
            if "/itemPopularity" in url:
                return FakeResponse(
                    {
                        "start_game_items": {"1": 100},
                        "early_game_items": {"1": 200},
                        "mid_game_items": {"1": 300},
                    }
                )
            return FakeResponse([])

        session = _FakeHttpSession(handler)
        api = DotaApi(
            self.data_dir,
            session=session,
            cache_dir=self.cache_dir,
            read_only_data=True,
            auto_swr=True,
        )
        cfg_path = self.tmp / "config.json"
        cfg = Config(llm=LlmConfig(), player_profile=PlayerProfile(default_position=1))
        cfg.data_dir = str(self.data_dir)
        cfg.cache_dir = str(self.tmp / "cache")
        save_config(cfg, cfg_path)
        ctrl = WebGuiController(cfg, cfg_path, enable_tray=False)
        web_api = AidotaWebApi(ctrl)
        coach = Coach(
            api,
            HeroCatalog(FAKE_HEROES),
            None,
            LlmConfig(),
            PlayerProfile(default_position=1),
            session=session,
            memory_path=self.tmp / "memory.json",
            state_dir=self.tmp / "state",
        )
        try:
            api.load_snapshots()
            meta_db.set_live_meta_provider(api)
            ctrl.assistant = type("_A", (), {"_coach": coach, "_api": api, "_catalog": coach.catalog})()

            self.assertEqual(coach.item_costs()["Blink Dagger"], 2250)
            self.assertEqual(web_api._item_costs_map()["Blink Dagger"], 2250)

            # Обновляем OpenDota кэш с новой ценой предмета -> read-only data/ не тронута,
            # а Coach, WebUI и meta_db видят обновление без перезапуска.
            state["blink_cost"] = 2400
            state["wr"] = 60
            self.assertTrue(api.refresh_snapshots())

            self.assertEqual((self.data_dir / "heroes.json").read_bytes(), orig_heroes_bytes)
            self.assertEqual((self.data_dir / "items.json").read_bytes(), orig_items_bytes)

            self.assertEqual(coach.item_costs()["Blink Dagger"], 2400)
            self.assertEqual(web_api._item_costs_map()["Blink Dagger"], 2400)
            self.assertEqual(api.current_patch, "7.40")

            # Live SWR overlay в meta_db + разделение статики и патч-кэша.
            api._sync.sync_match_hero(1, force=True)
            build = meta_db.get_hero_build("Anti-Mage", role=1)
            self.assertEqual(build["build_source"], "live_swr")
            self.assertEqual(build["patch"], "7.40")
            self.assertIn("Blink Dagger", build["starting"])

            picks = meta_db.recommend_picks(["Pudge"], role=1, top_k=3)
            self.assertTrue(picks)
            self.assertEqual(picks[0]["patch"], "7.40")

            # Без провайдера meta_db возвращает статику.
            meta_db.set_live_meta_provider(None)
            static_build = meta_db.get_hero_build("Anti-Mage", role=1)
            self.assertEqual(static_build["build_source"], "static")
        finally:
            ctrl.assistant = None
            web_api.close()
            coach.close()
            api.close()

    def test_app_refresh_handles_snapshot_none_doctor_and_build_allowlist(self) -> None:
        cfg = Config()
        cfg.data_dir = str(self.data_dir)
        cfg.cache_dir = str(self.tmp / "cache")
        cfg.logs_dir = str(self.tmp / "logs")
        cfg.gsi.enabled = False

        # Doctor + runtime_diagnostics отражают секцию meta_cache.
        meta_dir = cfg.meta_cache_path
        atomic_write_json(
            meta_dir / "sync_state.json",
            {
                "schema_version": 1,
                "backoff_until": 0.0,
                "consecutive_429": 0,
                "last_patch": "7.39",
                "last_error": "",
            },
        )
        atomic_write_json(
            meta_dir / "hero_stats.json",
            {"schema_version": 1, "fetched_at": 9999999999.0, "patch": "7.39", "heroes": FAKE_HEROES},
        )
        atomic_write_json(
            meta_dir / "items.json",
            {
                "schema_version": 1,
                "fetched_at": 9999999999.0,
                "patch": "7.39",
                "items": {"blink": {"dname": "Blink Dagger", "id": 1, "cost": 2250}},
            },
        )

        checks = get_checks(cfg, online=False)
        cache_checks = [c for c in checks if c.name == "Кэш меты"]
        self.assertEqual(len(cache_checks), 1)
        self.assertIn("7.39", cache_checks[0].detail)
        diag = runtime_diagnostics(cfg)
        self.assertEqual(diag["meta_cache"]["patch"], "7.39")

        # Assistant._refresh_catalog_in_background не пропускает обновление при snapshot_at is None.
        assistant = Assistant(cfg, text_mode=True, disable_overlay=True)
        try:
            with patch.object(assistant._api, "start_background_sync", return_value=True) as mock_bg:
                assistant._api.snapshot_time = None
                assistant._refresh_catalog_in_background()
                mock_bg.assert_called_once()
        finally:
            assistant.stop()

        # Проверка явного allowlist в build_exe.copy_dist_extras.
        fake_root = self.tmp / "pkg_root"
        fake_dist = self.tmp / "pkg_dist"
        (fake_root / "data").mkdir(parents=True)
        (fake_root / "data" / "heroes.json").write_text("[]", encoding="utf-8")
        (fake_root / "data" / "items.json").write_text("{}", encoding="utf-8")
        (fake_root / "data" / "coach_memory.json").write_text('{"turns": []}', encoding="utf-8")
        (fake_root / "data" / "player_profile.json").write_text('{"secret": 1}', encoding="utf-8")
        (fake_root / "data" / ".coach_memory_old.json").write_text("{}", encoding="utf-8")
        (fake_root / "README.md").write_text("readme", encoding="utf-8")
        (fake_dist / "data").mkdir(parents=True)
        (fake_dist / "data" / "coach_memory.json").write_text("stale", encoding="utf-8")

        with (
            patch.object(build_exe, "ROOT", fake_root),
            patch.object(build_exe, "DIST", fake_dist),
        ):
            build_exe.copy_dist_extras("1.2.0")

        shipped = sorted(p.name for p in (fake_dist / "data").iterdir())
        self.assertEqual(shipped, sorted(BUNDLED_DATA_ALLOWLIST))


if __name__ == "__main__":
    unittest.main()
