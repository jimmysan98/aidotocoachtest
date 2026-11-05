"""Поиск установленной Dota 2 и установка GSI-конфига."""

from __future__ import annotations

import re
from pathlib import Path

DOTA_DIR_NAMES = ("dota 2 beta",)
GSI_DATA = (
    ("buildings", "1"),
    ("provider", "1"),
    ("map", "1"),
    ("player", "1"),
    ("hero", "1"),
    ("abilities", "1"),
    ("items", "1"),
    ("draft", "1"),
    ("events", "1"),
    ("wearables", "1"),
)


def gsi_config_text(token: str, port: int = 3000, name: str = "AIDOTA assistant") -> str:
    lines = [
        f'"{name}"',
        "{",
        f'    "uri"       "http://127.0.0.1:{int(port)}/"',
        '    "timeout"   "5.0"',
        '    "buffer"    "0.1"',
        '    "throttle"  "0.1"',
        '    "heartbeat" "30.0"',
        '    "data"',
        "    {",
    ]
    lines += [f'        "{key}" "1"' for key, _ in GSI_DATA]
    lines += [
        "    }",
        '    "auth"',
        "    {",
        f'        "token" "{token}"',
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path = winreg.QueryValueEx(key, "SteamPath")[0]
            if steam_path:
                roots.append(Path(steam_path))
    except Exception:  # noqa: BLE001 — реестр может быть недоступен
        pass
    for candidate in (
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"D:\SteamLibrary",
        r"E:\SteamLibrary",
    ):
        roots.append(Path(candidate))
    return roots


def _library_paths(steam_root: Path) -> list[Path]:
    libraries: list[Path] = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = Path(match.group(1).replace("\\\\", "\\"))
        if path not in libraries:
            libraries.append(path)
    return libraries


def find_dota_installations() -> list[Path]:
    """Возвращает найденные каталоги игры «dota 2 beta»."""
    found: list[Path] = []
    seen: set[Path] = set()
    for root in _steam_roots():
        if not root.is_dir():
            continue
        for library in _library_paths(root):
            for name in DOTA_DIR_NAMES:
                candidate = library / "steamapps" / "common" / name
                try:
                    key = candidate.resolve()
                except OSError:
                    key = candidate
                if candidate.is_dir() and key not in seen:
                    seen.add(key)
                    found.append(candidate)
    return found


def cfg_dirs_for(install: Path) -> list[Path]:
    """Каталоги, куда Dota 2 кладёт gamestate_integration_*.cfg."""
    candidates = [
        install / "game" / "dota" / "cfg" / "gamestate_integration",
        install / "game" / "dota" / "cfg",
        install / "dota" / "cfg" / "gamestate_integration",
        install / "dota" / "cfg",
    ]
    return [path for path in candidates if path.is_dir()]


def find_gsi_dirs() -> list[Path]:
    result: list[Path] = []
    for install in find_dota_installations():
        result.extend(cfg_dirs_for(install))
    return result


def install_gsi(
    cfg_dir: Path,
    token: str,
    port: int = 3000,
    filename: str = "gamestate_integration_aidota.cfg",
) -> Path:
    cfg_dir = Path(cfg_dir)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / filename
    target.write_text(gsi_config_text(token, port), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Steam localconfig.vdf: авто-добавление -gamestateintegration
# --------------------------------------------------------------------------- #
def find_steam_localconfig_files() -> list[Path]:
    """Находит все localconfig.vdf пользователей Steam."""
    configs: list[Path] = []
    for root in _steam_roots():
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for user_dir in userdata.iterdir():
            if user_dir.is_dir() and user_dir.name.isdigit():
                cfg = user_dir / "config" / "localconfig.vdf"
                if cfg.is_file():
                    configs.append(cfg)
    return configs


def patch_localconfig_vdf(vdf_path: Path) -> bool:
    """Добавляет -gamestateintegration в параметры запуска Dota 2 (AppID 570)."""
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    if "-gamestateintegration" in text:
        return True  # Уже прописано

    # 1. Если есть блок 570
    match_570 = re.search(r'("570"\s*\{)([^}]*)(\})', text, re.IGNORECASE)
    if match_570:
        block_header = match_570.group(1)
        block_body = match_570.group(2)
        match_launch = re.search(r'("LaunchOptions"\s*")([^"]*)(")', block_body, re.IGNORECASE)
        if match_launch:
            old_val = match_launch.group(2)
            new_val = (old_val.strip() + " -gamestateintegration").strip()
            new_body = block_body[:match_launch.start(2)] + new_val + block_body[match_launch.end(2):]
        else:
            new_body = '\n                    "LaunchOptions"   "-gamestateintegration"' + block_body
        new_text = text[:match_570.start()] + block_header + new_body + match_570.group(3) + text[match_570.end():]
        try:
            vdf_path.write_text(new_text, encoding="utf-8")
            return True
        except OSError:
            return False

    # 2. Если блока 570 нет, но есть apps
    match_apps = re.search(r'("apps"\s*\{)', text, re.IGNORECASE)
    if match_apps:
        insertion = '\n                "570"\n                {\n                    "LaunchOptions"   "-gamestateintegration"\n                }'
        idx = match_apps.end()
        new_text = text[:idx] + insertion + text[idx:]
        try:
            vdf_path.write_text(new_text, encoding="utf-8")
            return True
        except OSError:
            return False

    return False


def add_dota_launch_option() -> list[Path]:
    """Прописывает параметр -gamestateintegration во все найденные localconfig.vdf."""
    patched: list[Path] = []
    for cfg in find_steam_localconfig_files():
        if patch_localconfig_vdf(cfg):
            patched.append(cfg)
    return patched


def install_gsi_full(config: Any) -> tuple[bool, str]:
    """Комплексная установка GSI: копирование cfg в Dota 2 + патч параметра запуска в Steam."""
    candidates = find_gsi_dirs()
    if getattr(config.dota, "cfg_dir", None):
        cfg_dir = config.path(config.dota.cfg_dir)
        if not cfg_dir.is_dir():
            return False, f"Каталог из config.json не найден: {cfg_dir}"
    elif candidates:
        cfg_dir = candidates[0]
    else:
        return False, (
            "Dota 2 не найдена на диске. Убедитесь, что игра установлена через Steam, "
            "или укажите dota.cfg_dir в config.json."
        )

    try:
        target = install_gsi(
            cfg_dir,
            config.gsi.token,
            config.gsi.port,
            config.dota.gsi_config_name,
        )
    except OSError as exc:
        return False, f"Не удалось записать конфиг в {cfg_dir}: {exc}"

    patched = add_dota_launch_option()
    launch_msg = (
        f"Параметр -gamestateintegration добавлен в {len(patched)} файл(ов) Steam localconfig.vdf."
        if patched
        else "Параметр -gamestateintegration уже был установлен или настройте его в Steam вручную."
    )
    return True, f"GSI успешно подключен!\nФайл: {target}\n{launch_msg}\nПерезапустите Dota 2."



# --------------------------------------------------------------------------- #
# Именованный Windows Mutex (Single Instance)
# --------------------------------------------------------------------------- #
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance(mutex_name: str = "Global\\AIDOTA_SingleInstance_Mutex") -> tuple[bool, Any]:
    """Возвращает (is_unique, handle). Если false — копия уже запущена."""
    import sys

    if sys.platform != "win32":
        return True, None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            return False, handle
        if handle == 0 or handle is None:
            # Fallback на Local Mutex, если Global запрещён политикой прав
            local_name = "Local\\AIDOTA_SingleInstance_Mutex"
            handle = kernel32.CreateMutexW(None, False, local_name)
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                return False, handle
        return True, handle
    except Exception:
        return True, None


def restore_existing_instance(window_title_part: str = "AIDOTA") -> bool:
    """Находит свёрнутое или скрытое окно AIDOTA и разворачивает его на передний план."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        found: list[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def enum_cb(hwnd: int, lparam: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if window_title_part.lower() in buf.value.lower():
                    found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        if found:
            hwnd = found[0]
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# Автозапуск Windows через реестр
# --------------------------------------------------------------------------- #
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "AIDOTA"


def is_autostart_enabled() -> bool:
    """Проверяет, включён ли автозапуск с Windows."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except Exception:
        return False


def set_autostart(enabled: bool, executable_path: str | None = None) -> bool:
    """Включает или выключает автозапуск с Windows."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                path = executable_path or sys.executable
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{path}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Steam shortcuts.vdf: добавление AIDOTA в сторонние игры Steam
# --------------------------------------------------------------------------- #
def find_steam_shortcuts_files() -> list[Path]:
    """Находит все shortcuts.vdf в Steam userdata."""
    shortcuts: list[Path] = []
    for root in _steam_roots():
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for user_dir in userdata.iterdir():
            if user_dir.is_dir() and user_dir.name.isdigit():
                cfg = user_dir / "config" / "shortcuts.vdf"
                if cfg.is_file() or (user_dir / "config").is_dir():
                    shortcuts.append(cfg)
    return shortcuts


def encode_steam_shortcut(
    idx: int,
    app_name: str,
    exe_path: Path,
    start_dir: Path,
    icon_path: Path | None = None,
) -> bytes:
    """Кодирует запись сторонней игры в бинарный формат Valve VDF."""
    import struct
    import zlib

    exe_str = str(exe_path.resolve())
    dir_str = str(start_dir.resolve())
    icon_str = str(icon_path.resolve()) if icon_path and icon_path.is_file() else ""

    # 32-битный appid по алгоритму Steam
    crc = zlib.crc32((exe_str + app_name).encode("utf-8")) | 0x80000000
    appid = crc & 0xFFFFFFFF if crc < 0x80000000 else crc - 0x100000000

    b = bytearray()
    b.extend(b"\x00" + str(idx).encode("ascii") + b"\x00")

    def _add_str(k: str, v: str) -> None:
        b.append(0x01)
        b.extend(k.encode("utf-8") + b"\x00")
        b.extend(v.encode("utf-8") + b"\x00")

    def _add_int(k: str, v: int) -> None:
        b.append(0x02)
        b.extend(k.encode("utf-8") + b"\x00")
        b.extend(struct.pack("<i", v))

    _add_int("appid", appid)
    _add_str("AppName", app_name)
    _add_str("Exe", f'"{exe_str}"')
    _add_str("StartDir", f'"{dir_str}"')
    _add_str("icon", icon_str)
    _add_str("ShortcutPath", "")
    _add_str("LaunchOptions", "")
    _add_int("IsHidden", 0)
    _add_int("AllowDesktopConfig", 1)
    _add_int("AllowOverlay", 1)
    _add_int("OpenVR", 0)
    _add_int("Devkit", 0)
    _add_str("DevkitGameID", "")
    _add_int("DevkitOverrideAppID", 0)
    _add_int("LastPlayTime", 0)
    _add_str("FlatpakAppID", "")
    b.extend(b"\x00tags\x00\x08")
    b.append(0x08)
    return bytes(b)


def add_to_steam_library(
    exe_path: Path | None = None,
    app_name: str = "AIDOTA",
) -> tuple[bool, str]:
    """Добавляет AIDOTA.exe в Steam shortcuts.vdf всех активных пользователей Steam."""
    import sys
    from .config import PROJECT_ROOT

    if exe_path is None:
        if getattr(sys, "frozen", False):
            target_exe = Path(sys.executable).resolve()
        else:
            candidates = [
                PROJECT_ROOT / "dist" / "AIDOTA" / "AIDOTA.exe",
                PROJECT_ROOT / "AIDOTA.exe",
            ]
            target_exe = next((c for c in candidates if c.is_file()), candidates[0])
    else:
        target_exe = Path(exe_path).resolve()

    start_dir = target_exe.parent
    icon_path = start_dir / "assets" / "icon.ico"
    if not icon_path.is_file():
        icon_path = PROJECT_ROOT / "assets" / "icon.ico"

    vdf_files = find_steam_shortcuts_files()
    if not vdf_files:
        return False, "Не найдены профили пользователей Steam. Убедитесь, что клиент Steam установлен."

    count_updated = 0
    for vdf_path in vdf_files:
        try:
            data = vdf_path.read_bytes() if vdf_path.is_file() else b"\x00shortcuts\x00\x08\x08"
            if not data.startswith(b"\x00shortcuts\x00"):
                data = b"\x00shortcuts\x00\x08\x08"

            # Проверяем, есть ли уже AIDOTA в shortcuts
            if b"AIDOTA" in data and str(target_exe).encode("utf-8") in data:
                continue

            # Подсчитываем сколько записей уже есть
            import re
            matches = list(re.finditer(rb"\x00(\d+)\x00\x01AppName\x00", data))
            next_idx = len(matches)

            # Резервная копия
            if vdf_path.is_file():
                bak = vdf_path.with_suffix(".vdf.bak")
                bak.write_bytes(data)

            entry_bytes = encode_steam_shortcut(next_idx, app_name, target_exe, start_dir, icon_path)

            if data.endswith(b"\x08\x08"):
                new_data = data[:-2] + entry_bytes + b"\x08\x08"
            elif data.endswith(b"\x08"):
                new_data = data[:-1] + entry_bytes + b"\x08"
            else:
                new_data = b"\x00shortcuts\x00" + entry_bytes + b"\x08\x08"

            vdf_path.parent.mkdir(parents=True, exist_ok=True)
            vdf_path.write_bytes(new_data)
            count_updated += 1
        except Exception:  # noqa: BLE001
            pass

    if count_updated > 0:
        return True, (
            f"AIDOTA успешно добавлена в библиотеку Steam ({count_updated} профиль)!\n\n"
            f"Исполняемый файл: {target_exe}\n\n"
            "ВАЖНО: Если Steam сейчас запущен, перезапустите его (Меню Steam -> Выход), "
            "чтобы AIDOTA появилась в списке ваших игр."
        )
    return True, (
        "AIDOTA уже зарегистрирована в вашей библиотеке Steam.\n"
        "Если ярлык не отображается, перезапустите клиент Steam."
    )


