"""Тесты уровня игры (skill_level) в конфиге и интерфейсах настроек.

Проверяем контракт E1 (в обновлённом виде — четыре уровня):
- ``PlayerProfile.skill_level`` по умолчанию "beginner";
- нормализация алиасов к ``beginner | amateur | advanced | expert``;
- сохранение/загрузка через config.json, сброс к эталонному пресету;
- веб-backend: ``get_state`` отдаёт уровень, ``save_settings`` принимает,
  нормализует, сохраняет и применяет к живому тренеру;
- Tk-диалог настроек: выпадающий список «Уровень игры» загружает текущее
  значение и отдаёт ``data["skill_level"]`` при сохранении;
- ``main.py --settings`` (on_save) сохраняет уровень в профиль.

Тесты не ходят в сеть; Tk-диалог проверяется без реального mainloop
(по образцу tests/test_settings_gui.py).
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aidota.config import (
    SKILL_LEVEL_LABELS,
    SKILL_LEVELS,
    Config,
    load_config,
    normalize_skill_level,
    reset_to_defaults,
    save_config,
)
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import make_temp_dir, remove_temp_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SkillLevelConfigTests(unittest.TestCase):
    """config.py: дефолт, нормализация, roundtrip через config.json."""

    def setUp(self) -> None:
        self.temp_dir = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.temp_dir)

    def _write(self, data: dict, name: str = "config.json") -> Path:
        path = self.temp_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_default_skill_level_is_beginner(self) -> None:
        """Дефолт профиля — новичок; в asdict() поле присутствует и известно UI."""
        config = Config()
        self.assertEqual(config.player_profile.skill_level, "beginner")
        self.assertEqual(_defaults_dict_skill_level(), "beginner")
        self.assertEqual(SKILL_LEVELS, ("beginner", "amateur", "advanced", "expert"))
        for code in SKILL_LEVELS:
            self.assertTrue(SKILL_LEVEL_LABELS[code].strip())

    def test_normalize_aliases(self) -> None:
        """Русские/английские алиасы и подписи -> канонические коды."""
        cases = {
            "новичок": "beginner",
            "Новичок": "beginner",
            "нуб": "beginner",
            "любитель": "amateur",
            "средний": "amateur",
            "опытный": "advanced",
            "продвинутый": "advanced",
            "advanced": "advanced",
            "профи": "expert",
            "Профи": "expert",
            "про": "expert",
            "эксперт": "expert",
            "expert": "expert",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_skill_level(raw), expected)

    def test_normalize_invalid_values_to_beginner(self) -> None:
        """Невалидные значения не ломают загрузку и трактуются как новичок."""
        for raw in (None, "", "   ", "не знаю", "супер-мега-про", [], {}, True, 99, -1):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_skill_level(raw), "beginner")

    def test_normalize_case_whitespace_and_numbers(self) -> None:
        """Регистр/пробелы и числовые коды 1–4 из старых конфигов."""
        self.assertEqual(normalize_skill_level("  EXPERT  "), "expert")
        self.assertEqual(normalize_skill_level(" Advanced "), "advanced")
        for raw, expected in (("1", "beginner"), ("2", "amateur"), ("3", "advanced"), ("4", "expert")):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_skill_level(raw), expected)
        self.assertEqual(normalize_skill_level(2), "amateur")
        self.assertEqual(normalize_skill_level(4), "expert")

    def test_load_from_config_normalizes_aliases_and_garbage(self) -> None:
        """config.json: алиас нормализуется, мусор -> beginner, старый конфиг -> beginner."""
        path = self._write({"player_profile": {"skill_level": "Опытный"}})
        self.assertEqual(load_config(path).player_profile.skill_level, "advanced")

        path = self._write({"player_profile": {"skill_level": "мусор"}}, name="bad.json")
        self.assertEqual(load_config(path).player_profile.skill_level, "beginner")

        path = self._write({"player_profile": {"skill_level": 4}}, name="numeric.json")
        self.assertEqual(load_config(path).player_profile.skill_level, "expert")

        # Старый config.json без поля — дополняется безопасным дефолтом.
        path = self._write({"gsi": {"port": 3010}}, name="old.json")
        self.assertEqual(load_config(path).player_profile.skill_level, "beginner")

    def test_save_roundtrip_persists_skill_level(self) -> None:
        """save_config пишет skill_level в JSON, load_config читает обратно."""
        config = Config()
        config.player_profile.skill_level = "expert"
        path = self.temp_dir / "saved.json"
        save_config(config, path)

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["player_profile"]["skill_level"], "expert")

        restored = load_config(path)
        self.assertEqual(restored.player_profile.skill_level, "expert")

    def test_reset_to_defaults_sets_skill_level(self) -> None:
        """Сброс к эталонному пресету даёт валидный уровень и пишет поле в файл."""
        path = self.temp_dir / "reset.json"
        cfg = reset_to_defaults(path)
        self.assertIn(cfg.player_profile.skill_level, SKILL_LEVELS)

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("skill_level", raw["player_profile"])
        self.assertEqual(raw["player_profile"]["skill_level"], cfg.player_profile.skill_level)


class SkillLevelWebApiTests(unittest.TestCase):
    """web_app.py: get_state/save_settings и мгновенное применение к тренеру."""

    def setUp(self) -> None:
        self.temp_dir = make_temp_dir()
        self.config_path = self.temp_dir / "config.json"
        self.config = Config()
        save_config(self.config, self.config_path)
        self.ctrl = WebGuiController(self.config, self.config_path)
        self.api = AidotaWebApi(self.ctrl)

    def tearDown(self) -> None:
        self.ctrl.tray.stop()
        self.ctrl.assistant = None
        remove_temp_dir(self.temp_dir)

    def test_get_state_returns_skill_level(self) -> None:
        """get_state отдаёт уровень из профиля (по умолчанию beginner)."""
        state = self.api.get_state()
        self.assertIn("skill_level", state)
        self.assertEqual(state["skill_level"], "beginner")

        self.config.player_profile.skill_level = "advanced"
        self.assertEqual(self.api.get_state()["skill_level"], "advanced")

    def test_save_settings_persists_and_normalizes_skill_level(self) -> None:
        """save_settings принимает алиас «Профи», сохраняет expert в config.json."""
        res = self.api.save_settings({"skill_level": "Профи"})
        self.assertTrue(res.get("success"), res)
        self.assertEqual(self.config.player_profile.skill_level, "expert")

        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["player_profile"]["skill_level"], "expert")

    def test_save_settings_invalid_skill_level_falls_back_to_beginner(self) -> None:
        """Невалидный уровень из веба -> beginner, сохранение успешно."""
        res = self.api.save_settings({"skill_level": "что-то странное"})
        self.assertTrue(res.get("success"), res)
        self.assertEqual(self.config.player_profile.skill_level, "beginner")

        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["player_profile"]["skill_level"], "beginner")

    def test_save_settings_applies_skill_level_to_live_coach(self) -> None:
        """Сохранение уровня на лету вызывает coach.set_skill_level без перезапуска."""
        applied: list[str] = []
        fake_llm = SimpleNamespace(base_url="", model="", api_key="")
        fake_coach = SimpleNamespace(
            llm=fake_llm,
            profile=SimpleNamespace(skill_level="beginner"),
            set_skill_level=applied.append,
        )
        fake_agent = SimpleNamespace(set_autonomy_level=lambda level: None)
        self.ctrl.assistant = SimpleNamespace(_coach=fake_coach, _agent=fake_agent)

        res = self.api.save_settings({"skill_level": "новичок"})

        self.assertTrue(res.get("success"))
        self.assertEqual(applied, ["beginner"])
        self.assertEqual(fake_coach.profile.skill_level, "beginner")

    def test_apply_skill_level_tolerates_missing_setter(self) -> None:
        """Если Coach ещё без set_skill_level, уровень пробрасывается в profile."""
        fake_coach = SimpleNamespace(profile=SimpleNamespace(skill_level="beginner"))
        fake_agent = SimpleNamespace()
        self.ctrl.assistant = SimpleNamespace(_coach=fake_coach, _agent=fake_agent)

        self.api._apply_skill_level("Опытный")  # Не должно упасть

        self.assertEqual(fake_coach.profile.skill_level, "advanced")


class SkillLevelSettingsDialogTests(unittest.TestCase):
    """Tk-диалог настроек: Combobox «Уровень игры» и сбор data["skill_level"]."""

    def test_dialog_loads_and_saves_skill_level(self) -> None:
        from aidota.settings_gui import SKILL_LEVEL_HINTS, SettingsDialog

        config = Config()
        config.player_profile.skill_level = "advanced"
        saved: dict[str, Any] = {}
        dlg = None
        try:
            dlg = SettingsDialog(None, config, on_save=saved.update)

            # Загрузка текущего значения: advanced -> 3-й пункт списка.
            self.assertEqual(dlg.skill_combo.current(), 2)
            self.assertEqual(dlg.skill_combo.get(), "Опытный")
            self.assertIn("без объяснения базовых", dlg.skill_hint_lbl.cget("text"))

            # Переключение на «Профи» обновляет подсказку.
            dlg.skill_combo.current(3)
            dlg._on_skill_level_changed()
            self.assertIn("мета", dlg.skill_hint_lbl.cget("text"))
            self.assertIn("Профи", dlg.skill_hint_lbl.cget("text"))

            # Сохранение отдаёт канонический код в data["skill_level"].
            dlg._save_settings()
            self.assertEqual(saved.get("skill_level"), "expert")
            self.assertIn(saved.get("skill_level"), SKILL_LEVELS)
            self.assertEqual(SKILL_LEVEL_HINTS[saved["skill_level"]], SKILL_LEVEL_HINTS["expert"])
        finally:
            if dlg is not None and hasattr(dlg, "window"):
                dlg.window.destroy()

    def test_dialog_defaults_to_beginner_hint(self) -> None:
        """Для нового профиля выбран «Новичок» с подсказкой про объяснение терминов."""
        from aidota.settings_gui import SettingsDialog

        dlg = None
        try:
            dlg = SettingsDialog(None, Config())
            self.assertEqual(dlg.skill_combo.current(), 0)
            self.assertEqual(dlg.skill_combo.get(), "Новичок")
            hint = dlg.skill_hint_lbl.cget("text")
            self.assertIn("Новичок", hint)
            self.assertIn("объясняет термины", hint)
            self.assertEqual(
                list(dlg.skill_combo.cget("values")),
                ["Новичок", "Любитель", "Опытный", "Профи"],
            )
        finally:
            if dlg is not None and hasattr(dlg, "window"):
                dlg.window.destroy()


class SkillLevelMainWiringTests(unittest.TestCase):
    """Отдельное окно настроек удалено: уровень настраивается в основном UI."""

    def test_no_separate_settings_dialog(self) -> None:
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('"--settings"', source)
        self.assertNotIn("SettingsDialog", source)

    def test_gui_and_web_expose_skill_level_setting(self) -> None:
        gui_source = (PROJECT_ROOT / "aidota" / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn("seg_skill_level", gui_source)
        web_html = (PROJECT_ROOT / "aidota" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="cfg-skill-level"', web_html)


def _defaults_dict_skill_level() -> str:
    """Берём значение по умолчанию из ``dataclasses.asdict(Config())``."""
    return dataclasses.asdict(Config())["player_profile"]["skill_level"]


if __name__ == "__main__":
    unittest.main()
