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
UNKNOWN = "unknown"


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
    (EXIT, ("выход", "пока", "заверши", "отключись", "выключись", "до свидания", "закройся")),
    (SILENCE, ("замолчи", "молчи", "тихо", "стоп", "без звука", "заткнись", "выключи голос", "не говори")),
    (VOICE, ("говори", "включи голос", "включи звук", "озвучь", "озвуч", "со звуком")),
    (REPEAT, ("повтори", "ещё раз", "еще раз", "повтор")),
    (OVERLAY_SHOW, ("покажи окно", "покажи оверлей", "открой окно", "верни окно", "покажи текст")),
    (OVERLAY_HIDE, ("скрой окно", "скрой оверлей", "спрячь окно", "убери окно", "скрой текст")),
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
    (PICK_ADVICE, ("кого мне пикнуть", "кого пикнуть", "что пикнуть", "кого взять из моих",
                   "кого взять", "посоветуй героя", "какого героя взять", "из моего пула",
                   "из пула", "какой герой из моих")),
    (DRAFT, ("драфт", "пики", "пик", "баны", "бан", "банить", "кого брать", "кого пик", "пикнуть", "контрпик", "драфту")),
    (STATUS, ("статус", "что по игре", "счёт", "счет", "таймер", "сколько времени", "что происходит", "состояние игры")),
    (BUY_NEXT, ("что купить дальше", "что дальше купить", "какой шмот", "какой шмот брать",
                "что собрать против них", "что собрать дальше", "следующий предмет",
                "следующий шмот", "что собрать против")),
    (ITEMS, ("купить", "предмет", "собери", "собрать", "сборка", "сборк", "закуп", "закупит", "билд", "что взять", "что брать", "артефакт")),
    (GAME_ANALYSIS, ("что я делаю не так", "я делаю не так", "делаю не так", "мои ошибки",
                     "ошибки", "анализ игры", "разбери игру", "что сейчас делать",
                     "какой план", "план на игру", "разбор игры")),
    (ADVICE, ("совет", "посоветуй", "подскажи", "как играть", "что делать", "анализ", "ситуация", "коуч", "как быть", "что не так", "объясни", "разбери")),
)

# Короткие слова сверяем как отдельные слова, иначе «пока» находится внутри «покажи».
_STRICT_WORDS = {"пока", "бан", "пик", "тихо", "стоп", "мета", "топ", "счёт", "счет"}

# Героя ищем только для команд, где он реально нужен.
_HERO_COMMANDS = {ITEMS, ADVICE}

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


def _strip_triggers(text: str) -> str:
    result = text
    for phrase in _TRIGGER_PHRASES:
        result = result.replace(phrase, " ")
    return result


def parse_command(text: str, catalog: HeroCatalog | None = None) -> Intent:
    """Преобразует распознанную фразу в намерение."""
    raw = (text or "").strip()
    if not raw:
        return Intent(UNKNOWN, raw=raw)
    lowered = re.sub(r"\s+", " ", raw.lower().replace("ё", "е"))

    for kind, keywords in _KEYWORDS:
        if _contains(lowered, keywords):
            hero = None
            if kind in _HERO_COMMANDS:
                hero = hero_from_text(_strip_triggers(lowered), catalog)
            if kind == SET_POSITION:
                return Intent(kind, position=_detect_position(lowered), raw=raw)
            if kind == SWITCH_MODEL:
                return Intent(kind, arg=_model_target(lowered), raw=raw)
            if kind == SET_PTT_KEY:
                return Intent(kind, arg=_ptt_key_target(lowered), raw=raw)
            if kind == TOGGLE_PTT:
                return Intent(kind, arg=_toggle_ptt_target(lowered), raw=raw)
            return Intent(kind, hero=hero, raw=raw)

    # Отдельного героя без команды тоже считаем выбором героя.
    hero = hero_from_text(lowered, catalog)
    if hero is not None:
        return Intent(SELECT_HERO, hero=hero, raw=raw)

    # «антимаг» vs «антимаги» и прочие падежи — нечёткий поиск по словам
    if catalog is not None and catalog.available:
        match = catalog.find(lowered, threshold=0.78)
        if match is not None and match.score >= 0.78:
            return Intent(SELECT_HERO, hero=match.hero, raw=raw, hero_score=match.score)

    return Intent(UNKNOWN, raw=raw)


def help_text() -> str:
    return (
        "Что я умею:\n"
        "• «клавиша <кнопка>» — сменить клавишу push-to-talk (например: клавиша v, клавиша f8, mouse4, capslock);\n"
        "• «включи рацию» / «свободный микрофон» — включить или выключить режим push-to-talk;\n"
        "• «что купить на <герой>» / «сборка <герой>» — предметы и порядок прокачки;\n"
        "• «что купить дальше» / «что собрать против них» — следующий предмет под ситуацию;\n"
        "• «кого мне пикнуть» / «кого взять из моих» — пик из вашего пула под драфт;\n"
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
