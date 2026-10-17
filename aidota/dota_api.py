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
            str(key): {"dname": str(value.get("dname") or key), "id": value.get("id")}
            for key, value in items_raw.items()
            if isinstance(value, dict)
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.data_dir / "heroes.json", heroes)
        self._write_json(self.data_dir / "items.json", items)
        with self._lock:
            self._heroes = heroes
            self._items, self._item_ids = self._parse_items(items)
            self.snapshot_time = time.time()
        log.info("Снапшоты обновлены: %d героев, %d предметов", len(heroes), len(items))
        return True

    @staticmethod
    def _parse_items(raw: dict) -> tuple[dict[str, str], dict[int, str]]:
        """Поддерживает и новый формат {name: {dname,id}}, и старый {name: dname}."""
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

    def online(self) -> bool:
        """Быстрая проверка доступности OpenDota (для doctor)."""
        try:
            response = self._session.get(f"{OPENDOTA}/heroStats", timeout=8)
            return getattr(response, "status_code", 0) == 200
        except Exception:  # noqa: BLE001
            return False
