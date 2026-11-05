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
import os
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
    load_config,
    normalize_skill_level,
    save_config,
)
from .context import (
    extract_own_abilities,
    get_live_gsi_snapshot,
)
from .doctor import get_checks
from .dota_install import add_to_steam_library, install_gsi_full
from .icons import (
    CDN as ICON_CDN,
    create_session,
    download_image,
    item_icon_url,
    read_image_data_uri,
)
from .live_advice import build_item_advice
from .settings_gui import test_llm_connection
from .tray import AidotaTray

log = logging.getLogger("aidota.web_app")


def _normalize_hero_key(value: Any) -> str:
    """``npc_dota_hero_QueenofPain`` / ``Rubick`` → короткий ключ для иконки."""
    text = str(value or "").strip().lower()
    if text.startswith("npc_dota_hero_"):
        text = text[len("npc_dota_hero_"):]
    return re.sub(r"[^a-z0-9_]+", "", text)


class AidotaWebApi:
    """Двусторонний API-мост между JavaScript веб-интерфейсом и ядром Python."""

    def __init__(self, app_controller: "WebGuiController"):
        self.ctrl = app_controller
        # Кэши иконок живут в памяти: JS дёргает API при каждой смене героя/слота.
        self._hero_icon_cache: dict[str, str] = {}
        self._item_icon_cache: dict[str, str] = {}
        self._icon_session_obj: Any | None = None
        self._heroes_cache: list[dict] | None = None
        self._items_cache: dict[str, str] | None = None
        self._item_costs_cache: dict[str, int] | None = None

    # ------------------------------------------------------------------ #
    # Общие источники живых данных
    # ------------------------------------------------------------------ #
    def _assistant_coach(self) -> Any | None:
        return getattr(self.ctrl.assistant, "_coach", None) if self.ctrl.assistant else None

    def _item_api(self) -> Any | None:
        """DotaApi живого ассистента (если он уже создан)."""
        coach = self._assistant_coach()
        api = getattr(coach, "api", None) if coach is not None else None
        if api is None and self.ctrl.assistant is not None:
            api = getattr(self.ctrl.assistant, "_api", None)
        return api

    def _heroes(self) -> list[dict]:
        """Снапшот героев (для CDN-фолбэка иконки): DotaApi → data/heroes.json."""
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
                    (self.ctrl.config.data_path / "heroes.json").read_text(encoding="utf-8")
                )
                if isinstance(data, list):
                    heroes = [h for h in data if isinstance(h, dict)]
            except Exception:  # noqa: BLE001
                heroes = []
        if heroes:
            self._heroes_cache = heroes
        return heroes

    def _items_map(self) -> dict[str, str]:
        """Словарь слаг → display-имя предмета (DotaApi → data/items.json)."""
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
                    (self.ctrl.config.data_path / "items.json").read_text(encoding="utf-8")
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
        """Display-имя предмета → цена (DotaApi.item_costs → data/items.json)."""
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
                (self.ctrl.config.data_path / "items.json").read_text(encoding="utf-8")
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
            dirs.append(self.ctrl.config.path(self.ctrl.config.vision.icons_dir))
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
                cache_dir = self.ctrl.config.cache_path / "item_icons"
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
            raw_state = self.ctrl.raw_gsi_payload()
            if not isinstance(raw_state, dict) or not raw_state:
                return []
            coach = self._assistant_coach()
            catalog = getattr(coach, "catalog", None) if coach is not None else None
            if catalog is None and self.ctrl.assistant is not None:
                catalog = getattr(self.ctrl.assistant, "_catalog", None)
            position = int(self.ctrl.config.player_profile.default_position or 1)
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
        cfg = self.ctrl.config
        coach_name = cfg.player_profile.coach_name or "Астра"
        pos = int(cfg.player_profile.default_position or 1)

        coach = getattr(self.ctrl.assistant, "_coach", None) if self.ctrl.assistant else None
        raw_state: dict | None = None
        gsi_age: float | None = None
        catalog = None
        items_map: dict[str, str] = {}
        if coach is not None:
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
        if catalog is None and self.ctrl.assistant is not None:
            catalog = getattr(self.ctrl.assistant, "_catalog", None)
        if not items_map and self.ctrl.assistant is not None:
            api_obj = getattr(self.ctrl.assistant, "_api", None)
            if api_obj is not None:
                try:
                    items_map = api_obj.items() or {}
                except Exception:  # noqa: BLE001
                    items_map = {}
        item_api = getattr(coach, "api", None) if coach is not None else None
        if item_api is None and self.ctrl.assistant is not None:
            item_api = getattr(self.ctrl.assistant, "_api", None)
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
            gsi_server = getattr(self.ctrl.assistant, "_gsi", None) if self.ctrl.assistant else None
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
            try:
                item_advice = build_item_advice(
                    gsi_snap,
                    hero_name=gsi_snap.get("hero_name") or "",
                    position=pos,
                    enemy_heroes=enemy_picks,
                    item_costs=item_costs_map,
                )
            except Exception:  # noqa: BLE001 — советы не должны ломать UI
                log.debug("build_item_advice в get_state не удался", exc_info=True)
                item_advice = []
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
        }

        return {
            "version": __version__,
            "coach": coach_name,
            "position": pos,
            "skill_level": normalize_skill_level(
                getattr(cfg.player_profile, "skill_level", "beginner")
            ),
            "assistant_running": self.ctrl.is_running,
            "gsi": gsi_data,
            "audio_devices": self.ctrl.audio_devices(),
        }

    def toggle_assistant(self) -> bool:
        """Запускает или останавливает фонового голосового тренера."""
        if self.ctrl.is_running:
            self.ctrl.stop_assistant()
            return False
        self.ctrl.start_assistant()
        return True

    def set_coach(self, coach_name: str) -> bool:
        """Переключает активного тренера (Астра / Джарвис)."""
        clean = (coach_name or "Астра").strip()
        self.ctrl.config.player_profile.coach_name = clean
        # Синхронизируем голос
        if "джарвис" in clean.lower():
            self.ctrl.config.tts.edge_voice = "ru-RU-DmitryNeural"
        else:
            self.ctrl.config.tts.edge_voice = "ru-RU-SvetlanaNeural"

        if self.ctrl.assistant:
            self.ctrl.assistant.set_coach_name(clean)
            if hasattr(self.ctrl.assistant, "_tts") and hasattr(self.ctrl.assistant._tts, "config"):
                self.ctrl.assistant._tts.config.edge_voice = self.ctrl.config.tts.edge_voice

        save_config(self.ctrl.config, self.ctrl.config_path)
        self.ctrl.tray.update_menu()
        return True

    def set_position(self, pos: int) -> bool:
        """Переключает позицию игрока (1–5)."""
        pos = max(1, min(5, int(pos or 1)))
        self.ctrl.config.player_profile.default_position = pos
        if self.ctrl.assistant:
            self.ctrl.assistant.set_position(pos)
        save_config(self.ctrl.config, self.ctrl.config_path)
        self.ctrl.tray.update_menu()
        return True

    def test_voice(self, phrase: str) -> bool:
        """Воспроизводит проверочную фразу через TTS."""
        text = str(phrase or "Тест звуковой системы AIDOTA PRO").strip()
        if self.ctrl.assistant and hasattr(self.ctrl.assistant, "_tts"):
            self.ctrl.assistant._tts.speak(text)
            return True
        from .tts import Tts

        tts = Tts(self.ctrl.config.tts)
        tts.start()
        tts.speak(text)
        return True

    def toggle_overlay(self) -> bool:
        """Показывает или скрывает внутриигровой оверлей."""
        if self.ctrl.assistant and getattr(self.ctrl.assistant, "_overlay", None):
            self.ctrl.assistant._overlay.toggle()
            return True
        return False

    def send_chat(self, text: str) -> str:
        """Обрабатывает текстовый вопрос игрока через ИИ и возвращает ответ тренера."""
        query = str(text or "").strip()
        if not query:
            return "Да, я на связи. Что подсказать?"
        if self.ctrl.assistant and hasattr(self.ctrl.assistant, "_coach"):
            reply = self.ctrl.assistant._coach.coach_chat(query)
            if hasattr(self.ctrl.assistant, "_tts"):
                self.ctrl.assistant._tts.speak(reply)
            return reply
        from .coach import Coach
        from .dota_api import DotaApi
        from .heroes import HeroCatalog

        api = DotaApi(self.ctrl.config.data_path)
        api.load_snapshots()
        coach = Coach(
            api,
            HeroCatalog([]),
            None,
            self.ctrl.config.llm,
            self.ctrl.config.player_profile,
            coach_name=self.ctrl.config.player_profile.coach_name or "Астра",
        )
        return coach.coach_chat(query)

    def test_llm_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Проверяет подключение к указанной модели LLM."""
        base_url = str(payload.get("base_url") or "").strip()
        model = str(payload.get("model") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()

        ok, msg = test_llm_connection("openai_compatible", base_url, api_key, model)
        return {"success": ok, "message": msg}

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет настройки пользователя в config.json."""
        try:
            cfg = self.ctrl.config
            if "llm_base_url" in payload:
                cfg.llm.base_url = str(payload["llm_base_url"]).strip()
            if "llm_model" in payload:
                cfg.llm.model = str(payload["llm_model"]).strip()
            if "llm_api_key" in payload:
                cfg.llm.api_key = str(payload["llm_api_key"]).strip()
            if "tts_provider" in payload:
                cfg.tts.provider = str(payload["tts_provider"]).strip()
            if "edge_voice" in payload:
                cfg.tts.edge_voice = str(payload["edge_voice"]).strip()
            if "input_device" in payload:
                cfg.audio.input_device = payload["input_device"]
            if "output_device" in payload:
                cfg.tts.output_device = payload["output_device"]
            if "ptt_key" in payload and payload["ptt_key"]:
                cfg.audio.ptt_key = str(payload["ptt_key"]).strip()
            if "autonomy_level" in payload:
                cfg.player_profile.autonomy_level = str(payload["autonomy_level"]).strip()
            if "skill_level" in payload:
                cfg.player_profile.skill_level = normalize_skill_level(payload["skill_level"])

            save_config(cfg, self.ctrl.config_path)

            # Обновляем инстанс ассистента на лету
            if self.ctrl.assistant:
                self.ctrl.assistant._coach.llm.base_url = cfg.llm.base_url
                self.ctrl.assistant._coach.llm.model = cfg.llm.model
                self.ctrl.assistant._coach.llm.api_key = cfg.llm.api_key
                self.ctrl.assistant._agent.set_autonomy_level(cfg.player_profile.autonomy_level)
                self._apply_skill_level(cfg.player_profile.skill_level)

            return {"success": True, "message": "Настройки успешно сохранены!"}
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка сохранения настроек: %s", exc)
            return {"success": False, "message": str(exc)}

    def _apply_skill_level(self, level: str) -> None:
        """Применяет уровень игры к живому тренеру без перезапуска приложения.

        ``set_skill_level`` в ``Coach``/``AutonomousCoachAgent`` может ещё не
        существовать (модуль новичкового тренера разрабатывается параллельно),
        поэтому используем ``getattr``/``hasattr`` и запасной путь через профиль.
        """
        level = normalize_skill_level(level)
        assistant = self.ctrl.assistant
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

    def run_doctor(self) -> list[dict[str, Any]]:
        """Запускает 12 проверок окружения и возвращает структурированный отчёт."""
        try:
            checks = get_checks(self.ctrl.config)
            return [
                {
                    "name": getattr(c, "name", "Проверка"),
                    "status": "OK" if getattr(c, "ok", False) else "WARN",
                    "details": getattr(c, "details", ""),
                }
                for c in checks
            ]
        except Exception as exc:  # noqa: BLE001
            return [{"name": "Ошибка диагностики", "status": "WARN", "details": str(exc)}]

    def install_gsi(self) -> dict[str, Any]:
        """Автоматическая установка конфига GSI в Dota 2."""
        ok, msg = install_gsi_full(self.ctrl.config)
        return {"success": ok, "message": msg}

    def add_to_steam(self) -> dict[str, Any]:
        """Добавление ярлыка AIDOTA в библиотеку Steam."""
        ok, msg = add_to_steam_library()
        return {"success": ok, "message": msg}

    def minimize_to_tray(self) -> bool:
        """Сворачивает окно приложения в системный трей."""
        if self.ctrl.window:
            try:
                self.ctrl.window.minimize()
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def get_draft_recommendation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Возвращает рекомендации по драфту и контрпикам."""
        from .meta_db import recommend_picks
        enemies = payload.get("enemies") or []
        allies = payload.get("allies") or []
        role = int(payload.get("role") or self.ctrl.config.player_profile.default_position or 1)
        picks = recommend_picks(enemies, allies, role=role, top_k=5)
        return {"picks": picks}

    def get_hero_build_guide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Возвращает детальный гайд по закупкам и прокачке героя."""
        from .meta_db import get_hero_build
        hero_name = str(payload.get("hero_name") or "Anti-Mage").strip()
        role = int(payload.get("role") or self.ctrl.config.player_profile.default_position or 1)
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
        raw_state = self.ctrl.raw_gsi_payload()
        catalog = None
        if self.ctrl.assistant:
            coach = getattr(self.ctrl.assistant, "_coach", None)
            catalog = getattr(coach, "catalog", None) or getattr(self.ctrl.assistant, "_catalog", None)
        from .context import collect_draft

        draft = collect_draft(raw_state, catalog)
        return {
            "enemies": list(draft.enemy_picks),
            "allies": list(draft.my_picks),
        }


