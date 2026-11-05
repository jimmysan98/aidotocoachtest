"""Иконки героев и предметов: CDN Steam, локальный кэш и data URI для веб-UI."""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .dota_api import DotaApi, USER_AGENT

log = logging.getLogger("aidota.icons")

CDN = "https://cdn.cloudflare.steamstatic.com"
ITEM_CDN_PATH = "/apps/dota2/images/dota_react/items"


def count_icons(icons_dir: Path) -> int:
    if not icons_dir.is_dir():
        return 0
    return len([p for p in icons_dir.glob("*.png") if p.stat().st_size > 0])


def create_session() -> Any | None:
    """Сессия requests с User-Agent AIDOTA (None, если requests недоступен)."""
    try:
        import requests

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        return session
    except Exception:  # noqa: BLE001 — без requests работаем без скачивания
        return None


def read_image_data_uri(path: Path | str) -> str:
    """PNG-файл → ``data:image/png;base64,...``; пустая строка при любой ошибке."""
    try:
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            return ""
        data = file_path.read_bytes()
        if not data:
            return ""
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:  # noqa: BLE001 — битая иконка не повод падать
        log.debug("Не удалось прочитать иконку %s: %s", path, exc)
        return ""


def download_image(session: Any, url: str, path: Path | str, *, timeout: float = 4.0) -> bool:
    """Атомарно скачивает картинку в ``path``. Никогда не бросает исключение.

    Возвращает ``True``, если файл на месте (уже был или успешно скачан).
    """
    try:
        if session is None:
            return False
        file_path = Path(path)
        if file_path.is_file() and file_path.stat().st_size > 0:
            return True
        response = session.get(url, timeout=timeout)
        status = getattr(response, "status_code", 200)
        content = getattr(response, "content", b"") or b""
        if status != 200 or not content:
            log.debug("Иконка не скачалась: %s (HTTP %s)", url, status)
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=file_path.name, suffix=".part", dir=file_path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp_name, file_path)
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


def item_icon_url(slug: str) -> str:
    """CDN-URL иконки предмета по слагу (``blink`` → .../blink.png)."""
    clean = str(slug or "").strip()
    if not clean:
        return ""
    return f"{CDN}{ITEM_CDN_PATH}/{clean}.png"


def _download(session, url: str, path: Path) -> bool:
    try:
        if path.is_file() and path.stat().st_size > 0:
            return False
    except OSError:
        return False
    return download_image(session, url, path, timeout=30.0)


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
