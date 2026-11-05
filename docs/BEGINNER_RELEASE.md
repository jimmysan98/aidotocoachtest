# AIDOTA: «Тренер для новичков» + релиз (установщик + GitHub-ready)

Цель второго этапа:
1. Сделать тренера похожим на живого наставника для новичков (чат-бот, объясняет
   термины, учит, а не только сыпет советами).
2. Собрать всё в готовый установщик `AIDOTA_Setup.exe`, который ставится «из коробки».
3. Привести репозиторий в вид, который не стыдно выложить на GitHub (лицензия, CI,
   changelog, чистая конфигурация без секретов, README с установкой и скриншотами).

Рабочая директория: `D:\deepseek-harness\default-workspace\aidota\aidota`
Python: `.\.venv\Scripts\python.exe` (3.15). Базовая линия тестов: **400 OK**.
Полный прогон: `& .\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q`
(после `OK` процесс может висеть из-за фоновых потоков — это не падение; смотри итог,
при необходимости `Get-Process python | Stop-Process -Force`).

---

## Файловые зоны (не пересекаться)

| Агент | Файлы |
|---|---|
| `beginner-coach` | `aidota/beginner.py` (новый), `aidota/coach_brain.py`, `aidota/coach.py`, `aidota/context.py`, `aidota/app.py`, `tests/test_beginner_coach.py` |
| `settings-ui` | `aidota/config.py`, `aidota/settings_gui.py`, `aidota/gui_app.py`, `aidota/web_app.py`, `aidota/web/index.html`, `aidota/web/app.js`, `main.py`, `tests/test_skill_level_ui.py` |
| `lead` | релиз: `build_exe.py`, `AIDOTA.spec`, `installer.iss`, `config.example.json`, `config.json` (санитайз), `default_preset.json` (санитайз), `aidota/__init__.py` (версия), `.gitignore`, `LICENSE`, `CHANGELOG.md`, `.github/workflows/ci.yml`, `requirements*.txt`, `docs/BUILD.md`, сборка `dist/AIDOTA` + `AIDOTA_Setup.exe` |
| `qa-release` | `tests/test_release_readiness.py` (новый), `README.md`, приёмка установщика |

---

## Контракт E. Тренер для новичков (beginner-coach)

### E1. Уровень игрока в профиле (settings-ui)
`PlayerProfile.skill_level: str = "beginner"`, допустимые значения:
`"beginner"` | `"amateur"` | `"advanced"` | `"expert"` (русские алиасы в UI: «Новичок»,
«Любитель», «Опытный», «Профи»). `config.py:_validate_profile` нормализует невалидное
значение в `"beginner"`. Поле должно сохраняться/загружаться через config.json
и быть доступно в `Config.player_profile.skill_level`.
Для `advanced`/`expert` тренер не разжёвывает базовые термины, а даёт глубокую
тактическую конкретику (тайминги паверспайков, окна драфта, темп карты, экономика
байбека, тайминги вижна/смоков, контроль объектов); в чате допустимо 3–5 предложений
(до 500 символов), для голоса — по-прежнему коротко.

UI (settings-ui):
- `aidota/settings_gui.py` (Tk-диалог настроек) — выпадающий список «Уровень игры».
- `aidota/gui_app.py` — сегментированная кнопка в разделе настроек, сохранение и
  мгновенное применение без перезапуска.
- `aidota/web_app.py` + `aidota/web/index.html` + `aidota/web/app.js` — поле в
  настройках веб-интерфейса, `get_state()` отдаёт `skill_level`, `save_settings`
  принимает и сохраняет.
- Отдельное окно настроек удалено: параметры меняются во вкладке «Настройки» основного UI.
- Сохранение идёт как `data["skill_level"]`, значение из UI -> строковый код.
- Tkinter: только главный поток GUI (см. `.agent/skills/aidota-dev/SKILL.md`).

### E2. Новый модуль `aidota/beginner.py`
- `SKILL_LEVELS = ("beginner", "amateur", "advanced")`;
- `normalize_skill_level(value) -> str` (алиасы «новичок», «нуб», «любитель»,
  «средний», «продвинутый», «про» и т.п.);
- `level_label(level) -> str` («Новичок» и т.д.);
- `GLOSSARY: dict[str, dict]` — **не менее 50 терминов** Dota 2 с простыми
  объяснениями по-русски: `{"aliases": tuple[str, ...], "simple": str,
  "example": str}`. Обязательно: БКБ, байбек, вижн, ластхит, денай, харасс/харас,
  ротация, ганк, инициация, кайт, сейв, диспел, сайленс, контроль, тайминг,
  паверспайк, стак, отвод (пулл), смок, руна, аегис, Рошан, Терзатель, нейтралка,
  аганим, шард, GPM, XPM, net worth, KDA, позиция 1–5, керри, мид, оффлейн,
  саппорт, драфт, контрпик, бан, пуш, сплитпуш, хайграунд, таверна, броня,
  магия/физ.урон, ультимейт, кулдаун, мана-берн, дебафф/бафф.
