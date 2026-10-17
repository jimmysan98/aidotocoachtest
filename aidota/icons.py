"""Скачивание иконок героев с CDN Steam (для определения героя под курсором)."""

from __future__ import annotations

import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .dota_api import DotaApi, USER_AGENT

log = logging.getLogger("aidota.icons")

CDN = "https://cdn.cloudflare.steamstatic.com"


def count_icons(icons_dir: Path) -> int:
    if not icons_dir.is_dir():
        return 0
    return len([p for p in icons_dir.glob("*.png") if p.stat().st_size > 0])


def _download(session, url: str, path: Path) -> bool:
    try:
        if path.is_file() and path.stat().st_size > 0:
            return False
        response = session.get(url, timeout=30)
        if response.status_code != 200 or not response.content:
            log.debug("Иконка не скачалась: %s (HTTP %s)", url, response.status_code)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".part", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(response.content)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001 — одна иконка не повод падать
        log.debug("Ошибка скачивания %s: %s", url, exc)
        return False


def fetch_hero_icons(
    api: DotaApi,
    icons_dir: Path,
    *,
    progress: Callable[[str], None] | None = None,
    workers: int = 8,
) -> tuple[int, int]:
    """Докачивает недостающие портреты и маленькие иконки. Возвращает (скачано, всего)."""
    if not api.heroes:
        api.load_snapshots()
    heroes = api.heroes
    if not heroes:
        raise RuntimeError("Нет списка героев: сначала загрузите data/heroes.json")

    import requests

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    icons_dir = Path(icons_dir)
    tasks: list[tuple[str, Path]] = []
    for hero in heroes:
        slug = str(hero.get("name", "")).replace("npc_dota_hero_", "")
        if not slug:
            continue
        img = str(hero.get("img") or "").split("?")[0]
        icon = str(hero.get("icon") or "").split("?")[0]
        if img.startswith("/apps/"):
            tasks.append((CDN + img, icons_dir / f"{slug}.png"))
        if icon.startswith("/apps/"):
            tasks.append((CDN + icon, icons_dir / "icons" / f"{slug}.png"))

    if progress:
        progress(f"Проверяю иконки героев в {icons_dir}…")
    downloaded = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_download, session, url, path) for url, path in tasks]
        for future in futures:
            if future.result():
                downloaded += 1
    if progress:
        progress(f"Иконки готовы: скачано {downloaded}, портретов {count_icons(icons_dir)}")
    return downloaded, len(tasks)
