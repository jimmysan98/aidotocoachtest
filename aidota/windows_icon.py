"""Управление иконкой приложения и интеграцией с панелью задач Windows.

Позволяет отображать собственную иконку AIDOTA (assets/icon.ico) в панели задач Windows
(вместо стандартного значка python.exe/pythonw.exe), переключателе Alt+Tab и строке заголовка.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from aidota import __version__
from aidota.config import PROJECT_ROOT

log = logging.getLogger("aidota.windows_icon")

APP_USER_MODEL_ID = f"AIDOTA.Dota2Coach.App.{__version__}"

# Win32 Constants
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_GCLP_HICON = -14
_GCLP_HICONSM = -34


def set_app_user_model_id() -> bool:
    """Устанавливает явный AppUserModelID для процесса в Windows.

    Это критически необходимо для Python-приложений: без этого вызова Windows
    группирует окна приложения под python.exe/pythonw.exe и показывает стандартный
    значок Python-файла в панели задач.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return hr == 0
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось установить AppUserModelID: %s", exc)
        return False


def get_app_icon_path() -> Path | None:
    """Возвращает абсолютный путь к файлу иконки приложения (.ico)."""
    candidates = (
        PROJECT_ROOT / "assets" / "icon.ico",
        Path(__file__).resolve().parent.parent / "assets" / "icon.ico",
        Path(sys.executable).parent / "assets" / "icon.ico",
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def apply_window_icon(hwnd: int) -> bool:
    """Отправляет WM_SETICON (BIG и SMALL) на указанный дескриптор окна (HWND)."""
    if sys.platform != "win32" or not hwnd:
        return False

    icon_path = get_app_icon_path()
    if not icon_path:
        return False

    try:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False

        icon_str = str(icon_path)

        # Загружаем иконки нужных размеров (32x32 для панели задач / Alt+Tab, 16x16 для заголовка)
        h_icon_big = user32.LoadImageW(0, icon_str, _IMAGE_ICON, 32, 32, _LR_LOADFROMFILE)
        h_icon_small = user32.LoadImageW(0, icon_str, _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)

        success = False
        if h_icon_big:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, h_icon_big)
            try:
                # Обновляем также класс окна
                if hasattr(user32, "SetClassLongPtrW"):
                    user32.SetClassLongPtrW(hwnd, _GCLP_HICON, h_icon_big)
                elif hasattr(user32, "SetClassLongW"):
                    user32.SetClassLongW(hwnd, _GCLP_HICON, h_icon_big)
            except Exception:
                pass
            success = True

        if h_icon_small:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, h_icon_small)
            try:
                if hasattr(user32, "SetClassLongPtrW"):
                    user32.SetClassLongPtrW(hwnd, _GCLP_HICONSM, h_icon_small)
                elif hasattr(user32, "SetClassLongW"):
                    user32.SetClassLongW(hwnd, _GCLP_HICONSM, h_icon_small)
            except Exception:
                pass
            success = True

        return success
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось применить иконку к HWND %s: %s", hwnd, exc)
        return False


def setup_window_handle(window: Any) -> bool:
    """Извлекает HWND из объекта окна (pywebview Window или Tk/CTk) и применяет иконку."""
    if sys.platform != "win32" or window is None:
        return False

    hwnd: int | None = None

    # pywebview: window.native — экземпляр WinForms Form
    native = getattr(window, "native", None)
    if native is not None:
        handle = getattr(native, "Handle", None)
        if handle is not None:
            try:
                hwnd = int(handle.ToInt64())
            except Exception:
                try:
                    hwnd = int(handle.ToInt32())
                except Exception:
                    hwnd = None

    # Tkinter / CustomTkinter: window.winfo_id()
    if hwnd is None and hasattr(window, "winfo_id"):
        try:
            hwnd = int(window.winfo_id())
        except Exception:
            hwnd = None

    if hwnd:
        return apply_window_icon(hwnd)
    return False
