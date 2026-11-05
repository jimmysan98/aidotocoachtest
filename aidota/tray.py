"""Системный трей для AIDOTA на базе pystray и Pillow."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image

from .config import PROJECT_ROOT

log = logging.getLogger("aidota.tray")


class AidotaTray:
    """Управление иконкой AIDOTA в системном трее Windows."""

    def __init__(
        self,
        *,
        on_show: Callable[[], None] | None = None,
        on_toggle_overlay: Callable[[], None] | None = None,
        get_coach: Callable[[], str] | None = None,
        set_coach: Callable[[str], None] | None = None,
        get_position: Callable[[], int] | None = None,
        set_position: Callable[[int], None] | None = None,
        get_autonomy: Callable[[], str] | None = None,
        set_autonomy: Callable[[str], None] | None = None,
        get_mic_enabled: Callable[[], bool] | None = None,
        toggle_mic: Callable[[], None] | None = None,
        on_open_settings: Callable[[], None] | None = None,
        on_open_doctor: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        icon_path: Path | str | None = None,
    ):
        self._on_show = on_show
        self._on_toggle_overlay = on_toggle_overlay
        self._get_coach = get_coach or (lambda: "Астра")
        self._set_coach = set_coach
        self._get_position = get_position or (lambda: 1)
        self._set_position = set_position
        self._get_autonomy = get_autonomy or (lambda: "medium")
        self._set_autonomy = set_autonomy
        self._get_mic_enabled = get_mic_enabled or (lambda: True)
        self._toggle_mic = toggle_mic
        self._on_open_settings = on_open_settings
        self._on_open_doctor = on_open_doctor
        self._on_exit = on_exit

        self._icon_path = Path(icon_path) if icon_path else (PROJECT_ROOT / "assets" / "icon.png")
        self._icon: pystray.Icon | None = None
        self._lock = threading.Lock()

    def _load_image(self) -> Image.Image:
        try:
            if self._icon_path.is_file():
                return Image.open(self._icon_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось загрузить иконку из %s: %s", self._icon_path, exc)

        # Fallback: генерация сине-красной иконки AIDOTA
        img = Image.new("RGBA", (64, 64), (17, 22, 28, 255))
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        draw.ellipse([8, 8, 56, 56], fill=(194, 60, 42, 255), outline=(89, 208, 107, 255), width=2)
        return img

    def _build_menu(self) -> pystray.Menu:
        from pystray import Menu, MenuItem as item

        def _action_show(icon, item) -> None:
            if self._on_show:
                self._on_show()

        def _action_overlay(icon, item) -> None:
            if self._on_toggle_overlay:
                self._on_toggle_overlay()

        def _action_coach(name: str):
            def handler(icon, item) -> None:
                if self._set_coach:
                    self._set_coach(name)
                self.update_menu()
            return handler

        def _is_coach(name: str):
            return lambda item: (self._get_coach().strip().lower() == name.strip().lower())

        def _action_pos(pos: int):
            def handler(icon, item) -> None:
                if self._set_position:
                    self._set_position(pos)
                self.update_menu()
            return handler

        def _is_pos(pos: int):
            return lambda item: (self._get_position() == pos)

        def _action_mic(icon, item) -> None:
            if self._toggle_mic:
                self._toggle_mic()
            self.update_menu()

        def _is_mic_enabled(item):
            return self._get_mic_enabled()

        def _action_settings(icon, item) -> None:
            if self._on_open_settings:
                self._on_open_settings()

        def _action_doctor(icon, item) -> None:
            if self._on_open_doctor:
                self._on_open_doctor()

        def _action_exit(icon, item) -> None:
            self.stop()
            if self._on_exit:
                self._on_exit()

        coach_submenu = Menu(
            item("Астра (женский голос)", _action_coach("Астра"), checked=_is_coach("Астра"), radio=True),
            item("Джарвис (мужской голос)", _action_coach("Джарвис"), checked=_is_coach("Джарвис"), radio=True),
        )

        pos_submenu = Menu(
            item("1 — Керри (Safe Lane)", _action_pos(1), checked=_is_pos(1), radio=True),
            item("2 — Мид (Mid Lane)", _action_pos(2), checked=_is_pos(2), radio=True),
            item("3 — Оффлейн (Hard Lane)", _action_pos(3), checked=_is_pos(3), radio=True),
            item("4 — Поддержка (Semi-Support)", _action_pos(4), checked=_is_pos(4), radio=True),
            item("5 — Полная поддержка (Hard Support)", _action_pos(5), checked=_is_pos(5), radio=True),
        )

        def _action_autonomy(lvl: str):
            def handler(icon, item) -> None:
                if self._set_autonomy:
                    self._set_autonomy(lvl)
                self.update_menu()
            return handler

        def _is_autonomy(lvl: str):
            return lambda item: (self._get_autonomy().strip().lower() == lvl.strip().lower())

        autonomy_submenu = Menu(
            item("Высокая (советы, паверспайки, макро)", _action_autonomy("high"), checked=_is_autonomy("high"), radio=True),
            item("Средняя (тайминги и кулдауны)", _action_autonomy("medium"), checked=_is_autonomy("medium"), radio=True),
            item("Только по запросу", _action_autonomy("off"), checked=_is_autonomy("off"), radio=True),
        )

        return Menu(
            item("Открыть AIDOTA", _action_show, default=True),
            item("Показать / Скрыть игровой оверлей", _action_overlay),
            Menu.SEPARATOR,
            item("Тренер", coach_submenu),
            item("Моя позиция", pos_submenu),
            item("Автономность тренера", autonomy_submenu),
            item("Микрофон", _action_mic, checked=_is_mic_enabled),
            Menu.SEPARATOR,
            item("⚙ Настройки", _action_settings),
            item("🩺 Диагностика", _action_doctor),
            Menu.SEPARATOR,
            item("Полный выход", _action_exit),
        )

    def start(self) -> bool:
        """Запускает иконку в трее в фоновом потоке."""
        with self._lock:
            if self._icon is not None:
                return True
            try:
                image = self._load_image()
                menu = self._build_menu()
                self._icon = pystray.Icon(
                    name="AIDOTA",
                    icon=image,
                    title="AIDOTA — Голосовой помощник",
                    menu=menu,
                )
                self._icon.run_detached()
                log.info("Системный трей запущен")
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось запустить иконку в трее: %s", exc)
                self._icon = None
                return False

    def stop(self) -> None:
        """Останавливает и удаляет иконку из трея."""
        with self._lock:
            if self._icon is not None:
                try:
                    self._icon.stop()
                except Exception as exc:  # noqa: BLE001
                    log.debug("Ошибка при остановке трея: %s", exc)
                finally:
                    self._icon = None

    def update_menu(self) -> None:
        """Обновляет состояние пунктов меню (отметки радио/чекбоксов)."""
        with self._lock:
            if self._icon is not None:
                try:
                    self._icon.menu = self._build_menu()
                    self._icon.update_menu()
                except Exception as exc:  # noqa: BLE001
                    log.debug("Не удалось обновить меню трея: %s", exc)

    def notify_minimized(
        self,
        title: str = "AIDOTA",
        message: str = "AIDOTA свёрнута в системный трей и продолжает работу",
    ) -> None:
        """Показывает всплывающее уведомление Windows о сворачивании в трей."""
        with self._lock:
            if self._icon is not None:
                try:
                    self._icon.notify(message, title)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Не удалось отправить уведомление в трей: %s", exc)
