"""Асинхронный SWR-синхронизатор меты OpenDota (Fork D1: 3 bulk + ленивый кэш 10 героев)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .paths import atomic_write_json, read_json_safe

log = logging.getLogger("aidota.meta_sync")

OPENDOTA_API = "https://api.opendota.com/api"
META_SYNC_USER_AGENT = "aidota/1.0 (Dota 2 voice assistant; local use)"

CACHE_SCHEMA_VERSION = 1
DEFAULT_BULK_TTL_SECONDS = 24.0 * 3600.0
MAX_BULK_TTL_SECONDS = 72.0 * 3600.0
DEFAULT_HERO_TTL_SECONDS = 48.0 * 3600.0
MAX_MATCH_HEROES = 10
DEFAULT_MIN_REQUEST_INTERVAL = 1.05
DEFAULT_429_BACKOFF_SECONDS = 60.0
MAX_429_BACKOFF_SECONDS = 3600.0

POPULARITY_KEYS: tuple[str, ...] = (
    "start_game_items",
    "early_game_items",
    "mid_game_items",
    "late_game_items",
)


def _coerce_cost(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = int(value)
    except (TypeError, ValueError):
        return None
    return cost if cost >= 0 else None


def _parse_retry_after(headers: Any, default_seconds: float) -> float:
    if not headers or not hasattr(headers, "items"):
        return default_seconds
    for k, v in headers.items():
        if str(k).strip().lower() == "retry-after":
            try:
                parsed = float(str(v).strip())
                if parsed > 0:
                    return min(MAX_429_BACKOFF_SECONDS, max(1.0, parsed))
            except ValueError:
                break
    return default_seconds


def _extract_latest_patch(patch_payload: Any) -> tuple[str, int | None]:
    if isinstance(patch_payload, list):
        for entry in reversed(patch_payload):
            if isinstance(entry, dict) and entry.get("name"):
                raw_id = entry.get("id")
                pid = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
                return str(entry["name"]).strip(), pid
    elif isinstance(patch_payload, dict) and patch_payload.get("name"):
        raw_id = patch_payload.get("id")
        pid = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
        return str(patch_payload["name"]).strip(), pid
    return "", None


class RateLimiter:
    """Потокобезопасный ограничитель частоты запросов (<= 1 req/s)."""

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        *,
        clock_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock_fn or time.monotonic
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def acquire(self, stop_event: threading.Event | None = None) -> bool:
        """Ждёт разрешённого окна перед HTTP-запросом; возвращает False при остановке."""
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            with self._lock:
                now = float(self._clock())
                wait_for = self._next_allowed_at - now
                if wait_for <= 0.0:
                    self._next_allowed_at = now + self.min_interval
                    return True
            if self._sleep is not None:
                self._sleep(wait_for)
            elif stop_event is not None:
                if stop_event.wait(wait_for):
                    return False
            else:
                time.sleep(wait_for)


class MetaSyncService:
    """SWR-синхронизатор OpenDota с локальным дисковым кэшем и фоновым воркером."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        session: Any | None = None,
        timeout: float = 15.0,
        bulk_ttl: float = DEFAULT_BULK_TTL_SECONDS,
        hero_ttl: float = DEFAULT_HERO_TTL_SECONDS,
        min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        clock_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        on_updated: Callable[[int], None] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = max(1.0, float(timeout))
        self.bulk_ttl = max(1.0, float(bulk_ttl))
        self.hero_ttl = max(1.0, float(hero_ttl))
        self._clock = clock_fn or time.time
        self._limiter = RateLimiter(min_interval, clock_fn=clock_fn, sleep_fn=sleep_fn)
        self._on_updated = on_updated

        if session is None:
            import requests

            sess = requests.Session()
            sess.headers["User-Agent"] = META_SYNC_USER_AGENT
            self._session: Any = sess
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._pending_bulk = False
        self._pending_heroes: deque[int] = deque()
        self._queued_hero_set: set[int] = set()

        self._cache_version = 0
        self._heroes: list[dict[str, Any]] | None = None
        self._items: dict[str, dict[str, Any]] | None = None
        self._bulk_fetched_at: float | None = None
        self._patch_name = ""
        self._patch_id: int | None = None

        self._popularity_cache: dict[int, tuple[float, str, dict[str, dict[str, int]]]] = {}
        self._matchups_cache: dict[int, tuple[float, str, list[dict[str, Any]]]] = {}

        self._backoff_until = 0.0
        self._consecutive_429 = 0
        self._last_sync_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None

        self.load_from_disk()

    # ------------------------------------------------------------------ #
    # Состояние и свойства
    # ------------------------------------------------------------------ #
    @property
    def cache_version(self) -> int:
        with self._lock:
            return self._cache_version

    @property
    def current_patch(self) -> str:
        with self._lock:
            return self._patch_name

    @property
    def bulk_fetched_at(self) -> float | None:
        with self._lock:
            return self._bulk_fetched_at

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def backoff_until(self) -> float:
        with self._lock:
            return self._backoff_until

    def cached_heroes(self) -> list[dict[str, Any]] | None:
        with self._lock:
            return list(self._heroes) if self._heroes is not None else None

    def cached_items(self) -> dict[str, dict[str, Any]] | None:
        with self._lock:
            return dict(self._items) if self._items is not None else None

    def _bump_version_locked(self) -> int:
        self._cache_version += 1
        return self._cache_version

    def _notify_updated(self, version: int) -> None:
        if self._on_updated is not None:
            try:
                self._on_updated(version)
            except Exception:  # noqa: BLE001
                log.debug("on_updated callback failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Дисковый кэш (schema_version=1, атомарная запись, защита от битого JSON)
    # ------------------------------------------------------------------ #
    def _sync_state_path(self) -> Path:
        return self.cache_dir / "sync_state.json"

    def _hero_stats_path(self) -> Path:
        return self.cache_dir / "hero_stats.json"

    def _items_path(self) -> Path:
        return self.cache_dir / "items.json"

    def _patch_path(self) -> Path:
        return self.cache_dir / "patch.json"

    def _popularity_path(self, hero_id: int) -> Path:
        return self.cache_dir / "hero_popularity" / f"{int(hero_id)}.json"

    def _matchups_path(self, hero_id: int) -> Path:
        return self.cache_dir / "hero_matchups" / f"{int(hero_id)}.json"

    def load_from_disk(self) -> bool:
        """Безопасно загружает валидный SWR-кэш и состояние 429 backoff с диска."""
        loaded_any = False
        state = read_json_safe(self._sync_state_path())
        hero_stats = read_json_safe(self._hero_stats_path())
        items_doc = read_json_safe(self._items_path())
        patch_doc = read_json_safe(self._patch_path())

        with self._lock:
            if isinstance(state, dict) and state.get("schema_version") == CACHE_SCHEMA_VERSION:
                self._backoff_until = float(state.get("backoff_until") or 0.0)
                self._consecutive_429 = max(0, int(state.get("consecutive_429") or 0))
                lsa = state.get("last_sync_at")
                self._last_sync_at = float(lsa) if isinstance(lsa, (int, float)) else None
                lsuc = state.get("last_success_at")
                self._last_success_at = float(lsuc) if isinstance(lsuc, (int, float)) else None
                if state.get("last_patch"):
                    self._patch_name = str(state["last_patch"])
                err = state.get("last_error")
                self._last_error = str(err) if err else None

            if isinstance(patch_doc, dict) and patch_doc.get("schema_version") == CACHE_SCHEMA_VERSION:
                pname = str(patch_doc.get("patch") or "").strip()
                if pname:
                    self._patch_name = pname
                pid = patch_doc.get("patch_id")
                if isinstance(pid, int) and not isinstance(pid, bool):
                    self._patch_id = pid

            heroes_ts: float | None = None
            items_ts: float | None = None
            if (
                isinstance(hero_stats, dict)
                and hero_stats.get("schema_version") == CACHE_SCHEMA_VERSION
                and isinstance(hero_stats.get("heroes"), list)
                and hero_stats["heroes"]
            ):
                self._heroes = [h for h in hero_stats["heroes"] if isinstance(h, dict)]
                ft = hero_stats.get("fetched_at")
                if isinstance(ft, (int, float)) and not isinstance(ft, bool):
                    heroes_ts = float(ft)
                if not self._patch_name and hero_stats.get("patch"):
                    self._patch_name = str(hero_stats["patch"])
                loaded_any = bool(self._heroes)

            if (
                isinstance(items_doc, dict)
                and items_doc.get("schema_version") == CACHE_SCHEMA_VERSION
                and isinstance(items_doc.get("items"), dict)
                and items_doc["items"]
            ):
                self._items = {
                    str(k): dict(v)
                    for k, v in items_doc["items"].items()
                    if isinstance(v, dict)
                }
                ft_i = items_doc.get("fetched_at")
                if isinstance(ft_i, (int, float)) and not isinstance(ft_i, bool):
                    items_ts = float(ft_i)
                loaded_any = loaded_any or bool(self._items)

            if heroes_ts is not None and items_ts is not None:
                self._bulk_fetched_at = min(heroes_ts, items_ts)
            elif heroes_ts is not None:
                self._bulk_fetched_at = heroes_ts
            elif items_ts is not None:
                self._bulk_fetched_at = items_ts

            if loaded_any and self._cache_version == 0:
                self._bump_version_locked()
        return loaded_any

    def _persist_sync_state_locked(self) -> None:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "backoff_until": self._backoff_until,
            "consecutive_429": self._consecutive_429,
            "last_sync_at": self._last_sync_at,
            "last_success_at": self._last_success_at,
            "last_patch": self._patch_name,
            "last_error": self._last_error or "",
            "cache_version": self._cache_version,
        }
        try:
            atomic_write_json(self._sync_state_path(), payload)
        except OSError as exc:
            log.warning("Не удалось сохранить sync_state.json: %s", exc)

    # ------------------------------------------------------------------ #
    # HTTP + 429 backoff + RateLimiter
    # ------------------------------------------------------------------ #
    def is_backed_off(self, now: float | None = None) -> bool:
        cur = float(self._clock()) if now is None else float(now)
        with self._lock:
            return cur < self._backoff_until

    def should_refresh_bulk(self, now: float | None = None) -> bool:
        """True, если bulk-кэш отсутствует (`fetched_at is None`) или истёк TTL (24–72ч)."""
        cur = float(self._clock()) if now is None else float(now)
        with self._lock:
            if cur < self._backoff_until:
                return False
            if self._bulk_fetched_at is None or not self._heroes or not self._items:
                return True
            return (cur - self._bulk_fetched_at) >= self.bulk_ttl

    def _get_json(self, path_or_url: str) -> tuple[Any | None, bool]:
        """Выполняет rate-limited GET; возвращает ``(data, rate_limited)``."""
        now = float(self._clock())
        with self._lock:
            if now < self._backoff_until:
                rem = int(max(1.0, self._backoff_until - now))
                self._last_error = f"OpenDota 429 backoff ({rem}с)"
                return None, True

        if not self._limiter.acquire(self._stop_event):
            return None, False

        url = path_or_url if path_or_url.startswith("http") else f"{OPENDOTA_API}{path_or_url}"
        try:
            response = self._session.get(url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_sync_at = float(self._clock())
                self._last_error = str(exc)
                self._persist_sync_state_locked()
            return None, False

        status = int(getattr(response, "status_code", 200) or 200)
        if status == 429:
            now_429 = float(self._clock())
            with self._lock:
                self._consecutive_429 += 1
                exp_wait = min(
                    MAX_429_BACKOFF_SECONDS,
                    DEFAULT_429_BACKOFF_SECONDS * (2 ** (self._consecutive_429 - 1)),
                )
                wait_sec = _parse_retry_after(getattr(response, "headers", None), exp_wait)
                self._backoff_until = now_429 + wait_sec
                self._last_sync_at = now_429
                self._last_error = f"HTTP 429 Retry-After {int(wait_sec)}s"
                self._persist_sync_state_locked()
            log.warning("OpenDota вернула 429 — пауза синхронизации на %d с", int(wait_sec))
            return None, True

        if status != 200:
            with self._lock:
                self._last_sync_at = float(self._clock())
                self._last_error = f"HTTP {status} от {url}"
                self._persist_sync_state_locked()
            return None, False

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_sync_at = float(self._clock())
                self._last_error = f"Некорректный JSON от {url}: {exc}"
                self._persist_sync_state_locked()
            return None, False
        return data, False

    def sync_bulk(self, *, force: bool = False) -> bool:
        """Выполняет до 3 bulk-запросов (`/heroStats`, `/constants/items`, `/constants/patch`)."""
        if not force and not self.should_refresh_bulk():
            return False
        if self.is_backed_off():
            return False

        heroes_raw, rate_limited = self._get_json("/heroStats")
        if rate_limited or not isinstance(heroes_raw, list) or not heroes_raw:
            return False

        items_raw, rate_limited = self._get_json("/constants/items")
        if rate_limited or not isinstance(items_raw, dict) or not items_raw:
            return False

        patch_raw, _ = self._get_json("/constants/patch")
        patch_name, patch_id = _extract_latest_patch(patch_raw)

        now = float(self._clock())
        clean_heroes = [dict(h) for h in heroes_raw if isinstance(h, dict)]
        clean_items: dict[str, dict[str, Any]] = {
            str(key): {
                "dname": str(val.get("dname") or key),
                "id": val.get("id"),
                "cost": _coerce_cost(val.get("cost")),
            }
            for key, val in items_raw.items()
            if isinstance(val, dict)
        }
        if not clean_heroes or not clean_items:
            return False

        with self._lock:
            eff_patch = patch_name or self._patch_name
            eff_patch_id = patch_id if patch_id is not None else self._patch_id
            hero_payload = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "fetched_at": now,
                "patch": eff_patch,
                "heroes": clean_heroes,
            }
            item_payload = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "fetched_at": now,
                "patch": eff_patch,
                "items": clean_items,
            }
            patch_payload = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "fetched_at": now,
                "patch": eff_patch,
                "patch_id": eff_patch_id,
            }
            try:
                atomic_write_json(self._hero_stats_path(), hero_payload, indent=1)
                atomic_write_json(self._items_path(), item_payload, indent=1)
                if eff_patch:
                    atomic_write_json(self._patch_path(), patch_payload, indent=2)
            except OSError as exc:
                self._last_sync_at = now
                self._last_error = f"Ошибка записи кэша OpenDota: {exc}"
                self._persist_sync_state_locked()
                return False

            self._heroes = clean_heroes
            self._items = clean_items
            self._patch_name = eff_patch
            self._patch_id = eff_patch_id
            self._bulk_fetched_at = now
            self._consecutive_429 = 0
            self._backoff_until = 0.0
            self._last_sync_at = now
            self._last_success_at = now
            self._last_error = None
            ver = self._bump_version_locked()
            self._persist_sync_state_locked()

        self._notify_updated(ver)
        log.info(
            "OpenDota bulk-кэш обновлён: %d героев, %d предметов (патч %s, cache_version=%d)",
            len(clean_heroes),
            len(clean_items),
            eff_patch or "?",
            ver,
        )
        return True

    # ------------------------------------------------------------------ #
    # Ленивый SWR-кэш для 10 героев активного матча (0 HTTP на горячем пути)
    # ------------------------------------------------------------------ #
    def _is_entry_stale(self, fetched_at: float, entry_patch: str, now: float) -> bool:
        if (now - fetched_at) >= self.hero_ttl:
            return True
        if self._patch_name and entry_patch and entry_patch != self._patch_name:
            return True
        return False

    def get_popularity(self, hero_id: int) -> tuple[dict[str, dict[str, int]], bool]:
        """Читает itemPopularity строго из RAM/дискового кэша: ``(data, is_stale)``."""
        try:
            hid = int(hero_id)
        except (TypeError, ValueError):
            return {}, False
        if hid <= 0:
            return {}, False
        now = float(self._clock())
        with self._lock:
            cached = self._popularity_cache.get(hid)
            if cached is not None:
                fetched_at, entry_patch, data = cached
                return data, self._is_entry_stale(fetched_at, entry_patch, now)

        doc = read_json_safe(self._popularity_path(hid))
        if (
            isinstance(doc, dict)
            and doc.get("schema_version") == CACHE_SCHEMA_VERSION
            and isinstance(doc.get("data"), dict)
        ):
            fetched_at = float(doc.get("fetched_at") or 0.0)
            entry_patch = str(doc.get("patch") or "")
            data = {
                str(phase): {
                    str(k): int(v)
                    for k, v in table.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
                for phase, table in doc["data"].items()
                if phase in POPULARITY_KEYS and isinstance(table, dict)
            }
            with self._lock:
                self._popularity_cache[hid] = (fetched_at, entry_patch, data)
                stale = self._is_entry_stale(fetched_at, entry_patch, now)
            return data, stale
        return {}, True

    def get_matchups(self, hero_id: int) -> tuple[list[dict[str, Any]], bool]:
        """Читает matchups героя строго из RAM/дискового кэша: ``(matchups, is_stale)``."""
        try:
            hid = int(hero_id)
        except (TypeError, ValueError):
            return [], False
        if hid <= 0:
            return [], False
        now = float(self._clock())
        with self._lock:
            cached = self._matchups_cache.get(hid)
            if cached is not None:
                fetched_at, entry_patch, rows = cached
                return rows, self._is_entry_stale(fetched_at, entry_patch, now)

        doc = read_json_safe(self._matchups_path(hid))
        if (
            isinstance(doc, dict)
            and doc.get("schema_version") == CACHE_SCHEMA_VERSION
            and isinstance(doc.get("matchups"), list)
        ):
            fetched_at = float(doc.get("fetched_at") or 0.0)
            entry_patch = str(doc.get("patch") or "")
            rows = [dict(r) for r in doc["matchups"] if isinstance(r, dict)]
            with self._lock:
                self._matchups_cache[hid] = (fetched_at, entry_patch, rows)
                stale = self._is_entry_stale(fetched_at, entry_patch, now)
            return rows, stale
        return [], True

    def sync_match_hero(self, hero_id: int, *, force: bool = False) -> bool:
        """Синхронизирует ``itemPopularity`` и ``matchups`` одного героя с OpenDota."""
        try:
            hid = int(hero_id)
        except (TypeError, ValueError):
            return False
        if hid <= 0 or self.is_backed_off():
            return False

        pop_data, pop_stale = self.get_popularity(hid)
        mu_data, mu_stale = self.get_matchups(hid)
        need_pop = force or not pop_data or pop_stale
        need_mu = force or not mu_data or mu_stale
        if not need_pop and not need_mu:
            return False

        updated = False
        now = float(self._clock())
        with self._lock:
            patch_str = self._patch_name

        if need_pop:
            raw_pop, limited = self._get_json(f"/heroes/{hid}/itemPopularity")
            if limited:
                return False
            if isinstance(raw_pop, dict):
                clean_pop = {
                    phase: {
                        str(k): int(v)
                        for k, v in table.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                    }
                    for phase, table in raw_pop.items()
                    if phase in POPULARITY_KEYS and isinstance(table, dict)
                }
                try:
                    atomic_write_json(
                        self._popularity_path(hid),
                        {
                            "schema_version": CACHE_SCHEMA_VERSION,
                            "fetched_at": now,
                            "patch": patch_str,
                            "hero_id": hid,
                            "data": clean_pop,
                        },
                        indent=1,
                    )
                    with self._lock:
                        self._popularity_cache[hid] = (now, patch_str, clean_pop)
                    updated = True
                except OSError as exc:
                    with self._lock:
                        self._last_error = f"Ошибка записи popularity({hid}): {exc}"

        if need_mu and not self._stop_event.is_set():
            raw_mu, limited = self._get_json(f"/heroes/{hid}/matchups")
            if limited:
                return updated
            if isinstance(raw_mu, list):
                clean_mu = [
                    {
                        "hero_id": int(row["hero_id"]),
                        "games_played": int(row.get("games_played") or 0),
                        "wins": int(row.get("wins") or 0),
                    }
                    for row in raw_mu
                    if isinstance(row, dict) and isinstance(row.get("hero_id"), int)
                ]
                try:
                    atomic_write_json(
                        self._matchups_path(hid),
                        {
                            "schema_version": CACHE_SCHEMA_VERSION,
                            "fetched_at": now,
                            "patch": patch_str,
                            "hero_id": hid,
                            "matchups": clean_mu,
                        },
                        indent=1,
                    )
                    with self._lock:
                        self._matchups_cache[hid] = (now, patch_str, clean_mu)
                    updated = True
                except OSError as exc:
                    with self._lock:
                        self._last_error = f"Ошибка записи matchups({hid}): {exc}"

        if updated:
            with self._lock:
                self._consecutive_429 = 0
                self._backoff_until = 0.0
                self._last_sync_at = now
                self._last_success_at = now
                self._last_error = None
                ver = self._bump_version_locked()
                self._persist_sync_state_locked()
            self._notify_updated(ver)
        return updated

    # ------------------------------------------------------------------ #
    # Фоновый воркер и чистое завершение
    # ------------------------------------------------------------------ #
    def _ensure_worker_started_locked(self) -> None:
        if self._stop_event.is_set():
            return
        if self._worker is not None and self._worker.is_alive():
            return
        worker = threading.Thread(
            target=self._worker_loop,
            name="aidota-meta-sync",
            daemon=True,
        )
        self._worker = worker
        worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            run_bulk = False
            next_hero: int | None = None
            with self._lock:
                if self._pending_bulk:
                    self._pending_bulk = False
                    run_bulk = True
                elif self._pending_heroes:
                    next_hero = self._pending_heroes.popleft()
                    self._queued_hero_set.discard(next_hero)

            if run_bulk:
                try:
                    self.sync_bulk(force=False)
                except Exception:  # noqa: BLE001
                    log.debug("Ошибка в sync_bulk", exc_info=True)
                continue

            if next_hero is not None:
                try:
                    self.sync_match_hero(next_hero, force=False)
                except Exception:  # noqa: BLE001
                    log.debug("Ошибка в sync_match_hero(%s)", next_hero, exc_info=True)
                continue

            self._wake_event.clear()
            with self._lock:
                has_more = self._pending_bulk or bool(self._pending_heroes)
            if has_more:
                continue
            self._wake_event.wait(timeout=1.0)
            with self._lock:
                still_empty = not self._pending_bulk and not self._pending_heroes
            if still_empty:
                # Засыпать не нужно — воркер запустится заново при новом enqueue.
                return

    def start_background_sync(
        self,
        *,
        hero_ids: Iterable[int] = (),
        sync_bulk_now: bool | None = None,
    ) -> bool:
        """Планирует фоновую синхронизацию bulk и до 10 героев без блокировки вызывающего потока."""
        need_bulk = self.should_refresh_bulk() if sync_bulk_now is None else bool(sync_bulk_now)
        self.enqueue_match_heroes(hero_ids, start_worker=False)
        with self._lock:
            has_work = need_bulk or bool(self._pending_bulk) or bool(self._pending_heroes)
            if not has_work:
                return False
            if need_bulk:
                self._pending_bulk = True
            self._ensure_worker_started_locked()
            self._wake_event.set()
        return True

    def enqueue_match_heroes(
        self,
        hero_ids: Iterable[int],
        *,
        start_worker: bool = True,
    ) -> int:
        """Ставит в фоновую SWR-очередь до 10 уникальных героев активного матча."""
        selected: list[int] = []
        seen: set[int] = set()
        for raw_id in hero_ids:
            try:
                hid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if hid <= 0 or hid in seen:
                continue
            seen.add(hid)
            selected.append(hid)
            if len(selected) >= MAX_MATCH_HEROES:
                break

        if not selected or self.is_backed_off():
            return 0

        to_queue: list[int] = []
        for hid in selected:
            pop_data, pop_stale = self.get_popularity(hid)
            mu_data, mu_stale = self.get_matchups(hid)
            if (not pop_data or pop_stale) or (not mu_data or mu_stale):
                to_queue.append(hid)

        if not to_queue:
            return 0

        added = 0
        with self._lock:
            for hid in to_queue:
                if hid not in self._queued_hero_set:
                    self._pending_heroes.append(hid)
                    self._queued_hero_set.add(hid)
                    added += 1
            if start_worker and added > 0:
                self._ensure_worker_started_locked()
                self._wake_event.set()
        return added

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Ждёт завершения текущей фоновой пачки задач (удобно в тестах)."""
        worker: threading.Thread | None
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    def stop(self, timeout: float = 2.0) -> None:
        """Останавливает фоновый поток и дожидается его завершения."""
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        with self._lock:
            self._worker = None

    def close(self) -> None:
        """Останавливает фоновый поток и закрывает собственную HTTP-сессию."""
        self.stop(timeout=2.0)
        if self._owns_session and self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass

    def diagnostics(self) -> dict[str, Any]:
        """Сводка состояния SWR-кэша для doctor и web diagnostics (без секретов)."""
        now = float(self._clock())
        with self._lock:
            fetched_at = self._bulk_fetched_at
            age_hours = round((now - fetched_at) / 3600.0, 2) if fetched_at is not None else None
            backoff_rem = round(max(0.0, self._backoff_until - now), 1)
            pop_files = (
                len(list((self.cache_dir / "hero_popularity").glob("*.json")))
                if (self.cache_dir / "hero_popularity").is_dir()
                else 0
            )
            mu_files = (
                len(list((self.cache_dir / "hero_matchups").glob("*.json")))
                if (self.cache_dir / "hero_matchups").is_dir()
                else 0
            )
            return {
                "patch": self._patch_name or "",
                "patch_id": self._patch_id,
                "bulk_fetched_at": fetched_at,
                "bulk_age_hours": age_hours,
                "last_sync_at": self._last_sync_at,
                "last_success_at": self._last_success_at,
                "backoff_until": self._backoff_until,
                "backoff_remaining_sec": backoff_rem,
                "consecutive_429": self._consecutive_429,
                "last_error": self._last_error or "",
                "cached_heroes": len(self._heroes or []),
                "cached_items": len(self._items or {}),
                "cached_popularity_heroes": pop_files,
                "cached_matchups_heroes": mu_files,
                "cache_version": self._cache_version,
                "cache_dir": str(self.cache_dir),
            }
