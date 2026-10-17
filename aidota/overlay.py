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

_BG = "#11161c"
_FG = "#e6edf3"
_MUTED = "#8b98a5"
_ACCENT = "#59d06b"
_BTN_BG = "#1a232e"
_BTN_HOVER = "#243242"
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


class Overlay:
    def __init__(
        self,
        config: OverlayConfig,
        project_root: Path,
        current_key: str = "v",
        ptt_enabled: bool = True,
        on_key_change: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.project_root = Path(project_root)
        self._current_key = str(current_key or "v")
        self._ptt_enabled = bool(ptt_enabled)
        self.on_key_change = on_key_change

        self._queue: queue.Queue = queue.Queue()
        self._root: Any = None
        self._text_widget: Any = None
        self._status_widget: Any = None
        self._ptt_btn: Any = None
        self._capturing_key = False
        self._key_bind_id: Any = None
        self._has_partial = False
        self._visible = True
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
    def start(self) -> bool:
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
            root = tk.Tk()
            root.title("AIDOTA — помощник по Dota 2")
            width = max(380, int(self.config.width))
            height = max(220, int(self.config.height))
            x = self.config.x
            y = self.config.y
            if x is None or y is None:
                x = max(10, root.winfo_screenwidth() - width - 24)
                y = 24
            root.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
            root.configure(bg=_BG)
            root.attributes("-alpha", max(0.3, min(1.0, float(self.config.alpha))))
            if self.config.topmost:
                root.attributes("-topmost", True)
            try:
                root.attributes("-toolwindow", True)
            except Exception:  # noqa: BLE001
                pass

            # Верхняя панель: Заголовок слева, кнопки управления справа
            header_frame = tk.Frame(root, bg=_BG)
            header_frame.pack(fill="x", padx=12, pady=(8, 2))

            header = tk.Label(
                header_frame,
                text="AIDOTA",
                bg=_BG,
                fg=_ACCENT,
                font=(tkfont.nametofont("TkDefaultFont").actual("family"), 12, "bold"),
                anchor="w",
            )
            header.pack(side="left")

            btn_box = tk.Frame(header_frame, bg=_BG)
            btn_box.pack(side="right")

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
            root.after(100, self._poll)
            log.info("Оверлей создан")
            return True
        except Exception as exc:  # noqa: BLE001 — нет дисплея и т.п.
            self._error = f"Не удалось создать окно оверлея: {exc}"
            log.warning(self._error)
            return False

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

            keysym = str(event.keysym).lower()
            if keysym in ("escape", "esc"):
                self._update_button_label()
                return

            mapped = _KEYSYM_MAP.get(keysym, keysym)
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

        if should_close:
            self._destroy()
            return

        self._root.after(100, self._poll)

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
            self._text_widget.insert("end", "AIDOTA: ", "ai_hdr")
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
        try:
            self._root.mainloop()
        finally:
            self._root = None
            self._started = False

    def hide(self) -> None:
        if self._root is not None:
            self._root.withdraw()
            self._visible = False

    def show(self) -> None:
        if self._root is not None:
            self._root.deiconify()
            self._root.lift()
            self._visible = True

    def close(self) -> None:
        self._queue.put(_CLOSE)

    def shutdown(self) -> None:
        """Аварийное закрытие (например, по Ctrl+C из другого потока)."""
        self._queue.put(_CLOSE)
