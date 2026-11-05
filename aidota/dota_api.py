"""Клиент OpenDota + локальные снапшоты героев/предметов.

Снапшоты лежат в data/ и позволяют работать без сети (старт приложения
не зависит от доступности OpenDota). Сеть используется для обновления
меты и популярных предметов; любые сетевые ошибки не роняют приложение.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("aidota.dota_api")

OPENDOTA = "https://api.opendota.com/api"
USER_AGENT = "aidota/1.0 (Dota 2 voice assistant; local use)"

POPULARITY_KEYS = (
    "start_game_items",
    "early_game_items",
    "mid_game_items",
    "late_game_items",
)


class DotaApiError(RuntimeError):
    pass


class DotaApi:
    def __init__(
        self,
        data_dir: Path,
        *,
        timeout: float = 15.0,
        session: Any | None = None,
        popularity_ttl: float = 6 * 3600,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.popularity_ttl = popularity_ttl
        self._lock = threading.RLock()

        self._heroes: list[dict] | None = None
        self._items: dict[str, str] | None = None
        self._item_ids: dict[int, str] = {}
        self._item_costs: dict[str, int] = {}
        self._popularity: dict[int, tuple[float, dict]] = {}
        self._popularity_error_at: dict[int, float] = {}
        self.last_error: str | None = None
        self.snapshot_time: float | None = None

        if session is None:
            import requests  # локальный импорт: тесты могут не иметь requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self._session = session

    # ------------------------------------------------------------------ #
    # Снапшоты
    # ------------------------------------------------------------------ #
    def load_snapshots(self) -> bool:
        """Загружает data/heroes.json и data/items.json. Возвращает True при успехе."""
        heroes = self._read_json(self.data_dir / "heroes.json")
        items = self._read_json(self.data_dir / "items.json")
        ok = False
        with self._lock:
            if isinstance(heroes, list) and heroes:
                self._heroes = heroes
                ok = True
                try:
                    self.snapshot_time = (self.data_dir / "heroes.json").stat().st_mtime
                except OSError:
                    self.snapshot_time = None
            if isinstance(items, dict) and items:
                self._items, self._item_ids = self._parse_items(items)
                self._item_costs = self._parse_item_costs(items)
                ok = True
        if not ok:
            log.warning("Снапшоты data/heroes.json или data/items.json не найдены")
        return ok

    def refresh_snapshots(self) -> bool:
        """Скачивает свежие данные OpenDota и сохраняет их в data/."""
        try:
            heroes = self._get_json(f"{OPENDOTA}/heroStats")
            items_raw = self._get_json(f"{OPENDOTA}/constants/items")
        except Exception as exc:  # noqa: BLE001 — сеть бывает любой
            self.last_error = str(exc)
            log.warning("Не удалось обновить снапшоты OpenDota: %s", exc)
            return False
        if not isinstance(heroes, list) or not heroes or not isinstance(items_raw, dict):
            self.last_error = "OpenDota вернула неожиданный формат данных"
            log.warning(self.last_error)
            return False
        items = {
            str(key): {
                "dname": str(value.get("dname") or key),
                "id": value.get("id"),
                "cost": self._coerce_cost(value.get("cost")),
            }
            for key, value in items_raw.items()
            if isinstance(value, dict)
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.data_dir / "heroes.json", heroes)
        self._write_json(self.data_dir / "items.json", items)
        with self._lock:
            self._heroes = heroes
            self._items, self._item_ids = self._parse_items(items)
            self._item_costs = self._parse_item_costs(items)
            self.snapshot_time = time.time()
        log.info("Снапшоты обновлены: %d героев, %d предметов", len(heroes), len(items))
        return True

    @staticmethod
    def _parse_items(raw: dict) -> tuple[dict[str, str], dict[int, str]]:
        """Поддерживает и новый формат {name: {dname,id,cost}}, и старый {name: dname}."""
        names: dict[str, str] = {}
        ids: dict[int, str] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                display = str(value.get("dname") or key)
                item_id = value.get("id")
            else:
                display = str(value)
                item_id = None
            names[str(key)] = display
            if isinstance(item_id, int) or (isinstance(item_id, str) and item_id.isdigit()):
                ids[int(item_id)] = display
        return names, ids

    @staticmethod
    def _coerce_cost(value: Any) -> int | None:
        """Аккуратно приводит ``cost`` из снапшота/OpenDota к int (или None)."""
        if value is None or isinstance(value, bool):
            return None
        try:
            cost = int(value)
        except (TypeError, ValueError):
            return None
        return cost if cost >= 0 else None

    @classmethod
    def _parse_item_costs(cls, raw: dict) -> dict[str, int]:
        """Слаг предмета → цена. Старый формат без ``cost`` даёт пустой словарь."""
        costs: dict[str, int] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            cost = cls._coerce_cost(value.get("cost"))
            if cost is not None:
                costs[str(key)] = cost
        return costs

    def ensure_loaded(self) -> None:
        """Гарантирует наличие героев: снапшот, затем сеть."""
        if self._heroes:
            return
        self.load_snapshots()
        if not self._heroes:
            log.info("Снапшотов нет — пробую загрузить данные с OpenDota…")
            self.refresh_snapshots()
        if not self._heroes:
            raise DotaApiError(
                "Нет данных о героях: отсутствует data/heroes.json и нет связи с OpenDota. "
                "Проверьте интернет или запустите python main.py --fetch-icons."
            )

    # ------------------------------------------------------------------ #
    # Герои и предметы
    # ------------------------------------------------------------------ #
    @property
    def heroes(self) -> list[dict]:
        with self._lock:
            return list(self._heroes or [])

    def hero_by_id(self, hero_id: Any) -> dict | None:
        for hero in self.heroes:
            if hero.get("id") == hero_id:
                return hero
        return None

    def items(self) -> dict[str, str]:
        if self._items is None:
            self.load_snapshots()
        return dict(self._items or {})

    def item_id_names(self) -> dict[int, str]:
        if self._items is None:
            self.load_snapshots()
        return dict(self._item_ids)

    def item_display(self, gsi_name: str | None) -> str:
        """'item_blink' -> 'Blink Dagger'; аккуратно относится к неизвестным именам."""
        if not gsi_name:
            return "?"
        key = str(gsi_name)
        if key.startswith("item_"):
            key = key[5:]
        key = key.replace("item_", "")
        display = self.items().get(key)
        if display:
            return display
        return key.replace("_", " ").strip().title()

    # ------------------------------------------------------------------ #
    # Предметы: слаги, цены и data-driven подсказки по закупкам
    # ------------------------------------------------------------------ #
    def _item_slug(self, name: Any) -> str | None:
        """Внутренний ключ предмета по display-имени, npc-имени или слагу."""
        if not isinstance(name, str):
            return None
        raw = name.strip()
        if not raw:
            return None
        items = self.items()
        candidate = raw
        for prefix in ("npc_dota_item_", "item_"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        if candidate in items:
            return candidate
        low = candidate.casefold()
        for slug in items:
            if str(slug).casefold() == low:
                return slug
        display_low = raw.casefold()
        for slug, display in items.items():
            shown = str(display or "").strip().casefold()
            if shown and (shown == display_low or shown == low):
                return slug
        return None

    def _ensure_item_costs(self) -> dict[str, int]:
        with self._lock:
            if self._items is None:
                self.load_snapshots()
            return dict(self._item_costs)

    def item_slug(self, name: Any) -> str | None:
        """Слаг для CDN (``blink``, ``sphere``, ...) по любому виду имени."""
        return self._item_slug(name)

    def item_cost(self, name: Any) -> int | None:
        """Цена предмета по display/npc/слагу; ``None``, если предмет/цена неизвестны."""
        slug = self._item_slug(name)
        if slug is None:
            return None
        cost = self._ensure_item_costs().get(slug)
        return int(cost) if isinstance(cost, int) else None

    def item_info(self, name: Any) -> dict | None:
        """``{"slug", "display", "cost"}`` по любому виду имени (или ``None``)."""
        slug = self._item_slug(name)
        if slug is None:
            return None
        display = self.items().get(slug) or self.item_display(slug)
        cost = self._ensure_item_costs().get(slug)
        return {
            "slug": slug,
            "display": display,
            "cost": int(cost) if isinstance(cost, int) else None,
        }

    def item_costs(self) -> dict[str, int]:
        """Display-имя → цена. Только предметы с известной ценой."""
        costs = self._ensure_item_costs()
        items = self.items()
        return {
            str(items.get(slug) or self.item_display(slug)): int(cost)
            for slug, cost in costs.items()
        }

    def hero_display(self, hero: dict | None) -> str:
        return (hero or {}).get("localized_name") or (hero or {}).get("name") or "?"

    # ------------------------------------------------------------------ #
    # Метрики
    # ------------------------------------------------------------------ #
    @staticmethod
    def winrate(hero: dict) -> tuple[float, str, int]:
        """(процент, 'pro'|'pub'|'none', число игр)."""
        pro_pick = int(hero.get("pro_pick") or 0)
        pro_win = int(hero.get("pro_win") or 0)
        if pro_pick > 0:
            return pro_win / pro_pick * 100.0, "pro", pro_pick
        pub_pick = int(hero.get("pub_pick") or 0)
        pub_win = int(hero.get("pub_win") or 0)
        if pub_pick > 0:
            return pub_win / pub_pick * 100.0, "pub", pub_pick
        return 0.0, "none", 0

    def meta(self, n: int = 5, min_pro_picks: int = 15) -> list[dict]:
        """Топ героев по винрейту с разумным минимумом игр."""
        heroes = self.heroes
        pro = [
            (h, self.winrate(h))
            for h in heroes
            if self.winrate(h)[1] == "pro" and self.winrate(h)[2] >= min_pro_picks
        ]
        pool = pro
        if len(pool) < n:
            pool = [(h, self.winrate(h)) for h in heroes if self.winrate(h)[1] != "none"]
        ranked = sorted(pool, key=lambda item: item[1][0], reverse=True)[:n]
        return [
            {"hero": h, "winrate": pct, "kind": kind, "games": games}
            for h, (pct, kind, games) in ranked
        ]

    # ------------------------------------------------------------------ #
    # Популярные предметы
    # ------------------------------------------------------------------ #
    def item_popularity(self, hero_id: int) -> dict[str, dict[str, int]]:
        """Словарь start_game_items / early_game_items / … (ключи OpenDota)."""
        now = time.time()
        with self._lock:
            cached = self._popularity.get(hero_id)
            if cached and now - cached[0] < self.popularity_ttl:
                return cached[1]
            failed_at = self._popularity_error_at.get(hero_id)
            if failed_at and now - failed_at < 60:
                return {}
        try:
            data = self._get_json(f"{OPENDOTA}/heroes/{int(hero_id)}/itemPopularity")
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.debug("itemPopularity(%s) недоступен: %s", hero_id, exc)
            with self._lock:
                self._popularity_error_at[hero_id] = time.time()
            return {}
        if not isinstance(data, dict):
            return {}
        result = {
            key: {str(k): int(v) for k, v in value.items() if isinstance(v, (int, float))}
            for key, value in data.items()
            if key in POPULARITY_KEYS and isinstance(value, dict)
        }
        with self._lock:
            self._popularity[hero_id] = (time.time(), result)
        return result

    def top_items(self, popularity: dict[str, dict[str, int]], phase: str, n: int = 6) -> list[str]:
        """Ключи itemPopularity — числовые id предметов; превращаем их в названия."""
        phase_data = popularity.get(phase) or {}
        ordered = sorted(phase_data.items(), key=lambda kv: kv[1], reverse=True)[:n]
        id_names = self.item_id_names()
        result: list[str] = []
        for key, _ in ordered:
            if isinstance(key, str) and key.isdigit() and int(key) in id_names:
                result.append(id_names[int(key)])
            else:
                result.append(self.item_display(key))
        return result

    # ------------------------------------------------------------------ #
    # Сеть
    # ------------------------------------------------------------------ #
    def _get_json(self, url: str) -> Any:
        response = self._session.get(url, timeout=self.timeout)
        status = getattr(response, "status_code", 200)
        if status != 200:
            raise DotaApiError(f"HTTP {status} от {url}")
        return response.json()

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # История и анализ сыгранных матчей
    # ------------------------------------------------------------------ #
    def recent_matches(self, account_id: int | str, limit: int = 5) -> list[dict]:
        """Возвращает последние сыгранные матчи игрока через OpenDota API."""
        try:
            acc_id = int(account_id)
        except (ValueError, TypeError):
            return []
        if acc_id <= 0:
            return []
        try:
            data = self._get_json(f"{OPENDOTA}/players/{acc_id}/recentMatches")
            if isinstance(data, list):
                return data[:max(1, limit)]
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Не удалось получить историю матчей: {exc}"
            log.warning(self.last_error)
        return []

    def match_details(self, match_id: int | str) -> dict | None:
        """Возвращает детальную информацию о конкретном матче."""
        try:
            m_id = int(match_id)
        except (ValueError, TypeError):
            return None
        if m_id <= 0:
            return None
        try:
            data = self._get_json(f"{OPENDOTA}/matches/{m_id}")
            if isinstance(data, dict) and "players" in data:
                return data
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Не удалось получить детали матча #{match_id}: {exc}"
            log.warning(self.last_error)
        return None

    def parse_match_analysis(
        self,
        match_data: dict,
        account_id: int | None = None,
        hero_name: str | None = None,
    ) -> dict[str, Any]:
        """Превращает сырые данные матча OpenDota в структурированную сводку для разбора."""
        players = match_data.get("players", [])
        if not isinstance(players, list) or not players:
            return {}

        target_player: dict | None = None
        if account_id is not None:
            try:
                acc = int(account_id)
                for p in players:
                    if p.get("account_id") == acc:
                        target_player = p
                        break
            except (ValueError, TypeError):
                pass

        if target_player is None and hero_name:
            target_norm = hero_name.strip().lower()
            for p in players:
                h = self.hero_by_id(p.get("hero_id"))
                if h and (
                    target_norm in (h.get("localized_name") or "").lower()
                    or target_norm in (h.get("name") or "").lower()
                ):
                    target_player = p
                    break

        if target_player is None:
            target_player = players[0]

        hero_id = target_player.get("hero_id")
        hero = self.hero_by_id(hero_id)
        hero_display_name = self.hero_display(hero)

        slot = target_player.get("player_slot", 0)
        is_radiant = slot < 128
        radiant_win = bool(match_data.get("radiant_win"))
        is_win = is_radiant == radiant_win

        duration_sec = int(match_data.get("duration") or 0)
        minutes = duration_sec // 60
        seconds = duration_sec % 60
        duration_str = f"{minutes}:{seconds:02d}"

        kills = int(target_player.get("kills") or 0)
        deaths = int(target_player.get("deaths") or 0)
        assists = int(target_player.get("assists") or 0)
        kda_ratio = (kills + assists) / max(1, deaths)

        gpm = int(target_player.get("gold_per_min") or 0)
        xpm = int(target_player.get("xp_per_min") or 0)
        net_worth = int(target_player.get("net_worth") or target_player.get("total_gold") or 0)
        last_hits = int(target_player.get("last_hits") or 0)
        denies = int(target_player.get("denies") or 0)
        hero_damage = int(target_player.get("hero_damage") or 0)
        tower_damage = int(target_player.get("tower_damage") or 0)
        hero_healing = int(target_player.get("hero_healing") or 0)

        # Предметы
        id_names = self.item_id_names()
        final_items: list[str] = []
        for i in range(6):
            item_id = target_player.get(f"item_{i}")
            if item_id and int(item_id) > 0:
                name = id_names.get(int(item_id)) or self.item_display(str(item_id))
                final_items.append(name)

        backpack_items: list[str] = []
        for i in range(3):
            item_id = target_player.get(f"backpack_{i}")
            if item_id and int(item_id) > 0:
                name = id_names.get(int(item_id)) or self.item_display(str(item_id))
                backpack_items.append(name)

        neutral_id = target_player.get("item_neutral")
        neutral_item = None
        if neutral_id and int(neutral_id) > 0:
            neutral_item = id_names.get(int(neutral_id)) or self.item_display(str(neutral_id))

        # Бенчмарки (перцентили среди игроков на том же герое)
        bm_raw = target_player.get("benchmarks") or {}
        benchmarks = {
            "gpm_pct": round((bm_raw.get("gold_per_min") or {}).get("pct", 0) * 100),
            "xpm_pct": round((bm_raw.get("xp_per_min") or {}).get("pct", 0) * 100),
            "hero_damage_pct": round((bm_raw.get("hero_damage_per_min") or {}).get("pct", 0) * 100),
            "kills_pct": round((bm_raw.get("kills_per_min") or {}).get("pct", 0) * 100),
            "deaths_pct": round((bm_raw.get("deaths_per_min") or {}).get("pct", 0) * 100),
            "tower_damage_pct": round((bm_raw.get("tower_damage") or {}).get("pct", 0) * 100),
            "last_hits_pct": round((bm_raw.get("last_hits_per_min") or {}).get("pct", 0) * 100),
        }

        # Ключевые покупки по времени (отсекаем базовые расходники)
        _CONSUMABLES = {
            "tango", "clarity", "flask", "ward_observer", "ward_sentry", "dust",
            "smoke_of_deceit", "bottle", "branches", "faerie_fire", "enchanted_mango",
            "tpscroll", "magic_stick",
        }
        purchase_log = target_player.get("purchase_log", []) or []
        key_timings: list[str] = []
        for entry in purchase_log:
            key = entry.get("key")
            t = entry.get("time")
            if key and t is not None and key not in _CONSUMABLES:
                m = max(0, int(t) // 60)
                item_display = self.item_display(key)
                key_timings.append(f"{item_display} ({m} мин)")

        # Союзники и враги
        allies: list[str] = []
        enemies: list[str] = []
        for p in players:
            h = self.hero_by_id(p.get("hero_id"))
            h_name = self.hero_display(h)
            p_radiant = p.get("player_slot", 0) < 128
            if p_radiant == is_radiant:
                if p is not target_player:
                    allies.append(h_name)
            else:
                enemies.append(h_name)

        return {
            "match_id": match_data.get("match_id"),
            "account_id": target_player.get("account_id"),
            "hero_id": hero_id,
            "hero_name": hero_display_name,
            "side": "Силы Света (Radiant)" if is_radiant else "Силы Тьмы (Dire)",
            "is_win": is_win,
            "result_str": "Победа" if is_win else "Поражение",
            "duration_str": duration_str,
            "duration_sec": duration_sec,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "kda_ratio": kda_ratio,
            "gpm": gpm,
            "xpm": xpm,
            "net_worth": net_worth,
            "last_hits": last_hits,
            "denies": denies,
            "hero_damage": hero_damage,
            "tower_damage": tower_damage,
            "hero_healing": hero_healing,
            "items": final_items,
            "backpack": backpack_items,
            "neutral_item": neutral_item,
            "benchmarks": benchmarks,
            "key_timings": key_timings[:8],
            "allies": allies,
            "enemies": enemies,
            "radiant_score": match_data.get("radiant_score", 0),
            "dire_score": match_data.get("dire_score", 0),
        }

    def online(self) -> bool:
        """Быстрая проверка доступности OpenDota (для doctor)."""
        try:
            response = self._session.get(f"{OPENDOTA}/heroStats", timeout=8)
            return getattr(response, "status_code", 0) == 200
        except Exception:  # noqa: BLE001
            return False
