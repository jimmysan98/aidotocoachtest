"""Release-readiness тесты: версия, секреты, упаковка, установщик, GitHub-гигиена.

Эти тесты не требуют сети и не запускают GUI — проверяют, что репозиторий и
собранные артефакты готовы к публикации релиза.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from aidota import __version__
from aidota import beginner, meta_db

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "AIDOTA"
SETUP = ROOT / "AIDOTA_Setup.exe"

RUNTIME_DEPS = (
    "vosk",
    "srt",
    "sounddevice",
    "soundfile",
    "edge-tts",
    "pyttsx3",
    "pywin32",
    "numpy",
    "opencv-python",
    "mss",
    "requests",
    "rapidfuzz",
    "customtkinter",
    "Pillow",
    "pystray",
    "pywebview",
)

SECRET_PATTERNS = (
    re.compile(r"sk-or-v1-[A-Za-z0-9]{12,}"),
    re.compile(r'sk-[A-Za-z0-9]{24,}'),
    re.compile(r"enc:[A-Za-z0-9+/=]{30,}"),
)


class VersionAndDocsTests(unittest.TestCase):
    def test_version_semver_and_installer_default(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
        self.assertIn(f'#define MyAppVersion "{__version__}"', installer)
        self.assertIn(f'"{__version__}"', installer)

    def test_changelog_has_current_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"[{__version__}]", changelog)
        self.assertIn("[1.0.0]", changelog)

    def test_readme_is_release_ready(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for section in (
            "Установка",
            "Возможности",
            "Скриншоты",
            "Уровни игры",
            "Разработка",
            "Тесты и качество",
            "Сборка установщика",
            "Лицензия",
        ):
            self.assertIn(section, readme, f"в README нет секции «{section}»")
        self.assertIn("AIDOTA_Setup.exe", readme)
        self.assertIn(__version__, readme)
        self.assertIn("assets/readme-banner.png", readme)

    def test_readme_images_exist(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in re.findall(r"!\[[^\]]*\]\((assets/[^)]+)\)", readme):
            self.assertTrue((ROOT / name).is_file(), f"нет картинки {name}")

    def test_license_and_third_party(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Permission is hereby granted", license_text)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Vosk", notices)
        self.assertIn("Dota 2", notices)

    def test_build_docs_exist(self):
        build = (ROOT / "docs" / "BUILD.md").read_text(encoding="utf-8")
        for needle in ("build_exe.py", "installer.iss", "Inno Setup", "AIDOTA_Setup.exe"):
            self.assertIn(needle, build)


class SecretsAndGitTests(unittest.TestCase):
    def test_example_config_has_no_secrets_and_defaults(self):
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["llm"]["api_key"], "")
        for preset in example["llm"]["presets"].values():
            if isinstance(preset, dict):
                self.assertEqual(preset.get("api_key", ""), "")
        raw = json.dumps(example, ensure_ascii=False)
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(raw), "в config.example.json найден секрет")
        self.assertEqual(example["audio"]["model_path"], "vosk-model-small-ru-0.22")
        self.assertIn(
            example["player_profile"]["skill_level"],
            ("beginner", "amateur", "advanced", "expert"),
        )

    def test_preset_matches_example(self):
        preset = json.loads((ROOT / "default_preset.json").read_text(encoding="utf-8"))
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(preset, example, "default_preset.json и config.example.json разошлись")

    def test_single_default_preset_file(self):
        self.assertTrue((ROOT / "default_preset.json").is_file())
        self.assertFalse(
            (ROOT / "aidota" / "default_preset.json").exists(),
            "дубликат aidota/default_preset.json должен быть удалён",
        )

    def test_gitignore_covers_secrets_and_artifacts(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            "config.json",
            ".agent/",
            "AIDOTA_Setup.exe",
            "dist/",
            "build/",
            "vosk-model-small-ru-0.22/",
            "logs/",
            ".cache/",
        ):
            self.assertIn(entry, gitignore, f"в .gitignore нет «{entry}»")

    def test_no_secrets_in_committable_sources(self):
        skip_dirs = {
            ".venv", ".git", "dist", "build", ".cache", "logs", "__pycache__",
            "vosk-model-ru-0.22", "vosk-model-small-ru-0.22", "hero_icons",
            "tests",  # в тестах есть демонстрационные (заведомо фальшивые) ключи
        }
        skip_files = {"config.json"}
        checked = 0
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.name in skip_files or path.suffix.lower() in (".png", ".ico", ".exe", ".dll", ".pyd"):
                continue
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            checked += 1
            for pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match is not None:
                    self.fail(
                        f"секрет в {path.relative_to(ROOT)}: {match.group(0)[:16]}…"
                    )
        self.assertGreater(checked, 20, "слишком мало проверенных файлов")


class RequirementsAndBuildTests(unittest.TestCase):
    def test_runtime_requirements(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for dep in RUNTIME_DEPS:
            self.assertIn(dep.lower(), text, f"в requirements.txt нет {dep}")
        self.assertNotIn("pyinstaller", text, "pyinstaller — только для dev-сборки")

    def test_dev_requirements(self):
        text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        self.assertIn("pyinstaller", text)
        self.assertIn("-r requirements.txt", text)

    def test_installer_script(self):
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8")
        for needle in (
            "OutputBaseFilename=AIDOTA_Setup",
            "LicenseFile=LICENSE",
            "InfoBeforeFile=installer_notes.txt",
            "PrivilegesRequired=lowest",
            "MinVersion=10.0",
        ):
            self.assertIn(needle, iss)
        self.assertIn('Excludes: "config.json', iss)
        self.assertTrue((ROOT / "installer_notes.txt").is_file())

    def test_build_script_does_not_ship_dev_config(self):
        script = (ROOT / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("FORBIDDEN_IN_DIST", script)
        self.assertIn('"config.json"', script)
        self.assertIn("config.example.json", script)
        self.assertIn("LICENSE", script)
        self.assertIn("vosk-model-small-ru-0.22", script)
        self.assertIn("/DMyAppVersion=", script)

    def test_pyinstaller_spec_collects_web_and_data(self):
        spec = (ROOT / "AIDOTA.spec").read_text(encoding="utf-8")
        for entry in ("aidota/web", "data", "hero_icons", "assets", "config.example.json"):
            self.assertIn(entry, spec)
        self.assertNotIn('"aidota/default_preset.json"', spec)
        self.assertIn("pyttsx3", spec)
        self.assertIn("webview", spec)

    def test_ci_workflow(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("windows-latest", workflow)
        self.assertIn("tools/run_tests.py", workflow)
        self.assertIn("timeout-minutes", workflow)
        self.assertTrue((ROOT / "tools" / "run_tests.py").is_file())

    def test_assets_are_complete(self):
        assets = ROOT / "assets"
        for name in ("logo.svg", "logo.png", "icon.ico", "readme-banner.png",
                     "screenshot-dashboard.png", "screenshot-settings.png", "screenshot-chat.png"):
            self.assertTrue((assets / name).is_file(), f"нет assets/{name}")
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover — Pillow есть в dev-окружении
            self.skipTest("Pillow не установлен")
        icon = Image.open(assets / "icon.ico")
        sizes = sorted(icon.info.get("sizes", []))
        self.assertIn((16, 16), sizes)
        self.assertIn((256, 256), sizes)

    @unittest.skipUnless(SETUP.is_file(), "установщик ещё не собран")
    def test_installer_artifact(self):
        self.assertGreater(SETUP.stat().st_size, 50 * 1024 * 1024)
        version_file = DIST / "version.txt"
        self.assertTrue(version_file.is_file())
        self.assertEqual(version_file.read_text(encoding="utf-8").strip(), __version__)
        for relative in (
            "AIDOTA.exe",
            "LICENSE",
            "CHANGELOG.md",
            "README.md",
            "config.example.json",
            "vosk-model-small-ru-0.22",
        ):
            self.assertTrue((DIST / relative).exists(), f"в дистрибутиве нет {relative}")
        web_candidates = (
            DIST / "aidota" / "web" / "index.html",
            DIST / "_internal" / "aidota" / "web" / "index.html",
        )
        self.assertTrue(any(path.is_file() for path in web_candidates), "в дистрибутиве нет веб-UI")
        modern_candidates = (
            DIST / "aidota" / "web" / "style-modern.css",
            DIST / "_internal" / "aidota" / "web" / "style-modern.css",
        )
        self.assertTrue(any(path.is_file() for path in modern_candidates), "нет style-modern.css")
        self.assertFalse((DIST / "config.json").exists(), "dev-config попал в дистрибутив")


class ProductSurfaceTests(unittest.TestCase):
    def test_skill_levels_and_glossary(self):
        self.assertEqual(
            tuple(beginner.SKILL_LEVELS),
            ("beginner", "amateur", "advanced", "expert"),
        )
        self.assertGreaterEqual(len(beginner.GLOSSARY), 50)
        self.assertIsNotNone(beginner.explain_term("что такое бкб"))
        self.assertIsNotNone(beginner.explain_term("объясни байбек"))
        for level in beginner.SKILL_LEVELS:
            self.assertTrue(beginner.level_label(level))

    def test_meta_db_coverage(self):
        self.assertGreaterEqual(len(meta_db.META_HEROES_DB), 120)
        self.assertIsNotNone(meta_db.get_hero_counters("Phantom Assassin"))
        self.assertTrue(meta_db.get_skill_order("Pudge"))
        self.assertTrue(meta_db.get_lane_tips("Anti-Mage", 1))

    def test_web_ui_modern_layer(self):
        html = (ROOT / "aidota" / "web" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "aidota" / "web" / "style-modern.css").read_text(encoding="utf-8")
        js = (ROOT / "aidota" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("style-modern.css", html)
        self.assertIn("ui-splash", html)
        self.assertIn("toast-stack", html)
        self.assertIn("cfg-skill-level", html)
        self.assertIn("@keyframes", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("ripple-ink", js)
        self.assertIn("showToast", js)


if __name__ == "__main__":
    unittest.main()