- `resolve_term(text) -> tuple[str, dict] | None` — находит термин/алиас в
  свободной фразе («что такое бкб», «объясни, что значит байбек», «как работает
  вижн»); устойчив к падежам (минимум: подстрочный поиск нормализованных форм).
- `explain_term(text) -> str | None` — 1–2 простых предложения + короткий пример
  из игры. Без markdown.
- `freshman_greeting(trainer_name) -> str` — приветствие-онбординг: 3 шага для
  новичка (что спросить, как пользоваться, с чего начать).
- `role_training_plan(position: int) -> str` — 3–4 простых шага/упражнения на роль.
- `next_drill(position: int, seed_text: str = "") -> str` — одно упражнение (ластхит,
  вижн, руны, карта) под роль; детерминированно для одинакового seed_text.

### E3. `coach_brain.py`
- `make_system_prompt(coach_name="Астра", dynamic_context="", *, skill_level="beginner")`
  — персона и правила не ломаются (тесты проверяют «Астра», «7000+ MMR», «J.A.R.V.I.S.»,
  «боевой тактический ИИ-ассистент»), но для новичка добавляются правила:
  объяснять термины в скобках (БКБ — золотой щит от магии), давать 1 конкретное
  действие и короткое «зачем», не ругать, не перегружать, предлагать потренировать.
- `render_system_prompt(..., skill_level="beginner")` — использует правила уровня.
- `detect_query_kind`: добавить `"explain"` (что такое/объясни/как работает/что
  значит) — чтобы знание бралось из глоссария.
- `sanitize_answer` для уровня beginner и kind="explain"/"chat" должен позволять
  до 3 предложений / 320 символов (остальные режимы без изменений).

### E4. `coach.py`
- `Coach.skill_level` property (из `self.profile.skill_level`, `getattr` с дефолтом
  "beginner"), `Coach.set_skill_level(level)` (нормализует и обновляет профиль).
- `coach_chat`:
  - при `kind == "explain"` для новичка — вернуть `beginner.explain_term(...)`,
    если термин найден (это гарантированно полезный и короткий ответ, без LLM);
    если LLM доступна — можно отдать глоссарий как knowledge и попросить объяснить
    ещё проще, но fallback — текст из глоссария;
  - пустой запрос у новичка — `freshman_greeting`;
  - в системный промпт передавать `skill_level`;
  - max_chars/max_sentences: beginner+explain/chat — до 320/3, остальное как было.
- `hero_intro_advice`: для новичка добавить `beginner.role_training_plan` в промпт
  и в fallback (коротко).
- `game_analysis`/`analyze_match`: для новичка системный промпт и fallback в формате
  «Что получилось / Главная ошибка / Упражнение», без жёсткого тона; в fallback
  матча — `beginner.next_drill`.
- `advice`: для новичка добавить в guidance «объясни простыми словами, назови 1
  действие и зачем».
- `Coach.set_skill_level` доступен при сохранении настроек (`app.py`).

### E5. `context.py` + `app.py`
- `AutonomousCoachAgent.__init__(..., skill_level: str = "beginner")` (совместимо),
  `set_skill_level`; для новичка подсказки (серия смертей, золото, Рошан, аегис,
  паверспайк) содержат короткое объяснение «почему это важно»; без новичкового
  режима тексты прежние (тесты `test_mid_timers` не ломать).
- `app.py`: при создании `AutonomousCoachAgent` передавать уровень из профиля; в
  обработчике сохранения настроек принимать `skill_level`, применять к coach и
  сохранять в config.json (через существующий save_config).

### E6. Тесты `tests/test_beginner_coach.py` (15–25)
Глоссарий (10+ терминов, падежи, мусор), правила новичка в промпте, `coach_chat`
«что такое бкб» без LLM даёт объяснение, приветствие, fallback-разбор матча с
«Упражнение», `hero_intro_advice` beginner, `AutonomousCoachAgent` beginner-советы,
`Coach.set_skill_level`, `sanitize_answer` beginner-лимиты, отсутствие markdown.

---

## Контракт F. Установщик и GitHub-ready (lead)

- `aidota/__init__.py`: версия **1.1.0**.
- `config.json`: убрать реальные ключи (`sk-or-v1-...` → `""`), машинные
  `input_device`/`output_device` → `null`, `audio.model_path` →
  `vosk-model-small-ru-0.22`; файл больше не должен содержать секретов.
