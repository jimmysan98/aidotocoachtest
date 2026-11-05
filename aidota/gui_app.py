"""Главное графическое Windows-приложение (Лаунчер / Дашборд) AIDOTA на CustomTkinter.

Резервный (fallback) интерфейс на случай недоступности WebView2. Оформлен в единой
современной «космической» тёмной теме AIDOTA (палитра синхронизирована с веб-UI):
стеклянные карточки с мягкими границами, аккуратная типографика, логотип в шапке,
статус-пилюли с пульсацией, плавное появление карточек, анимированное «думает…»,
count-up чисел статистики и сегментированный выбор уровня игры.

Важно: любые изменения виджетов выполняются только из главного потока GUI
(фоновые потоки общаются через ``after(0, ...)``), см. .agent/skills/aidota-dev/SKILL.md.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import customtkinter as ctk

from . import __version__
from .app import Assistant
from .audio import list_audio_devices
from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, load_config, save_config
from .context import get_live_gsi_snapshot
from .doctor import download_vosk_model, get_checks
from .dota_install import (
    install_gsi_full,
    is_autostart_enabled,
    set_autostart,
)
from .hotkeys import format_key_name
from .settings_gui import (
    PROVIDER_PRESETS,
    attach_context_menu,
    enable_universal_clipboard,
    play_test_chime,
    test_llm_connection,
)
from .tray import AidotaTray

log = logging.getLogger("aidota.gui_app")

# --------------------------------------------------------------------------- #
# Дизайн-токены (синхронизированы с палитрой веб-UI aidota/web/style.css)
# --------------------------------------------------------------------------- #
FONT_UI = "Segoe UI"

COLOR_BG = "#0b0e14"               # Obsidian Canvas
COLOR_DEEP = "#07090e"             # Самый тёмный фон (поля ввода, чат)
COLOR_HEADER = "#0f1521"           # Header Slate
COLOR_CARD = "#121926"             # Primary Card Frame
COLOR_CARD_SECONDARY = "#151d2a"   # Secondary Card / Container
COLOR_CARD_HOVER = "#1b2435"       # Hover-подсветка карточек
COLOR_INPUT = "#0d1420"            # Поля ввода
COLOR_BORDER = "#26344a"           # Border Slate
COLOR_BORDER_LIGHT = "#3a4d6b"     # Highlight Border

COLOR_ACCENT_RED = "#ef4444"       # Dota Dire Red
COLOR_ACCENT_RED_HOVER = "#f87171"
COLOR_ACCENT_GREEN = "#10b981"     # Radiant Emerald
COLOR_ACCENT_GREEN_HOVER = "#34d399"

COLOR_ASTRA = "#a855f7"            # Astra Astral Violet
COLOR_ASTRA_HOVER = "#c084fc"
COLOR_ASTRA_BG = "#1e1233"         # Astra Glow Card
COLOR_ASTRA_TEXT = "#d8b4fe"       # Astra Accent Text
COLOR_JARVIS = "#f59e0b"           # Jarvis Cyber Gold
COLOR_JARVIS_HOVER = "#fbbf24"
COLOR_JARVIS_BG = "#2a1d08"        # Jarvis Glow Card
COLOR_JARVIS_TEXT = "#fcd34d"      # Jarvis Accent Text

COLOR_CYAN = "#38bdf8"             # Tactical HUD Cyan
COLOR_BLUE = "#60a5fa"
COLOR_GOLD = "#fbbf24"
COLOR_TEXT = "#f8fafc"             # Crisp Bright White
COLOR_TEXT_MUTED = "#94a3b8"       # Slate Muted Text
COLOR_TEXT_DIM = "#64748b"         # Dim Captions

RADIUS_CARD = 14
RADIUS_BUTTON = 10
RADIUS_PILL = 999

# Уровень игры: подписи (для UI) <-> строковые коды (config.player_profile.skill_level)
SKILL_LEVEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Новичок", "beginner"),
    ("Любитель", "amateur"),
    ("Опытный", "advanced"),
    ("Профи", "expert"),
)
SKILL_LEVEL_CODES: dict[str, str] = {label: code for label, code in SKILL_LEVEL_OPTIONS}
SKILL_LEVEL_LABELS: dict[str, str] = {code: label for label, code in SKILL_LEVEL_OPTIONS}
SKILL_LEVEL_HINTS: dict[str, str] = {
    "beginner": "Объясняет термины простыми словами, даёт одно конкретное действие и не перегружает.",
    "amateur": "Короткие тактические советы и разбор ошибок без лишней теории.",
    "advanced": "Глубокий разбор таймингов, контрпиков, экономики и позиционирования.",
    "expert": "Плотный разбор уровня высокого MMR: паверспайки, драфт и микро-решения.",
}
_SKILL_LEVEL_ALIASES: dict[str, str] = {
    "beginner": "beginner", "новичок": "beginner", "новичек": "beginner", "начинающий": "beginner",
    "noob": "beginner", "нуб": "beginner", "easy": "beginner",
    "amateur": "amateur", "любитель": "amateur", "средний": "amateur", "обычный": "amateur",
    "advanced": "advanced", "опытный": "advanced", "продвинутый": "advanced", "уверенный": "advanced",
    "expert": "expert", "профи": "expert", "про": "expert", "эксперт": "expert", "pro": "expert", "высокий": "expert",
}


def _font(size: int = 12, weight: str = "normal", family: str = FONT_UI) -> ctk.CTkFont:
    """Единый шрифт интерфейса (создаётся только после инициализации Tk-корня)."""
    return ctk.CTkFont(family=family, size=size, weight=weight)


def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    try:
        text = value.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            return None
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, AttributeError):
        return None


def _blend_hex(color_from: str, color_to: str, t: float) -> str:
    """Плавная интерполяция двух hex-цветов (для анимаций появления/пульса)."""
    t = max(0.0, min(1.0, float(t)))
    base = _hex_to_rgb(str(color_from))
    target = _hex_to_rgb(str(color_to))
    if base is None or target is None:
        return color_to if t >= 1.0 else color_from
    r, g, b = (round(base[i] + (target[i] - base[i]) * t) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def _normalize_skill_level(value: Any) -> str:
    """Приводит значение из конфига/UI к строковому коду уровня игры."""
    text = str(value or "").strip().lower()
    return _SKILL_LEVEL_ALIASES.get(text, "beginner")


def _load_logo_image(size: int = 38) -> ctk.CTkImage | None:
    """Загружает логотип AIDOTA из assets (logo.png -> logo-256.png -> icon.png).

    Возвращает None, если файлов нет или Pillow недоступен — тогда шапка
    показывает аккуратный текстовый логотип. Файлы assets только читаются.
    """
    candidates = ("logo.png", "logo-256.png", "icon.png")
    # Pillow без поддержки Tk (нет нативного PIL._imagingtk) не умеет CTkImage —
    # в этом случае аккуратно откатываемся на текстовый логотип.
    try:
        import PIL._imagingtk  # noqa: F401 — проверка доступности Tk-моста Pillow
    except Exception:
        return None
    for name in candidates:
        path = PROJECT_ROOT / "assets" / name
        if not path.is_file():
            continue
        try:
            from PIL import Image

            with Image.open(path) as src:
                image = src.convert("RGBA")
            return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
        except Exception as exc:  # noqa: BLE001 - логотип не должен ронять приложение
            log.warning("Не удалось загрузить логотип %s: %s", path, exc)
    return None


class AidotaApp(ctk.CTk):
    """Главный лаунчер-дашборд AIDOTA с системным треем."""

    def __init__(self, config: Config | None = None, config_path: Path | None = None):
        super().__init__()

        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config = config or load_config(self.config_path)

        # Настройка внешнего вида CustomTkinter
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(f"AIDOTA · ИИ-тренер по Dota 2 — v{__version__}")
        self.geometry("1000x740")
        self.minsize(900, 640)
        self.configure(fg_color=COLOR_BG)

        # Иконка окна
        icon_path = PROJECT_ROOT / "assets" / "icon.ico"
        if icon_path.is_file():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # Состояние приложения
        self.assistant: Assistant | None = None
        self._is_coach_running = False
        self._capturing_hotkey_target: str | None = None
        self._role_buttons: dict[int, ctk.CTkButton] = {}

        # --- Состояние анимаций и визуальных индикаторов (только главный поток) ---
        self._logo_image: ctk.CTkImage | None = None
        self._gsi_online = False
        self._mic_state = "loading"
        self._pulse_phase = 0
        self._pulse_after_id: str | None = None
        self._reveal_after_id: str | None = None
        self._reveal_payload: list[tuple[Any, str, str | None]] = []
        self._reveal_items: list[dict[str, Any]] = []
        self._settings_sections: list[dict[str, Any]] = []
        self._matches_sections: list[dict[str, Any]] = []
        self._doctor_sections: list[dict[str, Any]] = []
        self._revealed_tabs: set[str] = set()
        self._thinking_active = False
        self._thinking_phase = 0
        self._thinking_coach = "Астра"
        self._thinking_after_id: str | None = None
        self._thinking_timeout_id: str | None = None
        self._stats_target: dict[str, Any] = {}
        self._stats_gold_shown = 0
        self._stats_after_id: str | None = None

        # Инициализация системного трея
        self.tray = AidotaTray(
            on_show=lambda: self.after(0, self.restore_from_tray),
            on_toggle_overlay=self._tray_toggle_overlay,
            get_coach=lambda: self.config.player_profile.coach_name,
            set_coach=self._tray_set_coach,
            get_position=lambda: self.config.player_profile.default_position,
            set_position=self._tray_set_position,
            get_autonomy=lambda: getattr(self.config.player_profile, "autonomy_level", "medium"),
            set_autonomy=self._tray_set_autonomy,
            get_mic_enabled=self._tray_get_mic_enabled,
            toggle_mic=self._tray_toggle_mic,
            on_open_settings=lambda: self.after(0, lambda: self._select_tab("⚙ Настройки")),
            on_open_doctor=lambda: self.after(0, lambda: self._select_tab("🩺 Диагностика")),
            on_exit=lambda: self.after(0, self.on_full_exit),
            icon_path=PROJECT_ROOT / "assets" / "icon.png",
        )
        self.tray.start()

        # Построение интерфейса
        self._build_ui()
        enable_universal_clipboard(self)

        # Запуск ассистента в фоне (оверлей скрыт по умолчанию)
        self._start_assistant()

        # Обработка закрытия окна (сворачивание в трей)
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)

        # Периодическое обновление индикаторов
        self._schedule_status_poll()

        # Анимации: плавное появление карточек и пульс статус-индикаторов
        self.after(140, self._start_reveal_animation)
        self.after(500, self._pulse_status_loop)

    # ================================================================== #
    # Построение UI
    # ================================================================== #
    def _build_ui(self) -> None:
        # ------------------------------------------------------------------ #
        # 1. Современная шапка: логотип + бренд + статус-пилюли
        # ------------------------------------------------------------------ #
        top_bar = ctk.CTkFrame(
            self,
            fg_color=COLOR_HEADER,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        top_bar.pack(fill="x", padx=16, pady=(14, 10))

        brand_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
        brand_frame.pack(side="left", padx=(14, 0), pady=10)

        # Логотип: CTkImage из assets/logo.png (fallback: logo-256/icon.png -> текст)
        self._logo_image = _load_logo_image(38)
        if self._logo_image is not None:
            logo_widget = ctk.CTkLabel(brand_frame, text="", image=self._logo_image, width=38, height=38)
        else:
            logo_widget = ctk.CTkLabel(
                brand_frame,
                text="✦",
                width=38,
                height=38,
                font=_font(24, "bold"),
                text_color=COLOR_ASTRA_TEXT,
                fg_color=COLOR_ASTRA_BG,
                corner_radius=12,
            )
        logo_widget.pack(side="left", padx=(0, 12))

        title_box = ctk.CTkFrame(brand_frame, fg_color="transparent")
        title_box.pack(side="left")

        title_row = ctk.CTkFrame(title_box, fg_color="transparent")
        title_row.pack(anchor="w")
        ctk.CTkLabel(title_row, text="AIDOTA", font=_font(20, "bold"), text_color=COLOR_TEXT).pack(side="left")
        ctk.CTkLabel(title_row, text="·", font=_font(17, "bold"), text_color=COLOR_TEXT_DIM).pack(side="left", padx=7)
        ctk.CTkLabel(title_row, text="ИИ-тренер", font=_font(15, "bold"), text_color=COLOR_ASTRA_TEXT).pack(side="left")

        meta_row = ctk.CTkFrame(title_box, fg_color="transparent")
        meta_row.pack(anchor="w", pady=(3, 0))
        ctk.CTkLabel(
            meta_row,
            text=f" PRO v{__version__} ",
            font=_font(9, "bold"),
            text_color=COLOR_CYAN,
            fg_color="#0b2537",
            corner_radius=RADIUS_PILL,
            height=17,
        ).pack(side="left")
        ctk.CTkLabel(
            meta_row,
            text="голосовой наставник по Dota 2",
            font=_font(10),
            text_color=COLOR_TEXT_DIM,
        ).pack(side="left", padx=(8, 0))

        # Статус-пилюли подключения (пульсирующие точки)
        status_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        status_box.pack(side="right", padx=10, pady=10)

        pill_gsi = ctk.CTkFrame(
            status_box,
            fg_color=COLOR_CARD,
            corner_radius=RADIUS_PILL,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        pill_gsi.pack(side="right", padx=(8, 0))
        self.lbl_gsi_dot = ctk.CTkLabel(pill_gsi, text="●", width=16, font=_font(12), text_color=COLOR_ACCENT_RED)
        self.lbl_gsi_dot.pack(side="left", padx=(10, 2), pady=7)
        self.lbl_gsi_status = ctk.CTkLabel(
            pill_gsi,
            text="Dota 2: ожидание GSI",
            font=_font(11, "bold"),
            text_color=COLOR_TEXT_MUTED,
        )
        self.lbl_gsi_status.pack(side="left", padx=(0, 12), pady=7)

        pill_mic = ctk.CTkFrame(
            status_box,
            fg_color=COLOR_CARD,
            corner_radius=RADIUS_PILL,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        pill_mic.pack(side="right")
        self.lbl_mic_dot = ctk.CTkLabel(pill_mic, text="●", width=16, font=_font(12), text_color=COLOR_TEXT_DIM)
        self.lbl_mic_dot.pack(side="left", padx=(10, 2), pady=7)
        self.lbl_mic_status = ctk.CTkLabel(
            pill_mic,
            text="Микрофон: загрузка…",
            font=_font(11, "bold"),
            text_color=COLOR_TEXT_MUTED,
        )
        self.lbl_mic_status.pack(side="left", padx=(0, 12), pady=7)

        # ------------------------------------------------------------------ #
        # 2. Вкладки приложения
        # ------------------------------------------------------------------ #
        self.tabview = ctk.CTkTabview(
            self,
            fg_color=COLOR_BG,
            segmented_button_fg_color=COLOR_HEADER,
            segmented_button_selected_color=COLOR_ASTRA,
            segmented_button_selected_hover_color=COLOR_ASTRA_HOVER,
            segmented_button_unselected_color=COLOR_HEADER,
            segmented_button_unselected_hover_color=COLOR_CARD,
            segmented_button_font=_font(12, "bold"),
            text_color=COLOR_TEXT,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            command=self._on_tab_changed,
        )
        self.tabview.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        self.tab_main = self.tabview.add("🎮 Дашборд (Тренер)")
        self.tab_matches = self.tabview.add("📊 Разбор матчей")
        self.tab_settings = self.tabview.add("⚙ Настройки")
        self.tab_doctor = self.tabview.add("🩺 Диагностика")

        self._build_tab_main()
        self._build_tab_matches()
        self._build_tab_settings()
        self._build_tab_doctor()

        # Карточки дашборда плавно «проявляются» при старте (анимация по after)
        self._reveal_items = self._collect_reveal_items(
            (self.card_astra, 0),
            (self.card_jarvis, 2),
            (self.tactical_bar, 4),
            (self.chat_frame, 6),
        )

    def _collect_reveal_items(self, *widgets_with_delay: tuple[Any, int]) -> list[dict[str, Any]]:
        """Собирает описание виджетов для анимации появления (fg/border -> целевые)."""
        items: list[dict[str, Any]] = []
        for widget, delay in widgets_with_delay:
            try:
                fg = str(widget.cget("fg_color"))
                border = str(widget.cget("border_color"))
                border_width = int(widget.cget("border_width") or 0)
            except Exception:
                continue
            items.append(
                {
                    "widget": widget,
                    "fg": fg,
                    "border": border if border_width > 0 else None,
                    "delay": delay,
                }
            )
        return items

    # ------------------------------------------------------------------ #
    # Анимации: появление карточек, пульс статусов, «думает…»
    # ------------------------------------------------------------------ #
    def _start_reveal_animation(self) -> None:
        """Плавно проявляет карточки дашборда после старта приложения."""
        self._reveal_widgets(getattr(self, "_reveal_items", []), duration_ms=420)

    def _reveal_widgets(self, sections: list[dict[str, Any]], duration_ms: int = 360) -> None:
        """Анимирует появление карточек: цвет плавно переходит от фона к целевому."""
        if not sections:
            return
        steps = 14
        frame_ms = max(14, int(duration_ms / steps))
        for index, item in enumerate(sections):
            widget = item.get("widget")
            if widget is None:
                continue
            try:
                target_fg = str(item.get("fg") or widget.cget("fg_color"))
                target_border = item.get("border")
                start_fg = _blend_hex(COLOR_BG, target_fg, 0.0)
                start_border = (
                    _blend_hex(COLOR_BORDER, str(target_border), 0.0) if target_border else None
                )
                try:
                    widget.configure(fg_color=start_fg)
                except Exception:
                    pass
            except Exception:
                continue
            base_delay = min(240, int(item.get("delay", index * 2)) * 25)

            for step in range(1, steps + 1):
                def apply(
                    step: int = step,
                    widget: Any = widget,
                    start_fg: str = start_fg,
                    target_fg: str = target_fg,
                    target_border: Any = target_border,
                    start_border: Any = start_border,
                    steps: int = steps,
                ) -> None:
                    try:
                        t = step / steps
                        widget.configure(fg_color=_blend_hex(start_fg, target_fg, t))
                        if target_border:
                            widget.configure(
                                border_color=_blend_hex(str(start_border), str(target_border), t)
                            )
                    except Exception:
                        pass

                self.after(base_delay + step * frame_ms, apply)

    def _finish_reveal_animation(self) -> None:
        """Мгновенно доводит карточки до финальных цветов (если анимация ещё идёт)."""
        for item in getattr(self, "_reveal_items", []):
            widget = item.get("widget")
            if widget is None:
                continue
            try:
                widget.configure(fg_color=item.get("fg"))
                if item.get("border"):
                    widget.configure(border_color=item.get("border"))
            except Exception:
                continue

    def _pulse_status_loop(self) -> None:
        """Пульсирует точки статусов GSI и микрофона."""
        try:
            self._pulse_phase = (getattr(self, "_pulse_phase", 0) + 1) % 2
            bright = bool(self._pulse_phase)
            gsi_online = False
            if self.assistant is not None:
                try:
                    gsi_online = bool(self.assistant.is_gsi_connected())
                except Exception:
                    gsi_online = False
            if hasattr(self, "lbl_gsi_dot"):
                gsi_color = COLOR_ACCENT_GREEN if gsi_online else COLOR_ACCENT_RED
                self.lbl_gsi_dot.configure(
                    text_color=gsi_color if bright else _blend_hex(gsi_color, COLOR_BG, 0.55)
                )
            if hasattr(self, "lbl_mic_dot"):
                mic_active = bool(getattr(self, "_is_coach_running", False))
                mic_color = COLOR_ASTRA if mic_active else COLOR_TEXT_DIM
                self.lbl_mic_dot.configure(
                    text_color=mic_color if bright else _blend_hex(mic_color, COLOR_BG, 0.55)
                )
        except Exception:
            pass
        try:
            self._pulse_after_id = self.after(700, self._pulse_status_loop)
        except Exception:
            pass

    def _start_thinking(self) -> None:
        """Показывает анимированное «думает…» в шапке чата."""
        if not hasattr(self, "lbl_thinking"):
            return
        self._thinking_active = True
        self._thinking_phase = 0
        try:
            self._thinking_coach = str(self.config.player_profile.coach_name or "Астра")
        except Exception:
            self._thinking_coach = "Астра"
        self._tick_thinking()
        try:
            if getattr(self, "_thinking_timeout_id", None):
                self.after_cancel(self._thinking_timeout_id)
            self._thinking_timeout_id = self.after(20000, self._stop_thinking)
        except Exception:
            pass

    def _tick_thinking(self) -> None:
        if not getattr(self, "_thinking_active", False) or not hasattr(self, "lbl_thinking"):
            return
        dots = "." * (1 + (getattr(self, "_thinking_phase", 0) % 3))
        try:
            self.lbl_thinking.configure(
                text=f"⏳ {self._thinking_coach} думает{dots}",
                text_color=COLOR_ASTRA_TEXT,
            )
        except Exception:
            return
        self._thinking_phase = (getattr(self, "_thinking_phase", 0) + 1) % 3
        try:
            self._thinking_after_id = self.after(360, self._tick_thinking)
        except Exception:
            pass

    def _stop_thinking(self) -> None:
        self._thinking_active = False
        for attr in ("_thinking_after_id", "_thinking_timeout_id"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
            setattr(self, attr, None)
        if hasattr(self, "lbl_thinking"):
            try:
                self.lbl_thinking.configure(text="", text_color=COLOR_TEXT_DIM)
            except Exception:
                pass

    def _on_tab_changed(self, *args: Any) -> None:
        """Ленивая анимация появления секций при первом открытии вкладки."""
        try:
            current = self.tabview.get()
        except Exception:
            return
        if not current or current in self._revealed_tabs:
            return
        mapping: dict[str, list[dict[str, Any]]] = {
            "Разбор матчей": self._matches_sections,
            "Настройки": self._settings_sections,
            "Диагностика": self._doctor_sections,
        }
        for marker, sections in mapping.items():
            if marker in current:
                if sections:
                    self._revealed_tabs.add(current)
                    self.after(20, lambda s=sections: self._reveal_widgets(s))
                break

    def _select_tab(self, name: str) -> None:
        try:
            self.restore_from_tray()
            self.tabview.set(name)
            self._on_tab_changed()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Вкладка 1: Главная (Тренер и чат)
    # ------------------------------------------------------------------ #
    def _build_tab_main(self) -> None:
        parent = self.tab_main

        # 1. Карточки выбора тренера (Астра / Джарвис)
        coach_frame = ctk.CTkFrame(parent, fg_color="transparent")
        coach_frame.pack(fill="x", padx=12, pady=(10, 10))

        # --- Карточка 1: АСТРА ---
        self.card_astra = ctk.CTkFrame(
            coach_frame,
            fg_color=COLOR_ASTRA_BG,
            corner_radius=RADIUS_CARD,
            border_width=2,
            border_color=COLOR_ASTRA,
            cursor="hand2",
        )
        self.card_astra.pack(side="left", fill="both", expand=True, padx=(0, 7))

        astra_top = ctk.CTkFrame(self.card_astra, fg_color="transparent")
        astra_top.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(astra_top, text="✦ АСТРА", font=_font(15, "bold"), text_color=COLOR_ASTRA_TEXT).pack(side="left")
        self.badge_astra = ctk.CTkLabel(
            astra_top,
            text="",
            width=64,
            height=18,
            font=_font(9, "bold"),
            text_color=COLOR_TEXT,
            fg_color="transparent",
            corner_radius=RADIUS_PILL,
        )
        self.badge_astra.pack(side="right")

        ctk.CTkLabel(
            self.card_astra,
            text="Макро-стратег · голос Светлана",
            font=_font(10, "bold"),
            text_color=COLOR_TEXT_MUTED,
        ).pack(anchor="w", padx=14, pady=(2, 0))
        ctk.CTkLabel(
            self.card_astra,
            text="Спокойная поддержка: тайминги рун и Рошана, контроль тильта и позиционка.",
            font=_font(11),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=380,
        ).pack(anchor="w", padx=14, pady=(4, 10))

        # --- Карточка 2: ДЖАРВИС ---
        self.card_jarvis = ctk.CTkFrame(
            coach_frame,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            cursor="hand2",
        )
        self.card_jarvis.pack(side="right", fill="both", expand=True, padx=(7, 0))

        jarvis_top = ctk.CTkFrame(self.card_jarvis, fg_color="transparent")
        jarvis_top.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(jarvis_top, text="⚡ ДЖАРВИС", font=_font(15, "bold"), text_color=COLOR_JARVIS_TEXT).pack(side="left")
        self.badge_jarvis = ctk.CTkLabel(
            jarvis_top,
            text="",
            width=64,
            height=18,
            font=_font(9, "bold"),
            text_color="#1a1206",
            fg_color="transparent",
            corner_radius=RADIUS_PILL,
        )
        self.badge_jarvis.pack(side="right")

        ctk.CTkLabel(
            self.card_jarvis,
            text="Вычислитель вероятностей · голос Дмитрий",
            font=_font(10, "bold"),
            text_color=COLOR_TEXT_MUTED,
        ).pack(anchor="w", padx=14, pady=(2, 0))
        ctk.CTkLabel(
            self.card_jarvis,
            text="Холодный расчёт шансов, анализ ультимейтов врагов, BKB/Blink и фокус.",
            font=_font(11),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=380,
        ).pack(anchor="w", padx=14, pady=(4, 10))

        # Клик по любой точке карточки выбирает тренера, наведение подсвечивает границу
        self._bind_coach_card(self.card_astra, "Астра", COLOR_ASTRA_HOVER)
        self._bind_coach_card(self.card_jarvis, "Джарвис", COLOR_JARVIS_HOVER)

        # 2. Тактический HUD-бар: живой статус матча и быстрый выбор позиции
        self.tactical_bar = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        self.tactical_bar.pack(fill="x", padx=12, pady=(0, 10))

        status_col = ctk.CTkFrame(self.tactical_bar, fg_color="transparent")
        status_col.pack(side="left", fill="x", expand=True, padx=14, pady=9)
        ctk.CTkLabel(
            status_col,
            text="СТАТУС МАТЧА",
            font=_font(9, "bold"),
            text_color=COLOR_TEXT_DIM,
        ).pack(anchor="w")
        self.lbl_tactical_status = ctk.CTkLabel(
            status_col,
            text="Ожидание подключения к Dota 2 — запустите матч или лобби",
            font=_font(12, "bold"),
            text_color=COLOR_TEXT_MUTED,
            anchor="w",
        )
        self.lbl_tactical_status.pack(anchor="w", pady=(1, 0))

        role_box = ctk.CTkFrame(self.tactical_bar, fg_color="transparent")
        role_box.pack(side="right", padx=(0, 12), pady=8)
        ctk.CTkLabel(
            role_box,
            text="ПОЗИЦИЯ",
            font=_font(9, "bold"),
            text_color=COLOR_TEXT_DIM,
        ).pack(anchor="w", padx=2)
        role_row = ctk.CTkFrame(role_box, fg_color="transparent")
        role_row.pack(anchor="e", pady=(1, 0))

        roles_def = [
            (1, "1 Керри"),
            (2, "2 Мид"),
            (3, "3 Офф"),
            (4, "4 Семи"),
            (5, "5 Фулл"),
        ]
        curr_pos = max(1, min(5, int(self.config.player_profile.default_position or 1)))
        for pos_num, pos_lbl in roles_def:
            btn = ctk.CTkButton(
                role_row,
                text=pos_lbl,
                width=62,
                height=26,
                corner_radius=RADIUS_BUTTON,
                font=_font(10, "bold"),
                border_width=1,
                fg_color=COLOR_ACCENT_GREEN if pos_num == curr_pos else COLOR_CARD,
                hover_color=COLOR_ACCENT_GREEN_HOVER if pos_num == curr_pos else COLOR_CARD_HOVER,
                border_color=COLOR_ACCENT_GREEN if pos_num == curr_pos else COLOR_BORDER,
                command=lambda p=pos_num: self._select_position(p),
            )
            btn.pack(side="left", padx=2)
            self._role_buttons[pos_num] = btn

        # 3. Журнал тренера + живая статистика матча и индикатор «думает…»
        self.chat_frame = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        self.chat_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        chat_top = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        chat_top.pack(fill="x", padx=14, pady=(9, 5))
        ctk.CTkLabel(
            chat_top,
            text="ЖУРНАЛ ТРЕНЕРА",
            font=_font(10, "bold"),
            text_color=COLOR_TEXT_DIM,
        ).pack(side="left")

        self.lbl_thinking = ctk.CTkLabel(
            chat_top,
            text="",
            font=_font(10, "bold"),
            text_color=COLOR_ASTRA_TEXT,
        )
        self.lbl_thinking.pack(side="right")

        # Мини-статистика матча (золото анимируется count-up)
        self.lbl_chat_stats = ctk.CTkLabel(
            chat_top,
            text="KDA —   ·   ЗОЛОТО —   ·   ВРЕМЯ --:--",
            font=_font(10, "bold"),
            text_color=COLOR_TEXT_DIM,
        )
        self.lbl_chat_stats.pack(side="right", padx=(0, 14))

        # Текстовое поле чата с теговой подсветкой
        self.txt_log = ctk.CTkTextbox(
            self.chat_frame,
            fg_color=COLOR_DEEP,
            text_color=COLOR_TEXT,
            font=_font(11),
            wrap="word",
            border_width=1,
            border_color=COLOR_BORDER,
            corner_radius=RADIUS_BUTTON,
        )
        self.txt_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Настройка цветовых тегов для красивого чата
        try:
            self.txt_log._textbox.configure(spacing1=2, spacing3=4, padx=10, pady=8)
            self.txt_log._textbox.tag_config("tag_astra", foreground=COLOR_ASTRA_TEXT, font=("Segoe UI", 11, "bold"))
            self.txt_log._textbox.tag_config("tag_jarvis", foreground=COLOR_JARVIS_TEXT, font=("Segoe UI", 11, "bold"))
            self.txt_log._textbox.tag_config("tag_user", foreground=COLOR_CYAN, font=("Segoe UI", 11, "bold"))
            self.txt_log._textbox.tag_config("tag_err", foreground=COLOR_ACCENT_RED_HOVER, font=("Segoe UI", 11, "bold"))
            self.txt_log._textbox.tag_config("tag_sys", foreground=COLOR_TEXT_MUTED, font=("Segoe UI", 10, "bold"))
            self.txt_log._textbox.tag_config("tag_alert", foreground=COLOR_GOLD, font=("Segoe UI", 11, "bold"))
            self.txt_log._textbox.tag_config("tag_msg", foreground=COLOR_TEXT, font=("Segoe UI", 11))
            self.txt_log.configure(state="disabled")
        except Exception:
            pass

        # 4. Быстрые чипы частых вопросов
        chips_frame = ctk.CTkFrame(parent, fg_color="transparent")
        chips_frame.pack(fill="x", padx=12, pady=(0, 8))

        quick_queries = [
            ("Что купить?", "Что мне сейчас лучше купить по ситуации?"),
            ("Контрпик", "Кто сейчас контрит моего героя и кого мне фокусить?"),
            ("План на игру", "Какой общий тактический план на игру для моей роли?"),
            ("Тайминги карты", "Напомни ближайшие важные тайминги рун, стаков и Рошана"),
            ("Как камбэкнуть?", "Мы отстаем, как нам безопасно камбэкнуть игру?"),
        ]
        for chip_title, chip_query in quick_queries:
            chip_btn = ctk.CTkButton(
                chips_frame,
                text=chip_title,
                height=28,
                corner_radius=RADIUS_PILL,
                font=_font(10, "bold"),
                fg_color=COLOR_CARD,
                hover_color=COLOR_CARD_HOVER,
                border_width=1,
                border_color=COLOR_BORDER,
                text_color=COLOR_TEXT_MUTED,
                command=lambda q=chip_query: self._on_quick_question(q),
            )
            chip_btn.pack(side="left", padx=(0, 6))

        # 5. Панель ввода и кнопок управления
        action_frame = ctk.CTkFrame(parent, fg_color="transparent")
        action_frame.pack(fill="x", padx=12, pady=(0, 10))

        self.entry_query = ctk.CTkEntry(
            action_frame,
            placeholder_text="Задать вопрос тренеру текстом или голосом (напр. «что купить», «рошан упал»)…",
            font=_font(12),
            height=38,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_INPUT,
            border_width=1,
            border_color=COLOR_BORDER,
            placeholder_text_color=COLOR_TEXT_DIM,
        )
        self.entry_query.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_query.bind("<Return>", lambda e: self._on_send_query())

        btn_send = ctk.CTkButton(
            action_frame,
            text="Отправить",
            fg_color=COLOR_ASTRA,
            hover_color=COLOR_ASTRA_HOVER,
            text_color=COLOR_TEXT,
            font=_font(12, "bold"),
            corner_radius=RADIUS_BUTTON,
            width=100,
            height=38,
            command=self._on_send_query,
        )
        btn_send.pack(side="left", padx=(0, 6))

        # Кнопка оверлея
        self.btn_overlay = ctk.CTkButton(
            action_frame,
            text=f"Оверлей [{format_key_name(self.config.overlay.toggle_key)}]",
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(11, "bold"),
            corner_radius=RADIUS_BUTTON,
            width=120,
            height=38,
            command=self._on_toggle_overlay_click,
        )
        self.btn_overlay.pack(side="left", padx=(0, 6))

        # Кнопка паузы
        self.btn_toggle_coach = ctk.CTkButton(
            action_frame,
            text="Пауза",
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(11, "bold"),
            corner_radius=RADIUS_BUTTON,
            width=78,
            height=38,
            command=self._on_toggle_coach,
        )
        self.btn_toggle_coach.pack(side="left", padx=(0, 6))

        # Кнопка очистки
        btn_clear = ctk.CTkButton(
            action_frame,
            text="Очистить",
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(11, "bold"),
            corner_radius=RADIUS_BUTTON,
            width=78,
            height=38,
            command=self._clear_log,
        )
        btn_clear.pack(side="right")

        # Применяем начальное состояние карточек
        self._sync_coach_card_ui()

    def _bind_coach_card(self, card: Any, coach_name: str, hover_accent: str) -> None:
        """Клик по любой точке карточки выбирает тренера, hover подсвечивает границу."""

        def on_click(_event: Any = None) -> None:
            self._select_coach(coach_name)

        def on_enter(_event: Any = None) -> None:
            try:
                card.configure(border_color=hover_accent)
            except Exception:
                pass

        def on_leave(_event: Any = None) -> None:
            try:
                under = card.winfo_containing(*card.winfo_pointerxy())
            except Exception:
                under = None
            if under is None or not str(under).startswith(str(card)):
                self._sync_coach_card_ui()

        widgets: list[Any] = [card]
        stack = list(card.winfo_children())
        while stack:
            widget = stack.pop()
            widgets.append(widget)
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                continue

        for widget in widgets:
            try:
                widget.bind("<Button-1>", on_click, add="+")
                widget.bind("<Enter>", on_enter, add="+")
                widget.bind("<Leave>", on_leave, add="+")
            except Exception:
                continue

    def _select_coach(self, coach_name: str) -> None:
        self.config.player_profile.coach_name = coach_name
        save_config(self.config, self.config_path)
        self._sync_coach_card_ui()
        if self.assistant:
            self.assistant.set_coach_name(coach_name)
        self.tray.update_menu()
        self._log_append("Система", f"Активный тренер изменён на «{coach_name}». Обращайтесь: «{coach_name}, ...»")

    def _sync_coach_card_ui(self) -> None:
        # Пользователь мог переключить тренера во время анимации появления — сразу
        # доводим карточки до финального состояния.
        self._finish_reveal_animation()
        active = (self.config.player_profile.coach_name or "Астра").strip().lower()
        is_jarvis = "джарвис" in active
        try:
            self.card_astra.configure(
                fg_color=COLOR_CARD_SECONDARY if is_jarvis else COLOR_ASTRA_BG,
                border_color=COLOR_BORDER if is_jarvis else COLOR_ASTRA,
                border_width=1 if is_jarvis else 2,
            )
            self.card_jarvis.configure(
                fg_color=COLOR_JARVIS_BG if is_jarvis else COLOR_CARD_SECONDARY,
                border_color=COLOR_JARVIS if is_jarvis else COLOR_BORDER,
                border_width=2 if is_jarvis else 1,
            )
            self.badge_astra.configure(
                text="" if is_jarvis else "АКТИВЕН",
                fg_color="transparent" if is_jarvis else COLOR_ASTRA,
                text_color=COLOR_TEXT,
            )
            self.badge_jarvis.configure(
                text="АКТИВЕН" if is_jarvis else "",
                fg_color=COLOR_JARVIS if is_jarvis else "transparent",
                text_color="#1a1206",
            )
        except Exception:
            pass
        # Обновляем целевые цвета для анимации, если она ещё не проиграла
        for item in self._reveal_items:
            widget = item.get("widget")
            if widget is self.card_astra:
                item["fg"] = str(self.card_astra.cget("fg_color"))
                item["border"] = COLOR_ASTRA if not is_jarvis else COLOR_BORDER
            elif widget is self.card_jarvis:
                item["fg"] = str(self.card_jarvis.cget("fg_color"))
                item["border"] = COLOR_JARVIS if is_jarvis else COLOR_BORDER

    def _select_position(self, pos: int) -> None:
        self.config.player_profile.default_position = pos
        save_config(self.config, self.config_path)
        for p, btn in self._role_buttons.items():
            is_active = p == pos
            btn.configure(
                fg_color=COLOR_ACCENT_GREEN if is_active else COLOR_CARD,
                hover_color=COLOR_ACCENT_GREEN_HOVER if is_active else COLOR_CARD_HOVER,
                border_color=COLOR_ACCENT_GREEN if is_active else COLOR_BORDER,
            )
        if self.assistant:
            self.assistant.set_position(pos)
        self.tray.update_menu()
        role_names = {1: "Керри", 2: "Мид", 3: "Оффлейн", 4: "Частичная поддержка", 5: "Полная поддержка"}
        self._log_append("Система", f"Выбрана позиция: {pos} — {role_names.get(pos, 'Керри')}")

    def _on_quick_question(self, query: str) -> None:
        if self.assistant:
            self._start_thinking()
            self.assistant.submit_text_command(query)

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Вкладка 2: Разбор матчей
    # ------------------------------------------------------------------ #
    def _build_tab_matches(self) -> None:
        parent = self.tab_matches

        top_frame = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        top_frame.pack(fill="x", padx=12, pady=(10, 8))
        self._matches_sections += self._collect_reveal_items((top_frame, 0))

        ctk.CTkLabel(
            top_frame,
            text="Steam32 ID игрока",
            font=_font(11, "bold"),
            text_color=COLOR_TEXT_MUTED,
        ).pack(side="left", padx=(14, 6), pady=12)

        self.entry_account_id = ctk.CTkEntry(
            top_frame,
            placeholder_text="Например: 123456789",
            width=170,
            height=34,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_INPUT,
            border_width=1,
            border_color=COLOR_BORDER,
            placeholder_text_color=COLOR_TEXT_DIM,
            font=_font(12),
        )
        if self.config.player_profile.account_id:
            self.entry_account_id.insert(0, str(self.config.player_profile.account_id))
        self.entry_account_id.pack(side="left", padx=6, pady=12)
        attach_context_menu(self.entry_account_id)

        btn_save_id = ctk.CTkButton(
            top_frame,
            text="Сохранить ID",
            width=120,
            height=34,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(11, "bold"),
            command=self._save_account_id,
        )
        btn_save_id.pack(side="left", padx=6, pady=12)

        self.btn_analyze_match = ctk.CTkButton(
            top_frame,
            text="Разобрать последний матч",
            fg_color=COLOR_ASTRA,
            hover_color=COLOR_ASTRA_HOVER,
            text_color=COLOR_TEXT,
            font=_font(12, "bold"),
            corner_radius=RADIUS_BUTTON,
            height=34,
            command=self._on_analyze_match_click,
        )
        self.btn_analyze_match.pack(side="right", padx=(6, 14), pady=12)

        # Статус разбора матча
        self.lbl_match_status = ctk.CTkLabel(
            parent,
            text="",
            font=_font(11, "bold"),
            text_color=COLOR_BLUE,
        )
        self.lbl_match_status.pack(fill="x", padx=16, pady=(0, 6))

        # Карточка с отчётом
        report_card = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        report_card.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._matches_sections += self._collect_reveal_items((report_card, 2))

        self.txt_match_report = ctk.CTkTextbox(
            report_card,
            fg_color=COLOR_DEEP,
            text_color=COLOR_TEXT,
            font=_font(12),
            wrap="word",
            border_width=1,
            border_color=COLOR_BORDER,
            corner_radius=RADIUS_BUTTON,
        )
        self.txt_match_report.pack(fill="both", expand=True, padx=12, pady=12)
        self.txt_match_report.insert(
            "1.0",
            "Для автоматического анализа сыгранных матчей:\n\n"
            "1. Введите ваш Steam32 Account ID выше (или включите GSI в матче — ID определится сам);\n"
            "2. Убедитесь, что в клиенте Dota 2 включена настройка:\n"
            "   «Настройки → Сообщество → Общедоступная история матчей»;\n"
            "3. Нажмите кнопку «Разобрать последний матч».\n\n"
            "AIDOTA загрузит полную статистику игры с OpenDota и сформирует подробный разбор "
            "с анализом ошибок от выбранного ИИ-тренера.",
        )

    # ------------------------------------------------------------------ #
    # Вкладка 3: Настройки
    # ------------------------------------------------------------------ #
    # --- Переиспользуемые элементы современной «карточной» вёрстки --- #
    def _make_card(self, parent: Any, bucket: list[dict[str, Any]] | None = None) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        card.pack(fill="x", pady=7)
        if bucket is not None:
            bucket += self._collect_reveal_items((card, len(bucket)))
        return card

    def _card_header(self, card: Any, title: str, subtitle: str = "", accent: str = COLOR_ASTRA) -> ctk.CTkFrame:
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(13, 7))
        bar = ctk.CTkFrame(head, fg_color=accent, corner_radius=2, width=4, height=18)
        bar.pack(side="left", padx=(0, 10))
        bar.pack_propagate(False)
        box = ctk.CTkFrame(head, fg_color="transparent")
        box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(box, text=title, font=_font(13, "bold"), text_color=COLOR_TEXT, anchor="w").pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                box,
                text=subtitle,
                font=_font(10),
                text_color=COLOR_TEXT_DIM,
                anchor="w",
            ).pack(anchor="w", pady=(1, 0))
        return head

    def _settings_row(self, card: Any, pady: int = 4) -> ctk.CTkFrame:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=pady)
        return row

    def _settings_label(self, master: Any, text: str, width: int = 120) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master,
            text=text,
            width=width,
            anchor="w",
            font=_font(11),
            text_color=COLOR_TEXT_MUTED,
        )

    def _settings_entry(self, master: Any, placeholder: str = "") -> ctk.CTkEntry:
        return ctk.CTkEntry(
            master,
            height=34,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_INPUT,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            placeholder_text=placeholder,
            placeholder_text_color=COLOR_TEXT_DIM,
            font=_font(12),
        )

    def _settings_option(self, master: Any, values: list[str]) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            master,
            values=values,
            height=34,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_CARD,
            button_color=COLOR_BORDER,
            button_hover_color=COLOR_BORDER_LIGHT,
            dropdown_fg_color=COLOR_CARD,
            dropdown_hover_color=COLOR_CARD_HOVER,
            dropdown_text_color=COLOR_TEXT,
            text_color=COLOR_TEXT,
            font=_font(12),
            dropdown_font=_font(12),
            anchor="w",
        )

    def _secondary_button(self, master: Any, text: str, width: int, command: Any, height: int = 34) -> ctk.CTkButton:
        return ctk.CTkButton(
            master,
            text=text,
            width=width,
            height=height,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(11, "bold"),
            command=command,
        )

    def _primary_button(self, master: Any, text: str, command: Any, width: int = 140, height: int = 34, color: str = COLOR_ASTRA, hover: str = COLOR_ASTRA_HOVER) -> ctk.CTkButton:
        return ctk.CTkButton(
            master,
            text=text,
            width=width,
            height=height,
            corner_radius=RADIUS_BUTTON,
            fg_color=color,
            hover_color=hover,
            text_color=COLOR_TEXT,
            font=_font(11, "bold"),
            command=command,
        )

    # ------------------------------------------------------------------ #
    # Вкладка 3: Настройки
    # ------------------------------------------------------------------ #
    def _build_tab_settings(self) -> None:
        parent = self.tab_settings

        scroll = ctk.CTkScrollableFrame(
            parent,
            fg_color="transparent",
            scrollbar_button_color=COLOR_CARD,
            scrollbar_button_hover_color=COLOR_BORDER_LIGHT,
        )
        scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # 1. Провайдер LLM
        sec_llm = self._make_card(scroll, bucket=self._settings_sections)
        self._card_header(sec_llm, "Искусственный интеллект", "Провайдер, ключ API и модель тренера")

        row1 = self._settings_row(sec_llm)
        self._settings_label(row1, "Провайдер").pack(side="left")
        self.opt_provider = self._settings_option(row1, list(PROVIDER_PRESETS.keys()))
        self.opt_provider.configure(command=self._on_provider_preset_selected)
        self.opt_provider.pack(side="left", fill="x", expand=True)

        row2 = self._settings_row(sec_llm)
        self._settings_label(row2, "API-ключ").pack(side="left")
        self.entry_api_key = self._settings_entry(row2, "Вставьте ключ здесь…")
        self.entry_api_key.configure(show="•")
        self.entry_api_key.insert(0, self.config.llm.api_key or "")
        self.entry_api_key.pack(side="left", fill="x", expand=True, padx=(0, 6))
        attach_context_menu(self.entry_api_key)

        self.btn_paste_key = self._secondary_button(row2, "Вставить", 90, self._paste_api_key)
        self.btn_paste_key.pack(side="right", padx=(4, 0))

        self.btn_show_key = ctk.CTkButton(
            row2,
            text="👁",
            width=38,
            height=34,
            corner_radius=RADIUS_BUTTON,
            fg_color=COLOR_CARD,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT_MUTED,
            font=_font(13),
            command=self._toggle_show_key,
        )
        self.btn_show_key.pack(side="right", padx=(4, 0))

        row3 = self._settings_row(sec_llm)
        self._settings_label(row3, "Base URL").pack(side="left")
        self.entry_base_url = self._settings_entry(row3)
        self.entry_base_url.insert(0, self.config.llm.base_url or "")
        self.entry_base_url.pack(side="left", fill="x", expand=True)
        attach_context_menu(self.entry_base_url)

        row4 = self._settings_row(sec_llm)
        self._settings_label(row4, "Модель").pack(side="left")
        self.entry_model = self._settings_entry(row4)
        self.entry_model.insert(0, self.config.llm.model or "")
        self.entry_model.pack(side="left", fill="x", expand=True, padx=(0, 6))
        attach_context_menu(self.entry_model)

        self.btn_test_api = self._secondary_button(row4, "Проверить API", 130, self._on_test_api_click)
        self.btn_test_api.pack(side="right")

        self.lbl_api_test_res = ctk.CTkLabel(
            sec_llm,
            text="",
            font=_font(10, "bold"),
            text_color=COLOR_TEXT_MUTED,
            anchor="w",
        )
        self.lbl_api_test_res.pack(anchor="w", padx=16, pady=(0, 10))

        # 2. Звук и микрофон
        sec_audio = self._make_card(scroll, bucket=self._settings_sections)
        self._card_header(sec_audio, "Аудио и микрофон", "Устройства ввода/вывода и режим Push-to-Talk")

        # Микрофон
        row_mic = self._settings_row(sec_audio)
        self._settings_label(row_mic, "Микрофон").pack(side="left")
        input_devs = [f"{d['id']}: {d['name']}" for d in list_audio_devices(kind="input")] or ["По умолчанию"]
        self.opt_input_device = self._settings_option(row_mic, input_devs)
        curr_in = self.config.audio.input_device
        for dev_str in input_devs:
            if curr_in is not None and dev_str.startswith(f"{curr_in}:"):
                self.opt_input_device.set(dev_str)
                break
        self.opt_input_device.pack(side="left", fill="x", expand=True)

        # Динамики
        row_out = self._settings_row(sec_audio)
        self._settings_label(row_out, "Динамики").pack(side="left")
        output_devs = [f"{d['id']}: {d['name']}" for d in list_audio_devices(kind="output")] or ["По умолчанию"]
        self.opt_output_device = self._settings_option(row_out, output_devs)
        curr_out = getattr(self.config.tts, "output_device", None)
        for dev_str in output_devs:
            if curr_out is not None and dev_str.startswith(f"{curr_out}:"):
                self.opt_output_device.set(dev_str)
                break
        self.opt_output_device.pack(side="left", fill="x", expand=True, padx=(0, 6))

        btn_chime = self._secondary_button(row_out, "Тест звука", 110, self._on_test_chime_click)
        btn_chime.pack(side="right")

        # Режим Push-to-Talk
        row_ptt = self._settings_row(sec_audio)
        self._settings_label(row_ptt, "Режим ввода").pack(side="left")
        self.seg_ptt = ctk.CTkSegmentedButton(
            row_ptt,
            values=["Push-to-Talk", "Свободный микрофон"],
            height=32,
            corner_radius=RADIUS_BUTTON,
            font=_font(11, "bold"),
            fg_color=COLOR_CARD,
            selected_color=COLOR_ASTRA,
            selected_hover_color=COLOR_ASTRA_HOVER,
            unselected_color=COLOR_CARD,
            unselected_hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_TEXT,
            command=self._on_ptt_mode_changed,
        )
        self.seg_ptt.set("Push-to-Talk" if self.config.audio.push_to_talk else "Свободный микрофон")
        self.seg_ptt.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_set_ptt = self._secondary_button(
            row_ptt,
            f"PTT: [{format_key_name(self.config.audio.ptt_key)}]",
            110,
            lambda: self._start_hotkey_capture("ptt"),
        )
        self.btn_set_ptt.pack(side="right")
        ctk.CTkFrame(sec_audio, fg_color="transparent", height=8).pack()

        # 3. Горячие клавиши оверлея
        sec_hotkey = self._make_card(scroll, bucket=self._settings_sections)
        self._card_header(sec_hotkey, "Игровой оверлей", "Комбинация показа и скрытия поверх Dota 2")

        row_hk = self._settings_row(sec_hotkey)
        self._settings_label(row_hk, "Скрыть / показать", 150).pack(side="left")

        self.lbl_current_toggle = ctk.CTkLabel(
            row_hk,
            text=f"  {format_key_name(self.config.overlay.toggle_key)}  ",
            font=_font(12, "bold"),
            text_color=COLOR_ACCENT_GREEN_HOVER,
            fg_color="#06251c",
            corner_radius=RADIUS_BUTTON,
            height=26,
            width=92,
        )
        self.lbl_current_toggle.pack(side="left", padx=(0, 8))

        self.btn_set_overlay_key = self._secondary_button(
            row_hk,
            "Задать хоткей",
            130,
            lambda: self._start_hotkey_capture("overlay"),
        )
        self.btn_set_overlay_key.pack(side="left")

        ctk.CTkLabel(
            row_hk,
            text="Нажмите комбинацию, например Alt+Z или F11",
            font=_font(10),
            text_color=COLOR_TEXT_DIM,
        ).pack(side="left", padx=10)
        ctk.CTkFrame(sec_hotkey, fg_color="transparent", height=10).pack()

        # 4. Автономность тренера
        sec_autonomy = ctk.CTkFrame(scroll, fg_color=COLOR_CARD_SECONDARY, corner_radius=8, border_width=1, border_color=COLOR_BORDER)
        sec_autonomy.pack(fill="x", pady=6)

        ctk.CTkLabel(
            sec_autonomy,
            text="Автономный агент тренера",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLOR_ACCENT_RED,
        ).pack(anchor="w", padx=12, pady=(8, 4))

        row_aut = ctk.CTkFrame(sec_autonomy, fg_color="transparent")
        row_aut.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row_aut, text="Автономность:", width=110, anchor="w").pack(side="left")
        self.seg_autonomy = ctk.CTkSegmentedButton(
            row_aut,
            values=["Высокая", "Средняя", "Только по запросу"],
            command=self._on_autonomy_changed,
        )
        cur_lvl = getattr(self.config.player_profile, "autonomy_level", "medium").lower()
        lbl_map = {"high": "Высокая", "medium": "Средняя", "off": "Только по запросу"}
        self.seg_autonomy.set(lbl_map.get(cur_lvl, "Средняя"))
        self.seg_autonomy.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            sec_autonomy,
            text="• Высокая: советы, детекция покупки BKB/Blink/Orchid врагами, макро-цели.\n"
                 "• Средняя: тайминги водных и активных рун, 6-й уровень мидера, отводы и стаки.\n"
                 "• Только по запросу: тренер говорит только когда к нему обращаются.",
            font=ctk.CTkFont(size=11),
            text_color=COLOR_TEXT_MUTED,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(2, 8))

        # 5. Уровень игры (стиль тренера)
        sec_skill = ctk.CTkFrame(scroll, fg_color=COLOR_CARD_SECONDARY, corner_radius=8, border_width=1, border_color=COLOR_BORDER)
        sec_skill.pack(fill="x", pady=6)

        ctk.CTkLabel(
            sec_skill,
            text="Уровень игры и стиль тренера",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLOR_ACCENT_RED,
        ).pack(anchor="w", padx=12, pady=(8, 4))

        row_skill = ctk.CTkFrame(sec_skill, fg_color="transparent")
        row_skill.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row_skill, text="Уровень:", width=110, anchor="w").pack(side="left")
        self.seg_skill_level = ctk.CTkSegmentedButton(
            row_skill,
            values=[label for label, _code in SKILL_LEVEL_OPTIONS],
            font=ctk.CTkFont(size=11, weight="bold"),
            selected_color=COLOR_ASTRA,
            selected_hover_color=COLOR_ASTRA_HOVER,
            unselected_color=COLOR_CARD,
            unselected_hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_TEXT,
            command=self._on_skill_level_changed,
        )
        current_skill = _normalize_skill_level(
            getattr(self.config.player_profile, "skill_level", "beginner")
        )
        self.seg_skill_level.set(SKILL_LEVEL_LABELS.get(current_skill, "Новичок"))
        self.seg_skill_level.pack(side="left", fill="x", expand=True)

        self.lbl_skill_hint = ctk.CTkLabel(
            sec_skill,
            text=SKILL_LEVEL_HINTS.get(current_skill, ""),
            font=ctk.CTkFont(size=11),
            text_color=COLOR_TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.lbl_skill_hint.pack(anchor="w", padx=12, pady=(0, 8))

        # 6. Интеграция с Windows
        sec_win = ctk.CTkFrame(scroll, fg_color=COLOR_CARD_SECONDARY, corner_radius=8, border_width=1, border_color=COLOR_BORDER)
        sec_win.pack(fill="x", pady=6)

        ctk.CTkLabel(
            sec_win,
            text="Интеграция с Windows",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLOR_ACCENT_RED,
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.chk_autostart = ctk.CTkCheckBox(
            sec_win,
            text="Запускать AIDOTA вместе с Windows",
            command=self._on_autostart_toggled,
        )
        if is_autostart_enabled():
            self.chk_autostart.select()
        self.chk_autostart.pack(anchor="w", padx=12, pady=4)

        self.chk_minimize = ctk.CTkCheckBox(
            sec_win,
            text="Сворачивать в системный трей при закрытии крестиком [X]",
        )
        if getattr(self.config, "minimize_to_tray", True):
            self.chk_minimize.select()
        self.chk_minimize.pack(anchor="w", padx=12, pady=(4, 10))

        # Панель кнопок сохранения и сброса
        btn_box = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_box.pack(fill="x", pady=12)

        btn_reset = ctk.CTkButton(
            btn_box,
            text="🔄 Сбросить настройки по умолчанию",
            fg_color=COLOR_CARD_SECONDARY,
            hover_color=COLOR_BORDER,
            font=ctk.CTkFont(size=13),
            height=40,
            command=self._reset_to_default_preset,
        )
        btn_reset.pack(side="left", fill="x", expand=True, padx=(0, 6))

        btn_save = ctk.CTkButton(
            btn_box,
            text="💾 Сохранить все настройки",
            fg_color=COLOR_ACCENT_GREEN,
            hover_color=COLOR_ACCENT_GREEN_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"),
            height=40,
            command=self._save_all_settings,
        )
        btn_save.pack(side="right", fill="x", expand=True, padx=(6, 0))

    # ------------------------------------------------------------------ #
    # Вкладка 4: Диагностика (Doctor)
    # ------------------------------------------------------------------ #
    def _build_tab_doctor(self) -> None:
        parent = self.tab_doctor

        # Панель быстрых действий
        actions_frame = ctk.CTkFrame(parent, fg_color=COLOR_CARD_SECONDARY, corner_radius=8, border_width=1, border_color=COLOR_BORDER)
        actions_frame.pack(fill="x", padx=10, pady=10)

        btn_gsi = ctk.CTkButton(
            actions_frame,
            text="🎮 Подключить к Dota 2 (GSI)",
            fg_color=COLOR_ACCENT_RED,
            hover_color=COLOR_ACCENT_RED_HOVER,
            font=ctk.CTkFont(weight="bold"),
            command=self._on_install_gsi_click,
        )
        btn_gsi.pack(side="left", padx=(10, 6), pady=10)

        btn_steam = ctk.CTkButton(
            actions_frame,
            text="➕ Добавить в Steam",
            fg_color="#1b2838",
            hover_color="#2a475e",
            font=ctk.CTkFont(weight="bold"),
            command=self._on_add_to_steam_click,
        )
        btn_steam.pack(side="left", padx=6, pady=10)

        self.btn_dl_vosk = ctk.CTkButton(
            actions_frame,
            text="📥 Скачать модель Vosk (~45MB)",
            command=self._on_download_vosk_click,
        )
        self.btn_dl_vosk.pack(side="left", padx=6, pady=10)

        btn_refresh = ctk.CTkButton(
            actions_frame,
            text="🔄 Обновить проверку",
            fg_color=COLOR_BORDER,
            hover_color="#34495e",
            command=self._run_doctor_checks,
        )
        btn_refresh.pack(side="right", padx=10, pady=10)

        # Прогресс-бар скачивания модели
        self.prog_vosk = ctk.CTkProgressBar(parent, height=12)
        self.prog_vosk.set(0)
        self.lbl_prog_text = ctk.CTkLabel(parent, text="", font=ctk.CTkFont(size=11), text_color=COLOR_BLUE)

        # Таблица результатов проверки
        self.txt_doctor_results = ctk.CTkTextbox(
            parent,
            fg_color=COLOR_CARD_SECONDARY,
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
            border_width=1,
            border_color=COLOR_BORDER,
        )
        self.txt_doctor_results.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Запуск первичной проверки
        self.after(500, self._run_doctor_checks)

    # ================================================================== #
    # Фоновый ассистент и взаимодействие
    # ================================================================== #
    def _start_assistant(self) -> None:
        try:
            self.assistant = Assistant(self.config, text_mode=False, config_path=self.config_path)
            self.assistant.on_log_message = self._on_assistant_log
            open_settings = lambda: self.after(0, lambda: self._select_tab("⚙ Настройки"))
            self.assistant.on_open_settings = open_settings
            if self.assistant._overlay is not None:
                self.assistant._overlay.on_open_settings = open_settings
            self.assistant.start_background(parent=self)
            self._is_coach_running = True
            coach = self.config.player_profile.coach_name or "Астра"
            self._log_append("Система", f"Помощник AIDOTA запущен. Активный тренер: {coach}. Обратитесь: «{coach}, ...»")
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка старта ассистента: %s", exc)
            self._log_append("Ошибка", f"Не удалось запустить ассистента: {exc}")

    def _on_assistant_log(self, speaker: str, message: str) -> None:
        spk = (speaker or "").lower()
        if "астра" in spk or "джарвис" in spk:
            self.after(0, self._stop_thinking)
        self.after(0, lambda: self._log_append(speaker, message))

    def _log_append(self, speaker: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        spk_lower = speaker.lower()
        if "астра" in spk_lower:
            tag = "tag_astra"
            header = f"[{ts}] ✦ АСТРА:"
        elif "джарвис" in spk_lower:
            tag = "tag_jarvis"
            header = f"[{ts}] ⚡ ДЖАРВИС:"
        elif "вы" in spk_lower or "игрок" in spk_lower:
            tag = "tag_user"
            header = f"[{ts}] 👤 ВЫ:"
        elif "ошибка" in spk_lower or "fail" in spk_lower:
            tag = "tag_err"
            header = f"[{ts}] ❌ ОШИБКА:"
        elif "тайминг" in spk_lower or "макро" in spk_lower or "рошан" in spk_lower:
            tag = "tag_alert"
            header = f"[{ts}] 🔔 ТАКТИЧЕСКИЙ ТАЙМИНГ:"
        else:
            tag = "tag_sys"
            header = f"[{ts}] ⚙️ {speaker.upper()}:"

        self.txt_log.insert("end", f"{header}\n", tag)
        self.txt_log.insert("end", f"{message}\n\n", "tag_msg")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _on_send_query(self) -> None:
        query = self.entry_query.get().strip()
        if not query:
            return
        self.entry_query.delete(0, "end")
        if self.assistant:
            self.assistant.submit_text_command(query)

    def _on_toggle_coach(self) -> None:
        if self._is_coach_running:
            if self.assistant and hasattr(self.assistant, "stop"):
                self.assistant.stop()
            self._is_coach_running = False
            self.btn_toggle_coach.configure(
                text="▶ Запустить",
                fg_color=COLOR_ACCENT_GREEN,
                hover_color=COLOR_ACCENT_GREEN_HOVER,
            )
            self._log_append("Система", "Тренер приостановлен.")
        else:
            self._start_assistant()
            self.btn_toggle_coach.configure(
                text="⏸ Пауза",
                fg_color=COLOR_CARD_SECONDARY,
                hover_color=COLOR_BORDER,
            )

    def _on_coach_changed(self, choice: str) -> None:
        self._select_coach(choice)

    def _on_role_changed(self, choice: str) -> None:
        pos_map = {
            "1 — Керри": 1,
            "2 — Мид": 2,
            "3 — Оффлейн": 3,
            "4 — Поддержка": 4,
            "5 — Полная поддержка": 5,
        }
        pos = pos_map.get(choice, 1)
        self._select_position(pos)

    def _on_toggle_overlay_click(self) -> None:
        if self.assistant and self.assistant._overlay:
            self.assistant._overlay.toggle()
            is_vis = getattr(self.assistant._overlay, "is_visible", lambda: True)()
            toggle_key = getattr(self.config.overlay, "toggle_key", "alt+z") or "alt+z"
            if is_vis:
                self.btn_overlay.configure(text=f"👁 Скрыть [{format_key_name(toggle_key)}]", fg_color=COLOR_ACCENT_RED)
            else:
                self.btn_overlay.configure(text=f"👁 Оверлей [{format_key_name(toggle_key)}]", fg_color=COLOR_CARD_SECONDARY)

    def _schedule_status_poll(self) -> None:
        self._update_live_indicators()
        self.after(1000, self._schedule_status_poll)

    def _update_live_indicators(self) -> None:
        if self.assistant:
            gsi_online = self.assistant.is_gsi_connected()
            if gsi_online:
                self.lbl_gsi_status.configure(text="🟢 Dota 2: В игре", text_color=COLOR_ACCENT_GREEN)
            else:
                self.lbl_gsi_status.configure(text="🔴 Dota 2: Ожидание GSI", text_color="#e74c3c")

            # Обновление тактического статуса
            if gsi_online and self.assistant._gsi:
                state = self.assistant._gsi.get_state()
                if state and isinstance(state, dict):
                    snap = get_live_gsi_snapshot(state, position=self.config.player_profile.default_position)
                    if snap.get("available") and snap.get("hero_name"):
                        h_name = snap.get("hero_name", "Герой")
                        t_fmt = snap.get("clock_formatted", "00:00")
                        kda = snap.get("kda", "0/0/0")
                        gold = snap.get("gold", 0)
                        stage = snap.get("game_stage", "матч")
                        self.lbl_tactical_status.configure(
                            text=f"⚔️ {h_name} • Время: {t_fmt} • KDA: {kda} • Золото: {gold} 💰 • {stage.upper()}",
                            text_color=COLOR_CYAN,
                        )
                    else:
                        self.lbl_tactical_status.configure(
                            text="🟢 Dota 2: Подключено. Ожидание начала матча / драфта...",
                            text_color=COLOR_ACCENT_GREEN,
                        )
            else:
                self.lbl_tactical_status.configure(
                    text="🎮 СТАТУС: Ожидание подключения к Dota 2 (запустите матч или лобби)",
                    text_color=COLOR_TEXT_MUTED,
                )

            mic_ready = getattr(self.assistant._voice, "ready", False)
            mic_muted = getattr(self.assistant._voice, "is_muted", False)
            ptt_on = self.config.audio.push_to_talk
            if not mic_ready:
                self.lbl_mic_status.configure(text="🎙 Микрофон: Загрузка...", text_color=COLOR_TEXT_MUTED)
            elif mic_muted:
                self.lbl_mic_status.configure(text="⏸ Микрофон: Пауза", text_color="#f39c12")
            elif ptt_on:
                key_name = format_key_name(self.config.audio.ptt_key)
                self.lbl_mic_status.configure(text=f"🎙 PTT [{key_name}]", text_color=COLOR_BLUE)
            else:
                self.lbl_mic_status.configure(text="🟢 Микрофон: Слушает", text_color=COLOR_ACCENT_GREEN)

    # ================================================================== #
    # Трей коллбэки
    # ================================================================== #
    def restore_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_toggle_overlay(self) -> None:
        if self.assistant and self.assistant._overlay:
            self.assistant._overlay.toggle()

    def _tray_set_coach(self, name: str) -> None:
        self.after(0, lambda: self._select_coach(name))

    def _tray_set_position(self, pos: int) -> None:
        self.after(0, lambda: self._select_position(pos))

    def _tray_get_mic_enabled(self) -> bool:
        if self.assistant:
            return not getattr(self.assistant._voice, "is_muted", False)
        return True

    def _tray_toggle_mic(self) -> None:
        if self.assistant:
            self.assistant.toggle_mic()

    def on_window_close(self) -> None:
        if getattr(self.config, "minimize_to_tray", True):
            self.withdraw()
            self.tray.notify_minimized(
                title="AIDOTA",
                message="AIDOTA свёрнута в системный трей и продолжает работу.",
            )
        else:
            self.on_full_exit()

    def on_full_exit(self) -> None:
        try:
            self.tray.stop()
        except Exception:
            pass
        if self.assistant:
            try:
                self.assistant.stop()
            except Exception:
                pass
        self.destroy()
        sys.exit(0)

    # ================================================================== #
    # Действия настроек и тестов
    # ================================================================== #
    def _toggle_show_key(self) -> None:
        if self.entry_api_key.cget("show") == "•":
            self.entry_api_key.configure(show="")
            self.btn_show_key.configure(text="🔒")
        else:
            self.entry_api_key.configure(show="•")
            self.btn_show_key.configure(text="👁")

    def _paste_api_key(self) -> None:
        try:
            text = self.clipboard_get()
            if text:
                clean = text.strip()
                self.entry_api_key.delete(0, "end")
                self.entry_api_key.insert(0, clean)
                self.lbl_api_test_res.configure(
                    text="Ключ успешно вставлен из буфера обмена!",
                    text_color=COLOR_ACCENT_GREEN,
                )
            else:
                self.lbl_api_test_res.configure(
                    text="Буфер обмена пуст.",
                    text_color="#e74c3c",
                )
        except Exception as exc:
            self.lbl_api_test_res.configure(
                text=f"Не удалось прочитать буфер обмена: {exc}",
                text_color="#e74c3c",
            )

    def _on_provider_preset_selected(self, preset_name: str) -> None:
        data = PROVIDER_PRESETS.get(preset_name)
        if data:
            self.entry_base_url.delete(0, "end")
            self.entry_base_url.insert(0, data["base_url"])
            self.entry_model.delete(0, "end")
            self.entry_model.insert(0, data["model"])

    def _on_test_api_click(self) -> None:
        provider = "openai_compatible"
        if "ollama" in self.opt_provider.get().lower():
            provider = "ollama"
        base_url = self.entry_base_url.get().strip()
        api_key = self.entry_api_key.get().strip()
        model = self.entry_model.get().strip()

        self.lbl_api_test_res.configure(text="Проверка подключения...", text_color=COLOR_BLUE)

        def _test():
            ok, msg = test_llm_connection(provider, base_url, api_key, model)
            color = COLOR_ACCENT_GREEN if ok else COLOR_ACCENT_RED
            self.after(0, lambda: self.lbl_api_test_res.configure(text=msg, text_color=color))

        threading.Thread(target=_test, daemon=True).start()

    def _on_test_chime_click(self) -> None:
        dev_text = self.opt_output_device.get()
        dev_id = None
        if ":" in dev_text:
            try:
                dev_id = int(dev_text.split(":")[0])
            except ValueError:
                pass
        play_test_chime(dev_id)

    def _on_ptt_mode_changed(self, mode: str) -> None:
        is_ptt = (mode == "Push-to-Talk")
        self.config.audio.push_to_talk = is_ptt
        if self.assistant and hasattr(self.assistant._voice, "set_push_to_talk"):
            self.assistant._voice.set_push_to_talk(is_ptt)

    def _start_hotkey_capture(self, target: str) -> None:
        self._capturing_hotkey_target = target
        if target == "ptt":
            self.btn_set_ptt.configure(text="[Нажмите клавишу...]")
        else:
            self.btn_set_overlay_key.configure(text="[Нажмите хоткей...]")

        def on_key(event):
            vk = int(getattr(event, "keycode", 0) or 0)
            key = str(getattr(event, "keysym", "")).lower()
            if key in ("alt_l", "alt_r", "control_l", "control_r", "shift_l", "shift_r"):
                return
            mods = []
            if event.state & 0x0001:
                mods.append("shift")
            if event.state & 0x0004:
                mods.append("ctrl")
            if event.state & 0x20000 or (event.state & 0x0008):
                mods.append("alt")

            if vk and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39 or 0x70 <= vk <= 0x87):
                main_key = format_key_name(vk).lower()
            else:
                main_key = key
                if main_key.startswith("cyrillic_"):
                    from .hotkeys import _CYRILLIC_KEYSYMS, _RU_TO_EN
                    ru_char = _CYRILLIC_KEYSYMS.get(main_key, "")
                    if ru_char in _RU_TO_EN:
                        main_key = _RU_TO_EN[ru_char]

            combo = "+".join(mods + [main_key]) if mods else main_key
            self.unbind("<Key>")

            if self._capturing_hotkey_target == "ptt":
                self.config.audio.ptt_key = main_key
                self.btn_set_ptt.configure(text=f"⌨ PTT: [{format_key_name(main_key)}]")
                if self.assistant:
                    self.assistant._voice.set_ptt_key(main_key)
            else:
                self.config.overlay.toggle_key = combo
                self.lbl_current_toggle.configure(text=format_key_name(combo))
                self.btn_set_overlay_key.configure(text="⌨ Задать хоткей")
                self.btn_overlay.configure(text=f"👁 Оверлей [{format_key_name(combo)}]")
                if self.assistant and hasattr(self.assistant, "_toggle_listener") and self.assistant._toggle_listener:
                    self.assistant._toggle_listener.set_toggle_combo(combo)

            self._capturing_hotkey_target = None
            save_config(self.config, self.config_path)

        self.bind("<Key>", on_key)

    def _on_autostart_toggled(self) -> None:
        enabled = bool(self.chk_autostart.get())
        set_autostart(enabled)
        self.config.autostart = enabled
        save_config(self.config, self.config_path)

    def _save_all_settings(self) -> None:
        self.config.llm.base_url = self.entry_base_url.get().strip()
        self.config.llm.api_key = self.entry_api_key.get().strip()
        model_val = self.entry_model.get().strip()
        if "openrouter.ai" in self.config.llm.base_url.lower() and (not model_val or model_val.lower() in ("openai", "default") or "/" not in model_val):
            model_val = "openai/gpt-4o-mini"
            self.entry_model.delete(0, "end")
            self.entry_model.insert(0, model_val)
        self.config.llm.model = model_val
        self.config.llm.max_tokens = 350
        self.config.minimize_to_tray = bool(self.chk_minimize.get())

        if hasattr(self, "seg_autonomy"):
            aut_val = self.seg_autonomy.get()
            aut_map = {"Высокая": "high", "Средняя": "medium", "Только по запросу": "off"}
            aut_lvl = aut_map.get(aut_val, "medium")
            self.config.player_profile.autonomy_level = aut_lvl
            if self.assistant:
                self.assistant.set_autonomy_level(aut_lvl)

        if hasattr(self, "seg_skill_level"):
            skill_label = self.seg_skill_level.get()
            skill_code = SKILL_LEVEL_CODES.get(skill_label, "beginner")
            self.config.player_profile.skill_level = skill_code
            if self.assistant:
                setter = getattr(self.assistant, "set_skill_level", None)
                if callable(setter):
                    setter(skill_code)
                elif hasattr(self.assistant, "_coach"):
                    setter = getattr(self.assistant._coach, "set_skill_level", None)
                    if callable(setter):
                        setter(skill_code)

        mic_val = self.opt_input_device.get()
        if ":" in mic_val:
            try:
                self.config.audio.input_device = int(mic_val.split(":")[0])
            except ValueError:
                self.config.audio.input_device = None

        out_val = self.opt_output_device.get()
        if ":" in out_val:
            try:
                self.config.tts.output_device = int(out_val.split(":")[0])
            except ValueError:
                self.config.tts.output_device = None

        if self.assistant:
            self.assistant._coach.llm.base_url = self.config.llm.base_url
            self.assistant._coach.llm.api_key = self.config.llm.api_key
            self.assistant._coach.llm.model = self.config.llm.model
            self.assistant._coach.llm.max_tokens = 350
            self.assistant._coach._models_cache = (0.0, [], None)

        save_config(self.config, self.config_path)
        self._log_append("Система", "Все настройки успешно сохранены в config.json.")

    def _on_autonomy_changed(self, value: str) -> None:
        aut_map = {"Высокая": "high", "Средняя": "medium", "Только по запросу": "off"}
        level = aut_map.get(value, "medium")
        self.config.player_profile.autonomy_level = level
        if self.assistant:
            self.assistant.set_autonomy_level(level)
        save_config(self.config, self.config_path)

    def _on_skill_level_changed(self, value: str) -> None:
        """Выбор уровня игры: сохраняет и мгновенно применяет к тренеру."""
        code = SKILL_LEVEL_CODES.get(value, "beginner")
        self.config.player_profile.skill_level = code
        if hasattr(self, "lbl_skill_hint"):
            self.lbl_skill_hint.configure(text=SKILL_LEVEL_HINTS.get(code, ""))
        if self.assistant:
            setter = getattr(self.assistant, "set_skill_level", None)
            if callable(setter):
                setter(code)
            elif hasattr(self.assistant, "_coach"):
                setter = getattr(self.assistant._coach, "set_skill_level", None)
                if callable(setter):
                    setter(code)
        save_config(self.config, self.config_path)

    def _tray_set_skill_level(self, level: str) -> None:
        code = _normalize_skill_level(level)
        self.config.player_profile.skill_level = code
        lbl = SKILL_LEVEL_LABELS.get(code, "Новичок")
        self.after(0, lambda: self.seg_skill_level.set(lbl) if hasattr(self, "seg_skill_level") else None)
        if self.assistant:
            setter = getattr(self.assistant, "set_skill_level", None)
            if callable(setter):
                setter(code)
        save_config(self.config, self.config_path)

    def _tray_set_autonomy(self, level: str) -> None:
        self.config.player_profile.autonomy_level = level
        if self.assistant:
            self.assistant.set_autonomy_level(level)
        lbl_map = {"high": "Высокая", "medium": "Средняя", "off": "Только по запросу"}
        lbl = lbl_map.get(level.lower(), "Средняя")
        self.after(0, lambda: self.seg_autonomy.set(lbl) if hasattr(self, "seg_autonomy") else None)
        save_config(self.config, self.config_path)

    def _reset_to_default_preset(self) -> None:
        try:
            from .config import reset_to_defaults
            new_cfg = reset_to_defaults(self.config_path)
            self.config = new_cfg
            self.entry_base_url.delete(0, "end")
            self.entry_base_url.insert(0, self.config.llm.base_url or "")
            self.entry_api_key.delete(0, "end")
            self.entry_api_key.insert(0, self.config.llm.api_key or "")
            self.entry_model.delete(0, "end")
            self.entry_model.insert(0, self.config.llm.model or "")
            aut = getattr(self.config.player_profile, "autonomy_level", "medium")
            lbl_map = {"high": "Высокая", "medium": "Средняя", "off": "Только по запросу"}
            if hasattr(self, "seg_autonomy"):
                self.seg_autonomy.set(lbl_map.get(aut.lower(), "Средняя"))
            if hasattr(self, "seg_skill_level"):
                default_skill = _normalize_skill_level(
                    getattr(self.config.player_profile, "skill_level", "beginner")
                )
                self.seg_skill_level.set(SKILL_LEVEL_LABELS.get(default_skill, "Новичок"))
                if hasattr(self, "lbl_skill_hint"):
                    self.lbl_skill_hint.configure(text=SKILL_LEVEL_HINTS.get(default_skill, ""))
            if self.assistant:
                self.assistant.set_autonomy_level(aut)
            self._sync_coach_card_ui()
            self._select_position(self.config.player_profile.default_position or 1)
            self._log_append("Система", "Конфигурация успешно сброшена к эталонному пресету разработчика.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка сброса пресета: %s", exc)
            self._log_append("Ошибка", f"Не удалось сбросить настройки: {exc}")

    # ================================================================== #
    # Разбор матчей
    # ================================================================== #
    def _save_account_id(self) -> None:
        raw = self.entry_account_id.get().strip()
        if raw.isdigit() and int(raw) > 0:
            self.config.player_profile.account_id = int(raw)
            save_config(self.config, self.config_path)
            self._log_append("Система", f"Steam32 Account ID сохранён: {raw}")
        else:
            self._log_append("Ошибка", "Введите корректный числовой Steam32 Account ID.")

    def _on_analyze_match_click(self) -> None:
        raw_id = self.entry_account_id.get().strip()
        acc_id = int(raw_id) if raw_id.isdigit() and int(raw_id) > 0 else self.config.player_profile.account_id
        if not acc_id:
            self.lbl_match_status.configure(text="Введите ваш Steam32 Account ID для поиска матча", text_color=COLOR_ACCENT_RED)
            return

        self.btn_analyze_match.configure(state="disabled")
        self.lbl_match_status.configure(text="Загрузка последнего матча с OpenDota и анализ через LLM...", text_color=COLOR_BLUE)

        def _worker():
            try:
                if self.assistant:
                    report = self.assistant._coach.analyze_match(account_id=acc_id)
                else:
                    report = "Помощник ещё инициализируется..."

                def show_res():
                    self.txt_match_report.configure(state="normal")
                    self.txt_match_report.delete("1.0", "end")
                    self.txt_match_report.insert("1.0", report)
                    self.txt_match_report.configure(state="disabled")
                    self.lbl_match_status.configure(text="Разбор матча успешно завершён!", text_color=COLOR_ACCENT_GREEN)
                    self.btn_analyze_match.configure(state="normal")

                self.after(0, show_res)
            except Exception as exc:  # noqa: BLE001
                def show_err():
                    self.lbl_match_status.configure(text=f"Ошибка: {exc}", text_color=COLOR_ACCENT_RED)
                    self.btn_analyze_match.configure(state="normal")

                self.after(0, show_err)

        threading.Thread(target=_worker, daemon=True).start()

    # ================================================================== #
    # Диагностика и установка
    # ================================================================== #
    def _run_doctor_checks(self) -> None:
        self.txt_doctor_results.configure(state="normal")
        self.txt_doctor_results.delete("1.0", "end")
        self.txt_doctor_results.insert("end", "Проверка окружения и зависимостей...\n\n")

        def _worker():
            checks = get_checks(self.config, online=True)
            lines = []
            for c in checks:
                icon = "✔ [ OK ]" if c.status == "OK" else ("⚠ [WARN]" if c.status == "WARN" else "✖ [FAIL]")
                lines.append(f"{icon}  {c.name:<18} {c.detail}")
                if c.hint and c.status != "OK":
                    lines.append(f"     -> Подсказка: {c.hint}\n")
            lines.append("\n" + "=" * 60)
            ok_cnt = sum(1 for c in checks if c.status == "OK")
            warn_cnt = sum(1 for c in checks if c.status == "WARN")
            fail_cnt = sum(1 for c in checks if c.status == "FAIL")
            lines.append(f"Итог: Успешно: {ok_cnt}, Предупреждений: {warn_cnt}, Ошибок: {fail_cnt}\n")

            def update_ui():
                self.txt_doctor_results.delete("1.0", "end")
                self.txt_doctor_results.insert("end", "\n".join(lines))
                self.txt_doctor_results.configure(state="disabled")

            self.after(0, update_ui)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_gsi_click(self) -> None:
        ok, msg = install_gsi_full(self.config)
        self.txt_doctor_results.configure(state="normal")
        self.txt_doctor_results.insert("end", f"\n[Установка GSI]:\n{msg}\n")
        self.txt_doctor_results.see("end")
        self.txt_doctor_results.configure(state="disabled")

    def _on_add_to_steam_click(self) -> None:
        from .dota_install import add_to_steam_library

        ok, msg = add_to_steam_library()
        self.txt_doctor_results.configure(state="normal")
        self.txt_doctor_results.insert("end", f"\n[Интеграция со Steam]:\n{msg}\n")
        self.txt_doctor_results.see("end")
        self.txt_doctor_results.configure(state="disabled")

    def _on_download_vosk_click(self) -> None:
        self.btn_dl_vosk.configure(state="disabled")
        self.prog_vosk.pack(fill="x", padx=12, pady=(0, 4))
        self.lbl_prog_text.pack(fill="x", padx=12, pady=(0, 6))

        def _progress(pct: float, text: str) -> None:
            self.after(0, lambda: self.prog_vosk.set(pct))
            self.after(0, lambda: self.lbl_prog_text.configure(text=text))

        def _worker():
            try:
                dest = PROJECT_ROOT
                download_vosk_model(dest, model_name="vosk-model-small-ru-0.22", progress_callback=_progress)
                self.after(0, lambda: self.lbl_prog_text.configure(text="Модель Vosk успешно установлена!", text_color=COLOR_ACCENT_GREEN))
                self.after(0, self._run_doctor_checks)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self.lbl_prog_text.configure(text=f"Ошибка скачивания: {exc}", text_color=COLOR_ACCENT_RED))
            finally:
                self.after(0, lambda: self.btn_dl_vosk.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()


def run_gui_app(config: Config | None = None, config_path: Path | None = None) -> int:
    """Точка входа для запуска графического приложения AIDOTA.
    По умолчанию использует современный игровой HTML/CSS/JS интерфейс (pywebview),
    с автоматическим откатом на CustomTkinter при необходимости.
    """
    use_ctk = any(arg.lower() in ("--ctk", "--gui=ctk", "--legacy-gui") for arg in sys.argv)
    if not use_ctk:
        try:
            import webview
            from .web_app import run_web_gui

            cfg = config or load_config(config_path)
            return run_web_gui(cfg, config_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось запустить веб-интерфейс pywebview (%s). Откатываюсь на CustomTkinter.", exc)

    app = AidotaApp(config=config, config_path=config_path)
    app.mainloop()
    return 0
