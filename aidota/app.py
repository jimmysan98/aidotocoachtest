"""Оркестратор AIDOTA: связывает ввод, GSI, OpenDota, Ollama, TTS и оверлей."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import commands
from .audio import AudioError, VoiceInput
from .coach import Coach
from .commands import Intent
from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, save_config, save_llm_only
from .context import gsi_stale_hint
from .dota_api import DotaApi
from .gsi import GsiError, GsiServer
from .heroes import HeroCatalog, HeroMatch
from .icons import count_icons, fetch_hero_icons
from .logging_setup import get_logger
from .overlay import Overlay
from .roles import position_label
from .tts import Tts
from .vision import HeroVision

BANNER = """
   ___   ____  ____   ___  _____  ____
  / _ | /  _/ / __ \\ / _ \\/_  _/ / __ \\
 / __ |/ _/  / /_/ // // / / /  / /_/ /
/_/ |_/____/ /_____//____/ /_/   \\____/    v{version}
"""


class Assistant:
    def __init__(self, config: Config, *, text_mode: bool = False, config_path=None):
        self.config = config
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.log = get_logger("app")
        self.root: Path = PROJECT_ROOT
        self.stop_event = threading.Event()
        self.text_mode = text_mode

        self._say_lock = threading.Lock()
        self._console_lock = threading.Lock()
        self._tts_enabled = bool(config.tts.enabled)
        self._tts_warned = False
        self._last_answer = ""
        self._last_hero: dict | None = None
        self._use_voice = False
        self._overlay: Overlay | None = (
            Overlay(
                config.overlay,
                PROJECT_ROOT,
                current_key=config.audio.ptt_key,
                ptt_enabled=config.audio.push_to_talk,
                on_key_change=self._on_overlay_key_change,
            )
            if config.overlay.enabled
            else None
        )
        self._tts = Tts(config.tts, config.cache_path)
        self._voice = VoiceInput(config.audio, PROJECT_ROOT)
        self._api = DotaApi(config.data_path)
        self._catalog = HeroCatalog([])
        self._gsi: GsiServer | None = None
        if config.gsi.enabled:
            self._gsi = GsiServer(config.gsi.host, config.gsi.port, config.gsi.token)
        self._coach = Coach(
            self._api,
            self._catalog,
            self._gsi,
            config.llm,
            config.player_profile,
            memory_size=config.memory_size,
        )
        self._vision = HeroVision(config.vision, PROJECT_ROOT, self._catalog)
        self._worker: threading.Thread | None = None

    # ================================================================== #
    # Жизненный цикл
    # ================================================================== #
    def run(self) -> int:
        overlay_started = False
        if self._overlay is not None:
            overlay_started = self._overlay.start()
            if not overlay_started:
                self.log.warning("Оверлей недоступен: %s", self._overlay.error)

        worker = threading.Thread(target=self._bootstrap, name="aidota-worker", daemon=True)
        self._worker = worker
        worker.start()

        try:
            if overlay_started:
                self._overlay.run()  # type: ignore[union-attr]
            else:
                while not self.stop_event.is_set():
                    self.stop_event.wait(0.4)
        except KeyboardInterrupt:
            self.log.info("Ctrl+C")
        finally:
            self.stop_event.set()
            worker.join(timeout=3)
            self._shutdown()
        return 0

    def stop(self) -> None:
        self.stop_event.set()
        if self._overlay is not None:
            self._overlay.shutdown()

    def _shutdown(self) -> None:
        self._voice.close()
        self._tts.close()
        if self._gsi is not None:
            self._gsi.stop()
        if self._overlay is not None:
            self._overlay.close()
        self.log.info("AIDOTA остановлена")

    # ================================================================== #
    # Инициализация
    # ================================================================== #
    def _bootstrap(self) -> None:
        try:
            self._status("Запуск…")
            self._load_catalog()
            self._start_gsi()
            self._prepare_vision()

            if self.text_mode:
                self._use_voice = False
                self.log.info("Текстовый режим: команды вводятся с клавиатуры")
            else:
                self._start_voice()

            self._greet()
            self._refresh_catalog_in_background()
            self._loop()
        except Exception:  # noqa: BLE001 — приложение не должно молча зависать
            self.log.exception("Критическая ошибка в рабочем потоке")
            self._say(
                "Произошла критическая ошибка, подроби в logs/aidota.log. "
                "Запустите python main.py --doctor, чтобы проверить окружение."
            )
        finally:
            self.stop_event.set()
            if self._overlay is not None:
                self._overlay.close()

    def _load_catalog(self) -> None:
        self._api.load_snapshots()
        if not self._api.heroes:
            self._status("Загружаю данные героев с OpenDota…")
            if not self._api.refresh_snapshots():
                self.log.warning("Данные героев недоступны: %s", self._api.last_error)
        heroes = self._api.heroes
        if heroes:
            self._catalog = HeroCatalog(heroes)
            self._coach.catalog = self._catalog
            self._vision.catalog = self._catalog
            self.log.info("Каталог героев: %d", len(self._catalog))
        else:
            self.log.warning("Каталог героев пуст — команды по героям будут ограничены")

    def _start_gsi(self) -> None:
        if self._gsi is None:
            self.log.info("GSI выключен в конфигурации")
            return
        try:
            self._gsi.start()
            self._status(f"GSI: {self.config.gsi.host}:{self.config.gsi.port}")
        except GsiError as exc:
            self.log.warning("GSI недоступен: %s", exc)
            self._say(
                "Внимание: не удалось запустить приёмник GSI. "
                "Советы будут без данных о текущей игре."
            )

    def _prepare_vision(self) -> None:
        if not self.config.vision.enabled:
            return
        icons_dir = self._vision.icons_dir
        count = count_icons(icons_dir)
        if self.config.vision.auto_download and count < 50:
            self._status("Скачиваю иконки героев…")
            try:
                fetch_hero_icons(self._api, icons_dir, progress=self._status)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось скачать иконки: %s", exc)
        self._vision.load()

    def _start_voice(self) -> None:
        self._status("Загружаю модель распознавания речи (1–2 минуты)…")
        try:
            self._voice.load_model(progress=self._status)
            self._voice.on_ptt_status = self._on_ptt_status
            self._voice.on_partial = self._on_partial
            self._voice.start()
            self._use_voice = True
            if self._voice.push_to_talk_enabled:
                self._status(f"Слушаю по клавише [{self._voice.ptt_key_label}] (зажмите, чтобы говорить)")
            else:
                self._status("Слушаю микрофон (постоянный ввод)")
        except AudioError as exc:
            self.log.warning("Голосовой ввод недоступен: %s", exc)
            self._use_voice = False
            self._say(
                f"Голосовой ввод недоступен: {exc} "
                "Перехожу в текстовый режим — команды можно вводить с клавиатуры."
            )

    def _on_ptt_status(self, is_active: bool) -> None:
        if is_active:
            status_text = f"🎙 Слушаю… (зажата [{self._voice.ptt_key_label}])"
            if self._overlay is not None:
                self._overlay.update_status(status_text)
            with self._console_lock:
                print(f"\r{status_text} ", end="", flush=True)
        else:
            status_text = "⏳ Обрабатываю…"
            if self._overlay is not None:
                self._overlay.update_status(status_text)
                self._overlay.update_partial("")
            with self._console_lock:
                print(f"\r{status_text:<60}", flush=True)

    def _on_partial(self, partial: str) -> None:
        if self._overlay is not None:
            self._overlay.update_status(f"🎙 {partial[:35]}…")
            self._overlay.update_partial(partial)
        with self._console_lock:
            print(f"\r🎤 {partial[:70]:<70}", end="", flush=True)

    def _on_overlay_key_change(self, key_val: str) -> None:
        if key_val in ("free", "off", "none"):
            self._voice.set_push_to_talk(False)
            self.config.audio.push_to_talk = False
            self._say("Режим свободного микрофона включён.")
        else:
            try:
                label = self._voice.set_ptt_key(key_val)
                self._voice.set_push_to_talk(True)
                self.config.audio.push_to_talk = True
                self.config.audio.ptt_key = key_val
                self._say(f"Клавиша push-to-talk установлена: [{label}]. Зажмите её, чтобы говорить.")
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Ошибка смены клавиши: %s", exc)
                self._say(f"Не удалось назначить клавишу «{key_val}»: {exc}")
        try:
            save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Не удалось сохранить config.json: %s", exc)

    def _refresh_catalog_in_background(self) -> None:
        snapshot_at = self._api.snapshot_time
        if snapshot_at is None or time.time() - snapshot_at < 7 * 24 * 3600:
            return

        def refresh() -> None:
            if self._api.refresh_snapshots():
                self._catalog = HeroCatalog(self._api.heroes)
                self._coach.catalog = self._catalog
                self._vision.catalog = self._catalog
                self.log.info("Каталог героев обновлён в фоне")

        threading.Thread(target=refresh, name="aidota-refresh", daemon=True).start()

    def _greet(self) -> None:
        parts = ["Ассистент готов."]
        parts.append(f"Позиция: {position_label(self._coach.position)}.")
        if self._use_voice:
            if self._voice.push_to_talk_enabled:
                parts.append(
                    f"Режим push-to-talk: зажмите [{self._voice.ptt_key_label}], чтобы говорить. "
                    "Для смены клавиши скажите «клавиша [кнопка]»."
                )
            else:
                parts.append("Свободный микрофон: скажите «помощь», чтобы узнать команды.")
        else:
            parts.append("Текстовый режим: введите «помощь» и нажмите Enter.")
        age = self._coach.gsi_age()
        if age is None:
            parts.append(
                "GSI пока не получает данные: проверьте параметр -gamestateintegration "
                "и cfg-файл Dota 2."
            )
        elif age > self.config.gsi.stale_seconds:
            parts.append("Данные GSI давно не обновлялись.")
        else:
            parts.append("GSI подключён.")
        self._say(" ".join(parts))

    # ================================================================== #
    # Главный цикл
    # ================================================================== #
    def _loop(self) -> None:
        while not self.stop_event.is_set():
            text = self._read_command()
            if text is None:
                # Голос остановлен или stdin закрыт (EOF / Ctrl+Z) — выходим.
                break
            text = text.strip()
            if not text:
                continue
            self.log.info("Ввод: %s", text)
            if self._use_voice:
                with self._console_lock:
                    print(f"Вы: {text}")
            if self._overlay is not None:
                self._overlay.add_user_message(text)
            intent = commands.parse_command(text, self._catalog)
            try:
                self._handle(intent)
            except Exception:  # noqa: BLE001 — одна команда не должна ронять цикл
                self.log.exception("Ошибка при обработке команды «%s»", text)
                self._say("Не получилось выполнить команду, подробности в logs/aidota.log.")

    def _read_command(self) -> str | None:
        if self._use_voice:
            # Перед новым прослушиванием выбрасываем звук, накопившийся,
            # пока думали LLM/сеть или говорил синтезатор.
            self._voice.discard_pending()
            return self._voice.listen()
        try:
            line = input("Вы: ")
        except (EOFError, KeyboardInterrupt):
            return None
        return line

    # ================================================================== #
    # Обработка команд
    # ================================================================== #
    def _handle(self, intent: Intent) -> None:
        kind = intent.kind
        if kind == commands.EXIT:
            self._say("До связи. Хорошей игры!")
            self.stop_event.set()
            if self._overlay is not None:
                self._overlay.close()
            return
        if kind == commands.HELP:
            self._say(commands.help_text())
            return
        if kind == commands.META:
            self._status("Считаю мету…")
            self._answer(self._coach.meta())
            return
        if kind == commands.STATUS:
            self._answer(self._coach.status())
            return
        if kind == commands.DRAFT:
            self._status("Анализирую драфт…")
            self._answer(self._coach.draft_advice())
            return
        if kind == commands.PICK_ADVICE:
            self._status("Подбираю героя из вашего пула…")
            self._answer(self._coach.pick_advice())
            return
        if kind == commands.GAME_ANALYSIS:
            self._status("Разбираю игру и ищу ошибки…")
            self._answer(self._coach.game_analysis())
            return
        if kind == commands.BUY_NEXT:
            self._status("Подбираю следующий предмет…")
            self._answer(self._coach.buy_next())
            return
        if kind == commands.SET_POSITION:
            if intent.position is None:
                self._say("Не понял позицию. Скажите: я керри / я мид / я тройка / я саппорт.")
                return
            self._coach.set_position(intent.position)
            self._say(
                f"Позиция на этот матч: {position_label(intent.position)}. "
                "Советы буду давать под неё."
            )
            return
        if kind == commands.SWITCH_MODEL:
            changed, message = self._coach.switch_model(intent.arg)
            if changed:
                try:
                    save_llm_only(self.config, self.config_path)
                except Exception as exc:  # noqa: BLE001 — не роняем команду из-за сохранения
                    self.log.warning("Не удалось сохранить config.json: %s", exc)
        if kind == commands.SET_PTT_KEY:
            key_arg = intent.arg
            if not key_arg:
                self._say(
                    f"Текущая клавиша push-to-talk: [{self._voice.ptt_key_label}]. "
                    "Чтобы сменить, скажите: «клавиша v», «клавиша f8» или «клавиша mouse4»."
                )
                return
            try:
                new_label = self._voice.set_ptt_key(key_arg)
                self.config.audio.ptt_key = key_arg
                self.config.audio.push_to_talk = True
                self._voice.set_push_to_talk(True)
                try:
                    save_config(self.config, self.config_path)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("Не удалось сохранить config.json: %s", exc)
                if self._overlay is not None:
                    self._overlay.update_ptt_key(self.config.audio.ptt_key, True)
                self._say(f"Клавиша push-to-talk установлена: [{new_label}]. Зажмите её, чтобы говорить.")
            except ValueError as exc:
                self._say(f"Не удалось распознать клавишу «{key_arg}»: {exc}. Попробуйте: V, F8, CapsLock, Mouse4, Space.")
            return

        if kind == commands.TOGGLE_PTT:
            enable = (intent.arg == "on")
            self._voice.set_push_to_talk(enable)
            self.config.audio.push_to_talk = enable
            if self._overlay is not None:
                self._overlay.update_ptt_key(self.config.audio.ptt_key, enable)
            try:
                save_config(self.config, self.config_path)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось сохранить config.json: %s", exc)
            if enable:
                self._say(f"Режим push-to-talk включён. Зажмите [{self._voice.ptt_key_label}], чтобы говорить.")
            else:
                self._say("Режим свободного микрофона включён. Слушаю всё время.")
            return

        if kind in (commands.ITEMS, commands.ADVICE):
            self._handle_advice(intent)
            return
        if kind == commands.SELECT_HERO:
            hero = intent.hero
            if hero is None:
                self._say("Не расслышал героя. Назовите его ещё раз или наведите курсор.")
                return
            self._last_hero = hero
            self._say(f"Запомнил: {hero.get('localized_name')}.")
            return
        if kind == commands.SILENCE:
            self._tts_enabled = False
            self._say("Хорошо, дальше без озвучки.", speak=False)
            return
        if kind == commands.VOICE:
            if self.config.tts.enabled:
                self._tts_enabled = True
                self._say("Озвучка включена.")
            else:
                self._say("Озвучка отключена в config.json (tts.enabled = false).", speak=False)
            return
        if kind == commands.REPEAT:
            if self._last_answer:
                self._say(self._last_answer)
            else:
                self._say("Повторять пока нечего.")
            return
        if kind == commands.OVERLAY_HIDE:
            if self._overlay is not None:
                self._overlay.hide()
            self._say("Окно скрыто. Скажите «покажи окно», чтобы вернуть.", speak=False)
            return
        if kind == commands.OVERLAY_SHOW:
            if self._overlay is not None:
                self._overlay.show()
            self._say("Окно снова на экране.", speak=False)
            return

        self._say(
            "Не понял команду. Скажите «помощь» — я перечислю, что умею. "
            "Или назовите героя, наведя на него курсор."
        )

    def _handle_advice(self, intent: Intent) -> None:
        hero = self._resolve_hero(intent)
        if hero is None:
            hero = self._ask_hero()
        if hero is None:
            self._say(
                "Не понял, о каком герое речь. Наведите курсор на героя в меню "
                "или скажите, например: «что купить на пудже»."
            )
            return
        self._last_hero = hero
        self._status(f"Думаю про {hero.get('localized_name')}…")
        screen_context = self._vision_context(exclude=hero)
        answer = self._coach.advice(hero, screen_context)
        self._answer(answer)

    def _answer(self, text: str) -> None:
        self._last_answer = text
        self._say(text)

    def _resolve_hero(self, intent: Intent) -> dict | None:
        if intent.hero is not None:
            return intent.hero
        current = self._coach.current_hero()
        if current is not None:
            return current
        vision_hero = self._vision_hero()
        if vision_hero is not None:
            return vision_hero
        return self._last_hero

    def _vision_hero(self) -> dict | None:
        if not self._vision.available:
            return None
        try:
            result = self._vision.hero_under_cursor()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("Ошибка определения героя: %s", exc)
            return None
        if not result:
            return None
        match = self._catalog.find(result["name"])
        return match.hero if match else None

    def _vision_context(self, exclude: dict | None = None) -> str:
        if not self._vision.available:
            return ""
        try:
            result = self._vision.hero_under_cursor()
        except Exception:  # noqa: BLE001
            return ""
        if not result:
            return ""
        if exclude is not None and result["name"] == exclude.get("localized_name"):
            return ""
        return f"под курсором герой {result['name']}"

    def _ask_hero(self) -> dict | None:
        self._say("Назовите героя, пожалуйста.", speak=False)
        text = self._read_command()
        if not text:
            return None
        text = text.strip()
        if self._overlay is not None and text:
            self._overlay.add_user_message(text)
        match: HeroMatch | None = self._catalog.find_in_tokens(text) if self._catalog.available else None
        if match is None and self._catalog.available:
            match = self._catalog.find(text)
        return match.hero if match else None

    # ================================================================== #
    # Вывод
    # ================================================================== #
    def _status(self, text: str) -> None:
        with self._console_lock:
            print(f"… {text}")
        if self._overlay is not None:
            self._overlay.update_status(text)

    def _say(self, text: str, *, speak: bool = True) -> None:
        text = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
        if not text:
            return
        idle_status = (
            f"Зажмите [{self._voice.ptt_key_label}]…"
            if (self._use_voice and self._voice.push_to_talk_enabled)
            else ("Слушаю…" if self._use_voice else "Текстовый режим")
        )
        with self._say_lock:
            with self._console_lock:
                print()
                print("AIDOTA: " + text)
                print("-" * 64)
            if self._overlay is not None:
                self._overlay.update(text, status=idle_status)
            if not (speak and self._tts_enabled):
                return
            if self._voice.ready:
                self._voice.set_muted(True)
            if self._overlay is not None:
                self._overlay.update_status("Озвучиваю…")
            try:
                ok = self._tts.speak(text, wait=True)
            finally:
                if self._voice.ready:
                    self._voice.set_muted(False)
                if self._overlay is not None:
                    self._overlay.update_status(idle_status)
            if not ok and not self._tts_warned:
                self._tts_warned = True
                self.log.warning("Озвучка недоступна: %s", self._tts.error)
                with self._console_lock:
                    print(f"(озвучка недоступна: {self._tts.error})")
