"""Глобальный перехват клавиши (push-to-talk) через низкоуровневые хуки Windows.

Поддерживает как любые клавиши клавиатуры (F1..F24, буквы, Space, CapsLock, Alt, Ctrl и др.),
так и кнопки мыши (Mouse 4, Mouse 5, Middle / колёсико).
Работает даже когда фокус в игре (Dota 2), без сторонних библиотек —
на чистом ctypes + WH_KEYBOARD_LL / WH_MOUSE_LL.
"""

from __future__ import annotations

import ctypes
import re
import threading
from ctypes import wintypes
from typing import Callable

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

WM_QUIT = 0x0012

# Виртуальные коды мыши в Windows
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04   # Колёсико мыши
VK_XBUTTON1 = 0x05  # Mouse 4 (боковая кнопка назад)
VK_XBUTTON2 = 0x06  # Mouse 5 (боковая кнопка вперёд)

_RU_TO_EN: dict[str, str] = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u", "ш": "i", "щ": "o", "з": "p",
    "х": "[", "ъ": "]", "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k",
    "д": "l", "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": ".",
}

_KEY_NAMES: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "лкт": 0x11, "контрол": 0x11, "контроль": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "шифт": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "menu": 0x12, "альт": 0x12, "алт": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "capslock": 0x14, "caps": 0x14, "капс": 0x14, "капслок": 0x14,
    "space": 0x20, "пробел": 0x20,
    "tab": 0x09, "таб": 0x09,
    "enter": 0x0D, "return": 0x0D, "энтер": 0x0D, "ввод": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "ескейп": 0x1B,
    "backspace": 0x08, "бэкспейс": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "ins": 0x2D,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
    "влево": 0x25, "вправо": 0x27, "вверх": 0x26, "вниз": 0x28,
    "tilde": 0xC0, "тильда": 0xC0, "`": 0xC0, "~": 0xC0, "ё": 0xC0,
    # Мышь
    "mouse4": VK_XBUTTON1, "m4": VK_XBUTTON1, "xbutton1": VK_XBUTTON1,
    "боковая1": VK_XBUTTON1, "боковая": VK_XBUTTON1, "мышь4": VK_XBUTTON1,
    "mouse5": VK_XBUTTON2, "m5": VK_XBUTTON2, "xbutton2": VK_XBUTTON2,
    "боковая2": VK_XBUTTON2, "мышь5": VK_XBUTTON2,
    "mouse3": VK_MBUTTON, "m3": VK_MBUTTON, "middle": VK_MBUTTON,
    "колесико": VK_MBUTTON, "колёсико": VK_MBUTTON, "колесо": VK_MBUTTON,
    "средняя": VK_MBUTTON, "мышь3": VK_MBUTTON,
    "mouse1": VK_LBUTTON, "m1": VK_LBUTTON, "лкм": VK_LBUTTON,
    "mouse2": VK_RBUTTON, "m2": VK_RBUTTON, "пкм": VK_RBUTTON,
}

_VK_LABELS: dict[int, str] = {
    VK_LBUTTON: "Mouse 1 (ЛКМ)",
    VK_RBUTTON: "Mouse 2 (ПКМ)",
    VK_MBUTTON: "Mouse 3 (Колёсико)",
    VK_XBUTTON1: "Mouse 4 (Боковая 1)",
    VK_XBUTTON2: "Mouse 5 (Боковая 2)",
    0x10: "Shift",
    0x11: "Ctrl",
    0x12: "Alt",
    0x14: "Caps Lock",
    0x20: "Space (Пробел)",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x08: "Backspace",
    0xC0: "~ (Тильда/Ё)",
}


def parse_key(name: str | None) -> int:
    """Имя клавиши -> виртуальный код (VK). Поднимает ValueError на неизвестном."""
    if name is None:
        raise ValueError("Клавиша push-to-talk не задана")
    text = str(name).strip().lower()
    if text in _KEY_NAMES:
        return _KEY_NAMES[text]

    # Numpad (num0 .. num9, numpad0 .. numpad9)
    num_match = re.fullmatch(r"(?:num|numpad)(\d)", text)
    if num_match:
        return 0x60 + int(num_match.group(1))

    # F1 .. F24 (включая русскую раскладку ф1 .. ф24)
    f_match = re.fullmatch(r"[fф](\d{1,2})", text)
    if f_match:
        number = int(f_match.group(1))
        if 1 <= number <= 24:
            return 0x70 + (number - 1)

    # Русская буква -> английская раскладка
    if text in _RU_TO_EN:
        text = _RU_TO_EN[text]

    if len(text) == 1 and text.isalnum():
        return ord(text.upper())

    raise ValueError(f"Неизвестная клавиша: {name}")


