# AIDOTA: план апгрейда ИИ-тренера («умнее»)

Цель: сделать ИИ-тренера (Астра / Джарвис) реально умнее без переписывания GUI:
точнее советы по билдам/контрпикам/прокачке/таймингам, меньше галлюцинаций и «воды»,
больше пользы от GSI/меты/памяти игрока, качественные запасные ответы без LLM.

Рабочая директория проекта: `D:\deepseek-harness\default-workspace\aidota\aidota`
Python: `.\.venv\Scripts\python.exe` (Python 3.15)
Тесты: `& .\.venv\Scripts\python.exe -m unittest tests.test_meta_db -v` (по модулям).
Полный прогон: `& .\.venv\Scripts\python.exe -m unittest discover -s tests -t . 2>&1`
(после `Ran N tests` процесс может не завершиться из-за фоновых потоков — это НЕ падение;
смотри итоговую строку `OK`/`FAILED`, при необходимости добей процесс через
`Get-Process python | Stop-Process -Force`).

Базовая линия на момент старта: **269 тестов, все проходят (OK)**. Ломать их нельзя.

---

## Правила проекта (обязательно прочитать)

1. `.agent/skills/aidota-dev/SKILL.md` — архитектурные стандарты AIDOTA.
2. Русский текст — только UTF-8 (файлы писать/читать как UTF-8). Не коммитить и не
   перекодировать файлы.
3. Никаких сетевых вызовов в тестах: только `unittest.mock` / `tests/helpers.FakeSession`.
4. Публичные API, которые уже используют другие модули/тесты, сохранять обратно
   совместимыми. Сигнатуры существующих функций не менять (можно добавлять
   необязательные параметры).
5. Каждый новый модуль/поведение — с unit-тестами в `tests/`.
6. Стиль ответов тренера: живой разговорный русский, 1–2 коротких предложения для
   озвучки; без markdown, списков, «воды» и выдуманных таймингов.

---

## Разделение файлов (write scope, не пересекаться)

| Агент | Файлы, которые он пишет |
|---|---|
| `knowledge-expander` | `aidota/meta_db.py`, `tests/test_meta_db_extra.py` |
| `context-intel` | `aidota/context.py`, `tests/test_context_extra.py` |
| `lead` | `aidota/coach.py`, `aidota/coach_brain.py`, `aidota/coach_memory.py`, `tests/test_coach_smart.py` |
| `qa-verifier` | `tests/test_coach_upgrade_qa.py`, `README.md` (только после готовности остальных) |

Чужие файлы не редактировать. Если нужен интерфейс в чужом файле — написать Lead.

---

## Контракт A. `aidota/meta_db.py` (владелец: knowledge-expander)

### A1. База знаний на всех героев
`META_HEROES_DB` должна покрывать **всех героев Dota 2** (ориентир — 127 записей из
`data/heroes.json`; ключ — короткий id без `npc_dota_hero_`, напр. `phantom_assassin`).
Для каждого героя обязательны поля:
`id`, `name` (англ. локализованное), `ru_name` (русское имя), `attr` (`str/agi/int/uni`),
`roles` (список 1–5), `tags` (угрозы/особенности, см. A2), `counters` (id героев, которых
этот герой контрит), `countered_by` (id героев, которые контрят его), `synergies`,
`builds` (хотя бы один ключ-роль с полями `starting`, `early_game`, `core` (список
`{"item","timing","tip"}`), `situational` (список `{"item","trigger","why"}`), `shard`,
`scepter`, `lane_tips`). Для популярных героев — билды на 2+ роли.

Сокращённо допустимо генерировать записи шаблоном по атрибуту/роли (как
`generate_fallback_build`), НО: `name/ru_name/roles/tags/counters/countered_by` должны
быть осмысленными для каждого героя. Никаких «Unknown Hero».

Существующие проверки `tests/test_meta_db.py` обязаны проходить без правок:
- `len(META_HEROES_DB) >= 10`; есть antimage, axe, juggernaut, crystal_maiden, witch_doctor;
- `get_hero_meta_entry("Антимаг")["id"] == "antimage"`; `get_hero_meta_entry("Акс")`;
- `get_hero_build("antimage", role=1)`: `hero_name == "Anti-Mage"`, в `core` есть Battle Fury,
  есть `shard`, `lane_tips`;
- `get_hero_build("antimage", role=1, enemy_heroes=["Phantom Assassin","Lion"])` содержит MKB;
- `get_hero_build("juggernaut", role=1, enemy_heroes=["Morphling","Axe"])` содержит Skadi/Vessel;
- `recommend_picks(["medusa","zuus"], role=1)`: top score >= 50, поля hero_id/name/ru_name/score;
- `recommend_picks(..., role=3/4)` возвращает только эту роль.

