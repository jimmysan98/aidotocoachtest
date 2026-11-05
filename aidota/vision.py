"""Определение героя под курсором по иконкам из hero_icons/.

Работает только в оконном/безрамочном режиме Dota 2: mss не может
снимать эксклюзивный полноэкранный режим.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Any

from .config import VisionConfig
from .heroes import HeroCatalog

log = logging.getLogger("aidota.vision")


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_position() -> tuple[int, int]:
    try:
        point = _Point()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except Exception:  # noqa: BLE001 — не Windows / нет user32
        pass
    return 0, 0


def count_icon_files(icons_dir: Path) -> int:
    if not icons_dir.is_dir():
        return 0
    return len([p for p in icons_dir.glob("*.png") if p.stat().st_size > 0])


class HeroVision:
    def __init__(
        self,
        config: VisionConfig,
        project_root: Path,
        catalog: HeroCatalog | None = None,
    ):
        self.config = config
        self.project_root = Path(project_root)
        self.catalog = catalog
        self.icons_dir = self._resolve_dir(config.icons_dir)
        self._templates: list[tuple[str, Any, str]] = []  # (имя, gray, kind)
        self._loaded = False
        self._error: str | None = None
        self.last_frame_mean: float | None = None

    def _resolve_dir(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self._loaded and bool(self._templates)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def template_count(self) -> int:
        return len(self._templates)

    # ------------------------------------------------------------------ #
    def load(self) -> bool:
        if self._loaded:
            return self.available
        self._loaded = True
        if not self.config.enabled:
            self._error = "Определение героя под курсором выключено в config.json"
            return False
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            self._error = f"OpenCV недоступен: {exc}"
            log.warning(self._error)
            return False

        portrait_files = sorted(self.icons_dir.glob("*.png")) if self.icons_dir.is_dir() else []
        icon_files = (
            sorted((self.icons_dir / "icons").glob("*.png"))
            if (self.icons_dir / "icons").is_dir()
            else []
        )
        for path, kind in [(p, "portrait") for p in portrait_files] + [
            (p, "icon") for p in icon_files
        ]:
            gray = None
            try:
                # cv2.imread не понимает не-ASCII пути (а проект может лежать
                # в «Документах»), поэтому читаем байты сами.
                import numpy as np

                buffer = np.frombuffer(path.read_bytes(), dtype=np.uint8)
                gray = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
            except Exception:  # noqa: BLE001
                gray = None
            if gray is None or gray.size == 0:
                continue
            name = self._display_name(path.stem)
            self._templates.append((name, gray, kind))
        if not self._templates:
            self._error = (
                f"Иконки героев не найдены в {self.icons_dir}. "
                "Запустите python main.py --fetch-icons."
            )
            log.warning(self._error)
            return False
        log.info("Загружено шаблонов героев: %d", len(self._templates))
        return True

    def _display_name(self, slug: str) -> str:
        if self.catalog is not None:
            hero = self.catalog.by_npc(f"npc_dota_hero_{slug}")
            if hero:
                return hero.get("localized_name") or slug
        return slug.replace("_", " ").title()

    # ------------------------------------------------------------------ #
    def _get_sct(self):
        if not hasattr(self, "_sct") or self._sct is None:
            import mss

            self._sct = mss.mss()
        return self._sct

    def _grab(self, left: int, top: int, width: int, height: int):
        sct = self._get_sct()
        try:
            monitor = sct.monitors[1]
            left = max(0, min(left, monitor["width"] - width - 1))
            top = max(0, min(top, monitor["height"] - height - 1))
            region = {"left": left, "top": top, "width": width, "height": height}
            return sct.grab(region)
        except Exception:
            # Recreate on error (e.g. resolution change)
            import mss

            self._sct = mss.mss()
            monitor = self._sct.monitors[1]
            left = max(0, min(left, monitor["width"] - width - 1))
            top = max(0, min(top, monitor["height"] - height - 1))
            region = {"left": left, "top": top, "width": width, "height": height}
            return self._sct.grab(region)

    def detect_at(self, x: int, y: int) -> dict[str, Any] | None:
        if not self.load():
            return None
        try:
            import cv2
            import numpy as np
        except Exception:  # noqa: BLE001
            return None

        radius = max(30, int(self.config.search_radius))
        try:
            shot = self._grab(x - radius, y - radius, radius * 2, radius * 2)
        except Exception as exc:  # noqa: BLE001 — mss может не работать в RDP/службе
            self._error = f"Не удалось снять экран: {exc}"
            return None
        frame = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)
        self.last_frame_mean = float(gray.mean())
        if self.last_frame_mean < 3.0:
            self._error = (
                "Снимок экрана почти чёрный. Если Dota 2 запущена в полноэкранном режиме, "
                "переключите её в «Оконный без рамки» (Borderless Windowed)."
            )
            return None
        self._error = None

        best_name: str | None = None
        best_score = float(self.config.threshold)
        # Маленькие иконки проверяем первыми: это в разы быстрее портретов,
        # а при уверенном совпадении дальше можно не искать.
        ordered = sorted(self._templates, key=lambda item: item[2] != "icon")
        for name, template, kind in ordered:
            if best_score >= 0.88:
                break
            for width in self._target_widths(template.shape[1], kind):
                if width > gray.shape[1]:
                    continue
                height = max(1, int(round(template.shape[0] * width / template.shape[1])))
                if height > gray.shape[0]:
                    continue
                resized = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
                result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > best_score:
                    best_score, best_name = float(max_val), name
        if best_name is None:
            return None
        return {"name": best_name, "score": round(best_score, 3)}

    @staticmethod
    def _target_widths(native_width: int, kind: str) -> list[int]:
        if kind == "portrait" or native_width >= 128:
            return [64, 96, 128, 160, 200, 240]
        return [24, 32, 40, 48, 56, 64, 80]

    def hero_under_cursor(self) -> dict[str, Any] | None:
        x, y = cursor_position()
        return self.detect_at(x, y)

    def describe(self) -> str:
        if self.available:
            return f"Определение героя: {self.template_count} шаблонов"
        return self._error or "Определение героя недоступно"