def format_key_name(key: int | str) -> str:
    """Возвращает понятное человеку отображение клавиши (например, 'V', 'F8', 'Mouse 4')."""
    try:
        vk = parse_key(key) if isinstance(key, str) else int(key)
    except Exception:
        return str(key).upper()

    if vk in _VK_LABELS:
        return _VK_LABELS[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x70 + 1}"
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Num {vk - 0x60}"
    return f"VK_0x{vk:02X}"


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class HotkeyListener:
    """Отслеживает нажатие/отпускание глобальной клавиши или кнопки мыши.

    on_down вызывается при нажатии, on_up — при отпускании.
    """

    def __init__(
        self,
        key: str | int,
        on_down: Callable[[], None] | None = None,
        on_up: Callable[[], None] | None = None,
    ):
        self.vk = parse_key(key) if isinstance(key, str) else int(key)
        self.on_down = on_down
        self.on_up = on_up
        self._stop = threading.Event()
        self._down = False
        self._kb_hook: int | None = None
        self._ms_hook: int | None = None
        self._tid: int = 0
        self._thread: threading.Thread | None = None

        # Держим ссылки на HOOKPROC, чтобы ctypes не собрал их GC
        self._kb_callback = HOOKPROC(self._dispatch_kb)
        self._ms_callback = HOOKPROC(self._dispatch_mouse)

        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self.error: str | None = None

    # ------------------------------------------------------------------ #
    def set_key(self, key: str | int) -> int:
        """Динамически переключает отслеживаемую клавишу."""
        new_vk = parse_key(key) if isinstance(key, str) else int(key)
        if self._down and self.on_up:
            try:
                self.on_up()
            except Exception:
                pass
        self._down = False
        self.vk = new_vk
        return new_vk

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aidota-hotkey", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        self._tid = self._kernel32.GetCurrentThreadId()
        mod = self._kernel32.GetModuleHandleW(None)

        self._kb_hook = self._user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._kb_callback, mod, 0
        )
        self._ms_hook = self._user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._ms_callback, mod, 0
        )

        if not self._kb_hook and not self._ms_hook:
            self.error = f"Не удалось установить хуки ввода (код {self._kernel32.GetLastError()})"
            return

        message = wintypes.MSG()
        while not self._stop.is_set():
            result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                break
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

        if self._kb_hook:
            self._user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if self._ms_hook:
            self._user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None

    def _handle_state(self, is_down: bool) -> None:
        if is_down:
            if not self._down:
                self._down = True
                if self.on_down is not None:
                    try:
                        self.on_down()
                    except Exception:  # noqa: BLE001
                        pass
        else:
            if self._down:
                self._down = False
                if self.on_up is not None:
                    try:
                        self.on_up()
                    except Exception:  # noqa: BLE001
                        pass

    def _dispatch_kb(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == 0:  # HC_ACTION
            keyboard = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if keyboard.vkCode == self.vk:
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._handle_state(True)
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._handle_state(False)
        return self._user32.CallNextHookEx(self._kb_hook, n_code, w_param, l_param)

    def _dispatch_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == 0:  # HC_ACTION
            mouse = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            target_vk = None
            is_down = None

            if w_param in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                target_vk = VK_MBUTTON
                is_down = (w_param == WM_MBUTTONDOWN)
            elif w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                btn = (mouse.mouseData >> 16) & 0xFFFF
                target_vk = VK_XBUTTON1 if btn == 1 else VK_XBUTTON2
                is_down = (w_param == WM_XBUTTONDOWN)
            elif w_param in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                target_vk = VK_LBUTTON
                is_down = (w_param == WM_LBUTTONDOWN)
            elif w_param in (WM_RBUTTONDOWN, WM_RBUTTONUP):
                target_vk = VK_RBUTTON
                is_down = (w_param == WM_RBUTTONDOWN)

            if target_vk is not None and target_vk == self.vk and is_down is not None:
                self._handle_state(is_down)
        return self._user32.CallNextHookEx(self._ms_hook, n_code, w_param, l_param)

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stop.set()
        if self._tid:
            self._user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