### A2. Теги угроз (`tags`) — словарь
`evasion`, `heal_regen`, `passives`, `saves_buffs`, `magic_burst_cc`, `single_target_cc`,
`illusions_swarms`, `right_clickers`, `high_mobility`, `invis`, `summons`, `silence`,
`global_presence`, `split_push`, `tanky`, `burst`. Теги используются тренером для подбора
контр-предметов; при сомнении ставить 2–4 честных тега.

### A3. Новые функции (публичный контракт; их вызывает `coach_brain.py`)

```python
def get_hero_counters(hero_name: str) -> dict[str, Any] | None:
    """{
        "hero_id": "phantom_assassin",
        "name": "Phantom Assassin",
        "ru_name": "Phantom Assassin",  # можно русское имя
        "tags": ["evasion", "right_clickers"],
        "counter_items": ["Monkey King Bar", "Ghost Scepter", "Silver Edge"],
        "counter_heroes": ["Axe", "Legion Commander"],  # англ. имена героев
        "tip": "Одна короткая фраза-объяснение по-русски.",
    }"""

def get_skill_order(hero_name: str) -> dict[str, Any] | None:
    """{
        "hero_id": "...", "ru_name": "...",
        "priority": "max Stifling Dagger",   # что максить
        "level1": "Stifling Dagger",         # что брать на 1-м уровне
        "tip": "Короткая фраза-совет по-русски.",
    }"""

def get_hero_threats(hero_names: list[str]) -> list[dict[str, Any]]:
    """[{
        "hero_id": "huskar", "ru_name": "Huskar",
        "tags": ["heal_regen", "tanky"],
        "threat": "реген и живучесть в лейте",
        "counter_items": ["Spirit Vessel", "Eye of Skadi", "Shiva's Guard"],
    }]  # только найденные герои, порядок как на входе"""

def get_lane_tips(hero_name: str, role: int, enemy_heroes: list[str] | None = None) -> str:
    """1–2 предложения тактики на линию под роль и врагов (не пустая строка)."""
```

Функции должны корректно искать героя по id / английскому / русскому имени (через
`get_hero_meta_entry`), возвращать `None`/`[]` для неизвестного героя, не падать на мусоре.

### A4. Улучшить `recommend_picks`
Скоринг: жёсткие контрпики (+30), кого контрит враг (−25), синергия с союзниками (+20),
покрытие ролей/тегов драфта (напр. нужен контроль/сейв/пуш), штраф за героев, которых
враги уже забанили/взяли (не предлагать). `bridge`-поля ответа сохранить.
Добавить в каждый результат `countered_by_preview` (2–3 имени врагов, которые опасны).

### A5. Тесты
`tests/test_meta_db_extra.py`: покрытие >= 120 героев; каждая запись имеет обязательные
поля; `get_hero_counters`/`get_skill_order`/`get_hero_threats`/`get_lane_tips` работают
для 10+ разных героев и для мусорного входа; `recommend_picks` не предлагает героя из
enemy-списка; синергия союзника повышает счёт.

---

## Контракт B. `aidota/context.py` (владелец: context-intel)

Сохранить все существующие публичные функции/классы и их тесты (`tests/test_context.py`,
`tests/test_mid_timers.py`). Добавить/обогатить:

### B1. `get_live_gsi_snapshot(...)` — богаче, обратно совместимо
Новые ключи (добавлять, старые не ломать):
- `"hero_items_display": list[str]` — инвентарь (уже считается),
- `"neutral_item": str | None`, `"aghanims_scepter": bool`, `"aghanims_shard": bool`,
- `"buyback_cost": int | None`, `"buyback_available": bool`,
- `"has_tp": bool`,
- `"my_team_players": list[dict]` и `"enemy_players": list[dict]` — по элементам:
  `{"name", "level", "kills", "deaths", "assists", "alive", "respawn_seconds", "net_worth",
    "items": list[str]}`; парсить `_extract_allplayers` + `allplayers`/`hero` GSI;
  отсутствующие поля — `None`/пустые списки, никаких падений на кривом payload,
- `"enemy_threats": list[str]` — 3–5 коротких фраз, что реально опасно в текущем драфте
  (напр. «У врага Huskar — срезай реген Spirit Vessel»), строится из пиков врага и
  (если есть) их видимых предметов/уровней,
- `"objectives": str` — ближайшая макро-цель (руна/Рошан/Терзатель/вышка) по таймеру.

### B2. `analyze_enemy_threats(payload, catalog, items) -> list[str]`
Публичная функция (её использует и `get_live_gsi_snapshot`, и тренер). Возвращает
короткие фразы по-русски с учётом вражеских героев; если врагов нет — `[]`.

