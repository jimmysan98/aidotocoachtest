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
