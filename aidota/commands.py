"""Разбор голосовых/текстовых команд. Чистая логика без сети и звука."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .heroes import HeroCatalog, normalize

# Виды намерений
ITEMS = "items"
ADVICE = "advice"
META = "meta"
DRAFT = "draft"
STATUS = "status"
HELP = "help"
EXIT = "exit"
SILENCE = "silence"
VOICE = "voice"
REPEAT = "repeat"
OVERLAY_SHOW = "overlay_show"
OVERLAY_HIDE = "overlay_hide"
SELECT_HERO = "select_hero"
SET_POSITION = "set_position"
PICK_ADVICE = "pick_advice"
GAME_ANALYSIS = "game_analysis"
BUY_NEXT = "buy_next"
SWITCH_MODEL = "switch_model"
SET_PTT_KEY = "set_ptt_key"
TOGGLE_PTT = "toggle_ptt"
SETTINGS = "settings"
SET_API_KEY = "set_api_key"
MATCH_ANALYSIS = "match_analysis"
START_ITEMS = "start_items"
SKILLS_ORDER = "skills_order"
COUNTER_ITEMS = "counter_items"
MID_SIX = "mid_six"
PULL_ALERT = "pull_alert"
STACK_ALERT = "stack_alert"
WISDOM_ALERT = "wisdom_alert"
TORMENTOR_ALERT = "tormentor_alert"
NEUTRALS_ALERT = "neutrals_alert"
ROSHAN_DOWN = "roshan_down"
ENEMY_ULT = "enemy_ult"
LANE_PLAN = "lane_plan"
GREETING = "greeting"
CHAT = "chat"
UNKNOWN = "unknown"

ASTRA_REGEX = re.compile(r"(?<![0-9a-zа-яё])(?:астра|астру|астре|астрой|остра|астрочка|astra)(?![0-9a-zа-яё])", re.IGNORECASE)
JARVIS_REGEX = re.compile(r"(?<![0-9a-zа-яё])(?:джарвис|жарвис|джарвес|ярвис|jarvis|джарвиса|джарвису|джпрвис)(?![0-9a-zа-яё])", re.IGNORECASE)



@dataclass(frozen=True)
class Intent:
    kind: str
    hero: dict[str, Any] | None = None
    raw: str = ""
    hero_score: float = 0.0
    position: int | None = None
    arg: str | None = None

    @property
    def hero_name(self) -> str | None:
        if not self.hero:
            return None
        return self.hero.get("localized_name") or self.hero.get("name")


# Порядок важен: более специфичные фразы проверяются раньше.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (GREETING, (
        "астра привет", "привет астра", "здравствуй астра", "хай астра", "астра ты тут", "астра на связи",
        "джарвис привет", "привет джарвис", "здравствуй джарвис", "хай джарвис", "джарвис ты тут", "джарвис на связи",
    )),
    (EXIT, ("выход", "пока", "заверши", "отключись", "выключись", "до свидания", "закройся")),

    (SILENCE, ("замолчи", "молчи", "тихо", "стоп", "без звука", "заткнись", "выключи голос", "не говори")),
    (VOICE, ("говори", "включи голос", "включи звук", "озвучь", "озвуч", "со звуком")),
    (REPEAT, ("повтори", "ещё раз", "еще раз", "повтор")),
    (OVERLAY_SHOW, ("покажи окно", "покажи оверлей", "открой окно", "верни окно", "покажи текст")),
    (OVERLAY_HIDE, ("скрой окно", "скрой оверлей", "спрячь окно", "убери окно", "скрой текст")),
    (SETTINGS, ("настройки", "открой настройки", "покажи настройки", "параметры", "settings")),
    (SET_API_KEY, ("апи ключ", "api key", "ключ ии", "установи ключ", "поставь ключ", "api-key")),
    (HELP, ("помощь", "что умеешь", "команды", "справка", "help", "как пользоваться")),
    (SWITCH_MODEL, ("переключи модель", "смени модель", "переключи на", "смени на", "поставь модель",
                    "включи модель", "используй модель", "модель на", "переключись на",
                    "другая модель", "смени ии", "переключи ии", "поменяй модель")),
    (SET_PTT_KEY, ("клавиша микрофона", "клавиша рации", "кнопка рации", "смени клавишу",
                   "поменяй клавишу", "поставь клавишу", "кнопка микрофона", "клавиша на", "кнопка на",
                   "клавиша", "кнопка", "бинд", "ptt key", "ptt")),
    (TOGGLE_PTT, ("включи рацию", "включи push to talk", "включи пуш ту ток", "режим рации",
                  "режим клавиши", "выключи рацию", "выключи push to talk", "свободный микрофон",
                  "постоянный микрофон", "постоянный ввод", "push to talk")),
    (SET_POSITION, ("я на позиции", "моя позиция", "я керри", "я кэрри", "я мид", "на миду",
                    "в миду", "я тройка", "на тройке", "я четверка", "на четверке", "я саппорт",
                    "я пятерка", "я пятёрка", "я единица", "я двойка", "я оффлейн",
                    "я офлейнер", "я хард", "я софт", "позиция", "позиции")),
    (META, ("мета", "топ геро", "топ по винрейт", "сильные герои", "лучшие герои", "винрейт", "winrate")),
    (PICK_ADVICE, (
        "кого мне пикнуть", "кого пикнуть", "что пикнуть", "кого взять из моих",
        "кого взять", "посоветуй героя", "какого героя взять", "из моего пула",
        "из пула", "какой герой из моих", "чем играть", "на чем играть", "на ком играть",
        "кем играть", "кого выбрать", "какого героя выбрать", "кем сыграть", "на ком сыграть",
    )),
    (DRAFT, ("драфт", "пики", "пик", "баны", "бан", "банить", "кого брать", "кого пик", "пикнуть", "контрпик", "драфту")),
    (STATUS, ("статус", "что по игре", "счёт", "счет", "таймер", "сколько времени", "что происходит", "состояние игры")),
    (BUY_NEXT, ("что купить дальше", "что дальше купить", "какой шмот", "какой шмот брать",
                "что собрать против них", "что собрать дальше", "следующий предмет", "следующий шмот")),
    (COUNTER_ITEMS, (
        "что купить против", "что собрать против", "что брать против", "какой шмот против",
        "что взять против", "предметы против", "предмет против", "сборка против",
        "контрпредмет", "контрпредметы", "контр предметы", "контр шмот",
        "как контрить", "как законтрить", "чем контрить", "чем законтрить",
        "как играть против", "против кого", "против",
    )),
    (MID_SIX, (
        "у мидера 6", "мидер 6", "мидер шестой", "мидер апнул 6", "мидер шестого уровня",
        "вражеский мидер 6", "вражеский мидер шестой", "мидер ушел", "мидер ушёл",
        "мидер сс", "мид сс", "мидер пропал",
    )),
    (PULL_ALERT, (
        "когда отвод", "когда делать отвод", "тайминг отвода", "время отвода",
        "напомни про отвод", "напомни отвод", "отвод крипов", "как делать отвод",
        "отвод", "отводы",
    )),
    (STACK_ALERT, (
        "когда стакать", "тайминг стаков", "время стакать", "как стакать",
        "напомни про стаки", "напомни стаки", "стак нейтралов", "стакать",
        "стаки", "стак",
    )),
    (WISDOM_ALERT, (
        "руна мудрости", "руны мудрости", "когда руна мудрости", "тайминг мудрости",
        "время руны мудрости", "мудрость", "руна опыта",
    )),
    (TORMENTOR_ALERT, (
        "терзатель", "терзателя", "когда терзатель", "тайминг терзателя", "время терзателя", "бесплатный шард", "шард",
    )),
    (NEUTRALS_ALERT, (
        "нейтралки", "нейтралка", "нейтральные предметы", "когда нейтралки", "тайминг нейтралок", "тир нейтралок", "разряд нейтралок",
    )),
    (START_ITEMS, (
        "что купить на старте", "что брать на старте", "стартовый закуп", "закуп на старте",
        "закуп в начале", "начальный закуп", "стартовые предметы", "стартовый шмот",
        "закуп на первую минуту", "с чего начать закуп", "что в начале купить", "что купить в начале",
        "закуп на 0", "закуп на ноль", "на старте",
    )),
    (SKILLS_ORDER, (
        "что качать", "какой скилл максить", "порядок прокачки", "что максить",
        "прокачка скиллов", "как качать способности", "прокачка способностей",
        "какой навык качать", "как качать скиллы", "какой скилл прокачать",
        "какой скилл мне прокачать", "какой скилл качать", "какой скилл",
        "какие скиллы качать", "какие скилы качать", "какие скиллы",
        "какой мне сейчас скилл прокачать", "какой мне сейчас скрил прокачать",
        "какой сейчас скилл", "что мне прокачать", "что прокачать",
        "скилл прокачать", "скил прокачать", "скилл качать", "скил качать",
        "прокачать способность", "какую способность качать", "какую способность прокачать",
        "способность прокачать", "какой навык", "прокачать", "качать",
    )),
    (ITEMS, ("купить", "покупать", "покупай", "предмет", "собери", "собрать", "сборка", "сборк", "закуп", "закупит", "закупать", "билд", "что взять", "что брать", "артефакт")),
    (MATCH_ANALYSIS, ("разбор матча", "анализ матча", "последний матч", "последняя игра",
                      "разбор последней игры", "как я сыграл", "статистика матча",
                      "статистика последней игры", "разбери матч", "проанализируй матч",
                      "разбор матчей", "анализ последней игры")),
    (ROSHAN_DOWN, ("рошан упал", "рошан умер", "минус рошан", "убили рошана", "рошан кд", "таймер рошана")),
    (ENEMY_ULT, (
        "равага ушла", "купол ушел", "хроносфера ушла", "блэкхол ушел", "блэк хол ушел",
        "дум ушел", "глобал ушел", "эхо слэм ушел", "эхослэм ушел", "арена ушла",
        "равага кд", "купол кд", "блэкхол кд", "дум кд", "глобал кд",
        "ульт ушел", "ульта ушла", "ульт в кд", "ульт", "ульта", "ультанул", "ультанула", "ультимейт",
    )),
    (LANE_PLAN, ("план на линию", "лайн матчап", "как стоять линию", "что делать на линии", "план на лайн", "матчап на линии")),
    (GAME_ANALYSIS, ("что я делаю не так", "я делаю не так", "делаю не так", "мои ошибки",
                     "ошибки", "анализ игры", "разбери игру", "что сейчас делать",
                     "какой план", "план на игру", "разбор игры")),
    (ADVICE, (
        "совет", "посоветуй", "подскажи", "как играть", "что делать", "как выиграть",
        "как победить", "как стоять", "анализ", "ситуация", "коуч", "как быть",
        "что не так", "объясни", "разбери", "дай совет",
    )),
)

# Короткие слова сверяем как отдельные слова, иначе «пока» находится внутри «покажи».
_STRICT_WORDS = {"пока", "бан", "пик", "тихо", "стоп", "мета", "топ", "счёт", "счет", "отвод", "отводы", "стак", "стаки", "против"}

# Героя ищем только для команд, где он реально нужен.
_HERO_COMMANDS = {ITEMS, ADVICE, ENEMY_ULT, START_ITEMS, SKILLS_ORDER, COUNTER_ITEMS}

_TRIGGER_PHRASES = (
    "я играю за",
    "играю за",
    "играю на",
    "выбери героя",
    "выбери",
    "герой",
    "героя",
    "про героя",
    "по герою",
    "для героя",
    "это",
)


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if keyword in _STRICT_WORDS or len(keyword) <= 3:
            pattern = rf"(?<![0-9a-zа-яё]){re.escape(keyword)}(?![0-9a-zа-яё])"
            if re.search(pattern, text):
                return True
        elif keyword in text:
            return True
    return False


def hero_from_text(text: str, catalog: HeroCatalog | None) -> dict[str, Any] | None:
    """Ищет героя во фразе: сначала по словам, потом по всей строке."""
    if catalog is None or not catalog.available or not text:
        return None
    match = catalog.find_in_tokens(text)
    if match is None:
        match = catalog.find(text)
    return match.hero if match else None


# Подсказки для определения позиции; порядок важен — длинные фразы раньше.
_POSITION_HINTS: tuple[tuple[str, int], ...] = (
    ("первой позиции", 1), ("первая позиция", 1), ("первую позицию", 1),
    ("позиции один", 1), ("позиция один", 1),
    ("софт-саппорт", 4), ("софт саппорт", 4), ("софт", 4),
    ("хард-саппорт", 5), ("хард саппорт", 5), ("фулл саппорт", 5),
    ("оффлейн", 3), ("офлейн", 3), ("оффлайнер", 3),
    ("керри", 1), ("кэрри", 1), ("carry", 1),
    ("саппорт", 5),
    ("тройка", 3), ("тройке", 3),
    ("четверка", 4), ("четвёрка", 4),
    ("пятерка", 5), ("пятёрка", 5),
    ("двойка", 2), ("единица", 1), ("единичка", 1),
    ("вторая", 2), ("второй", 2), ("первая", 1), ("первой", 1), ("первый", 1),
    ("третья", 3), ("третий", 3),
    ("четвертая", 4), ("четвёртая", 4), ("четвертый", 4), ("четвёртый", 4),
    ("пятая", 5), ("пятый", 5),
    ("позиция 1", 1), ("позиция 2", 2), ("позиция 3", 3), ("позиция 4", 4), ("позиция 5", 5),
    ("позиция два", 2), ("позиция три", 3), ("позиция четыре", 4), ("позиция пять", 5),
    ("поз 1", 1), ("поз 2", 2), ("поз 3", 3), ("поз 4", 4), ("поз 5", 5),
    ("миду", 2),
)


def _detect_position(text: str) -> int | None:
    """Возвращает позицию 1–5 по фразе («я керри», «я тройка» и т.п.)."""
    for hint, position in _POSITION_HINTS:
        if hint in text:
            return position
    # голое «мид» — только как отдельное слово, чтобы не ловить «пирамида»
    if re.search(r"(?<![a-zа-яё])мид(?![a-zа-яё])", text):
        return 2
    return None


_MODEL_PHRASES = (
    "переключи на", "смени на", "переключись на", "поставь модель", "включи модель",
    "используй модель", "модель на", "переключи модель", "смени модель", "поменяй модель",
)


def _model_target(text: str) -> str | None:
    """Извлекает название модели/пресета после команды смены модели."""
    for phrase in _MODEL_PHRASES:
        if phrase in text:
            rest = text.split(phrase, 1)[1].strip()
            rest = re.sub(r"^(модель|на|в|под)\s+", "", rest).strip()
            rest = re.sub(r"^«|»|'|\"", "", rest).strip()
            rest = re.sub(r"[.,!?…]+$", "", rest).strip()
            if rest:
                return rest
            return None
    return None


_PTT_KEY_PHRASES = (
    "клавиша микрофона", "клавиша рации", "кнопка рации", "смени клавишу на", "поменяй клавишу на",
    "поставь клавишу", "кнопка микрофона", "клавиша на", "кнопка на",
    "смени клавишу", "поменяй клавишу", "клавиша", "кнопка", "бинд", "ptt key", "ptt",
)


def _ptt_key_target(text: str) -> str | None:
    for phrase in _PTT_KEY_PHRASES:
        if phrase in text:
            rest = text.split(phrase, 1)[1].strip()
            rest = re.sub(r"^(на|в|под|для)\s+", "", rest).strip()
            rest = re.sub(r"^«|»|'|\"", "", rest).strip()
            rest = re.sub(r"[.,!?…]+$", "", rest).strip()
            if rest:
                # Убираем пробелы например "мышь 4" -> "мышь4", "f 8" -> "f8"
                rest = re.sub(r"\s+", "", rest)
                return rest
    return None


def _toggle_ptt_target(text: str) -> str:
    if any(w in text for w in ("выключи", "отключи", "свободный", "постоянный", "без рации")):
        return "off"
    return "on"


def _api_key_target(text: str) -> str | None:
    for phrase in ("апи ключ", "api key", "ключ ии", "установи ключ", "поставь ключ", "api-key"):
        if phrase in text:
            rest = text.split(phrase, 1)[1].strip()
            rest = re.sub(r"^(на|в|равен|это)\s+", "", rest).strip()
            rest = re.sub(r"^«|»|'|\"", "", rest).strip()
            rest = re.sub(r"[.,!?…]+$", "", rest).strip()
            if rest:
                return rest
    return None


_MATCH_ID_RE = re.compile(r"\b(\d{7,12})\b")


def _match_target(text: str) -> str | None:
    """Извлекает числовой ID матча (7–12 цифр), если указан."""
    m = _MATCH_ID_RE.search(text)
    if m:
        return m.group(1)
    return None


def _strip_triggers(text: str) -> str:
    result = text
    for phrase in _TRIGGER_PHRASES:
        result = result.replace(phrase, " ")
    return result


def _ult_target(text: str) -> str:
    for word, ability in (
        ("раваг", "ravage"),
        ("тайд", "ravage"),
        ("купол", "chronosphere"),
        ("хроносфер", "chronosphere"),
        ("войд", "chronosphere"),
        ("блэкхол", "black_hole"),
        ("блэк хол", "black_hole"),
        ("энигм", "black_hole"),
        ("дум", "doom"),
        ("глобал", "global_silence"),
        ("сайленс", "global_silence"),
        ("эхо", "echo_slam"),
        ("шейкер", "echo_slam"),
        ("арен", "arena_of_blood"),
        ("марс", "arena_of_blood"),
    ):
        if word in text:
            return ability
    return "ultimate"


def check_wake_word(text: str, active_trainer: str | None) -> tuple[bool, str]:
    """Проверяет строгое обращение по имени активного тренера (Астра / Джарвис).
    Возвращает (is_match, cleaned_text).
    """
    if not text:
        return False, ""
    lowered = re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()
    if active_trainer is None:
        return True, lowered

    trainer = active_trainer.strip().lower()
    has_astra = bool(ASTRA_REGEX.search(lowered))
    has_jarvis = bool(JARVIS_REGEX.search(lowered))

    if trainer in ("астра", "astra"):
        if has_jarvis:
            return False, ""
        if not has_astra:
            return False, ""
        cleaned = ASTRA_REGEX.sub("", lowered).strip()
        cleaned = re.sub(r"^[,:!–—\-\s]+|[,:!–—\-\s]+$", "", cleaned).strip()
        return True, cleaned

    if trainer in ("джарвис", "jarvis"):
        if has_astra:
            return False, ""
        if not has_jarvis:
            return False, ""
        cleaned = JARVIS_REGEX.sub("", lowered).strip()
        cleaned = re.sub(r"^[,:!–—\-\s]+|[,:!–—\-\s]+$", "", cleaned).strip()
        return True, cleaned

    return True, lowered


def parse_command(
    text: str,
    catalog: HeroCatalog | None = None,
    active_trainer: str | None = None,
) -> Intent | None:
    """Преобразует распознанную фразу в намерение.

    Если указан active_trainer (Астра / Джарвис), команда выполняется ТОЛЬКО
    при наличии строгого обращения по имени. При обращении к другому тренеру
    или отсутствии имени возвращается None.
    """
    raw = (text or "").strip()
    if not raw:
        if active_trainer is not None:
            return None
        return Intent(UNKNOWN, raw=raw)

    lowered = re.sub(r"\s+", " ", raw.lower().replace("ё", "е"))

    if active_trainer is not None:
        ok, cleaned_by_trainer = check_wake_word(lowered, active_trainer)
        if not ok:
            return None
        cleaned = cleaned_by_trainer
        if not cleaned:
            return Intent(GREETING, raw=raw)
    else:
        # Backward-compatible: сначала проверяем прямые приветствия
        for kind, keywords in _KEYWORDS:
            if kind == GREETING and _contains(lowered, keywords):
                return Intent(GREETING, raw=raw)

        name_pat = r"(?:астра|астру|астре|астрой|остра|астрочка|astra|джарвис|жарвис|джарвес|ярвис|jarvis|джпрвис)"
        cleaned = re.sub(rf"^{name_pat}\s*[,:!–—\-]?\s*", "", lowered).strip()
        cleaned = re.sub(rf"\s*[,:!–—\-]?\s*{name_pat}$", "", cleaned).strip()
        if not cleaned:
            return Intent(GREETING, raw=raw)

    # Ищем команды сначала по очищенному тексту, потом по исходному
    for text_variant in (cleaned, lowered):
        for kind, keywords in _KEYWORDS:
            if kind == GREETING:
                continue
            if _contains(text_variant, keywords):
                hero = None
                if kind in _HERO_COMMANDS:
                    hero = hero_from_text(_strip_triggers(text_variant), catalog)
                if kind == SET_POSITION:
                    return Intent(kind, position=_detect_position(text_variant), raw=raw)
                if kind == SWITCH_MODEL:
                    return Intent(kind, arg=_model_target(text_variant), raw=raw)
                if kind == SET_PTT_KEY:
                    return Intent(kind, arg=_ptt_key_target(text_variant), raw=raw)
                if kind == TOGGLE_PTT:
                    return Intent(kind, arg=_toggle_ptt_target(text_variant), raw=raw)
                if kind == SET_API_KEY:
                    return Intent(kind, arg=_api_key_target(text_variant), raw=raw)
                if kind == MATCH_ANALYSIS:
                    return Intent(kind, arg=_match_target(text_variant), raw=raw)
                if kind == ENEMY_ULT:
                    return Intent(kind, hero=hero, arg=_ult_target(text_variant), raw=raw)
                if kind == ROSHAN_DOWN:
                    return Intent(kind, raw=raw)
                if kind == LANE_PLAN:
                    return Intent(kind, raw=raw)
                return Intent(kind, hero=hero, raw=raw)

    # Отдельного героя без команды считаем выбором героя ТОЛЬКО если это не вопрос/диалог
    # (например: «я на оракле», «мой герой пудж», «играю за снайпера», или просто «пудж»).
    # Если во фразе есть вопросительные слова или просьба — это вопрос к тренеру (CHAT), а не выбор героя.
    _question_markers = (
        "как", "что", "почему", "зачем", "куда", "где", "когда", "кто", "кому", "чем", "кем", "чего", "чему", "подскажи", "помоги",
        "какой", "какие", "какую", "каком", "какому", "каких", "какая", "какое", "сколько", "собери", "купи", "качать", "прокачать",
        "посоветуй", "подобрать", "взять", "собрать", "делать", "стоит", "надо", "нужно", "играть", "сыграть", "выиграть",
    )
    words = re.findall(r"[0-9a-zа-яёіїєґ]+", cleaned.lower())
    is_question = any(m in words for m in _question_markers)

    if not is_question:
        for text_variant in (cleaned, lowered):
            hero = hero_from_text(text_variant, catalog)
            if hero is not None:
                return Intent(SELECT_HERO, hero=hero, raw=raw)

        # «антимаг» vs «антимаги» и прочие падежи — нечёткий поиск по словам
        if catalog is not None and catalog.available:
            for text_variant in (cleaned, lowered):
                match = catalog.find(text_variant, threshold=0.78)
                if match is not None and match.score >= 0.78:
                    return Intent(SELECT_HERO, hero=match.hero, raw=raw, hero_score=match.score)

    # Если было обращение к активному тренеру (или в тексте назван тренер),
    # любая свободная фраза/вопрос — это обращение к ИИ-тренеру (чат-бот / войс-бот)
    hero = hero_from_text(cleaned, catalog)
    if active_trainer is not None:
        if cleaned:
            return Intent(CHAT, hero=hero, arg=cleaned, raw=raw)
    else:
        # Без strict trainer: если фраза содержала имя тренера («астра, ...» / «джарвис, ...»)
        if cleaned and cleaned != lowered:
            return Intent(CHAT, hero=hero, arg=cleaned, raw=raw)

    return Intent(UNKNOWN, raw=raw)


def help_text() -> str:
    return (
        "Что я умею:\n"
        "• «клавиша <кнопка>» — сменить клавишу push-to-talk (например: клавиша v, клавиша f8, mouse4, capslock);\n"
        "• «включи рацию» / «свободный микрофон» — включить или выключить режим push-to-talk;\n"
        "• «что купить на <герой>» / «сборка <герой>» — предметы и порядок прокачки;\n"
        "• «что купить дальше» / «что собрать против них» — следующий предмет под ситуацию;\n"
        "• «кого мне пикнуть» / «кого взять из моих» — пик из вашего пула под драфт;\n"
        "• «разбор матча [ID]» / «последний матч» / «как я сыграл» — детальный анализ сыгранной игры через OpenDota и ИИ;\n"
        "• «мои ошибки» / «что я делаю не так» / «анализ игры» — разбор ошибок и план;\n"
        "• «совет» / «как играть» — разбор текущей игры по данным GSI;\n"
        "• «драфт» / «кого банить» — анализ пиков и банов;\n"
        "• «мета» / «топ героев» — сильнейшие герои по винрейту;\n"
        "• «статус» — время, счёт, герой, предметы, ближайшие цели;\n"
        "• «я керри» / «я тройка» / «играю в миду» — сменить позицию на матч;\n"
        "• «я играю за пуджа» — запомнить героя (или наведите курсор на героя);\n"
        "• «переключи модель на <имя>» — сменить ИИ-модель или пресет;\n"
        "• «повтори», «замолчи», «говори», «скрой окно», «покажи окно»;\n"
        "• «выход» — завершить работу.\n"
        "Подсказка: если GSI подключён, вашего героя и роль я определю сам."
    )


def normalize_phrase(text: str) -> str:
    """Публичная обёртка для тестов/отладки."""
    return normalize(text)