### B3. Детектор ошибок `analyze_player_mistakes`
Добавить проверки (без дублей, только при осмысленных данных): нет ТП-свитка в инвентаре
до 10-й минуты; куплен Aghanim's Shard не на своём герое? — нет, лучше: пустой слот
нейтралки уже есть; добавить «мало HP и нет реген-предмета» — не надо. Реально полезные:
- нет ТП-свитка после 5-й минуты (уже есть? проверить, не дублировать),
- золото > 3000 при живом герое после 25-й минуты (байбек/слот),
- мёртв > 30 секунд респавна и есть невыкупленный байбек? (нет данных) — не надо.
Добавить: `"Нет ТП-свитка: под угрозой ганга не сможешь быстро сменить линию."` (если
нет ТП и минута >= 5), `"Слишком много смертей подряд"` не нужно (это делает автономный
агент). Главное — не спамить.

### B4. `AutonomousCoachAgent.process_tick` — умнее, не болтливее
- Совет по серии смертей делать с учётом роли (кор/саппорт) и героя;
- детект «враг забрал Рошана/аегис» по GSI (`map.aegis`/`roshan_respawn_timer`), если
  поля есть; иначе пропускать;
- предупреждение о паверспайке врага по видимым предметам (`_DANGEROUS_ENEMY_ITEMS`
  расширить: Orchid, Blink, BKB, Silver Edge, Diffusal, Scythe of Vyse, Aeon Disk);
- не повторять один и тот же совет (кэш текста на 3–5 минут);
- API/сигнатуры `process_tick`, `reset`, `set_autonomy_level` сохранить.

### B5. Тесты
`tests/test_context_extra.py`: синтетические payload'ы (полный/пустой/кривой), проверка
новых ключей snapshot, `analyze_enemy_threats` на драфте с Huskar/PA/Doom, отсутствие
падений, `AutonomousCoachAgent` не повторяет совет и срабатывает на power spike.

---

## Контракт C. `coach_brain.py` + `coach_memory.py` + `coach.py` (владелец: lead)

### C1. `aidota/coach_brain.py`
```python
def detect_query_kind(text: str) -> str
    # "pick" | "build" | "counter" | "skill" | "lane" | "timing" | "why" | "matchup" | "status" | "chat"

def build_brief(payload, catalog, items, *, position: int, profile=None, coach_name: str = "") -> dict
    # стадия, минута, роль, герой, инвентарь, золото, KDA, драфт, угрозы, ошибки,
    # цели, паверспайки; максимум ~1000 символов при рендере

def retrieve_knowledge(query, *, hero=None, position=1, enemy_heroes=None) -> list[str]
    # знания из meta_db (билд/контрпредметы/прокачка/лейн) + _COUNTER_HINTS/_HERO_SKILL_TIPS

def render_context(brief, knowledge, *, max_chars=1800) -> str
def render_system_prompt(coach_name, brief_text, knowledge, *, kind="chat") -> str
def sanitize_answer(text, *, max_chars=240, max_sentences=2) -> str
def ensure_short_voice_answer(text) -> str
```
Правила: не выдумывать предметы/тайминги; если данных нет — так и сказать одной фразой;
для `kind == "why"` допускается 3 предложения. `sanitize_answer` убирает markdown,
звёздочки, списки, эмодзи, кавычки-ёлочки, вводные «Конечно», «Отличный вопрос»,
«Я запомнил», обрезает по границе предложения, гарантирует заглавную букву и точку.

### C2. `aidota/coach_memory.py`
`CoachMemory(path: Path, max_turns=30)`:
- `recall(limit=5) -> str` — недавние реплики компактно,
- `remember(question, answer)`,
- `remember_lesson(text, match_id=None)` / `lessons(limit=5) -> list[str]`,
- `remember_fact(key, value)` / `facts() -> dict`,
- `summary()` — для системного промпта,
- атомарная запись JSON (tmp+replace), ошибки глушить, потокобезопасно.
Путь по умолчанию: `<data_dir>/coach_memory.json` (создаётся только при живой сессии,
в тестах — временная директория).

### C3. `coach.py` — интеграция ума
- `Coach.__init__` получает `memory_path: Path | None = None` (необязательный, обратная
  совместимость) и создаёт `self.brain`/`self.long_memory`;
- `coach_chat`: определить `kind`, собрать бриф + только релевантные знания, отправить
  компактный промпт; для `pick/build/counter/skill` подмешать данные `meta_db`
  (get_hero_build/get_hero_counters/get_skill_order); после ответа — `sanitize_answer`,
  запись в память;
- температура/токены: фактические вопросы (`build/counter/skill/pick/match`) — `temperature
  <= 0.35`, креативный чат — `<= 0.6`; `max_tokens` для голосовых ответов <= 200, для
  разборов матча/игры — до 600. Не мутировать `self.llm.temperature` глобально.
- `_openai_chat`: максимум 2 раунда tool-calling (не зацикливаться), новые инструменты
  `get_hero_build_guide` (или переиспользовать `get_hero_item_build`) и
  `search_coach_knowledge` (поиск знаний по вопросу); при ошибке tools — повтор без tools;
