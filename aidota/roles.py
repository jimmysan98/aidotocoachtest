"""Роли и позиции Dota 2: метки, нормализация, базовые предикаты."""

from __future__ import annotations

from typing import Any

POSITION_LABELS: dict[int, str] = {
    1: "керри",
    2: "мид",
    3: "оффлейн",
    4: "софт-саппорт",
    5: "хард-саппорт",
}

POSITION_SHORT: dict[int, str] = {
    1: "поз. 1 (керри)",
    2: "поз. 2 (мид)",
    3: "поз. 3 (оффлейн)",
    4: "поз. 4 (софт-саппорт)",
    5: "поз. 5 (хард-саппорт)",
}


def normalize_position(value: Any) -> int | None:
    """Превращает 1–5, '1'–'5' или название роли в номер позиции (1–5)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 5 else None
    text = str(value).strip().lower().replace("ё", "е")
    if text.isdigit():
        return int(text) if 1 <= int(text) <= 5 else None

    aliases: dict[str, int] = {
        "1": 1, "поз1": 1, "позиция1": 1, "керри": 1, "кэрри": 1, "carry": 1,
        "2": 2, "поз2": 2, "позиция2": 2, "мид": 2, "мидер": 2, "mid": 2,
        "3": 3, "поз3": 3, "позиция3": 3, "оффлейн": 3, "офлейн": 3, "оффлайнер": 3, "offlane": 3,
        "4": 4, "поз4": 4, "позиция4": 4, "софт": 4, "софт-саппорт": 4, "софт саппорт": 4,
        "четверка": 4, "четвёрка": 4,
        "5": 5, "поз5": 5, "позиция5": 5, "саппорт": 5, "хард": 5, "хард-саппорт": 5,
        "хард саппорт": 5, "пятерка": 5, "пятёрка": 5, "фулл саппорт": 5,
    }
    if text in aliases:
        return aliases[text]
    # «тройка», «двойка», «единица», «четверка» и т.п.
    numerals = {
        "первая": 1, "первый": 1, "единица": 1, "единичка": 1, "керри": 1,
        "вторая": 2, "второй": 2, "двойка": 2,
        "третья": 3, "третий": 3, "тройка": 3, "тройке": 3,
        "четвертая": 4, "четвёртая": 4, "четвертый": 4, "четвёртый": 4,
        "пятая": 5, "пятый": 5,
    }
    return numerals.get(text)


def position_label(position: int | None) -> str:
    if position is None:
        return "неизвестно"
    return POSITION_SHORT.get(int(position), "неизвестно")


def is_core(position: int | None) -> bool:
    return position in (1, 2, 3)


def is_support(position: int | None) -> bool:
    return position in (4, 5)
