"""Глобальный перехват клавиш (push-to-talk, переключение оверлея Alt+Z и др.) через хуки Windows.

Поддерживает:
- Одиночные клавиши (V, C, Space, CapsLock, F1-F24 и т.д.)
- Кнопки мыши (Mouse 1..5, колёсико)
- Комбинации клавиш (Alt+Z, Ctrl+Shift+O, Win+X и т.д.)
Работает даже когда фокус в игре (Dota 2), на чистом ctypes без сторонних библиотек.
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
WM_HOTKEY = 0x0312
WM_USER = 0x0400
WM_UPDATE_HOTKEY = WM_USER + 101

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
TOGGLE_HOTKEY_ID = 1001

# Виртуальные коды мыши в Windows
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04   # Колёсико мыши
VK_XBUTTON1 = 0x05  # Mouse 4 (боковая кнопка назад)
VK_XBUTTON2 = 0x06  # Mouse 5 (боковая кнопка вперёд)

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

_RU_TO_EN: dict[str, str] = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u", "ш": "i", "щ": "o", "з": "p",
    "х": "[", "ъ": "]", "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k",
    "д": "l", "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": ".",
}

_CYRILLIC_KEYSYMS: dict[str, str] = {
    "cyrillic_a": "ф", "cyrillic_be": "б", "cyrillic_ve": "в", "cyrillic_ghe": "г",
    "cyrillic_de": "д", "cyrillic_ie": "е", "cyrillic_io": "ё", "cyrillic_zhe": "ж",
    "cyrillic_ze": "з", "cyrillic_i": "и", "cyrillic_shorti": "й", "cyrillic_ka": "к",
    "cyrillic_el": "л", "cyrillic_em": "м", "cyrillic_en": "н", "cyrillic_o": "о",
    "cyrillic_pe": "п", "cyrillic_er": "р", "cyrillic_es": "с", "cyrillic_te": "т",
    "cyrillic_u": "у", "cyrillic_ef": "ф", "cyrillic_ha": "х", "cyrillic_tse": "ц",
    "cyrillic_che": "ч", "cyrillic_sha": "ш", "cyrillic_shcha": "щ", "cyrillic_hardsign": "ъ",
    "cyrillic_yeru": "ы", "cyrillic_softsign": "ь", "cyrillic_e": "э", "cyrillic_yu": "ю",
    "cyrillic_ya": "я",
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
        raise ValueError("Клавиша не задана")
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

    # Русские keysym от Tkinter (например cyrillic_em -> м)
    if text in _CYRILLIC_KEYSYMS:
        text = _CYRILLIC_KEYSYMS[text]

    # Русская буква -> английская раскладка (м -> v, ф -> a и т.д.)
    if text in _RU_TO_EN:
        text = _RU_TO_EN[text]

    if len(text) == 1 and text.isalnum():
        return ord(text.upper())

    raise ValueError(f"Неизвестная клавиша: {name}")


def parse_combination(text: str | None) -> tuple[int, bool, bool, bool]:
    """Разбирает комбинацию клавиш (например 'alt+z', 'ctrl+shift+f1', 'alt+v').

    Возвращает кортеж (key_vk, alt_required, ctrl_required, shift_required).
    """
    if not text:
        raise ValueError("Комбинация клавиш не задана")
    normalized = str(text).strip()
    if "+" in normalized:
        parts = [p.strip().lower() for p in normalized.split("+") if p.strip()]
    elif " " in normalized and any(m in normalized.lower() for m in ("alt", "ctrl", "shift", "альт", "контрол", "шифт")):
        parts = [p.strip().lower() for p in normalized.split() if p.strip()]
    else:
        parts = [normalized.lower()]
    if not parts:
        raise ValueError("Пустая комбинация клавиш")

    alt_required = False
    ctrl_required = False
    shift_required = False
    main_key = None

    for p in parts:
        if p in ("alt", "альт", "алт"):
            alt_required = True
        elif p in ("ctrl", "control", "контрол", "контроль"):
            ctrl_required = True
        elif p in ("shift", "шифт"):
            shift_required = True
        else:
            main_key = p

    if main_key is None:
        # Если комбинация только из модификаторов (например 'alt')
        if alt_required:
            return (VK_MENU, False, False, False)
        if ctrl_required:
            return (VK_CONTROL, False, False, False)
        if shift_required:
            return (VK_SHIFT, False, False, False)
        raise ValueError(f"Не удалось определить основную клавишу в «{text}»")

    key_vk = parse_key(main_key)
    return (key_vk, alt_required, ctrl_required, shift_required)


def parse_hotkey(combo: str) -> tuple[int, int]:
    """Разбирает комбинацию на Win32 модификаторы (MOD_*) и vkCode."""
    vk, alt, ctrl, shift = parse_combination(combo)
    mods = MOD_NOREPEAT
    if alt:
        mods |= MOD_ALT
    if ctrl:
        mods |= MOD_CONTROL
    if shift:
        mods |= MOD_SHIFT
    return mods, vk


def format_key_name(key: int | str) -> str:
    """Возвращает понятное человеку отображение клавиши (например, 'V', 'Alt+Z', 'Mouse 4')."""
    if isinstance(key, str) and "+" in key:
        try:
            vk, alt, ctrl, shift = parse_combination(key)
            parts = []
            if ctrl:
                parts.append("Ctrl")
            if alt:
                parts.append("Alt")
            if shift:
                parts.append("Shift")
            parts.append(format_key_name(vk))
            return "+".join(parts)
        except Exception:
            return key.upper()

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


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# Настройка 64-битных сигнатур Win32 API (предотвращает усечение указателей и ошибку 126)
_k32 = ctypes.windll.kernel32
_u32 = ctypes.windll.user32

_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

_k32.GetCurrentThreadId.restype = wintypes.DWORD
_k32.GetCurrentThreadId.argtypes = []

_k32.GetLastError.restype = wintypes.DWORD
_k32.GetLastError.argtypes = []

_u32.SetWindowsHookExW.restype = wintypes.HHOOK
_u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]

_u32.UnhookWindowsHookEx.restype = wintypes.BOOL
_u32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

_u32.CallNextHookEx.restype = ctypes.c_longlong
_u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]

_u32.GetMessageW.restype = wintypes.BOOL
_u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

_u32.TranslateMessage.restype = wintypes.BOOL
_u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

_u32.DispatchMessageW.restype = ctypes.c_longlong
_u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

_u32.PostThreadMessageW.restype = wintypes.BOOL
_u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

_u32.RegisterHotKey.restype = wintypes.BOOL
_u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]

_u32.UnregisterHotKey.restype = wintypes.BOOL
_u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

_u32.GetAsyncKeyState.restype = wintypes.SHORT
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]

_u32.MapVirtualKeyW.restype = wintypes.UINT
_u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


class HotkeyListener:
    """Отслеживает нажатие/отпускание глобальной клавиши или кнопки мыши.

    Поддерживает:
    - обычную клавишу push-to-talk (on_down / on_up)
    - дополнительный хоткей переключения оверлея (например Alt+Z через on_toggle)
    """

    def __init__(
        self,
        key: str | int,
        on_down: Callable[[], None] | None = None,
        on_up: Callable[[], None] | None = None,
        toggle_combo: str | None = None,
        on_toggle: Callable[[], None] | None = None,
    ):
        self.vk = parse_key(key) if isinstance(key, str) else int(key)
        self.scan_code: int = 0
        self.toggle_scan_code: int = 0
        try:
            self.scan_code = int(ctypes.windll.user32.MapVirtualKeyW(self.vk, 0))
        except Exception:
            self.scan_code = 0
        self.on_down = on_down
        self.on_up = on_up

        self.on_toggle = on_toggle
        self.toggle_vk: int | None = None
        self.toggle_alt: bool = False
        self.toggle_ctrl: bool = False
        self.toggle_shift: bool = False

        self._hotkey_registered = False
        self._stop = threading.Event()
        self._down = False
        self._toggle_down = False
        self._kb_hook: int | None = None
        self._ms_hook: int | None = None
        self._tid: int = 0
        self._thread: threading.Thread | None = None

        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self.error: str | None = None

        # Держим ссылки на HOOKPROC, чтобы ctypes не собрал их GC
        self._kb_callback = HOOKPROC(self._dispatch_kb)
        self._ms_callback = HOOKPROC(self._dispatch_mouse)

        if toggle_combo:
            self.set_toggle_combo(toggle_combo)

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
        try:
            self.scan_code = int(self._user32.MapVirtualKeyW(new_vk, 0))
        except Exception:
            self.scan_code = 0
        return new_vk

    def set_toggle_combo(self, combo: str | None) -> None:
        """Устанавливает комбинацию для переключения скрытия/показа окна (например 'alt+z')."""
        if not combo:
            self.toggle_vk = None
            self.toggle_scan_code = 0
        else:
            try:
                vk, alt, ctrl, shift = parse_combination(combo)
                self.toggle_vk = vk
                self.toggle_alt = alt
                self.toggle_ctrl = ctrl
                self.toggle_shift = shift
                try:
                    self.toggle_scan_code = int(self._user32.MapVirtualKeyW(vk, 0))
                except Exception:
                    self.toggle_scan_code = 0
            except Exception:
                self.toggle_vk = None
                self.toggle_scan_code = 0

        if self._tid:
            try:
                self._user32.PostThreadMessageW(self._tid, WM_UPDATE_HOTKEY, 0, 0)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aidota-hotkey", daemon=True)
        self._thread.start()
        return True

    def _register_hotkey_native(self) -> None:
        if self.toggle_vk is not None:
            mods = MOD_NOREPEAT
            if self.toggle_alt:
                mods |= MOD_ALT
            if self.toggle_ctrl:
                mods |= MOD_CONTROL
            if self.toggle_shift:
                mods |= MOD_SHIFT
            res = self._user32.RegisterHotKey(None, TOGGLE_HOTKEY_ID, mods, self.toggle_vk)
            self._hotkey_registered = bool(res)

    def _unregister_hotkey_native(self) -> None:
        if self._hotkey_registered:
            try:
                self._user32.UnregisterHotKey(None, TOGGLE_HOTKEY_ID)
            except Exception:
                pass
            self._hotkey_registered = False

    def _run(self) -> None:
        self._tid = self._kernel32.GetCurrentThreadId()
        mod = self._kernel32.GetModuleHandleW(None)

        self._register_hotkey_native()

        self._kb_hook = self._user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._kb_callback, mod, 0
        )
        self._ms_hook = self._user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._ms_callback, mod, 0
        )

        if not self._kb_hook and not self._ms_hook and not self._hotkey_registered:
            self.error = f"Не удалось установить хуки ввода (код {self._kernel32.GetLastError()})"
            return

        message = wintypes.MSG()
        while not self._stop.is_set():
            result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                break

            if message.message == WM_HOTKEY:
                if message.wParam == TOGGLE_HOTKEY_ID:
                    if self.on_toggle is not None:
                        try:
                            self.on_toggle()
                        except Exception:  # noqa: BLE001
                            pass
                continue
            elif message.message == WM_UPDATE_HOTKEY:
                self._unregister_hotkey_native()
                self._register_hotkey_native()
                continue

            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

        self._unregister_hotkey_native()

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

    def _is_mod_pressed(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _dispatch_kb(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == 0:  # HC_ACTION
            keyboard = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = keyboard.vkCode
            is_key_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_key_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

            # Получаем виртуальный код из аппаратного скан-кода (раскладко-независимо)
            mapped_vk = 0
            if keyboard.scanCode:
                try:
                    mapped_vk = int(self._user32.MapVirtualKeyW(keyboard.scanCode, 1))
                except Exception:
                    mapped_vk = 0

            # 1. Проверяем комбинацию скрытия/показа окна (резерв, если не сработал RegisterHotKey)
            is_toggle_match = (
                self.toggle_vk is not None
                and (
                    vk == self.toggle_vk
                    or (bool(getattr(self, "toggle_scan_code", 0)) and keyboard.scanCode == self.toggle_scan_code)
                    or (mapped_vk == self.toggle_vk)
                )
            )
            if not self._hotkey_registered and is_toggle_match:
                alt_down = (
                    bool(keyboard.flags & 0x20)
                    or (w_param in (WM_SYSKEYDOWN, WM_SYSKEYUP))
                    or self._is_mod_pressed(VK_MENU)
                    or self._is_mod_pressed(0xA4)
                    or self._is_mod_pressed(0xA5)
                )
                ctrl_down = (
                    self._is_mod_pressed(VK_CONTROL)
                    or self._is_mod_pressed(0xA2)
                    or self._is_mod_pressed(0xA3)
                )
                shift_down = (
                    self._is_mod_pressed(VK_SHIFT)
                    or self._is_mod_pressed(0xA0)
                    or self._is_mod_pressed(0xA1)
                )

                alt_ok = alt_down if self.toggle_alt else not alt_down
                ctrl_ok = ctrl_down if self.toggle_ctrl else not ctrl_down
                shift_ok = shift_down if self.toggle_shift else not shift_down

                if is_key_down and alt_ok and ctrl_ok and shift_ok:
                    if not self._toggle_down:
                        self._toggle_down = True
                        if self.on_toggle is not None:
                            try:
                                self.on_toggle()
                            except Exception:  # noqa: BLE001
                                pass
                elif is_key_up:
                    self._toggle_down = False

            # 2. Основная клавиша Push-to-Talk (проверяем виртуальный код, скан-код и раскладко-независимый маппинг)
            is_ptt_match = (
                (vk == self.vk)
                or (bool(getattr(self, "scan_code", 0)) and keyboard.scanCode == self.scan_code)
                or (mapped_vk == self.vk)
            )
            if is_ptt_match:
                if is_key_down:
                    self._handle_state(True)
                elif is_key_up:
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