- `execute_tool`: `get_hero_counters` — данные из `meta_db` в приоритете, затем старый
  `_COUNTER_HINTS`; `get_hero_item_build` — из meta_db; добавить `search_coach_knowledge`;
- запасные ответы (`start_items`, `skills_order`, `counter_advice`, `coach_chat`,
  `hero_intro_advice`, `_fallback_advice`, `buy_next`) — из `meta_db`, а не из общих фраз;
- `get_dynamic_context`/`_build_messages`: сохранить маркеры, которые проверяют тесты
  (`GSI Live`, `ПРОФИЛЬ ИГРОКА`, формат `GSI Снимок: 11:00 ... KDA 3/1/5`), но добавить
  блоки «ТАКТИЧЕСКИЙ БРИФ», «ЗНАНИЯ ПО ВОПРОСУ», «ДОЛГОВРЕМЕННАЯ ПАМЯТЬ»;
- `analyze_match`: добавить в промпт сравнение билда с эталоном meta_db, тайминги
  ключевых предметов, benchmarks (gpm/xpm/hero_damage/tower_damage percentiles), запись
  урока в `long_memory` и `update_player_profile_mistakes` (data/player_profile.json);
  считать невыкупленный байбек? — нет; увеличить max_tokens;
- `game_analysis`/`buy_next`/`draft_advice`/`pick_advice` — использовать бриф и знания;
- публичные сигнатуры всех существующих методов сохранить.

### C4. Тесты
`tests/test_coach_smart.py` — прямое тестирование `coach_brain` и интеграции с `Coach`
(FakeSession): `detect_query_kind` на 15+ фразах; `sanitize_answer` (markdown/длина/
вводные); `retrieve_knowledge` для билда/контрпика/скилла; `coach_chat` не падает без
GSI; ответ с markdown санитизируется; `CoachMemory` roundtrip (tmp dir); tool-calling
цикл (2 раунда) с FakeSession; `get_hero_counters` для героя вне `_COUNTER_HINTS`.

---

## Контракт D. QA (владелец: qa-verifier)
- `tests/test_coach_upgrade_qa.py`: сквозные проверки «умности»: ответы тренера содержат
  конкретные предметы/имена героев, не содержат markdown; для неизвестного героя —
  честный fallback; память переживает пересоздание Coach; `recommend_picks` на 8+ драфтах
  возвращает роли корректно; `get_live_gsi_snapshot` не падает на мусоре.
- Прогнать полный набор тестов, добиться зелёного результата; отчёт Lead'у: список
  падений/причин, статистика (было 269 тестов, стало N).
- Обновить README.md: раздел про умного тренера (база знаний на всех героев, брифинг по
  GSI, память игрока, защита от галлюцинаций, умные fallback'и), не ломая остальные
  разделы. Только после завершения A/B/C.

---

## Итог (выполнено)

- **База знаний:** `aidota/meta_db.py` — 127 героев (русские имена, роли, теги угроз,
  контрпики/контрится, синергии, билды по ролям), новые API `get_hero_counters`,
  `get_skill_order`, `get_hero_threats`, `get_lane_tips`, умный `recommend_picks`
  (учёт драфта, синергий, банов, тегов). Тесты: `tests/test_meta_db_extra.py` (44).
- **Контекст:** `aidota/context.py` — богатый GSI-снимок (командные составы, нейтралка,
  аганим/шард, байбек, ТП, угрозы, цели), публичный `analyze_enemy_threats`, докрученный
  детектор ошибок и `AutonomousCoachAgent` (роль/герой, аегис/Рошан, паверспайки врага,
  анти-дубли). Тесты: `tests/test_context_extra.py` (27).
- **Мозг тренера:** `aidota/coach_brain.py` (детекция типа вопроса, тактический бриф,
  retrieval знаний, промпты с защитой от галлюцинаций, санитайзер для озвучки),
  `aidota/coach_memory.py` (долговременная память: реплики, уроки матчей, факты),
  интеграция в `aidota/coach.py` (маршрутизация знаний по упомянутому герою, «холодная»
  температура для фактов, tool-loop до 2 раундов, умные fallback, память в разборе
  матчей, failover на бесплатный ИИ). Тесты: `tests/test_coach_smart.py` (36).
- **QA:** `tests/test_coach_upgrade_qa.py` (24 сквозных теста), README обновлён.
- **Тесты:** было 269 → стало **400, все OK** (`python -m unittest discover -s tests -t .`).
- **Проверено вживую:** через бесплатный резервный ИИ вопрос «что собрать против
  фантомки» дал ответ по базе знаний (Monkey King Bar / Bloodthorn / Silver Edge), т.е.
  маршрутизация «герой из вопроса → знания → LLM» работает end-to-end.

