"""Оркестратор AIDOTA: связывает ввод, GSI, OpenDota, Ollama, TTS и оверлей."""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import commands
from .audio import AudioError, VoiceInput
from .coach import Coach
from .commands import Intent
from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, save_config, save_llm_only
from .context import AutonomousCoachAgent, _dig, gsi_stale_hint
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
        # Колбэк «открыть настройки в главном окне» задаёт GUI-оболочка
        # (веб-дашборд или Tk-GUI). Отдельного окна настроек больше нет.
        self.on_open_settings: Any = None
        self._overlay: Overlay | None = (
            Overlay(
                config.overlay,
                PROJECT_ROOT,
                current_key=config.audio.ptt_key,
                ptt_enabled=config.audio.push_to_talk,
                on_key_change=self._on_overlay_key_change,
                on_settings_save=self._on_settings_save,
                on_test_api=self._on_test_api,
                on_test_audio=self._on_test_audio,
                on_test_match=self._on_test_match,
                full_config=self.config,
                on_open_settings=self._request_open_settings,
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
        coach_name = "Джарвис" if "dmitry" in str(getattr(config.tts, "edge_voice", "")).lower() else "Астра"
        self._coach = Coach(
            self._api,
            self._catalog,
            self._gsi,
            config.llm,
            config.player_profile,
            coach_name=coach_name,
            memory_size=config.memory_size,
        )
        autonomy = getattr(config.player_profile, "autonomy_level", "medium")
        self._agent = AutonomousCoachAgent(
            autonomy_level=autonomy,
            cooldown_seconds=45.0,
            skill_level=getattr(config.player_profile, "skill_level", "beginner"),
        )
        self._vision = HeroVision(config.vision, PROJECT_ROOT, self._catalog)
        self._worker: threading.Thread | None = None
        self._text_queue: queue.Queue[str] = queue.Queue()

    # ================================================================== #
    # Жизненный цикл
    # ================================================================== #
    def run(self) -> int:
        overlay_started = False
        if self._overlay is not None:
            overlay_started = self._overlay.start()
            if not overlay_started:
                self.log.warning("Оверлей недоступен: %s", self._overlay.error)

        # Запускаем глобальный хоткей для скрытия/показа окна (по умолчанию Alt+Z)
        if self._overlay is not None and self.config.overlay.enabled:
            toggle_key = getattr(self.config.overlay, "toggle_key", "alt+z") or "alt+z"
            try:
                from .hotkeys import HotkeyListener

                self._toggle_listener = HotkeyListener(
                    key="v",  # dummy
                    toggle_combo=toggle_key,
                    on_toggle=self._overlay.toggle,
                )
                self._toggle_listener.start()
                self.log.info("Глобальный хоткей скрытия/показа окна активен: [%s]", toggle_key)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось включить хоткей переключения оверлея: %s", exc)

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

    def start_background(self, parent: Any = None) -> bool:
        """Запускает ассистента в фоновом потоке для GUI-приложения."""
        overlay_started = False
        if self._overlay is not None:
            overlay_started = self._overlay.start(parent=parent)
            if not overlay_started:
                self.log.warning("Оверлей недоступен: %s", self._overlay.error)

        if self._overlay is not None and self.config.overlay.enabled:
            toggle_key = getattr(self.config.overlay, "toggle_key", "alt+z") or "alt+z"
            try:
                from .hotkeys import HotkeyListener

                self._toggle_listener = HotkeyListener(
                    key="v",  # dummy
                    toggle_combo=toggle_key,
                    on_toggle=self._overlay.toggle,
                )
                self._toggle_listener.start()
                self.log.info("Глобальный хоткей скрытия/показа окна активен: [%s]", toggle_key)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось включить хоткей переключения оверлея: %s", exc)

        worker = threading.Thread(target=self._bootstrap, name="aidota-worker", daemon=True)
        self._worker = worker
        worker.start()
        return True

    def is_gsi_connected(self) -> bool:
        """Возвращает True, если от Dota 2 поступают свежие данные GSI."""
        if self._gsi is None:
            return False
        payload = getattr(self._gsi, "last_payload", None)
        if payload is None:
            return False
        last_t = getattr(self._gsi, "_last_time", 0.0)
        return (time.time() - last_t) < 45.0

    def set_coach_name(self, name: str) -> None:
        """Переключает активного тренера (Астра / Джарвис) на лету."""
        self._coach.set_coach_name(name)
        if self._overlay is not None:
            self._overlay.set_coach_name(name)
        if name == "Джарвис":
            self.config.tts.edge_voice = "ru-RU-DmitryNeural"
        else:
            self.config.tts.edge_voice = "ru-RU-SvetlanaNeural"
        self._tts.config.edge_voice = self.config.tts.edge_voice
        self.config.player_profile.coach_name = name

    def set_position(self, pos: int) -> int:
        """Устанавливает игровую роль (позиции 1-5)."""
        res = self._coach.set_position(pos)
        self.config.player_profile.default_position = res
        return res

    def set_autonomy_level(self, level: str) -> None:
        """Переключает уровень автономности тренера ('high', 'medium', 'off')."""
        clean_lvl = level.strip().lower()
        if hasattr(self, "_agent") and self._agent is not None:
            self._agent.set_autonomy_level(clean_lvl)
        self.config.player_profile.autonomy_level = clean_lvl
        if self.config_path:
            save_config(self.config, self.config_path)

    def get_autonomy_level(self) -> str:
        """Возвращает текущий уровень автономности тренера."""
        if hasattr(self, "_agent") and self._agent is not None:
            return self._agent.autonomy_level
        return getattr(self.config.player_profile, "autonomy_level", "medium")

    def set_skill_level(self, level: str) -> str:
        """Применяет уровень игрока к тренеру, автономному агенту и конфигу."""
        normalized = self._coach.set_skill_level(level)
        try:
            setattr(self.config.player_profile, "skill_level", normalized)
        except Exception as exc:  # noqa: BLE001 — профиль не должен ронять настройку
            self.log.debug("Не удалось сохранить skill_level в конфиг: %s", exc)
        if hasattr(self, "_agent") and self._agent is not None:
            self._agent.set_skill_level(normalized)
        if self.config_path:
            try:
                save_config(self.config, self.config_path)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось сохранить config.json: %s", exc)
        return normalized

    def toggle_mic(self) -> bool:
        """Переключает микрофон между активным состоянием и паузой."""
        if self._voice.ready:
            new_mute = not self._voice.is_muted
            self._voice.set_muted(new_mute)
            return not new_mute
        return False

    def submit_text_command(self, text: str) -> None:
        """Выполняет текстовую команду пользователя из интерфейса."""
        clean = text.strip()
        if not clean:
            return
        if not self.stop_event.is_set():
            self._text_queue.put(clean)
        else:
            def _exec():
                coach_name = self._coach.coach_name
                intent = commands.parse_command(clean, self._catalog, active_trainer=coach_name)
                if intent is None:
                    lowered = clean.lower()
                    has_other = False
                    if coach_name.lower().startswith("астр") and bool(commands.JARVIS_REGEX.search(lowered)):
                        has_other = True
                    elif coach_name.lower().startswith("джарв") and bool(commands.ASTRA_REGEX.search(lowered)):
                        has_other = True
                    if has_other:
                        return
                    intent = commands.parse_command(clean, self._catalog, active_trainer=None)
                if intent is None or intent.kind == commands.UNKNOWN:
                    intent = commands.Intent(commands.CHAT, arg=clean, raw=clean)
                if self._overlay is not None:
                    self._overlay.add_user_message(clean)
                if hasattr(self, "on_log_message") and self.on_log_message:
                    try:
                        self.on_log_message("Вы", clean)
                    except Exception:
                        pass
                try:
                    self._handle(intent)
                except Exception as exc:  # noqa: BLE001
                    self.log.exception("Ошибка обработки команды: %s", exc)
                    self._say("Не удалось выполнить команду.")

            threading.Thread(target=_exec, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._overlay is not None:
            self._overlay.shutdown()

    def _shutdown(self) -> None:
        if hasattr(self, "_toggle_listener") and self._toggle_listener is not None:
            self._toggle_listener.stop()
            self._toggle_listener = None
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

            def _skills_monitor() -> None:
                while not self.stop_event.is_set():
                    try:
                        alert = self._coach.check_skills_alerts()
                        if alert:
                            self._say(alert)

                        payload = self._coach.gsi_payload()
                        if payload and self._overlay is not None:
                            hud_text = self._overlay_hud_text(payload)
                            if hud_text:
                                self._overlay.update_hud(hud_text)
                        if payload and hasattr(self, "_agent") and self._agent is not None:
                            agent_advice = self._agent.process_tick(
                                payload,
                                catalog=self._catalog,
                                items=self._api.items(),
                                position=self._coach.position,
                            )
                            if agent_advice:
                                self.log.info("Автономный совет тренера: %s", agent_advice)
                                self._say(agent_advice)
                    except Exception as exc:  # noqa: BLE001
                        self.log.debug("Ошибка в мониторе скиллов/автономного тренера: %s", exc)
                    self.stop_event.wait(1.0)

            threading.Thread(target=_skills_monitor, name="aidota-skills", daemon=True).start()

            self._loop()
        except Exception as exc:  # noqa: BLE001 — приложение не должно молча зависать
            self.log.exception("Критическая ошибка в рабочем потоке: %s", exc)
            self._say(
                f"Внимание: ошибка в рабочем потоке ({exc}). "
                "Перехожу в текстовый режим. Подробности в logs/aidota.log."
            )
            self._use_voice = False
            try:
                self._loop()
            except Exception as loop_exc:  # noqa: BLE001
                self.log.exception("Аварийный цикл остановлен: %s", loop_exc)
        finally:
            self.stop_event.set()
            if self._overlay is not None:
                self._overlay.close()

    def _load_catalog(self) -> None:
        try:
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
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Ошибка инициализации каталога героев: %s", exc)

    def _start_gsi(self) -> None:
        if self._gsi is None:
            self.log.info("GSI выключен в конфигурации")
            return
        try:
            self._gsi.start()
            self._status(f"GSI: {self.config.gsi.host}:{self.config.gsi.port}")
        except (GsiError, Exception) as exc:
            self.log.warning("GSI недоступен: %s", exc)
            self._say(
                "Внимание: не удалось запустить приёмник GSI (возможно, порт 3000 занят). "
                "Советы будут даваться без данных о текущем матче."
            )

    def _prepare_vision(self) -> None:
        if not self.config.vision.enabled:
            return
        try:
            icons_dir = self._vision.icons_dir
            count = count_icons(icons_dir)
            if self.config.vision.auto_download and count < 50:
                self._status("Скачиваю иконки героев…")
                try:
                    fetch_hero_icons(self._api, icons_dir, progress=self._status)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("Не удалось скачать иконки: %s", exc)
            self._vision.load()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Ошибка подготовки компьютерного зрения: %s", exc)

    def _start_voice(self) -> None:
        self._status("Загружаю модель распознавания речи…")
        try:
            self._voice.load_model(progress=self._status)
            self._voice.on_ptt_status = self._on_ptt_status
            self._voice.on_partial = self._on_partial
            try:
                self._voice.start()
            except (AudioError, Exception) as dev_exc:
                if self.config.audio.input_device is not None:
                    self.log.warning(
                        "Не удалось открыть микрофон %s (%s). Пробую микрофон по умолчанию...",
                        self.config.audio.input_device,
                        dev_exc,
                    )
                    self.config.audio.input_device = None
                    self._voice.config.input_device = None
                    self._voice.start()
                else:
                    raise dev_exc
            self._use_voice = True
            if self._voice.push_to_talk_enabled:
                self._status(f"Слушаю по клавише [{self._voice.ptt_key_label}] (зажмите, чтобы говорить)")
            else:
                self._status("Слушаю микрофон (постоянный ввод)")
        except (AudioError, Exception) as exc:
            self.log.warning("Голосовой ввод недоступен: %s", exc)
            self._use_voice = False
            self._say(
                f"Голосовой ввод недоступен ({exc}). "
                "Перехожу в текстовый режим — пишите команды в чат."
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

    def _request_open_settings(self) -> None:
        """Просит GUI-оболочку открыть вкладку «Настройки» (без отдельного окна)."""
        callback = self.on_open_settings
        if callable(callback):
            try:
                callback()
                return
            except Exception as exc:  # noqa: BLE001
                self.log.debug("Не удалось открыть вкладку настроек: %s", exc)
        if self._overlay is not None:
            self._overlay.update_status("⚙ Настройки — во вкладке «Настройки» главного окна")

    def _overlay_hud_text(self, payload: dict | None) -> str:
        """Компактная HUD-строка для оверлея: герой, уровень, время, золото, KDA."""
        if not isinstance(payload, dict):
            return ""
        map_data = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        hero = payload.get("hero") if isinstance(payload.get("hero"), dict) else {}
        parts: list[str] = []

        hero_name = ""
        npc = str(hero.get("name") or "")
        if npc:
            try:
                hero_name = self._catalog.display_name(npc) or ""
            except Exception:  # noqa: BLE001
                hero_name = ""
            if not hero_name:
                hero_name = npc.replace("npc_dota_hero_", "").replace("_", " ").title()
        if hero_name:
            parts.append(hero_name)
        level = _dig(payload, "hero", "level")
        if isinstance(level, int):
            parts.append(f"ур.{level}")
        if hero.get("alive") is False:
            respawn = _dig(payload, "hero", "respawn_seconds")
            parts.append(f"мёртв {respawn}с" if isinstance(respawn, (int, float)) else "мёртв")

        clock = map_data.get("clock_time")
        if isinstance(clock, (int, float)):
            total = max(0, int(clock))
            parts.append(f"{total // 60:02d}:{total % 60:02d}")

        gold = player.get("gold")
        if isinstance(gold, (int, float)):
            parts.append(f"Золото {int(gold)}")
        net_worth = player.get("net_worth")
        if isinstance(net_worth, (int, float)):
            parts.append(f"NW {int(net_worth)}")

        kills = _dig(payload, "player", "kills")
        deaths = _dig(payload, "player", "deaths")
        assists = _dig(payload, "player", "assists")
        if all(isinstance(v, int) for v in (kills, deaths, assists)):
            parts.append(f"KDA {kills}/{deaths}/{assists}")

        return "  ·  ".join(parts)

    def _on_test_api(self, data: dict[str, Any]) -> tuple[bool, str]:
        from .settings_gui import test_llm_connection

        return test_llm_connection(
            data.get("provider", ""),
            data.get("base_url", ""),
            data.get("api_key", ""),
            data.get("model", ""),
        )

    def _on_test_audio(self, device: int | str | None) -> tuple[bool, str]:
        from .settings_gui import play_test_chime

        return play_test_chime(device)

    def _on_settings_save(self, data: dict[str, Any]) -> None:
        self.log.info("Применение настроек: %s", {k: v for k, v in data.items() if k != "api_key"})
        # 1. LLM
        provider = data.get("provider") or self.config.llm.provider
        base_url = data.get("base_url") or self.config.llm.base_url
        api_key = data.get("api_key") if data.get("api_key") is not None else self.config.llm.api_key
        model = data.get("model") or self.config.llm.model

        self.config.llm.provider = provider
        self.config.llm.base_url = base_url
        self.config.llm.api_key = api_key
        self.config.llm.model = model

        prov_name = str(data.get("provider_name", "")).lower()
        matched_preset = None
        for p_name in ("deepseek", "openrouter", "groq", "openai", "ollama"):
            if p_name in prov_name:
                matched_preset = p_name
                break
        if matched_preset and matched_preset in self.config.llm.presets:
            preset = self.config.llm.presets[matched_preset]
            preset["api_key"] = api_key
            preset["base_url"] = base_url
            preset["model"] = model

        self._coach.llm.provider = provider
        self._coach.llm.base_url = base_url
        self._coach.llm.api_key = api_key
        self._coach.llm.model = model
        self._coach._models_cache = (0.0, [], None)

        # 2. Аудио ввод (микрофон)
        input_dev = data.get("input_device")
        if input_dev != self.config.audio.input_device:
            self.config.audio.input_device = input_dev
            try:
                dev_name = self._voice.set_input_device(input_dev)
                self.log.info("Микрофон переключён на: %s", dev_name)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Ошибка переключения микрофона: %s", exc)

        # 3. Аудио вывод и голос (TTS)
        output_dev = data.get("output_device")
        if output_dev != getattr(self.config.tts, "output_device", None):
            self.config.tts.output_device = output_dev
            try:
                self._tts.set_output_device(output_dev)
                self.log.info("Вывод звука переключён на: %s", output_dev)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Ошибка переключения вывода звука: %s", exc)

        if "tts_provider" in data:
            self.config.tts.provider = data["tts_provider"]
            self._tts.config.provider = data["tts_provider"]
        if "edge_voice" in data:
            self.config.tts.edge_voice = data["edge_voice"]
            self._tts.config.edge_voice = data["edge_voice"]

        if "coach_name" in data:
            self._coach.set_coach_name(data["coach_name"])
            if self._overlay is not None:
                self._overlay.set_coach_name(data["coach_name"])
        elif "edge_voice" in data:
            c_name = "Джарвис" if "dmitry" in str(data["edge_voice"]).lower() else "Астра"
            self._coach.set_coach_name(c_name)
            if self._overlay is not None:
                self._overlay.set_coach_name(c_name)


        # 4. Push-to-Talk
        if "push_to_talk" in data:
            ptt_enabled = bool(data["push_to_talk"])
            self.config.audio.push_to_talk = ptt_enabled
            self._voice.set_push_to_talk(ptt_enabled)

        # 5. Account ID игрока (Dota 2 Steam32) и позиция
        if "account_id" in data:
            raw_acc = data["account_id"]
            acc_val = None
            if raw_acc is not None and str(raw_acc).strip().isdigit() and int(raw_acc) > 0:
                acc_val = int(raw_acc)
            self.config.player_profile.account_id = acc_val
            self._coach.profile.account_id = acc_val

        if "default_position" in data:
            new_pos = int(data["default_position"])
            self.config.player_profile.default_position = new_pos
            self._coach.profile.default_position = new_pos
            self._coach.set_position(new_pos)

        # 6. Уровень автономности
        if "autonomy_level" in data:
            self.set_autonomy_level(str(data["autonomy_level"]))

        # 6b. Уровень игры (beginner / amateur / advanced / expert)
        if "skill_level" in data:
            self.set_skill_level(str(data["skill_level"]))

        # 7. Хоткей скрытия/показа окна
        if "toggle_key" in data and data["toggle_key"]:
            new_toggle = str(data["toggle_key"]).strip()
            self.config.overlay.toggle_key = new_toggle
            if hasattr(self, "_toggle_listener") and self._toggle_listener is not None:
                self._toggle_listener.set_toggle_combo(new_toggle)


        try:
            save_config(self.config, self.config_path)
            self.log.info("Конфигурация сохранена в %s", self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Не удалось сохранить config.json: %s", exc)

    def _on_test_match(self, account_id: int | None) -> tuple[bool, str]:
        try:
            res = self._coach.analyze_match(account_id=account_id)
            return True, res
        except Exception as exc:  # noqa: BLE001
            return False, f"Ошибка анализа матча: {exc}"

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
        try:
            parts = [f"{self._coach.coach_name} на связи, тренер готов к игре."]
            parts.append(f"Позиция: {position_label(self._coach.position)}.")
            if self.config.llm.provider == "openai_compatible" and not (self.config.llm.api_key or "").strip():
                parts.append(
                    "Внимание: API-ключ ИИ не указан. Нажмите кнопку ⚙ в окне AIDOTA, чтобы ввести ключ."
                )
                if self._overlay is not None:
                    self._overlay.add_ai_message(
                        "⚠️ Для получения советов через ИИ укажите API-ключ в Настройках (кнопка ⚙ в правом верхнем углу окна).\n"
                        "Поддерживаются DeepSeek, OpenRouter, Groq, OpenAI и другие.",
                        status="Требуется API-ключ",
                    )
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
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Ошибка при приветствии: %s", exc)

    # ================================================================== #
    # Главный цикл
    # ================================================================== #
    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                text = self._read_command()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Ошибка чтения команды: %s", exc)
                time.sleep(0.2)
                continue

            if text is None:
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)
                continue

            text = text.strip()
            if not text:
                continue

            coach_name = self._coach.coach_name
            is_ptt = (self._voice is not None and getattr(self._voice, "push_to_talk_enabled", False))
            from_text_input = not self._use_voice

            # 1. Проверяем строгий фильтр активного тренера
            intent = commands.parse_command(text, self._catalog, active_trainer=coach_name)

            if intent is None:
                # Проверяем, упоминался ли ДРУГОЙ тренер
                lowered = text.lower()
                has_other = False
                if coach_name.lower().startswith("астр"):
                    if bool(commands.JARVIS_REGEX.search(lowered)):
                        has_other = True
                elif coach_name.lower().startswith("джарв"):
                    if bool(commands.ASTRA_REGEX.search(lowered)):
                        has_other = True

                if has_other:
                    self.log.debug("Фраза адресована другому тренеру — проигнорирована: %s", text)
                    continue

                if is_ptt or from_text_input:
                    # В режиме Push-To-Talk или при прямом вводе текста пользователь обращается к тренеру напрямую
                    intent = commands.parse_command(text, self._catalog, active_trainer=None)
                    if intent is None or intent.kind == commands.UNKNOWN:
                        intent = commands.Intent(commands.CHAT, arg=text, raw=text)
                else:
                    # В режиме свободного микрофона: если имя тренера не названо — строго молчим
                    self.log.debug("Фраза без имени текущего тренера («%s») в открытом микрофоне — проигнорирована: %s", coach_name, text)
                    continue

            self.log.info("Ввод: %s", text)
            if self._use_voice:
                with self._console_lock:
                    print(f"Вы: {text}")
            if self._overlay is not None:
                self._overlay.add_user_message(text)
            if hasattr(self, "on_log_message") and self.on_log_message:
                try:
                    self.on_log_message("Вы", text)
                except Exception:
                    pass
            try:
                self._handle(intent)
            except Exception:  # noqa: BLE001 — одна команда не должна ронять цикл
                self.log.exception("Ошибка при обработке команды «%s»", text)
                self._say("Не получилось выполнить команду, подробности в logs/aidota.log.")

    def _read_command(self) -> str | None:
        try:
            return self._text_queue.get_nowait()
        except queue.Empty:
            pass

        if self._use_voice:
            try:
                return self._voice.listen(external_queue=self._text_queue)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Ошибка голосового ввода: %s. Перехожу в текстовый режим.", exc)
                self._use_voice = False
                return None

        # Текстовый режим: читаем из консоли ТОЛЬКО если stdin интерактивен
        if sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            try:
                return input("Вы: ")
            except (EOFError, KeyboardInterrupt, RuntimeError, OSError):
                return None

        # В GUI-режиме без микрофона ждём команду из очереди интерфейса
        try:
            return self._text_queue.get(timeout=0.3)
        except queue.Empty:
            return ""

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
        if kind == commands.GREETING:
            self._say("Да, я на связи! Чем помочь по игре?")
            return
        if kind == commands.ROSHAN_DOWN:
            self._say(self._coach.record_roshan())
            return
        if kind == commands.ENEMY_ULT:
            self._say(self._coach.record_ult(intent.arg))
            return
        if kind == commands.LANE_PLAN:
            enemy = None
            if hasattr(self, "_vision") and self._vision.available:
                det = self._vision.hero_under_cursor()
                if det:
                    enemy = det.get("name")
            self._say(self._coach.lane_plan(enemy_hero=enemy))
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
        if kind == commands.MATCH_ANALYSIS:
            self._status("Анализирую матч через OpenDota и ИИ…")
            match_arg = intent.arg
            match_id = int(match_arg) if (match_arg and match_arg.isdigit()) else None
            self._answer(self._coach.analyze_match(match_id=match_id))
            return
        if kind == commands.GAME_ANALYSIS:
            self._status("Разбираю игру и ищу ошибки…")
            self._answer(self._coach.game_analysis())
            return
        if kind == commands.BUY_NEXT:
            self._status("Подбираю следующий предмет…")
            self._answer(self._coach.buy_next())
            return
        if kind == commands.START_ITEMS:
            hero = self._resolve_hero(intent) or self._coach.current_hero()
            self._status("Подбираю стартовый закуп…")
            self._answer(self._coach.start_items(hero, user_query=intent.raw))
            return
        if kind == commands.SKILLS_ORDER:
            hero = self._resolve_hero(intent) or self._coach.current_hero()
            if intent.hero is not None:
                self._last_hero = intent.hero
            self._status("Смотрю прокачку способностей…")
            self._answer(self._coach.skills_order(hero, user_query=intent.raw))
            return
        if kind == commands.COUNTER_ITEMS:
            enemy_hero = intent.hero
            if not enemy_hero:
                self._say("Не понял, против какого героя подобрать предметы. Назовите героя, например: «что собрать против Джаггернаута».")
                return
            enemy_name = enemy_hero.get("localized_name") or enemy_hero.get("name")
            player_hero = self._coach.current_hero() or self._last_hero
            self._status(f"Подбираю предметы против {enemy_name}…")
            self._answer(self._coach.counter_advice(enemy_hero, player_hero, user_query=intent.raw))
            return
        if kind == commands.MID_SIX:
            self._say("Принято! Вражеский мидер шестого уровня. Не подставляйтесь на линиях, отойдите ближе к башне.")
            return
        if kind == commands.PULL_ALERT:
            payload = self._coach.gsi_payload()
            team = _dig(payload, "player", "team_name") if payload else None
            self._say(self._coach.pull_instructions(team))
            return
        if kind == commands.STACK_ALERT:
            self._say("Стакать нейтралов нужно на 53–55-й секунде каждой минуты: ударь крипов и уводи из лагеря.")
            return
        if kind == commands.WISDOM_ALERT:
            payload = self._coach.gsi_payload()
            clock_sec = _dig(payload, "map", "clock_time") if payload else None
            if clock_sec is not None and clock_sec >= 0:
                next_min = ((int(clock_sec) // 420) + 1) * 7
                rem_sec = next_min * 60 - int(clock_sec)
                self._say(f"Следующая руна мудрости появится на {next_min}-й минуте, осталось {rem_sec} секунд.")
            else:
                self._say("Руны мудрости появляются каждые 7 минут: на 7-й, 14-й, 21-й и 28-й минутах. Не забудьте забрать свою и украсть вражескую!")
            return

        if kind == commands.TORMENTOR_ALERT:
            payload = self._coach.gsi_payload()
            clock_sec = _dig(payload, "map", "clock_time") if payload else None
            if clock_sec is not None and clock_sec >= 0:
                if clock_sec < 1200:
                    rem_sec = 1200 - int(clock_sec)
                    rem_min = rem_sec // 60
                    self._say(f"Терзатель появится на 20-й минуте, осталось {rem_min} минут {rem_sec % 60} секунд.")
                else:
                    self._say("Терзатель уже доступен на карте! Заберите его всей командой за бесплатный Шард.")
            else:
                self._say("Терзатель появляется на двадцатой минуте и возрождается каждые 10 минут.")
            return

        if kind == commands.NEUTRALS_ALERT:
            payload = self._coach.gsi_payload()
            clock_sec = _dig(payload, "map", "clock_time") if payload else None
            if clock_sec is not None and clock_sec >= 0:
                sec = int(clock_sec)
                if sec < 420:
                    self._say(f"Нейтральные предметы 1-го разряда появятся на 7-й минуте, через {420 - sec} секунд.")
                elif sec < 1020:
                    self._say(f"Нейтральные предметы 2-го разряда появятся на 17-й минуте, через {(1020 - sec) // 60} минут.")
                elif sec < 1620:
                    self._say(f"Нейтральные предметы 3-го разряда появятся на 27-й минуте, через {(1620 - sec) // 60} минут.")
                elif sec < 2220:
                    self._say("Нейтральные предметы 4-го разряда появятся на 37-й минуте.")
                else:
                    self._say("Доступны нейтралки высших разрядов!")
            else:
                self._say("Разряды нейтральных предметов открываются на 7-й, 17-й, 27-й, 37-й и 60-й минутах.")
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

        if kind == commands.SETTINGS:
            self._request_open_settings()
            self._say(
                "Настройки — во вкладке «Настройки» главного окна AIDOTA.",
                speak=False,
            )
            return

        if kind == commands.SET_API_KEY:
            key_val = (intent.arg or "").strip()
            if not key_val:
                self._request_open_settings()
                self._say(
                    "Укажите API-ключ во вкладке «Настройки» главного окна "
                    "или скажите «апи ключ <ваш_ключ>»."
                )
                return
            self.config.llm.api_key = key_val
            self._coach.llm.api_key = key_val
            try:
                save_config(self.config, self.config_path)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Не удалось сохранить config.json: %s", exc)
            self._say("API-ключ сохранён и применён.")
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
            self._coach.set_manual_hero(hero)
            hero_name = hero.get("localized_name") or hero.get("name")
            self._status(f"Составляю план для {hero_name}…")
            answer = self._coach.hero_intro_advice(hero, user_query=intent.raw)
            self._answer(answer)
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

        if kind == commands.CHAT:
            query = (intent.arg or intent.raw).strip()
            self._status(f"{self._coach.coach_name} думает…")
            answer = self._coach.coach_chat(query, voice=self._use_voice)
            self._answer(answer)
            return

        raw_text = (intent.raw or "").strip()
        if raw_text:
            self._status(f"{self._coach.coach_name} думает…")
            answer = self._coach.coach_chat(raw_text, voice=self._use_voice)
            self._answer(answer)
            return

        self._say(
            "Не понял команду. Скажите «помощь» — я перечислю, что умею. "
            "Или назовите героя, наведя на него курсор."
        )

    def _handle_advice(self, intent: Intent) -> None:
        hero = self._resolve_hero(intent)
        if hero is None:
            query = (intent.raw or "дай совет по игре").strip()
            self._status(f"{self._coach.coach_name} думает…")
            answer = self._coach.coach_chat(query, voice=self._use_voice)
            self._answer(answer)
            return
        self._last_hero = hero
        self._status(f"Думаю про {hero.get('localized_name')}…")
        screen_context = self._vision_context(exclude=hero)
        answer = self._coach.advice(hero, screen_context, user_query=intent.raw)
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
            if hasattr(self, "on_log_message") and self.on_log_message:
                try:
                    self.on_log_message(self._coach.coach_name, text)
                except Exception:
                    pass
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
                    self._voice.discard_pending()
                if self._overlay is not None:
                    self._overlay.update_status(idle_status)
            if not ok and not self._tts_warned:
                self._tts_warned = True
                self.log.warning("Озвучка недоступна: %s", self._tts.error)
                with self._console_lock:
                    print(f"(озвучка недоступна: {self._tts.error})")