class WebGuiController:
    """Контроллер запуска веб-интерфейса и фоновых сервисов."""

    def __init__(self, config: Config, config_path: Path):
        self.config = config
        self.config_path = config_path
        self.assistant: Assistant | None = None
        self.is_running = False
        self._assistant_thread: threading.Thread | None = None
        self.window = None
        self._audio_cache: tuple[float, dict[str, Any]] | None = None

        # Инициализация системного трея
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
        """Запускает Assistant в фоновом потоке."""
        if self.is_running:
            return
        try:
            self.assistant = Assistant(self.config, text_mode=False, config_path=self.config_path)
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
            devices = list_audio_devices()
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

    cfg_path = config_path or DEFAULT_CONFIG_PATH
    ctrl = WebGuiController(config, cfg_path)
    api = AidotaWebApi(ctrl)

    web_dir = Path(__file__).resolve().parent / "web"
    index_file = web_dir / "index.html"

    window = webview.create_window(
        title=f"✦ AIDOTA PRO v{__version__} — Персональный ИИ-тренер Dota 2",
        url=str(index_file.resolve()),
        js_api=api,
        width=1050,
        height=750,
        min_size=(880, 620),
        background_color="#0b0e14",
    )
    ctrl.window = window

    # Автоматически стартуем ассистента в фоне
    ctrl.start_assistant()

    try:
        webview.start(debug=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("pywebview завершился с ошибкой или прерван: %s", exc)
    finally:
        ctrl.on_exit()

    return 0
