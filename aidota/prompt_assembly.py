"""Трёхсегментная сборка промпта для стабильного KV-кэша префикса (Fork A1)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_reasoning_markup(text: str) -> str:
    """Удаляет <think>-блоки из истории диалога перед переиспользованием."""
    if not text:
        return ""
    return _THINK_TAG_RE.sub("", text).strip()


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    """Промпт, строго разделённый на стабильный префикс и динамический суффикс.

    - ``static_prefix`` (Segment 0): личность тренера, строгие правила формата/речи.
    - ``match_prefix`` (Segment 1): стабильная часть на время матча (роль, пул, драфт).
    - ``dynamic_suffix`` (Segment 2): изменяемая на каждом тике/ходе информация
      (текущее время/HP/золото, тактический бриф, знания, личная цель, память).
    """

    static_prefix: str
    match_prefix: str = ""
    dynamic_suffix: str = ""

    @property
    def system_prefix(self) -> str:
        """Байтово-стабильный системный префикс (Segment 0 + Segment 1)."""
        parts = [p.strip() for p in (self.static_prefix, self.match_prefix) if p and p.strip()]
        return "\n\n".join(parts)

    def combined_system_for_legacy(self) -> str:
        """Объединённый вид для обратной совместимости (без использования в KV-пути)."""
        parts = [
            p.strip()
            for p in (self.static_prefix, self.match_prefix, self.dynamic_suffix)
            if p and p.strip()
        ]
        return "\n\n".join(parts)

    def build_messages(
        self,
        history_turns: Sequence[tuple[str, str]],
        user_prompt: str,
        *,
        char_budget: int = 6000,
        epoch_size: int = 4,
    ) -> list[dict[str, str]]:
        """Формирует список сообщений: неизменный system, эпохи истории, суффикс + запрос."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prefix}
        ]
        trimmed = trim_history_epoch(
            history_turns, char_budget=char_budget, epoch_size=epoch_size
        )
        for question, answer in trimmed:
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": answer})

        suffix = self.dynamic_suffix.strip()
        prompt_clean = (user_prompt or "").strip()
        if suffix and prompt_clean:
            tail = f"{suffix}\n\nЗАПРОС ИГРОКА:\n{prompt_clean}"
        elif suffix:
            tail = suffix
        else:
            tail = prompt_clean
        messages.append({"role": "user", "content": tail})
        return messages


def trim_history_epoch(
    history_turns: Sequence[tuple[str, str]],
    *,
    char_budget: int = 6000,
    epoch_size: int = 4,
) -> list[tuple[str, str]]:
    """Обрезает историю целыми эпохами, чтобы не ломать KV-кэш при каждом новом вопросе."""
    cleaned: list[tuple[str, str]] = []
    for q, a in history_turns:
        q_clean = strip_reasoning_markup(str(q or "")).strip()
        a_clean = strip_reasoning_markup(str(a or "")).strip()
        if q_clean and a_clean:
            cleaned.append((q_clean, a_clean))

    if not cleaned:
        return []

    budget = max(400, int(char_budget))
    step = max(1, int(epoch_size))
    start = 0

    def _chars(pairs: Sequence[tuple[str, str]]) -> int:
        return sum(len(q) + len(a) for q, a in pairs)

    while start + step < len(cleaned) and _chars(cleaned[start:]) > budget:
        start += step

    # Если даже последняя эпоха превышает бюджет, отступаем по одному ходу внутри неё.
    while start < len(cleaned) - 1 and _chars(cleaned[start:]) > budget:
        start += 1

    return cleaned[start:]


def build_match_stable_prefix(
    *,
    position: int,
    mmr_bracket: str = "",
    hero_pool: dict[str, list[str]] | None = None,
    signature_heroes: Iterable[str] = (),
    my_picks: Iterable[str] = (),
    enemy_picks: Iterable[str] = (),
) -> str:
    """Строит Segment 1 (фиксированный на матч профиль и состав команд, детерминированно)."""
    lines: list[str] = [f"ПАРАМЕТРЫ МАТЧА:\n- Позиция игрока: Pos {int(position)}"]
    mmr = str(mmr_bracket or "").strip()
    if mmr:
        lines.append(f"- Ранг игрока: {mmr}")
    if hero_pool:
        pool_parts: list[str] = []
        for pos in sorted(hero_pool, key=lambda key: str(key)):
            names = [str(n).strip() for n in (hero_pool.get(pos) or []) if str(n).strip()]
            if names:
                pool_parts.append(f"поз.{pos}: {', '.join(names[:5])}")
        if pool_parts:
            lines.append(f"- Пул героев: {'; '.join(pool_parts[:5])}")
    sigs = [str(h).strip() for h in signature_heroes if str(h).strip()]
    if sigs:
        lines.append(f"- Сигнатурные герои: {', '.join(sigs[:5])}")

    allies = sorted({str(h).strip() for h in my_picks if str(h).strip()})
    enemies = sorted({str(h).strip() for h in enemy_picks if str(h).strip()})
    if allies:
        lines.append(f"- Союзники в драфте: {', '.join(allies)}")
    if enemies:
        lines.append(f"- Враги в драфте: {', '.join(enemies)}")
    return "\n".join(lines)


def build_dynamic_suffix(
    *,
    profile_text: str = "",
    custom_focus: str = "",
    memory_summary: str = "",
    gsi_summary: str = "",
    brief_text: str = "",
    knowledge: Iterable[str] = (),
    extra_sections: Iterable[str] = (),
) -> str:
    """Собирает Segment 2 — динамический хвост конкретного хода/тика."""
    parts: list[str] = []
    if profile_text and profile_text.strip():
        parts.append("ПРОФИЛЬ ИГРОКА:\n" + profile_text.strip())
    focus = " ".join(str(custom_focus or "").split())[:180]
    if focus:
        parts.append(f"ЛИЧНАЯ ЦЕЛЬ ИГРОКА В ЭТОМ МАТЧЕ: {focus}.")
    if memory_summary and memory_summary.strip():
        parts.append("ДОЛГОВРЕМЕННАЯ ПАМЯТЬ:\n" + memory_summary.strip())
    if gsi_summary and gsi_summary.strip():
        parts.append(f"ТЕКУЩАЯ СИТУАЦИЯ В МАТЧЕ (GSI Live):\n{gsi_summary.strip()}")
    if brief_text and brief_text.strip():
        parts.append(f"ТАКТИЧЕСКИЙ БРИФ:\n{brief_text.strip()}")
    knowledge_list = [str(k).strip() for k in knowledge if str(k).strip()]
    if knowledge_list:
        parts.append("ЗНАНИЯ ПО ВОПРОСУ:\n" + "\n".join(f"- {k}" for k in knowledge_list))
    for sec in extra_sections:
        if sec and str(sec).strip():
            parts.append(str(sec).strip())
    return "\n\n".join(parts)
