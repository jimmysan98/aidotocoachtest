"""Современный графический интерфейс AIDOTA на базе HTML/CSS/JS (Edge WebView2 / pywebview).

Обеспечивает премиальный киберспортивный интерфейс в стиле Dota 2 HUD / Panorama:
- Интерактивный дашборд с мониторингом матча и живым аудио-визуализатором;
- Полнофункциональный ИИ-чат с озвучкой и быстрыми вопросами;
- Макро-радар и тайминги (Руны мудрости, Активные руны, Терзатель, Рошан, Отводы, Стаки);
- 1-клик пресеты ИИ (Бесплатный Pollinations, OpenRouter Free, DeepSeek, Groq, Ollama);
- Управление голосами нейросети Edge TTS (Светлана, Дмитрий, Дария);
- Самодиагностика и интеграция с Dota 2 / Steam в один клик.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .app import Assistant
from .audio import list_audio_devices
from .config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    Config,
    normalize_coach_tone,
    normalize_detail_mode,
    normalize_skill_level,
    save_config,
)
from .context import (
    extract_own_abilities,
    get_game_mode,
    get_live_gsi_snapshot,
    is_turbo,
)
from .doctor import get_checks, runtime_diagnostics
from .dota_install import add_to_steam_library, install_gsi_full
from .icons import (
    CDN as ICON_CDN,
)
from .icons import (
    create_session,
    download_image,
    item_icon_url,
    read_image_data_uri,
)
from .live_advice import build_item_advice
from .llm_router import compute_operating_mode, detect_provider_by_key
from .map_timers import compute_map_timers, compute_roshan_state, extract_match_id
from .security import mask_api_key_for_display, redact_secrets, register_config_secrets
from .settings_gui import test_llm_connection
from .telemetry import TelemetrySink
from .tray import AidotaTray
from .tts import SpeechPriority
from .windows_icon import get_app_icon_path, set_app_user_model_id, setup_window_handle

log = logging.getLogger("aidota.web_app")


def _normalize_hero_key(value: Any) -> str:
    """``npc_dota_hero_QueenofPain`` / ``Rubick`` → короткий ключ для иконки."""
    text = str(value or "").strip().lower()
    if text.startswith("npc_dota_hero_"):
        text = text[len("npc_dota_hero_"):]
    return re.sub(r"[^a-z0-9_]+", "", text)


class AidotaWebApi:
    """Двусторонний API-мост между JavaScript веб-интерфейсом и ядром Python."""

    def __init__(self, app_controller: WebGuiController):
        self._ctrl = app_controller
        self._cache_lock = threading.RLock()
        # Кэши иконок живут в памяти: JS дёргает API при каждой смене героя/слота.
        self._hero_icon_cache: dict[str, str] = {}
        self._item_icon_cache: dict[str, str] = {}
        self._icon_session_obj: Any | None = None
        self._heroes_cache: list[dict] | None = None
        self._items_cache: dict[str, str] | None = None
        self._item_costs_cache: dict[str, int] | None = None
        self._advice_cache_key: Any = None
        self._advice_cached_data: list[dict[str, Any]] = []
        self._meta_api: Any | None = None
        self._cached_data_version: int = -1
        self._standalone_coach: Any | None = None
        self._standalone_tts: Any | None = None
        self._telemetry = TelemetrySink(getattr(app_controller.config, "logs_path", None))

    # ------------------------------------------------------------------ #
    # Общие источники живых данных
    # ------------------------------------------------------------------ #
    def _assistant_coach(self) -> Any | None:
        return getattr(self._ctrl.assistant, "_coach", None) if self._ctrl.assistant else None

    def _item_api(self) -> Any | None:
        """DotaApi живого ассистента или локальный SWR-клиент (без сетевых вызовов)."""
        coach = self._assistant_coach()
        api = getattr(coach, "api", None) if coach is not None else None
        if api is None and self._ctrl.assistant is not None:
            api = getattr(self._ctrl.assistant, "_api", None)
        if api is not None:
            return api
        with self._cache_lock:
            if self._meta_api is None:
                try:
                    from .dota_api import DotaApi
                    from .meta_db import set_live_meta_provider

                    cfg = self._ctrl.config
                    local_api = DotaApi(
                        cfg.bundled_data_path,
                        cache_dir=cfg.meta_cache_path,
                        auto_swr=True,
                    )
                    local_api.load_snapshots()
                    set_live_meta_provider(local_api)
                    self._meta_api = local_api
                except Exception:  # noqa: BLE001
                    self._meta_api = None
            return self._meta_api

    def _sync_cache_version(self) -> int:
        """Сбрасывает WebUI-кэши героев, цен и советов при изменении ``cache_version``."""
        api = self._item_api()
        api_ver = 0
        if api is not None:
            raw_ver = getattr(api, "cache_version", 0)
            if isinstance(raw_ver, int) and not isinstance(raw_ver, bool):
                api_ver = raw_ver
        try:
            from .meta_db import live_cache_version

            db_ver = live_cache_version()
        except Exception:  # noqa: BLE001
            db_ver = 0
        combined = api_ver * 100000 + db_ver
        with self._cache_lock:
            if combined != self._cached_data_version:
                self._heroes_cache = None
                self._items_cache = None
                self._item_costs_cache = None
                self._advice_cache_key = None
                self._advice_cached_data = []
                self._cached_data_version = combined
        return combined

    def _heroes(self) -> list[dict]:
        """Снапшот героев (SWR-кэш / DotaApi → read-only data/heroes.json)."""
        self._sync_cache_version()
        if self._heroes_cache is not None:
            return self._heroes_cache
        heroes: list[dict] = []
        api = self._item_api()
        if api is not None:
            try:
                heroes = [h for h in (getattr(api, "heroes", []) or []) if isinstance(h, dict)]
            except Exception:  # noqa: BLE001
                heroes = []
        if not heroes:
            try:
                data = json.loads(
                    (self._ctrl.config.bundled_data_path / "heroes.json").read_text(encoding="utf-8")
                )
                if isinstance(data, list):
                    heroes = [h for h in data if isinstance(h, dict)]
            except Exception:  # noqa: BLE001
                heroes = []
        if heroes:
            self._heroes_cache = heroes
        return heroes

    def _items_map(self) -> dict[str, str]:
        """Словарь слаг → display-имя предмета (SWR-кэш / DotaApi → read-only data/items.json)."""
        self._sync_cache_version()
        if self._items_cache is not None:
            return self._items_cache
        result: dict[str, str] = {}
        api = self._item_api()
        if api is not None:
            try:
                raw = api.items() or {}
                if isinstance(raw, dict):
                    result = {str(key): str(value) for key, value in raw.items()}
            except Exception:  # noqa: BLE001
                result = {}
        if not result:
            try:
                data = json.loads(
                    (self._ctrl.config.bundled_data_path / "items.json").read_text(encoding="utf-8")
                )
                if isinstance(data, dict):
                    for key, value in data.items():
                        if isinstance(value, dict):
                            result[str(key)] = str(value.get("dname") or key)
                        else:
                            result[str(key)] = str(value)
            except Exception:  # noqa: BLE001
                result = {}
        if result:
            self._items_cache = result
        return result

    def _item_costs_map(self) -> dict[str, int]:
        """Display-имя предмета → цена (SWR-кэш / DotaApi.item_costs → read-only data/items.json)."""
        self._sync_cache_version()
        if self._item_costs_cache is not None:
            return self._item_costs_cache
        api = self._item_api()
        if api is not None:
            getter = getattr(api, "item_costs", None)
            if callable(getter):
                try:
                    data = getter() or {}
                    if isinstance(data, dict) and data:
                        result = {
                            str(key): int(value)
                            for key, value in data.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                        }
                        if result:
                            self._item_costs_cache = result
                            return result
                except Exception:  # noqa: BLE001
                    pass
        result = {}
        try:
            data = json.loads(
                (self._ctrl.config.bundled_data_path / "items.json").read_text(encoding="utf-8")
            )
            if isinstance(data, dict):
                for value in data.values():
                    if not isinstance(value, dict):
                        continue
                    display = value.get("dname")
                    cost = value.get("cost")
                    if isinstance(display, str) and isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        result[display] = int(cost)
        except Exception:  # noqa: BLE001
            result = {}
        if result:
            self._item_costs_cache = result
        return result

    # ------------------------------------------------------------------ #
    # Иконки и свежие советы (API для JS через webview.api)
    # ------------------------------------------------------------------ #
    def _icon_session(self) -> Any | None:
        """HTTP-сессия для скачивания иконок.

        По умолчанию переиспользуем сессию DotaApi (там уже выставлен
        User-Agent), иначе создаём свою. Тесты подменяют ``_icon_session_obj``
        на фейковую сессию — сеть в тестах не нужна.
        """
        if self._icon_session_obj is None:
            api = self._item_api()
            session = getattr(api, "_session", None) if api is not None else None
            if session is not None and callable(getattr(session, "get", None)):
                self._icon_session_obj = session
            else:
                try:
                    self._icon_session_obj = create_session()
                except Exception:  # noqa: BLE001
                    self._icon_session_obj = None
        return self._icon_session_obj

    def _hero_icon_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        try:
            dirs.append(self._ctrl.config.cache_path / "hero_icons")
        except Exception:  # noqa: BLE001
            pass
        try:
            dirs.append(self._ctrl.config.path(self._ctrl.config.vision.icons_dir))
        except Exception:  # noqa: BLE001
            pass
        dirs.append(PROJECT_ROOT / "hero_icons")
        dirs.append(Path(__file__).resolve().parent / "hero_icons")
        unique: list[Path] = []
        seen: set[str] = set()
        for directory in dirs:
            key = str(directory).casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(directory)
        return unique

    def _hero_cdn_url(self, key: str) -> str:
        """CDN-URL портрета героя из data/heroes.json (поле ``img``), иначе ''."""
        if not key:
            return ""
        target = key.casefold()
        for hero in self._heroes():
            name = str(hero.get("name") or "")
            slug = name[len("npc_dota_hero_"):] if name.startswith("npc_dota_hero_") else name
            localized = _normalize_hero_key(hero.get("localized_name"))
            if slug.casefold() != target and localized != target:
                continue
            img = str(hero.get("img") or "").split("?")[0]
            if img.startswith("/apps/"):
                return ICON_CDN + img
            if img.startswith("http"):
                return img
            return ""
        return ""

    def get_hero_icon(self, hero_key: str) -> str:
        """Data URI PNG портрета героя; CDN-URL как фолбэк; никогда не падает."""
        try:
            key = _normalize_hero_key(hero_key)
            if not key:
                return ""
            cached = self._hero_icon_cache.get(key)
            if cached is not None:
                return cached
            for directory in self._hero_icon_dirs():
                uri = read_image_data_uri(directory / f"{key}.png")
                if uri:
                    self._hero_icon_cache[key] = uri
                    return uri
            url = self._hero_cdn_url(key)
            if url:
                self._hero_icon_cache[key] = url
            return url
        except Exception:  # noqa: BLE001
            log.debug("get_hero_icon(%r) не удался", hero_key, exc_info=True)
            return ""

    def _item_icon_slug(self, item_key: Any) -> str:
        """Слаг предмета по любому виду ключа (``item_blink``/``Blink Dagger``)."""
        raw = str(item_key or "").strip()
        if not raw:
            return ""
        api = self._item_api()
        if api is not None:
            resolver = getattr(api, "item_slug", None)
            if callable(resolver):
                try:
                    slug = resolver(raw)
                    if isinstance(slug, str) and slug:
                        return slug
                except Exception:  # noqa: BLE001
                    pass
        candidate = raw[len("npc_dota_item_"):] if raw.startswith("npc_dota_item_") else raw
        if candidate.startswith("item_"):
            candidate = candidate[5:]
        items = self._items_map()
        if candidate in items:
            return candidate
        low = candidate.casefold()
        for slug in items:
            if str(slug).casefold() == low:
                return str(slug)
        for slug, display in items.items():
            if str(display or "").strip().casefold() == raw.casefold():
                return str(slug)
        return re.sub(r"[^a-z0-9_]+", "_", candidate.casefold()).strip("_")

    def get_item_icon(self, item_key: str) -> str:
        """Data URI PNG иконки предмета; CDN-URL при ошибке скачивания; '' — нет ключа."""
        try:
            slug = self._item_icon_slug(item_key)
            if not slug:
                return ""
            cached = self._item_icon_cache.get(slug)
            if cached is not None:
                return cached
            try:
                cache_dir = self._ctrl.config.cache_path / "item_icons"
            except Exception:  # noqa: BLE001
                cache_dir = PROJECT_ROOT / ".cache" / "item_icons"
            path = cache_dir / f"{slug}.png"
            uri = read_image_data_uri(path)
            if uri:
                self._item_icon_cache[slug] = uri
                return uri
            url = item_icon_url(slug)
            if url and download_image(self._icon_session(), url, path, timeout=4.0):
                uri = read_image_data_uri(path)
                if uri:
                    self._item_icon_cache[slug] = uri
                    return uri
            if url:
                # Не кэшируем CDN-фолбэк: сеть может восстановиться к следующему тику.
                return url
            return ""
        except Exception:  # noqa: BLE001
            log.debug("get_item_icon(%r) не удался", item_key, exc_info=True)
            return ""

    def get_live_advice(self) -> list[dict[str, Any]]:
        """Свежие советы по закупке (build_item_advice) по текущему GSI."""
        try:
            raw_state = self._ctrl.raw_gsi_payload()
            if not isinstance(raw_state, dict) or not raw_state:
                return []
            coach = self._assistant_coach()
            catalog = getattr(coach, "catalog", None) if coach is not None else None
            if catalog is None and self._ctrl.assistant is not None:
                catalog = getattr(self._ctrl.assistant, "_catalog", None)
            position = int(self._ctrl.config.player_profile.default_position or 1)
            item_costs = self._item_costs_map()
            snapshot = get_live_gsi_snapshot(
                raw_state,
                catalog,
                self._items_map(),
                position=position,
                item_costs=item_costs,
            )
            return build_item_advice(
                snapshot,
                hero_name=snapshot.get("hero_name") or "",
                position=position,
                enemy_heroes=list(snapshot.get("enemy_picks") or []),
                item_costs=item_costs,
            )
        except Exception:  # noqa: BLE001
            log.debug("get_live_advice не удался", exc_info=True)
            return []

    def _inventory_items(
        self,
        keys: list[str],
        displays: list[str],
        api: Any | None,
    ) -> list[dict[str, Any]]:
        """Слоты инвентаря для фронта: key/slug/display/cost."""
        result: list[dict[str, Any]] = []
        costs = self._item_costs_map()
        for index, raw_key in enumerate(keys):
            display = displays[index] if index < len(displays) else ""
            info: dict[str, Any] = {}
            if api is not None:
                resolver = getattr(api, "item_info", None)
                if callable(resolver):
                    try:
                        info = resolver(raw_key) or {}
                    except Exception:  # noqa: BLE001
                        info = {}
            slug = str(info.get("slug") or "") or self._item_icon_slug(raw_key)
            shown = str(info.get("display") or display or "")
            cost = info.get("cost")
            if not isinstance(cost, int) or isinstance(cost, bool):
                calculator = getattr(api, "item_cost", None) if api is not None else None
                if callable(calculator):
                    try:
                        cost = calculator(raw_key)
                    except Exception:  # noqa: BLE001
                        cost = None
            if not isinstance(cost, int) or isinstance(cost, bool):
                cost = costs.get(shown)
            result.append(
                {
                    "key": raw_key,
                    "slug": slug,
                    "display": shown,
                    "cost": int(cost) if isinstance(cost, int) and not isinstance(cost, bool) else None,
                }
            )
        return result

    def get_state(self) -> dict[str, Any]:
        """Возвращает полный срез состояния для синхронизации фронтенда.

        Важно: ассистент и GSI могут быть ещё не запущены — метод обязан
        возвращать безопасные значения и НИКОГДА не падать, иначе веб-UI
        перестаёт обновлять живые данные (герой, золото, KDA, таймеры).
        """
        cfg = self._ctrl.config
        coach_name = cfg.player_profile.coach_name or "Астра"
        pos = int(cfg.player_profile.default_position or 1)
        data_ver = self._sync_cache_version()

        coach = self._assistant_coach()
        raw_state: dict | None = None
        gsi_age: float | None = None
        catalog = None
        items_map: dict[str, str] = {}
        if coach is not None:
            coach_name = getattr(coach, "coach_name", None) or coach_name
            try:
                pos = int(getattr(coach, "position", pos) or pos)
            except (TypeError, ValueError):
                pass
            try:
                raw_state = coach.gsi_payload()
            except Exception:  # noqa: BLE001
                raw_state = None
            try:
                gsi_age = coach.gsi_age()
            except Exception:  # noqa: BLE001
                gsi_age = None
            catalog = getattr(coach, "catalog", None)
            try:
                items_map = coach.api.items() or {}
            except Exception:  # noqa: BLE001
                items_map = {}
        if catalog is None and self._ctrl.assistant is not None:
            catalog = getattr(self._ctrl.assistant, "_catalog", None)
        if not items_map and self._ctrl.assistant is not None:
            api_obj = getattr(self._ctrl.assistant, "_api", None)
            if api_obj is not None:
                try:
                    items_map = api_obj.items() or {}
                except Exception:  # noqa: BLE001
                    items_map = {}
        item_api = self._item_api()
        item_costs_map: dict[str, int] = {}
        if item_api is not None:
            costs_getter = getattr(item_api, "item_costs", None)
            if callable(costs_getter):
                try:
                    raw_costs = costs_getter() or {}
                    if isinstance(raw_costs, dict):
                        item_costs_map = {
                            str(key): int(value)
                            for key, value in raw_costs.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                        }
                except Exception:  # noqa: BLE001
                    item_costs_map = {}
        if not item_costs_map:
            # Контракт v1.2: оценка нетворса и цены советов по data/items.json.
            item_costs_map = self._item_costs_map()
        if raw_state is None:
            gsi_server = getattr(self._ctrl.assistant, "_gsi", None) if self._ctrl.assistant else None
            if gsi_server is not None:
                try:
                    raw_state = gsi_server.snapshot()
                except Exception:  # noqa: BLE001
                    raw_state = None
                if raw_state is not None and gsi_age is None:
                    try:
                        gsi_age = gsi_server.age()
                    except Exception:  # noqa: BLE001
                        gsi_age = None

        gsi_snap = (
            get_live_gsi_snapshot(
                raw_state,
                catalog,
                items_map,
                position=pos,
                item_costs=item_costs_map,
            )
            if raw_state
            else {}
        )
        raw_map = raw_state.get("map") if isinstance(raw_state, dict) else {}
        raw_player = raw_state.get("player") if isinstance(raw_state, dict) else {}
        raw_hero = raw_state.get("hero") if isinstance(raw_state, dict) else {}
        if not isinstance(raw_map, dict):
            raw_map = {}
        if not isinstance(raw_player, dict):
            raw_player = {}
        if not isinstance(raw_hero, dict):
            raw_hero = {}

        gsi_connected = bool(raw_state) and (gsi_age is None or float(gsi_age) < 45.0)
        hero_npc = str(raw_hero.get("name") or "")
        hero_short = hero_npc.replace("npc_dota_hero_", "") if hero_npc.startswith("npc_dota_hero_") else ""
        clock_seconds = gsi_snap.get("clock_seconds")
        if clock_seconds is None:
            clock_seconds = raw_map.get("clock_time") or 0

        # Ультимейт и его кулдаун: в реальном GSI abilities — верхнеуровневая секция.
        ult_cooldown = 0
        ult_ready = None
        abilities = extract_own_abilities(raw_state)
        if isinstance(abilities, dict):
            for ability in abilities.values():
                if not isinstance(ability, dict) or not ability.get("ultimate"):
                    continue
                try:
                    ult_cooldown = max(0, int(ability.get("cooldown") or 0))
                    ult_ready = bool(ability.get("can_cast", True)) and ult_cooldown <= 0
                except (TypeError, ValueError):
                    pass
                break

        inventory = gsi_snap.get("inventory") or []
        inventory_keys = gsi_snap.get("inventory_keys") or []
        inventory_items = self._inventory_items(inventory_keys, inventory, item_api)
        enemy_picks = list(gsi_snap.get("enemy_picks") or [])
        item_advice: list[dict[str, Any]] = []
        if gsi_snap.get("available"):
            advice_key = (
                data_ver,
                gsi_snap.get("hero_name") or "",
                pos,
                tuple(inventory_keys),
                tuple(enemy_picks),
                int(clock_seconds or 0) // 20,
            )
            if advice_key == self._advice_cache_key and self._advice_cached_data:
                item_advice = self._advice_cached_data
            else:
                try:
                    item_advice = build_item_advice(
                        gsi_snap,
                        hero_name=gsi_snap.get("hero_name") or "",
                        position=pos,
                        enemy_heroes=enemy_picks,
                        item_costs=item_costs_map,
                    )
                    self._advice_cache_key = advice_key
                    self._advice_cached_data = item_advice
                except Exception:  # noqa: BLE001 — советы не должны ломать UI
                    log.debug("build_item_advice в get_state не удался", exc_info=True)
                    item_advice = []
        coach_for_timers = coach if coach is not None else self._standalone_coach
        tracker = getattr(coach_for_timers, "cooldown_tracker", None)
        match_id = extract_match_id(raw_state) if raw_state else None
        if tracker is not None and match_id and hasattr(tracker, "sync_match_id"):
            try:
                tracker.sync_match_id(match_id)
            except Exception:  # noqa: BLE001
                pass
        roshan_death_sec = getattr(tracker, "roshan_death_sec", None)
        is_daytime = raw_map.get("daytime") if isinstance(raw_map.get("daytime"), bool) else None
        macro_timers = compute_map_timers(
            clock_seconds if raw_state else None,
            turbo=is_turbo(get_game_mode(raw_state)) if raw_state else False,
            roshan_death_sec=roshan_death_sec,
            is_daytime=is_daytime,
        )

        gsi_data = {
            "connected": gsi_connected,
            "age": round(float(gsi_age), 1) if gsi_age is not None else None,
            "hero_name": gsi_snap.get("hero_name") or "",
            "hero_npc": hero_short,
            "hero_level": gsi_snap.get("hero_level") or 1,
            # Устаревшее поле: JS берёт портрет через API get_hero_icon.
            "hero_icon": "",
            "hero_icon_key": hero_short,
            "kills": gsi_snap.get("kills", 0) or 0,
            "deaths": gsi_snap.get("deaths", 0) or 0,
            "assists": gsi_snap.get("assists", 0) or 0,
            "networth": gsi_snap.get("net_worth", 0) or 0,
            "networth_estimated": bool(gsi_snap.get("net_worth_estimated", False)),
            "gold": gsi_snap.get("gold", 0) or 0,
            "last_hits": gsi_snap.get("last_hits", 0) or 0,
            "denies": raw_player.get("denies", 0) or 0,
            "gpm": gsi_snap.get("gpm", raw_player.get("gpm", 0)) or 0,
            "xpm": gsi_snap.get("xpm", raw_player.get("xpm", 0)) or 0,
            "radiant_score": raw_map.get("radiant_score", 0) or 0,
            "dire_score": raw_map.get("dire_score", 0) or 0,
            "clock_time": clock_seconds,
            "inventory": inventory,
            "inventory_items": inventory_items,
            "neutral_item": gsi_snap.get("neutral_item"),
            "ult_cooldown": ult_cooldown,
            "ult_ready": ult_ready,
            "enemy_picks": enemy_picks,
            "item_advice": item_advice,
            "macro_timers": macro_timers,
        }

        return {
            "version": __version__,
            "coach": coach_name,
            "position": pos,
            "skill_level": normalize_skill_level(
                getattr(cfg.player_profile, "skill_level", "beginner")
            ),
            "assistant_running": self._ctrl.is_running,
            "gsi": gsi_data,
            "macro_timers": macro_timers,
            "audio_devices": self._ctrl.audio_devices(),
            "rust_active": False,
            "operating_mode": compute_operating_mode(cfg.llm),
            "runtime_diagnostics": runtime_diagnostics(
                cfg, self._assistant_coach() or self._standalone_coach, api=item_api
            ),
            "settings": self.get_settings(),
        }

    def get_settings(self) -> dict[str, Any]:
        """Возвращает текущую конфигурацию приложения без утечки открытого API-ключа."""
        cfg = self._ctrl.config
        plain_key = str(cfg.llm.api_key or "").strip()
        return {
            "llm_provider": cfg.llm.provider or "ollama",
            "llm_base_url": cfg.llm.base_url or "https://api.deepseek.com/v1",
            "llm_model": cfg.llm.model or "deepseek-chat",
            "llm_routing_mode": getattr(cfg.llm, "routing_mode", "auto") or "auto",
            "llm_api_key": "",
            "llm_api_key_set": bool(plain_key),
            "llm_api_key_masked": mask_api_key_for_display(plain_key),
            "tts_provider": cfg.tts.provider or "piper",
            "edge_voice": cfg.tts.edge_voice or "ru-RU-SvetlanaNeural",
            "input_device": cfg.audio.input_device,
            "output_device": cfg.tts.output_device,
            "ptt_key": cfg.audio.ptt_key or "v",
            "autonomy_level": cfg.player_profile.autonomy_level or "medium",
            "skill_level": normalize_skill_level(getattr(cfg.player_profile, "skill_level", "beginner")),
            "coach_tone": normalize_coach_tone(getattr(cfg.player_profile, "coach_tone", "balanced")),
            "detail_mode": normalize_detail_mode(getattr(cfg.player_profile, "detail_mode", "auto")),
            "custom_focus": str(getattr(cfg.player_profile, "custom_focus", "") or ""),
            "speak_chat": bool(getattr(cfg.player_profile, "speak_chat", True)),
            "audio_devices": self._ctrl.audio_devices(),
        }

    def toggle_mic(self) -> bool:
        """Переключает состояние микрофона активного ассистента."""
        if self._ctrl.assistant and hasattr(self._ctrl.assistant, "toggle_mic"):
            return bool(self._ctrl.assistant.toggle_mic())
        return False

    def toggle_assistant(self) -> bool:
        """Запускает или останавливает фонового голосового тренера."""
        if self._ctrl.is_running:
            self._ctrl.stop_assistant()
            return False
        self._ctrl.start_assistant()
        return True

    def set_coach(self, coach_name: str) -> bool:
        """Переключает активного тренера (Астра / Джарвис)."""
        clean = (coach_name or "Астра").strip()
        self._ctrl.config.player_profile.coach_name = clean
        # Синхронизируем голос
        if "джарвис" in clean.lower():
            self._ctrl.config.tts.edge_voice = "ru-RU-DmitryNeural"
        else:
            self._ctrl.config.tts.edge_voice = "ru-RU-SvetlanaNeural"

        if self._ctrl.assistant:
            self._ctrl.assistant.set_coach_name(clean)
            if hasattr(self._ctrl.assistant, "_tts") and hasattr(self._ctrl.assistant._tts, "config"):
                self._ctrl.assistant._tts.config.edge_voice = self._ctrl.config.tts.edge_voice

        save_config(self._ctrl.config, self._ctrl.config_path)
        if self._ctrl.tray is not None:
            self._ctrl.tray.update_menu()
        return True

    def set_position(self, pos: int) -> bool:
        """Переключает позицию игрока (1–5)."""
        pos = max(1, min(5, int(pos or 1)))
        self._ctrl.config.player_profile.default_position = pos
        if self._ctrl.assistant:
            self._ctrl.assistant.set_position(pos)
        save_config(self._ctrl.config, self._ctrl.config_path)
        if self._ctrl.tray is not None:
            self._ctrl.tray.update_menu()
        return True

    def test_voice(self, phrase: str) -> bool:
        """Воспроизводит проверочную фразу через TTS."""
        text = str(phrase or "Тест звуковой системы AIDOTA PRO").strip()
        if self._ctrl.assistant and hasattr(self._ctrl.assistant, "_tts"):
            self._ctrl.assistant._tts.speak(text)
            return True
        from .tts import Tts

        tts = Tts(self._ctrl.config.tts)
        tts.start()
        tts.speak(text)
        return True

    def toggle_overlay(self) -> bool:
        """Показывает или скрывает внутриигровой оверлей."""
        if self._ctrl.assistant and getattr(self._ctrl.assistant, "_overlay", None):
            self._ctrl.assistant._overlay.toggle()
            return True
        return False

    def record_roshan(self) -> dict[str, Any]:
        """Фиксирует смерть Рошана в каноническом CooldownTrackerSkill из WebUI."""
        coach = self._assistant_coach() or self._get_or_create_standalone_coach()
        payload: dict[str, Any] | None = None
        if hasattr(coach, "gsi_payload"):
            try:
                payload = coach.gsi_payload()
            except Exception:  # noqa: BLE001
                payload = None
        if payload is None and hasattr(self._ctrl, "raw_gsi_payload"):
            try:
                payload = self._ctrl.raw_gsi_payload()
            except Exception:  # noqa: BLE001
                payload = None
        raw_map = payload.get("map") if isinstance(payload, dict) and isinstance(payload.get("map"), dict) else {}
        clock_sec = raw_map.get("clock_time")
        match_id = extract_match_id(payload) if payload else None
        tracker = getattr(coach, "cooldown_tracker", None)
        if tracker is not None:
            message = tracker.record_roshan(clock_sec, match_id=match_id)
            death_sec = tracker.roshan_death_sec
        else:
            message = coach.record_roshan() if hasattr(coach, "record_roshan") else "Рошан зафиксирован."
            death_sec = clock_sec
        roshan = compute_roshan_state(death_sec, clock_sec)
        return {
            "success": True,
            "message": message,
            "roshan_death_sec": death_sec,
            "roshan": roshan,
        }

    def _get_or_create_standalone_coach(self) -> Any:
        with self._cache_lock:
            if self._standalone_coach is not None:
                return self._standalone_coach
            from .coach import Coach
            from .dota_api import DotaApi
            from .heroes import HeroCatalog

            cfg = self._ctrl.config
            api = self._meta_api
            if api is None:
                api = DotaApi(
                    cfg.bundled_data_path,
                    cache_dir=cfg.meta_cache_path,
                    auto_swr=True,
                )
                api.load_snapshots()
                self._meta_api = api
            catalog = HeroCatalog(api.heroes or self._heroes())
            self._standalone_coach = Coach(
                api,
                catalog,
                None,
                cfg.llm,
                cfg.player_profile,
                coach_name=cfg.player_profile.coach_name or "Астра",
                memory_path=cfg.state_data_path / "coach_memory.json",
                state_dir=cfg.state_data_path,
                telemetry=self._telemetry,
            )
            return self._standalone_coach

    def _get_or_create_standalone_tts(self) -> Any:
        with self._cache_lock:
            if self._standalone_tts is None:
                from .tts import HumanTTSManager

                self._standalone_tts = HumanTTSManager(
                    self._ctrl.config.tts,
                    self._ctrl.config.cache_path,
                )
            return self._standalone_tts

    def send_chat(self, text: str) -> str:
        """Обрабатывает текстовый вопрос игрока и неблокирующе ставит озвучку."""
        query = str(text or "").strip()
        if not query:
            return "Да, я на связи. Что подсказать?"
        speak = bool(getattr(self._ctrl.config.player_profile, "speak_chat", True))
        if self._ctrl.assistant and hasattr(self._ctrl.assistant, "_coach"):
            reply = self._ctrl.assistant._coach.coach_chat(query)
            if speak and hasattr(self._ctrl.assistant, "_tts"):
                tts = self._ctrl.assistant._tts
                if hasattr(tts, "bump_generation"):
                    tts.bump_generation()
                tts.speak(reply, wait=False, priority=SpeechPriority.LLM_COACH)
            return reply
        coach = self._get_or_create_standalone_coach()
        reply = coach.coach_chat(query)
        if speak and getattr(self._ctrl.config.tts, "enabled", True):
            tts = self._get_or_create_standalone_tts()
            if hasattr(tts, "bump_generation"):
                tts.bump_generation()
            tts.speak(reply, wait=False, priority=SpeechPriority.LLM_COACH)
        return reply

    def test_llm_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Проверяет подключение к указанной модели LLM (включая Ollama)."""
        base_url = str(payload.get("base_url") or "").strip()
        model = str(payload.get("model") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        provider = str(
            payload.get("provider") or payload.get("llm_provider") or ""
        ).strip().lower()

        if not provider:
            if "11434" in base_url or "ollama" in base_url.lower():
                provider = "ollama"
            else:
                provider = "openai_compatible"

        if not api_key and provider != "ollama":
            # Если пользователь нажал «Проверить» без повторного ввода уже сохранённого ключа
            cfg_key = str(self._ctrl.config.llm.api_key or "").strip()
            if cfg_key:
                api_key = cfg_key
                register_config_secrets(self._ctrl.config)

        if not base_url:
            base_url = (
                "http://127.0.0.1:11434"
                if provider == "ollama"
                else "https://text.pollinations.ai/openai"
            )
        if not model:
            model = "gemma3" if provider == "ollama" else "gemini"

        ok, msg = test_llm_connection(provider, base_url, api_key, model)
        return {"success": ok, "message": redact_secrets(msg)}

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет настройки пользователя в config.json без сплита конфигурации."""
        try:
            cfg = self._ctrl.config
            explicit_provider = str(payload.get("llm_provider") or "").strip().lower()
            if explicit_provider in ("ollama", "openai_compatible"):
                cfg.llm.provider = explicit_provider

            if "llm_base_url" in payload:
                val = str(payload["llm_base_url"]).strip()
                if val:
                    cfg.llm.base_url = val
                    if not explicit_provider:
                        cfg.llm.provider = (
                            "ollama"
                            if ("11434" in val or "ollama" in val.lower())
                            else "openai_compatible"
                        )
                elif not cfg.llm.base_url:
                    cfg.llm.base_url = (
                        "http://127.0.0.1:11434"
                        if explicit_provider == "ollama"
                        else "https://text.pollinations.ai/openai"
                    )
            if "llm_model" in payload:
                val = str(payload["llm_model"]).strip()
                if val:
                    cfg.llm.model = val
                elif not cfg.llm.model:
                    cfg.llm.model = "gemma3" if explicit_provider == "ollama" else "gemini"

            if payload.get("llm_api_key_clear"):
                cfg.llm.api_key = ""
            elif "llm_api_key" in payload:
                incoming_key = str(payload["llm_api_key"] or "").strip()
                if incoming_key:
                    cfg.llm.api_key = incoming_key
                elif not cfg.llm.api_key:
                    cfg.llm.api_key = ""

            if not explicit_provider and cfg.llm.api_key:
                detected = detect_provider_by_key(cfg.llm.api_key)
                if detected is not None and "pollinations" in (cfg.llm.base_url or "").lower():
                    _, det_url, det_model = detected
                    cfg.llm.base_url = det_url
                    if cfg.llm.model in ("", "openai", "gemini"):
                        cfg.llm.model = det_model

            if "llm_routing_mode" in payload and payload["llm_routing_mode"]:
                rm = str(payload["llm_routing_mode"]).strip().lower()
                if rm in ("auto", "fast_only", "custom"):
                    cfg.llm.routing_mode = rm

            if "tts_provider" in payload and payload["tts_provider"]:
                cfg.tts.provider = str(payload["tts_provider"]).strip()
            if "edge_voice" in payload and payload["edge_voice"]:
                cfg.tts.edge_voice = str(payload["edge_voice"]).strip()
            if "input_device" in payload:
                cfg.audio.input_device = payload["input_device"]
            if "output_device" in payload:
                cfg.tts.output_device = payload["output_device"]
            if "ptt_key" in payload and payload["ptt_key"]:
                cfg.audio.ptt_key = str(payload["ptt_key"]).strip()
            if "autonomy_level" in payload and payload["autonomy_level"]:
                cfg.player_profile.autonomy_level = str(payload["autonomy_level"]).strip()
            if "skill_level" in payload and payload["skill_level"]:
                cfg.player_profile.skill_level = normalize_skill_level(payload["skill_level"])
            if "coach_tone" in payload and payload["coach_tone"]:
                cfg.player_profile.coach_tone = normalize_coach_tone(payload["coach_tone"])
            if "detail_mode" in payload and payload["detail_mode"]:
                cfg.player_profile.detail_mode = normalize_detail_mode(payload["detail_mode"])
            if "custom_focus" in payload:
                cfg.player_profile.custom_focus = " ".join(str(payload["custom_focus"] or "").split())[:200]
            if "speak_chat" in payload:
                cfg.player_profile.speak_chat = bool(payload["speak_chat"])

            register_config_secrets(cfg)
            save_config(cfg, self._ctrl.config_path)

            # Обновляем инстансы тренера атомарно на лету
            if self._standalone_coach is not None and hasattr(self._standalone_coach, "set_llm_config"):
                self._standalone_coach.set_llm_config(cfg.llm)
            if self._ctrl.assistant:
                coach_inst = getattr(self._ctrl.assistant, "_coach", None)
                if coach_inst is not None:
                    if hasattr(coach_inst, "set_llm_config"):
                        coach_inst.set_llm_config(cfg.llm)
                    else:
                        coach_inst.llm.base_url = cfg.llm.base_url
                        coach_inst.llm.model = cfg.llm.model
                        coach_inst.llm.api_key = cfg.llm.api_key
                    if "coach_tone" in payload and hasattr(coach_inst, "set_coach_tone"):
                        coach_inst.set_coach_tone(cfg.player_profile.coach_tone)
                    if "detail_mode" in payload and hasattr(coach_inst, "set_detail_mode"):
                        coach_inst.set_detail_mode(cfg.player_profile.detail_mode)
                    if "custom_focus" in payload and hasattr(coach_inst, "set_custom_focus"):
                        coach_inst.set_custom_focus(cfg.player_profile.custom_focus)
                self._ctrl.assistant._agent.set_autonomy_level(cfg.player_profile.autonomy_level)
                self._apply_skill_level(cfg.player_profile.skill_level)

            return {"success": True, "message": "Настройки успешно сохранены!"}
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка сохранения настроек: %s", exc)
            return {"success": False, "message": redact_secrets(str(exc))}

    def _apply_skill_level(self, level: str) -> None:
        """Применяет уровень игры к живому тренеру без перезапуска приложения.

        ``set_skill_level`` в ``Coach``/``AutonomousCoachAgent`` может ещё не
        существовать (модуль новичкового тренера разрабатывается параллельно),
        поэтому используем ``getattr``/``hasattr`` и запасной путь через профиль.
        """
        level = normalize_skill_level(level)
        assistant = self._ctrl.assistant
        if not assistant:
            return
        for target_name in ("_coach", "_agent"):
            target = getattr(assistant, target_name, None)
            if target is None:
                continue
            setter = getattr(target, "set_skill_level", None)
            if callable(setter):
                try:
                    setter(level)
                    continue
                except Exception:  # noqa: BLE001
                    log.debug(
                        "set_skill_level недоступен для %s", target_name, exc_info=True
                    )
            profile = getattr(target, "profile", None)
            if profile is not None:
                try:
                    profile.skill_level = level
                except Exception:  # noqa: BLE001
                    log.debug("Не удалось обновить профиль %s", target_name, exc_info=True)

    def run_doctor(self, online: bool = False) -> list[dict[str, Any]]:
        """Запускает 12 проверок окружения и возвращает структурированный отчёт."""
        try:
            checks = get_checks(self._ctrl.config, online=bool(online))
            return [
                {
                    "name": getattr(c, "name", "Проверка"),
                    "status": getattr(c, "status", "WARN"),
                    "details": redact_secrets(str(getattr(c, "detail", "") or "")),
                    "hint": redact_secrets(str(getattr(c, "hint", "") or "")),
                }
                for c in checks
            ]
        except Exception as exc:  # noqa: BLE001
            return [{"name": "Ошибка диагностики", "status": "FAIL", "details": redact_secrets(str(exc)), "hint": ""}]

    def install_gsi(self) -> dict[str, Any]:
        """Автоматическая установка конфига GSI в Dota 2."""
        ok, msg = install_gsi_full(self._ctrl.config)
        return {"success": ok, "message": msg}

    def add_to_steam(self) -> dict[str, Any]:
        """Добавление ярлыка AIDOTA в библиотеку Steam."""
        ok, msg = add_to_steam_library()
        return {"success": ok, "message": msg}

    def minimize_to_tray(self) -> bool:
        """Сворачивает окно приложения в системный трей."""
        if self._ctrl.window:
            try:
                self._ctrl.window.minimize()
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def get_draft_recommendation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Возвращает рекомендации по драфту и контрпикам."""
        from .meta_db import recommend_picks
        enemies = payload.get("enemies") or []
        allies = payload.get("allies") or []
        role = int(payload.get("role") or self._ctrl.config.player_profile.default_position or 1)
        picks = recommend_picks(enemies, allies, role=role, top_k=5)
        return {"picks": picks}

    def get_hero_build_guide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Возвращает детальный гайд по закупкам и прокачке героя."""
        from .meta_db import get_hero_build
        hero_name = str(payload.get("hero_name") or "Anti-Mage").strip()
        role = int(payload.get("role") or self._ctrl.config.player_profile.default_position or 1)
        enemies = payload.get("enemies") or []
        build = get_hero_build(hero_name, role=role, enemy_heroes=enemies)
        return {"build": build}

    def get_meta_heroes_list(self) -> list[dict[str, Any]]:
        """Возвращает список героев для драфта и базы знаний."""
        from .meta_db import META_HEROES_DB
        items = []
        for hid, data in META_HEROES_DB.items():
            items.append({
                "id": hid,
                "name": data["name"],
                "ru_name": data["ru_name"],
                "roles": data.get("roles", [1]),
                "attr": data.get("attr", "str"),
            })
        return sorted(items, key=lambda x: x["ru_name"])

    def get_live_draft_heroes(self) -> dict[str, Any]:
        """Считывает текущих героев из матча Dota 2 (GSI)."""
        raw_state = self._ctrl.raw_gsi_payload()
        catalog = None
        if self._ctrl.assistant:
            coach = getattr(self._ctrl.assistant, "_coach", None)
            catalog = getattr(coach, "catalog", None) or getattr(self._ctrl.assistant, "_catalog", None)
        from .context import collect_draft

        draft = collect_draft(raw_state, catalog)
        return {
            "enemies": list(draft.enemy_picks),
            "allies": list(draft.my_picks),
        }

    def close(self) -> None:
        """Освобождает HTTP-сессию и фоновые ресурсы."""
        if self._icon_session_obj is not None:
            try:
                self._icon_session_obj.close()
            except Exception:  # noqa: BLE001
                pass
            self._icon_session_obj = None
        if self._standalone_coach is not None and hasattr(self._standalone_coach, "close"):
            try:
                self._standalone_coach.close()
            except Exception:  # noqa: BLE001
                pass
            self._standalone_coach = None
        if self._meta_api is not None:
            try:
                from .meta_db import _LIVE_META_LOCK, _LIVE_META_PROVIDER, set_live_meta_provider

                with _LIVE_META_LOCK:
                    is_current = _LIVE_META_PROVIDER is self._meta_api
                if is_current:
                    set_live_meta_provider(None)
            except Exception:  # noqa: BLE001
                pass
            if hasattr(self._meta_api, "close"):
                try:
                    self._meta_api.close()
                except Exception:  # noqa: BLE001
                    pass
            self._meta_api = None
        if self._standalone_tts is not None and hasattr(self._standalone_tts, "close"):
            try:
                self._standalone_tts.close()
            except Exception:  # noqa: BLE001
                pass
            self._standalone_tts = None


class WebGuiController:
    """Контроллер запуска веб-интерфейса и фоновых сервисов."""

    def __init__(self, config: Config, config_path: Path, enable_tray: bool = True):
        self.config = config
        self.config_path = config_path
        self.enable_tray = enable_tray
        self.assistant: Assistant | None = None
        self.is_running = False
        self._assistant_thread: threading.Thread | None = None
        self.window = None
        self._audio_cache: tuple[float, dict[str, Any]] | None = None

        # Инициализация системного трея
        self.tray: AidotaTray | None = None
        if self.enable_tray:
            self.tray = AidotaTray(
                on_show=self.restore_window,
                on_toggle_overlay=self._tray_toggle_overlay,
                get_coach=lambda: self.config.player_profile.coach_name,
                set_coach=self._tray_set_coach,
                get_position=lambda: self.config.player_profile.default_position,
                set_position=self._tray_set_position,
                get_autonomy=lambda: getattr(self.config.player_profile, "autonomy_level", "medium"),
                set_autonomy=self._tray_set_autonomy,
                on_open_settings=self.open_settings_tab,
                on_open_doctor=self.open_doctor_tab,
                on_exit=self.on_exit,
                icon_path=PROJECT_ROOT / "assets" / "icon.png",
            )
            self.tray.start()

    def start_assistant(self) -> None:
        """Запускает Assistant в фоновом потоке с общим Config."""
        if self.is_running:
            return
        try:
            # Отключаем старый оверлей Tkinter через runtime-флаг (без deepcopy Config):
            # веб-интерфейс сам является основным окном.
            self.assistant = Assistant(
                self.config,
                text_mode=False,
                config_path=self.config_path,
                disable_overlay=True,
            )
            # Кнопка/голос «Настройки» больше не открывает отдельное окно:
            # переключаем пользователя на вкладку настроек основного интерфейса.
            self.assistant.on_open_settings = self.open_settings_tab
            if getattr(self.assistant, "_overlay", None) is not None:
                self.assistant._overlay.on_open_settings = self.open_settings_tab
            self._assistant_thread = threading.Thread(
                target=self.assistant.run,
                daemon=True,
                name="aidota-web-assistant",
            )
            self._assistant_thread.start()
            self.is_running = True
            log.info("Фоновый ассистент AIDOTA успешно запущен.")
            setup_window_handle(self.window)
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось запустить ассистента: %s", exc)

    def raw_gsi_payload(self) -> dict[str, Any] | None:
        """Сырой GSI-снапшот (через Coach или напрямую из GsiServer)."""
        coach = getattr(self.assistant, "_coach", None) if self.assistant else None
        if coach is not None:
            try:
                payload = coach.gsi_payload()
                if payload:
                    return payload
            except Exception:  # noqa: BLE001
                pass
        gsi_server = getattr(self.assistant, "_gsi", None) if self.assistant else None
        if gsi_server is None:
            return None
        try:
            return gsi_server.snapshot()
        except Exception:  # noqa: BLE001
            return None

    def audio_devices(self, max_age: float = 30.0) -> dict[str, Any]:
        """Список аудиоустройств с кэшем: не дёргаем sounddevice каждый тик UI."""
        now = time.time()
        if self._audio_cache is not None and now - self._audio_cache[0] < max_age:
            return self._audio_cache[1]
        try:
            res = list_audio_devices()
            if isinstance(res, tuple) and len(res) == 2:
                devices = {"inputs": res[0], "outputs": res[1]}
            elif isinstance(res, dict):
                devices = res
            else:
                devices = {"inputs": [], "outputs": []}
        except Exception:  # noqa: BLE001
            devices = {"inputs": [], "outputs": []}
        self._audio_cache = (now, devices)
        return devices

    def open_settings_tab(self) -> None:
        """Показывает окно и открывает вкладку «Настройки» в веб-интерфейсе."""
        self._open_tab("settings")

    def open_doctor_tab(self) -> None:
        """Показывает окно и открывает вкладку «Диагностика»."""
        self._open_tab("doctor")

    def _open_tab(self, tab: str) -> None:
        self.restore_window()
        if self.window is not None:
            try:
                self.window.evaluate_js(
                    f"document.querySelector('[data-tab=\"{tab}\"]').click();"
                )
            except Exception:  # noqa: BLE001
                pass

    def stop_assistant(self) -> None:
        """Останавливает Assistant."""
        if not self.is_running:
            return
        try:
            if self.assistant:
                self.assistant.stop()
                self.assistant = None
            self.is_running = False
            log.info("Фоновый ассистент AIDOTA остановлен.")
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка при остановке ассистента: %s", exc)

    def restore_window(self) -> None:
        """Восстанавливает окно из системного трея."""
        if self.window:
            try:
                self.window.restore()
                self.window.show()
            except Exception:  # noqa: BLE001
                pass

    def _tray_toggle_overlay(self) -> None:
        if self.assistant and getattr(self.assistant, "_overlay", None):
            self.assistant._overlay.toggle()

    def _tray_set_coach(self, name: str) -> None:
        clean = (name or "Астра").strip()
        self.config.player_profile.coach_name = clean
        if self.assistant:
            self.assistant.set_coach_name(clean)
        save_config(self.config, self.config_path)

    def _tray_set_position(self, pos: int) -> None:
        self.config.player_profile.default_position = pos
        if self.assistant:
            self.assistant.set_position(pos)
        save_config(self.config, self.config_path)

    def _tray_set_autonomy(self, level: str) -> None:
        self.config.player_profile.autonomy_level = level
        if self.assistant:
            self.assistant.set_autonomy_level(level)
        save_config(self.config, self.config_path)

    def on_exit(self) -> None:
        """Полный выход из приложения."""
        self.stop_assistant()
        if self.tray is not None:
            self.tray.stop()
        if self.window:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                pass
        sys.exit(0)


def run_web_gui(config: Config, config_path: Path | None = None) -> int:
    """Запускает современный веб-интерфейс AIDOTA через pywebview."""
    import webview

    set_app_user_model_id()
    icon_path = get_app_icon_path()

    cfg_path = config_path or DEFAULT_CONFIG_PATH
    ctrl = WebGuiController(config, cfg_path)
    api = AidotaWebApi(ctrl)

    web_dir = Path(__file__).resolve().parent / "web"
    index_file = web_dir / "index.html"

    window = webview.create_window(
        title=f"AIDOTA PRO v{__version__} — ИИ-тренер Dota 2",
        url=str(index_file.resolve()),
        js_api=api,
        width=1050,
        height=750,
        min_size=(880, 620),
        background_color="#090a0f",
    )
    ctrl.window = window

    def _on_window_shown():
        setup_window_handle(window)

    window.events.shown += _on_window_shown

    try:
        webview.start(
            ctrl.start_assistant,
            debug=False,
            icon=str(icon_path) if icon_path else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("pywebview завершился с ошибкой или прерван: %s", exc)
    finally:
        ctrl.on_exit()

    return 0
