"""Оверлей на Tkinter: окно диалога (чат с ассистентом) с поддержкой Push-to-Talk.

Отображает:
- Реплики пользователя: «Вы: <текст>» (синий цвет + живой предпросмотр речи)
- Ответы ассистента: «AIDOTA: <текст>» (зелёный цвет)
- Кнопку быстрого выбора / смены клавиши Push-to-Talk
- Кнопку очистки экрана
- Автоматическую прокрутку к свежим сообщениям
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from .config import OverlayConfig
from .hotkeys import format_key_name, parse_key

log = logging.getLogger("aidota.overlay")

_BG = "#080b13"
_FG = "#eaf0ff"
_MUTED = "#93a2bd"
_ACCENT = "#a855f7"
_ACCENT_ALT = "#38bdf8"
_BTN_BG = "#121a28"
_BTN_HOVER = "#1c2941"
_CLOSE = object()

_KEYSYM_MAP: dict[str, str] = {
    "control_l": "ctrl", "control_r": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "alt_l": "alt", "alt_r": "alt",
    "caps_lock": "capslock",
    "space": "space",
    "return": "enter",
    "escape": "esc",
    "backspace": "backspace",
    "tab": "tab",
    "grave": "tilde",
    "asciitilde": "tilde",
}

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


def set_click_through(hwnd: int, enabled: bool) -> bool:
    """Делает окно прозрачным для кликов мыши через Win32 API."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            new_style = style & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("Ошибка установки стиля click-through: %s", exc)
        return False