- `config.example.json` — пример для GitHub (пустые ключи, null-устройства).
- `default_preset.json` (единственный, в корне): пустые api_key (в т.ч. обфусцированные
  `enc:` удалить), `skill_level: "beginner"`, `model_path: "vosk-model-small-ru-0.22"`;
  удалить дубликат `aidota/default_preset.json`, поправить `AIDOTA.spec`.
- `build_exe.py`: не копировать dev `config.json` в дистрибутив; копировать
  `config.example.json` + `LICENSE` + `CHANGELOG.md` + `README.md`; версия берётся из
  `aidota.__version__` и передаётся в Inno Setup через `/DMyAppVersion=...`; понятное
  сообщение, если Inno Setup не найден (ссылка на скачивание).
- `installer.iss`: `#ifndef MyAppVersion`, publisher/links (GitHub placeholder),
  `LicenseFile=LICENSE`, `InfoBeforeFile` с примечанием про ключ API/модель, иконка,
  русский+английский, `PrivilegesRequired=lowest`, `MinVersion=10.0`,
  VersionInfo, исключить из файлов dev-мусор (`config.json`, `.cache`, `logs`).
- `.gitignore`: `config.json`, `.agent/`, `AIDOTA_Setup.exe`, `docs/` (?) — секреты,
  сборочные артефакты, `vosk-model-small-ru-0.22/`, рантайм-данные; не игнорировать
  `docs/COACH_UPGRADE.md`, `docs/BEGINNER_RELEASE.md` (они остаются в репо).
- `requirements.txt` — только рантайм (точно по импортам, включая `pywebview`,
  `soundfile`, `mss` и т.д.); `requirements-dev.txt` — pyinstaller и dev-утилиты.
- `.github/workflows/ci.yml`: windows-latest, Python 3.12, установка
  `requirements-dev.txt`, прогон unittest, `timeout-minutes`.
- `LICENSE` (MIT, «AIDOTA contributors»), `CHANGELOG.md` (1.0.0 → 1.1.0),
  `docs/BUILD.md` (как собрать exe и установщик, как выпустить релиз).
- Сборка: `dist\AIDOTA\AIDOTA.exe --version` работает; `AIDOTA_Setup.exe`
  пересобран и проверен тихой установкой в отдельную папку (`/VERYSILENT`), запуском
  установленного exe и удалением; размер ~100–130 МБ; внутри модель small Vosk, web,
  data, hero_icons, LICENSE, README, CHANGELOG.

---

## Контракт G. QA релиза (qa-release)

- `tests/test_release_readiness.py` (10–18 тестов, без сети):
  - версия `aidota.__version__` = `1.1.0`, README/CHANGELOG согласованы;
  - `config.example.json` существует и в нём нет ключей/секретов;
  - в файлах, которые попадут в git, нет паттернов `sk-`, `enc:` у ключей,
    реальных токенов; `config.json` в `.gitignore`;
  - `requirements.txt` покрывает импорты (`pywebview`, `soundfile`, `mss`,
    `customtkinter`, `pywin32`, `rapidfuzz`, `edge-tts`, `vosk`, `sounddevice`,
    `numpy`, `opencv-python`, `pystray`, `Pillow`, `requests`);
  - `installer.iss`/`build_exe.py`/`AIDOTA.spec` содержат версию, LICENSE, модель;
  - `AIDOTA_Setup.exe` существует и > 50 МБ, `dist/AIDOTA/AIDOTA.exe` и
    `vosk-model-small-ru-0.22` на месте, есть `aidota/web`, `LICENSE`, `CHANGELOG`;
  - README содержит секции «Установка», «Тренер для новичков», «Разработка»,
    «Сборка установщика», ссылку на LICENSE;
  - `.github/workflows/ci.yml` существует и запускает тесты.
- README.md — финальный GitHub-вид: логотип `assets/icon.png`, бейджи (python,
  tests, license), блок установки через `AIDOTA_Setup.exe`, список возможностей
  (включая новичкового тренера и «что такое БКБ»), быстрый старт, структура,
  разработка/тесты, сборка релиза, лицензия. Стиль — русский, аккуратные таблицы.
- Приёмка: прогнать полный набор тестов (400+), проверить установленный exe
  (`--version`), отчёт Lead'у.

---

## Контракт H. Современный дизайн интерфейса (ui-web-designer + ui-tk-designer)

Цель: современный премиальный вид — новый логотип, аккуратные кнопки, плавные
анимации, при этом вся функциональность и все существующие id/API сохраняются.
Основной UI — веб-интерфейс (`aidota/web/*` через pywebview). Запасной — CustomTkinter
(`aidota/gui_app.py`). Внутриигровой оверлей (`aidota/overlay.py`) тоже освежить.

