"""AIDOTA — голосовой помощник по Dota 2.

Запуск:            python main.py
Текстовый режим:   python main.py --text
Диагностика:       python main.py --doctor
Установка GSI:     python main.py --install-gsi
Иконки героев:     python main.py --fetch-icons
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aidota import __version__
from aidota.config import Config, load_config, save_config
from aidota.logging_setup import get_logger, setup_logging

BANNER = r"""
   ___   ____  ____   ___  _____  ____
  / _ | /  _/ / __ \ / _ \/_  _/ / __ \
 / __ |/ _/  / /_/ // // / / /  / /_/ /
/_/ |_/____/ /_____//____/ /_/   \____/   v{version}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aidota",
        description="Голосовой помощник по Dota 2: советы, предметы, драфт, GSI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python main.py                 обычный запуск (голос + оверлей)\n"
            "  python main.py --text          команды с клавиатуры\n"
            "  python main.py --doctor        проверить окружение\n"
            "  python main.py --install-gsi   установить cfg для Dota 2\n"
            "  python main.py --fetch-icons   скачать иконки героев\n"
        ),
    )
    parser.add_argument("--config", type=Path, help="путь к config.json")
    parser.add_argument("--text", "--no-voice", action="store_true", dest="text",
                        help="текстовый режим: ввод команд с клавиатуры")
    parser.add_argument("--ptt", action="store_true",
                        help="режим push-to-talk: слушать только при зажатой клавише")
    parser.add_argument("--no-ptt", action="store_true",
                        help="отключить push-to-talk: постоянный микрофон")
    parser.add_argument("--ptt-key", type=str, default=None,
                        help="клавиша push-to-talk (по умолчанию из config.json, напр. v, f8, mouse4)")
    parser.add_argument("--no-tts", action="store_true", help="отключить озвучку")
    parser.add_argument("--no-overlay", action="store_true", help="без окна-оверлея")
    parser.add_argument("--no-gsi", action="store_true", help="не запускать приёмник GSI")
    parser.add_argument("--doctor", action="store_true", help="самодиагностика и выход")
    parser.add_argument("--online", action="store_true",
                        help="в --doctor проверить доступность OpenDota")
    parser.add_argument("--install-gsi", action="store_true",
                        help="установить gamestate_integration_aidota.cfg в Dota 2")
    parser.add_argument("--fetch-icons", action="store_true",
                        help="скачать/докачать иконки героев")
    parser.add_argument("--update-data", action="store_true",
                        help="обновить снапшоты героев и предметов с OpenDota")
    parser.add_argument("--log-level", default=None,
                        help="DEBUG, INFO, WARNING (по умолчанию из config.json)")
    parser.add_argument("--version", action="version", version=f"aidota {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — старые консоли
            pass

    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        config.log_level = args.log_level.upper()
    setup_logging(config.logs_path, config.log_level)
    log = get_logger("main")

    if args.no_tts:
        config.tts.enabled = False
    if args.no_overlay:
        config.overlay.enabled = False
    if args.no_gsi:
        config.gsi.enabled = False
    if args.ptt:
        config.audio.push_to_talk = True
    if args.no_ptt:
        config.audio.push_to_talk = False
    if args.ptt_key:
        config.audio.ptt_key = args.ptt_key

    if args.doctor:
        from aidota.doctor import run_doctor

        return run_doctor(config, online=args.online)
    if args.fetch_icons:
        return _fetch_icons(config)
    if args.update_data:
        return _update_data(config)
    if args.install_gsi:
        return _install_gsi(config)

    from aidota.hotkeys import format_key_name

    ptt_info = (
        f"Push-to-talk [{format_key_name(config.audio.ptt_key)}] (зажмите для ввода)"
        if config.audio.push_to_talk
        else "Свободный микрофон"
    )

    print(BANNER.format(version=__version__))
    print(f"  GSI:   {config.gsi.host}:{config.gsi.port} (токен {'задан' if config.gsi.token else 'пуст'})")
    print(f"  Модель Vosk: {config.audio.model_path}")
    print(f"  Ввод:  {ptt_info}")
    print(f"  Озвучка: {'включена' if config.tts.enabled else 'выключена'}")
    print(f"  LLM:   {config.llm.provider} / {config.llm.model}")
    print(f"  Позиция: {config.player_profile.default_position}")
    print(f"  Логи: {config.path(config.logs_dir) / 'aidota.log'}")
    print("  Подсказка: python main.py --doctor — проверка окружения\n")

    from aidota.app import Assistant

    assistant = Assistant(config, text_mode=args.text, config_path=args.config)
    try:
        return assistant.run()
    except KeyboardInterrupt:
        assistant.stop()
        log.info("Остановка по Ctrl+C")
        return 130


# --------------------------------------------------------------------------- #
def _fetch_icons(config: Config) -> int:
    from aidota.dota_api import DotaApi
    from aidota.icons import fetch_hero_icons

    api = DotaApi(config.data_path)
    api.load_snapshots()
    if not api.heroes:
        print("Скачиваю данные героев с OpenDota…")
        if not api.refresh_snapshots():
            print("Не удалось получить список героев: проверьте интернет.")
            return 1
    icons_dir = config.path(config.vision.icons_dir)
    downloaded, total = fetch_hero_icons(api, icons_dir, progress=print)
    print(f"Готово: скачано {downloaded} из {total} файлов в {icons_dir}")
    return 0


def _update_data(config: Config) -> int:
    from aidota.dota_api import DotaApi

    api = DotaApi(config.data_path)
    print("Скачиваю свежие данные с OpenDota…")
    if not api.refresh_snapshots():
        print(f"Не удалось обновить данные: {api.last_error}")
        return 1
    print(f"Готово: {len(api.heroes)} героев, {len(api.items())} предметов.")
    return 0


def _install_gsi(config: Config) -> int:
    from aidota.dota_install import find_gsi_dirs, install_gsi

    if config.dota.cfg_dir:
        cfg_dir = config.path(config.dota.cfg_dir)
        if not cfg_dir.is_dir():
            print(f"Каталог из config.json не найден: {cfg_dir}")
            return 2
    else:
        candidates = find_gsi_dirs()
        if not candidates:
            print(
                "Не нашёл установленную Dota 2.\n"
                "Установите игру через Steam или укажите dota.cfg_dir в config.json.\n"
                "Вручную: положите gamestate_integration_aidota.cfg в\n"
                "...\\steamapps\\common\\dota 2 beta\\game\\dota\\cfg\\gamestate_integration\\"
            )
            return 2
        cfg_dir = candidates[0]
    try:
        target = install_gsi(
            cfg_dir, config.gsi.token, config.gsi.port, config.dota.gsi_config_name
        )
    except OSError as exc:
        print(
            f"Не удалось записать конфиг в {cfg_dir}: {exc}\n"
            "Запустите установку из обычной консоли Windows с правами пользователя "
            "или укажите доступный каталог в dota.cfg_dir (config.json)."
        )
        return 2
    print(f"GSI-конфиг установлен: {target}")
    print(
        "\nОсталось два шага:\n"
        "  1. Steam → Dota 2 → Свойства → Параметры запуска: добавьте -gamestateintegration\n"
        "  2. Перезапустите Dota 2. Данные появятся в матче и на экране выбора героев.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