class Overlay:
    def __init__(
        self,
        config: OverlayConfig,
        project_root: Path,
        current_key: str = "v",
        ptt_enabled: bool = True,
        on_key_change: Callable[[str], None] | None = None,
        on_settings_save: Callable[[dict[str, Any]], None] | None = None,
        on_test_api: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
        on_test_audio: Callable[[int | str | None], tuple[bool, str]] | None = None,
        on_test_match: Callable[[int | None], tuple[bool, str]] | None = None,
        full_config: Any = None,
        on_open_settings: Callable[[], None] | None = None,
    ):
        self.config = config
        self.project_root = Path(project_root)
        self._current_key = str(current_key or "v")
        self._ptt_enabled = bool(ptt_enabled)
        self.on_key_change = on_key_change
        self.on_settings_save = on_settings_save
        self.on_test_api = on_test_api
        self.on_test_audio = on_test_audio
        self.on_test_match = on_test_match
        self.full_config = full_config
        self.on_open_settings = on_open_settings
        self._settings_dialog: Any = None

        self._queue: queue.Queue = queue.Queue()
        self._root: Any = None
        self._text_widget: Any = None
        self._status_widget: Any = None
        self._ptt_btn: Any = None
        self._capturing_key = False
        coach_name = "Джарвис" if "dmitry" in str(getattr(getattr(full_config, "tts", None), "edge_voice", "")).lower() else "Астра"
        self._coach_name = coach_name
        self._header_label: Any = None
        self._has_partial = False
        self._visible = True
        self._click_through = False
        self._ct_btn: Any = None
        self._icon_cache: dict[str, Any] = {}
        self._images_keepalive: list[Any] = []
        self._started = False
        self._error: str | None = None

    # ------------------------------------------------------------------ #
    @property
    def started(self) -> bool:
        return self._started

    @property
    def error(self) -> str | None:
        return self._error

    # ------------------------------------------------------------------ #
    def start(self, parent: Any = None) -> bool:
        """Создаёт окно. Вызывается только из главного потока."""
        if not self.config.enabled:
            self._error = "Оверлей выключен в config.json"
            return False
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception as exc:  # noqa: BLE001
            self._error = f"Tkinter недоступен: {exc}"
            log.warning(self._error)
            return False

        try:
            self._has_parent = parent is not None
            if parent is not None:
                root = tk.Toplevel(parent)
                root.withdraw()
                self._visible = False
            else:
                root = tk.Tk()
                self._visible = True
            root.title(f"{self._coach_name} — голосовой тренер Dota 2")
            width = max(380, int(self.config.width))
            height = max(220, int(self.config.height))
            x = self.config.x
            y = self.config.y
            if x is None or y is None:
                x = max(10, root.winfo_screenwidth() - width - 24)
                y = 24
            root.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
            root.configure(bg=_BG)
            self._target_alpha = max(0.3, min(1.0, float(self.config.alpha)))
            try:
                root.attributes("-alpha", 0.0)
            except Exception:  # noqa: BLE001
                pass
            if self.config.topmost:
                root.attributes("-topmost", True)
            try:
                root.attributes("-toolwindow", True)
            except Exception:  # noqa: BLE001
                pass

            # Тонкая градиентная полоса-акцент (фиолет → циан / золото → красный у Джарвиса)
            accent_canvas = tk.Canvas(root, height=3, bg=_BG, highlightthickness=0, bd=0)
            accent_canvas.pack(fill="x", side="top")
            self._accent_canvas = accent_canvas
            root.after(30, self._draw_accent_gradient)

            # Верхняя панель: Заголовок слева, кнопки управления справа
            header_frame = tk.Frame(root, bg=_BG)
            header_frame.pack(fill="x", padx=12, pady=(8, 2))

            header = tk.Label(
                header_frame,
                text=f"{self._coach_name.upper()}",
                bg=_BG,
                fg=_ACCENT,
                font=(tkfont.nametofont("TkDefaultFont").actual("family"), 12, "bold"),
                anchor="w",
            )
            logo_photo = self._load_logo_photo()
            if logo_photo is not None:
                logo_label = tk.Label(header_frame, image=logo_photo, bg=_BG)
                logo_label.pack(side="left", padx=(0, 7))
                self._logo_label = logo_label
            header.pack(side="left")
            self._header_label = header


            btn_box = tk.Frame(header_frame, bg=_BG)
            btn_box.pack(side="right")

            hide_btn = tk.Button(
                btn_box,
                text="—",
                bg=_BTN_BG,
                fg=_MUTED,
                activebackground=_BTN_HOVER,
                activeforeground="#ffffff",
                relief="flat",
                borderwidth=1,
                highlightthickness=0,
                cursor="hand2",
                font=("Segoe UI", 9, "bold"),
                padx=6,
                pady=2,
                command=self.hide,
            )
            hide_btn.pack(side="left", padx=(0, 6))

            clear_btn = tk.Button(
                btn_box,
                text="🗑",
                bg=_BTN_BG,
                fg=_MUTED,
                activebackground=_BTN_HOVER,
                activeforeground="#ffffff",
                relief="flat",
                borderwidth=1,
                highlightthickness=0,
                cursor="hand2",
                font=("Segoe UI", 8),
                padx=5,
                pady=2,
                command=self.clear,
            )
            clear_btn.pack(side="left", padx=(0, 6))

            ct_btn = tk.Button(
                btn_box,
                text="🖱",
                bg=_BTN_BG,
                fg=_MUTED,
                activebackground=_BTN_HOVER,
                activeforeground="#ffffff",
                relief="flat",
                borderwidth=1,
                highlightthickness=0,
                cursor="hand2",
                font=("Segoe UI", 8),
                padx=5,
                pady=2,
                command=self.toggle_click_through,
            )
            ct_btn.pack(side="left", padx=(0, 6))
            self._ct_btn = ct_btn

            ptt_btn = tk.Button(
                btn_box,
                text=self._button_text(),
                bg=_BTN_BG,
                fg=_ACCENT if self._ptt_enabled else _MUTED,
                activebackground=_BTN_HOVER,
                activeforeground="#ffffff",
                relief="flat",
                borderwidth=1,
                highlightthickness=0,
                cursor="hand2",
                font=("Segoe UI", 9, "bold"),
                padx=8,
                pady=2,
                command=self._open_key_menu,
            )
            ptt_btn.pack(side="left")
            self._ptt_btn = ptt_btn

            # Живая HUD-строка матча: герой, уровень, время, золото, KDA.
            hud = tk.Label(
                root,
                text="",
                bg=_BG,
                fg="#38bdf8",
                anchor="w",
                font=("Segoe UI", 9, "bold"),
            )
            hud.pack(fill="x", padx=12, pady=(1, 0))
            self._hud_widget = hud

            status = tk.Label(
                root, text="Запуск…", bg=_BG, fg=_MUTED, anchor="w", font=("Segoe UI", 9)
            )
            status.pack(fill="x", padx=12, pady=(0, 2))

            body = tk.Frame(root, bg=_BG)
            body.pack(fill="both", expand=True, padx=8, pady=(4, 8))
            text_widget = tk.Text(
                body,
                wrap="word",
                bg=_BG,
                fg=_FG,
                insertbackground=_FG,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                font=("Segoe UI", 11),
                padx=6,
                pady=4,
            )
            scroll = tk.Scrollbar(body, command=text_widget.yview)
            text_widget.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            text_widget.pack(side="left", fill="both", expand=True)

            # Настройка стилей для чата
            text_widget.tag_configure("user_hdr", foreground="#58a6ff", font=("Segoe UI", 10, "bold"))
            text_widget.tag_configure("user_body", foreground="#f0f6fc", font=("Segoe UI", 11))
            text_widget.tag_configure("ai_hdr", foreground="#59d06b", font=("Segoe UI", 10, "bold"))
            text_widget.tag_configure("ai_body", foreground="#d2d9e0", font=("Segoe UI", 11))
            text_widget.tag_configure("partial_hdr", foreground="#79c0ff", font=("Segoe UI", 10, "italic"))
            text_widget.tag_configure("partial_body", foreground="#8b98a5", font=("Segoe UI", 10, "italic"))
            text_widget.tag_configure("sys_msg", foreground="#8b98a5", font=("Segoe UI", 10, "italic"))

            text_widget.insert("1.0", "AIDOTA: ", "ai_hdr")
            text_widget.insert("end", "Скажите «помощь», чтобы узнать команды.\n\n", "ai_body")
            text_widget.configure(state="disabled")

            root.protocol("WM_DELETE_WINDOW", self.close)
            self._root = root
            self._text_widget = text_widget
            self._status_widget = status
            self._started = True
            root.after(60, lambda: self._fade_to(self._target_alpha))
            root.after(150, self._poll)
            log.info("Оверлей создан")
            return True
        except Exception as exc:  # noqa: BLE001 — нет дисплея и т.п.
            self._error = f"Не удалось создать окно оверлея: {exc}"
            log.warning(self._error)
            return False

    # ------------------------------------------------------------------ #
    # Современное оформление: градиент, логотип, плавное появление
    # ------------------------------------------------------------------ #
    def _draw_accent_gradient(self) -> None:
        """Рисует тонкую градиентную полосу под цвет активного тренера."""
        root = self._root
        canvas = getattr(self, "_accent_canvas", None)
        if root is None or canvas is None:
            return
        try:
            canvas.delete("all")
            width = max(2, canvas.winfo_width())
            name = (self._coach_name or "").lower()
            if "джарвис" in name or "jarvis" in name:
                start, end = (245, 158, 11), (239, 68, 68)
            else:
                start, end = (168, 85, 247), (56, 189, 248)
            for x in range(0, width, 3):
                t = x / max(1, width - 1)
                color = "#%02x%02x%02x" % (
                    int(start[0] + (end[0] - start[0]) * t),
                    int(start[1] + (end[1] - start[1]) * t),
                    int(start[2] + (end[2] - start[2]) * t),
                )
                canvas.create_line(x, 0, x + 3, 0, fill=color, width=3)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось нарисовать акцент оверлея: %s", exc)

    def _load_logo_photo(self, size: int = 24) -> Any | None:
        """Загружает логотип AIDOTA для шапки оверлея (Pillow → PhotoImage)."""
        try:
            from PIL import Image, ImageTk
        except Exception:  # noqa: BLE001 — Pillow не обязателен для работы оверлея
            return None
        for name in ("logo.png", "icon.png"):
            path = self.project_root / "assets" / name
            if not path.is_file():
                continue
            try:
                image = Image.open(path).convert("RGBA")
                image.thumbnail((size, size))
                photo = ImageTk.PhotoImage(image)
                self._images_keepalive.append(photo)
                return photo
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось загрузить логотип оверлея %s: %s", path, exc)
        return None

    def _fade_to(self, target: float, step: float = 0.14) -> None:
        """Плавно меняет прозрачность окна (alpha) через after-цикл."""
        root = self._root
        if root is None:
            return
        try:
            current = float(root.attributes("-alpha"))
        except Exception:  # noqa: BLE001
            return
        if abs(current - target) <= 0.02:
            try:
                root.attributes("-alpha", target)
            except Exception:  # noqa: BLE001
                pass
            return
        next_alpha = current + step if target > current else current - step
        next_alpha = max(0.0, min(1.0, next_alpha))
        try:
            root.attributes("-alpha", next_alpha)
            root.after(16, lambda: self._fade_to(target, step))
        except Exception:  # noqa: BLE001
            pass

    def _fade_out_and_withdraw(self) -> None:
        root = self._root
        if root is None:
            return
        self._fade_to(0.0, 0.22)

        def _withdraw() -> None:
            if self._root is not None:
                try:
                    self._root.withdraw()
                except Exception:  # noqa: BLE001
                    pass
                self._visible = False

        try:
            root.after(150, _withdraw)
        except Exception:  # noqa: BLE001
            pass

    def _set_hud_text(self, text: str) -> None:
        """Обновляет HUD-строку матча без лишних перерисовок (anti-flicker)."""
        widget = getattr(self, "_hud_widget", None)
        if widget is None:
            return
        clean = " ".join(str(text or "").split())
        if getattr(self, "_hud_last", None) == clean:
            return
        try:
            widget.configure(text=clean)
            self._hud_last = clean
        except Exception:  # noqa: BLE001
            pass

    def update_hud(self, text: str) -> None:
        """Потокобезопасно обновляет строку HUD: герой/уровень/золото/KDA."""
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        if self._root is not None:
            try:
                self._root.after(0, lambda: self._set_hud_text(clean))
                return
            except Exception:  # noqa: BLE001
                pass
        self._queue.put(("HUD", clean))

    # ------------------------------------------------------------------ #
    def _button_text(self) -> str:
        if not self._ptt_enabled:
            return "🔊 Свободный ▾"
        return f"🎙 {format_key_name(self._current_key)} ▾"

    def _update_button_label(self) -> None:
        if self._ptt_btn is None or self._root is None:
            return
        self._ptt_btn.configure(
            text=self._button_text(),
            bg=_BTN_BG,
            fg=_ACCENT if self._ptt_enabled else _MUTED,
        )

    def _open_key_menu(self) -> None:
        """Открывает выпадающее меню со списком популярных клавиш и опцией захвата нажатия."""
        if self._root is None:
            return
        import tkinter as tk

        menu = tk.Menu(
            self._root,
            tearoff=0,
            bg="#18202a",
            fg=_FG,
            activebackground="#253547",
            activeforeground="#ffffff",
            font=("Segoe UI", 9),
            relief="flat",
            borderwidth=1,
        )

        menu.add_command(label="🎯 Нажать клавишу прямо сейчас…", command=self._start_key_capture)
        menu.add_separator()

        presets = [
            ("Клавиша V (по умолчанию)", "v"),
            ("Клавиша C", "c"),
            ("Клавиша B", "b"),
            ("Клавиша G", "g"),
            ("Клавиша X", "x"),
            ("Клавиша T", "t"),
            ("Клавиша F8", "f8"),
            ("Клавиша F1", "f1"),
            ("Клавиша Caps Lock", "capslock"),
            ("Клавиша Space (Пробел)", "space"),
            ("Клавиша Alt", "alt"),
            ("Клавиша Ctrl", "ctrl"),
            ("Клавиша ~ (Тильда/Ё)", "tilde"),
            ("Мышь: Mouse 4 (боковая кнопка)", "mouse4"),
            ("Мышь: Mouse 5 (боковая кнопка)", "mouse5"),
            ("Мышь: Mouse 3 (колёсико)", "mouse3"),
        ]

        for label, key_val in presets:
            menu.add_command(
                label=label,
                command=lambda k=key_val: self._on_key_selected(k),
            )

        menu.add_separator()
        menu.add_command(
            label="🔊 Свободный микрофон (без клавиши)",
            command=lambda: self._on_key_selected("free"),
        )

        try:
            x = self._ptt_btn.winfo_rootx()
            y = self._ptt_btn.winfo_rooty() + self._ptt_btn.winfo_height() + 2
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _start_key_capture(self) -> None:
        """Переводит кнопку в режим ожидания нажатия любой клавиши."""
        if self._root is None:
            return
        self._capturing_key = True
        self._ptt_btn.configure(
            text="⏳ Нажмите клавишу…",
            bg="#d97706",
            fg="#ffffff",
        )

        def on_press(event):  # noqa: ANN001
            if not self._capturing_key:
                return
            self._capturing_key = False
            try:
                self._root.unbind("<Key>", self._key_bind_id)
            except Exception:  # noqa: BLE001
                pass

            vk = int(getattr(event, "keycode", 0) or 0)
            keysym = str(getattr(event, "keysym", "")).lower()
            if keysym in ("escape", "esc"):
                self._update_button_label()
                return

            if vk and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39 or 0x70 <= vk <= 0x87):
                mapped = format_key_name(vk).lower()
            else:
                mapped = _KEYSYM_MAP.get(keysym, keysym)
                if mapped.startswith("cyrillic_"):
                    from .hotkeys import _CYRILLIC_KEYSYMS, _RU_TO_EN
                    ru_char = _CYRILLIC_KEYSYMS.get(mapped, "")
                    if ru_char in _RU_TO_EN:
                        mapped = _RU_TO_EN[ru_char]
            self._on_key_selected(mapped)

        self._key_bind_id = self._root.bind("<Key>", on_press)

    def _on_key_selected(self, key_val: str) -> None:
        if key_val in ("free", "off", "none"):
            self._ptt_enabled = False
        else:
            self._ptt_enabled = True
            self._current_key = key_val

        self._update_button_label()

        if self.on_key_change is not None:
            threading.Thread(
                target=self.on_key_change,
                args=(key_val,),
                name="aidota-key-change",
                daemon=True,
            ).start()

    # ------------------------------------------------------------------ #
    # Управление содержимым чата
    # ------------------------------------------------------------------ #
    def add_user_message(self, text: str) -> None:
        """Добавляет в окно распознанную речь пользователя."""
        self._queue.put(("USER_MSG", str(text)))

    def add_ai_message(self, text: str, status: str | None = None) -> None:
        """Добавляет в окно ответ ассистента."""
        self._queue.put(("AI_MSG", str(text), status))

    def update_partial(self, partial_text: str) -> None:
        """Живое отображение слов прямо во время речи."""
        self._queue.put(("PARTIAL_MSG", str(partial_text)))

    def update(self, text: str, status: str | None = None) -> None:
        """Совместимость: добавляет сообщение ассистента."""
        self.add_ai_message(text, status=status)

    def update_status(self, status: str) -> None:
        self._queue.put(("STATUS", str(status)))

    def update_ptt_key(self, key: str, enabled: bool = True) -> None:
        """Обновляет отображаемую клавишу в интерфейсе из внешнего потока."""
        self._queue.put(("SET_KEY", str(key), bool(enabled)))

    def clear(self) -> None:
        """Очищает историю сообщений."""
        self._queue.put(("CLEAR",))

    def request_open_settings(self) -> None:
        """Запрашивает открытие окна настроек из любого потока."""
        self._queue.put(("OPEN_SETTINGS",))

    def open_settings(self) -> None:
        """Переключает на вкладку «Настройки» главного интерфейса AIDOTA.

        Отдельное окно настроек удалено: все параметры доступны в основном
        приложении (веб-дашборд или Tk-GUI), поэтому оверлей только просит
        интерфейс показать нужную вкладку.
        """
        callback = getattr(self, "on_open_settings", None)
        if callable(callback):
            try:
                callback()
                return
            except Exception:  # noqa: BLE001
                log.debug("Не удалось открыть вкладку настроек", exc_info=True)
        log.debug("on_open_settings не задан — настройки доступны в главном окне AIDOTA.")

    # ------------------------------------------------------------------ #
    def _poll(self) -> None:
        if self._root is None:
            return
        should_close = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _CLOSE:
                should_close = True
                break
            if not isinstance(item, tuple) or not item:
                continue

            cmd = item[0]
            if cmd == "SET_KEY":
                self._current_key = item[1]
                self._ptt_enabled = item[2]
                self._update_button_label()
            elif cmd == "OPEN_SETTINGS":
                self.open_settings()
            elif cmd == "STATUS":
                if self._status_widget is not None:
                    self._status_widget.configure(text=item[1])
            elif cmd == "USER_MSG":
                self._append_user_message(item[1])
            elif cmd == "AI_MSG":
                self._append_ai_message(item[1])
                if len(item) > 2 and item[2] is not None:
                    if self._status_widget is not None:
                        self._status_widget.configure(text=item[2])
            elif cmd == "PARTIAL_MSG":
                self._set_partial_user_text(item[1])
            elif cmd == "CLEAR":
                self._clear_all()
            elif cmd == "HUD":
                self._set_hud_text(item[1])
            elif cmd == "HIDE":
                self._fade_out_and_withdraw()
            elif cmd == "SHOW":
                self._show_with_fade()
            elif cmd == "TOGGLE":
                self._safe_toggle_visibility()

        if should_close:
            self._destroy()
            return

        self._root.after(150, self._poll)

    def _clear_partial(self) -> None:
        if self._has_partial and self._text_widget is not None:
            try:
                self._text_widget.configure(state="normal")
                self._text_widget.delete("partial_start", "end")
                self._text_widget.configure(state="disabled")
            except Exception:  # noqa: BLE001
                pass
            self._has_partial = False

    def _set_partial_user_text(self, partial: str) -> None:
        if self._text_widget is None:
            return
        self._clear_partial()
        clean = partial.strip()
        if not clean:
            return
        try:
            self._text_widget.configure(state="normal")
            self._text_widget.mark_set("partial_start", "end-1c")
            self._text_widget.mark_gravity("partial_start", "left")
            self._text_widget.insert("end", "Вы: ", "partial_hdr")
            self._text_widget.insert("end", f"{clean}…\n", "partial_body")
            self._text_widget.see("end")
            self._text_widget.configure(state="disabled")
            self._has_partial = True
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка вывода partial: %s", exc)

    def _append_user_message(self, text: str) -> None:
        if self._text_widget is None:
            return
        self._clear_partial()
        clean = text.strip()
        if not clean:
            return
        try:
            self._text_widget.configure(state="normal")
            self._trim_history()
            self._text_widget.insert("end", "Вы: ", "user_hdr")
            self._text_widget.insert("end", f"{clean}\n\n", "user_body")
            self._text_widget.see("end")
            self._text_widget.configure(state="disabled")
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка добавления user message: %s", exc)

    def set_coach_name(self, name: str) -> None:
        if not name or not name.strip():
            return
        self._coach_name = name.strip()
        accent = "#f39c12" if "джарвис" in self._coach_name.lower() else "#9d4edd"
        if hasattr(self, "_header_label") and self._header_label is not None and self._root is not None:
            def update() -> None:
                try:
                    self._header_label.configure(text=f"✦ {self._coach_name.upper()}", fg=accent)
                    self._root.title(f"{self._coach_name} — голосовой тренер Dota 2")
                except Exception:  # noqa: BLE001
                    pass
            self._root.after(0, update)

    def set_click_through(self, enabled: bool) -> None:
        """Включает или выключает прозрачность для кликов мыши."""
        self._click_through = bool(enabled)
        if self._root is not None:
            try:
                hwnd = self._root.winfo_id()
                set_click_through(hwnd, self._click_through)
                if self._ct_btn is not None:
                    self._ct_btn.configure(fg=_ACCENT if self._click_through else _MUTED)
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка установки click-through: %s", exc)

    def toggle_click_through(self) -> None:
        self.set_click_through(not self._click_through)

    def _find_hero_icon(self, text: str) -> Any | None:
        if not text:
            return None
        icons_dir = self.project_root / "hero_icons"
        if not icons_dir.is_dir():
            return None
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None

        lowered = text.lower()
        matched_path: Path | None = None
        for img_path in icons_dir.glob("*.png"):
            stem = img_path.stem.lower()
            clean_stem = stem.replace("_", " ")
            if len(clean_stem) >= 3 and (clean_stem in lowered or stem in lowered):
                matched_path = img_path
                break

        if matched_path is None:
            return None

        cache_key = str(matched_path)
        if cache_key in self._icon_cache:
            return self._icon_cache[cache_key]

        try:
            img = Image.open(matched_path).convert("RGBA")
            img = img.resize((24, 24), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._icon_cache[cache_key] = photo
            self._images_keepalive.append(photo)
            if len(self._images_keepalive) > 100:
                self._images_keepalive = self._images_keepalive[-50:]
            return photo
        except Exception:
            return None

    def _append_ai_message(self, text: str) -> None:
        if self._text_widget is None:
            return
        self._clear_partial()
        clean = text.strip()
        if not clean:
            return
        try:
            self._text_widget.configure(state="normal")
            self._trim_history()
            icon = self._find_hero_icon(clean)
            if icon is not None:
                self._text_widget.image_create("end", image=icon)
                self._text_widget.insert("end", " ")
            self._text_widget.insert("end", f"{self._coach_name}: ", "ai_hdr")
            self._text_widget.insert("end", f"{clean}\n\n", "ai_body")
            self._text_widget.see("end")
            self._text_widget.configure(state="disabled")
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка добавления ai message: %s", exc)

    def _clear_all(self) -> None:
        if self._text_widget is None:
            return
        try:
            self._text_widget.configure(state="normal")
            self._text_widget.delete("1.0", "end")
            self._text_widget.insert("1.0", "История очищена.\n\n", "sys_msg")
            self._text_widget.configure(state="disabled")
            self._has_partial = False
        except Exception:  # noqa: BLE001
            pass

    def _trim_history(self) -> None:
        if self._text_widget is None:
            return
        try:
            total_lines = int(self._text_widget.index("end-1c").split(".")[0])
            if total_lines > 400:
                self._text_widget.delete("1.0", f"{total_lines - 250}.0")
        except Exception:  # noqa: BLE001
            pass

    def _safe_toggle_visibility(self) -> None:
        """Потокобезопасное переключение видимости оверлея в главном потоке."""
        if self._root is None:
            return
        try:
            state = self._root.state()
            is_hidden = (not self._visible) or (state == "withdrawn") or (state == "iconic")
            if is_hidden:
                self._show_with_fade()
            else:
                self._fade_out_and_withdraw()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка переключения видимости оверлея: %s", exc)

    def _show_with_fade(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            root.attributes("-alpha", 0.0)
            root.deiconify()
            root.attributes("-topmost", True)
            root.lift()
            self._visible = True
            self._fade_to(getattr(self, "_target_alpha", 0.9), 0.18)
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка показа оверлея: %s", exc)


    def _destroy(self) -> None:
        root = self._root
        self._root = None
        self._started = False
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
            log.info("Оверлей закрыт")

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        if self._root is None:
            return
        if getattr(self, "_has_parent", False):
            return
        try:
            self._root.mainloop()
        finally:
            self._root = None
            self._started = False

    def hide(self) -> None:
        if self._root is not None:
            try:
                self._root.after(0, self._fade_out_and_withdraw)
                return
            except Exception:
                pass
        self._queue.put(("HIDE",))

    def show(self) -> None:
        if self._root is not None:
            try:
                self._root.after(0, self._show_with_fade)
                return
            except Exception:
                pass
        self._queue.put(("SHOW",))

    def toggle(self) -> None:
        """Потокобезопасный вызов переключения видимости оверлея."""
        if self._root is not None:
            try:
                self._root.after(0, self._safe_toggle_visibility)
                return
            except Exception:
                pass
        self._queue.put(("TOGGLE",))

    def is_visible(self) -> bool:
        """Возвращает текущую видимость оверлея."""
        if self._root is not None:
            try:
                state = self._root.state()
                return bool(self._visible and state not in ("withdrawn", "iconic"))
            except Exception:
                pass
        return bool(self._visible)

    def close(self) -> None:
        self._queue.put(_CLOSE)

    def shutdown(self) -> None:
        """Аварийное закрытие (например, по Ctrl+C из другого потока)."""
        self._queue.put(_CLOSE)