### H1. Логотип и ассеты (владелец: ui-web-designer)
- `assets/logo.svg` — векторный логотип AIDOTA: гексагон/щит + стилизованная «A» +
  искра ИИ/руна Dota; градиент фиолетовый Astra (`#a855f7`) → циан (`#38bdf8`).
- `assets/logo.png` (512×512), `assets/logo-256.png`, `assets/icon.png` (256) и
  пересобранный `assets/icon.ico` (16/24/32/48/64/128/256) — единый новый стиль
  (иконка приложения, трей, установщик, README).
- `tools/generate_logo.py` — воспроизводимая генерация PNG/ICO из кода (Pillow),
  чтобы ассеты можно было пересоздать (`python tools/generate_logo.py`).
- `assets/readme-banner.png` (1280×320) — баннер для README (логотип + название +
  подпись «голосовой ИИ-тренер по Dota 2»), тёмный фон, без мелкого текста.

### H2. Веб-UI (владелец: ui-web-designer)
- Современная дизайн-система: CSS-переменные (палитра, отступы 4/8/12/16/24/32,
  радиусы, тени, тайминги), тёмная «космическая» тема, стеклянные панели
  (backdrop-filter), градиентные акценты, аккуратная типографика (system font stack,
  никаких внешних CDN — приложение должно работать офлайн).
- Кнопки: варианты primary (градиент) / secondary (стекло) / icon / danger / ghost,
  состояния hover (подъём/свечение), active (нажатие), focus-visible (контур),
  disabled; micro-анимации; у icon-кнопок — title/aria-label.
- Навигация: анимированный индикатор активной вкладки, плавная смена контента
  (fade/slide), сохранение текущей вкладки; на мобильной ширине — аккуратный скролл.
- Анимации (CSS, с уважением `prefers-reduced-motion`): появление карточек
  (stagger), hover-lift, пульс статус-пилюли (подключено/думает/ошибка), «печатает…»
  (три точки) в чате, анимированный waveform/полоски при прослушивании микрофона,
  count-up числовых значений статистики, toast-уведомления, модалки с пружиной,
  shimmer-логотип при загрузке.
- Чат: современные пузыри сообщений, аватар тренера (Астра/Джарвис), автоскролл,
  плавное появление, состояние «думает», красивый input с кнопкой отправки.
- Настройки: селект «Уровень игры» (Новичок/Любитель/Опытный/Профи → значения
  beginner/amateur/advanced/expert), читается из `state.skill_level`, отправляется
  в `save_settings` как `skill_level`; подсказка-описание выбранного уровня.
- Сохранить ВСЕ существующие id, data-атрибуты, id кнопок/вкладок, вызовы `api.*`
  из `app.js`: дизайн меняется, контракт с `web_app.py` — нет. Если очень нужно
  переименовать id — обновите и JS, и убедитесь, что тесты/функции живы.
- Офлайн-режим: никаких google fonts/CDN, всё локально.

### H3. Оверлей (владелец: ui-web-designer)
`aidota/overlay.py`: современная полупрозрачная карточка с градиентной рамкой и
логотипом, плавное появление/скрытие (fade/slide через после-циклы Tk), пульс
индикатора прослушивания, аккуратные отступы и читаемость поверх игры. Обязательно:
только главный поток GUI, `deiconify/topmost/lift` при показе, тесты
`tests/test_overlay.py` зелёные.

### H4. Tk fallback (владелец: ui-tk-designer)
`aidota/gui_app.py`: современный вид по образцу веб-UI — палитра, карточки с
аккуратными скруглениями, кнопки с hover-цветами, логотип в шапке (`CTkImage` из
`assets/logo.png`, fallback — текст), анимации: пульс статус-точки/микрофона,
плавное появление карточек (изменение цвета/размера через `after`), анимированный
«думает…», сегментированный выбор уровня игры (4 значения) с сохранением в config
и применением к тренеру. Только главный поток GUI. Существующие тесты не ломать.

### H5. Проверки дизайна
- `python -m compileall` и полный набор тестов зелёные.
- Для веб-UI: все id из `app.js` присутствуют в `index.html` (проверить скриптом),
  JS синтаксически валиден (`node --check aidota/web/app.js`, если Node есть).
- Если доступен Edge/Chrome — сделать headless-скриншот страницы для визуальной
  проверки (не коммитить скриншоты в репозиторий, только в отчёт Lead'у).
- Логотип: `assets/icon.ico` открывается Pillow, размеры 16–256 на месте.
