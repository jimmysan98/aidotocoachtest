"""LaneMatchupSkill: автоматический краткий план на линию в начале матча.

Формирует чёткий план на стадию лайнинга (первые 8–10 минут) на основе:
- выбранной роли (позиция 1–5 из roles.py)
- нашего героя
- героя оппонента на линии (из GSI / драфта / курсора HeroVision)
"""

from __future__ import annotations

import logging
from typing import Any

from ..roles import position_label

log = logging.getLogger("aidota.skills.lane_matchup")

_POSITION_TIPS: dict[int, str] = {
    1: "Держите крипов ближе к своей башне через агр. Не жалейте золото на танго и стик, фокуситесь на добивании и отходе в лесной лагерь с 6–7 уровня.",
    2: "Заблочьте первую пачку крипов на рампе. Контролируйте активные руны каждые две минуты и водяные руны на 2:00 и 4:00.",
    3: "Максимально мешайте фармить вражескому керри. Выбивайте ресурсы, пушьте линию под башню и будьте готовы забрать лотус в пруду на третьей минуте.",
    4: "Заблочьте вражеский большой лагерь сентри-вардом. Разменивайтесь по хп с пятёркой, заберите лотус на 3:00 и будьте готовы выйти в мид под активную руну на 6:00.",
    5: "Сделайте отвод на малый лагерь на :17 или :47 секунде для сброса линии. Обеспечьте керри вижн, держите ресурсы и на 6:40 выдвигайтесь к руне мудрости.",
}

_ENEMY_SPECIFIC_TIPS: dict[str, str] = {
    "viper": "Против Вайпера не стойте в луже и купите побольше фласок — выигрывайте по макро.",
    "huskar": "Против Хускара избегайте затяжных разменов на 1–2 уровне, играйте аккуратно до прихода саппорта.",
    "pudge": "Против Пуджа держитесь за крипами, чтобы он не зацепил хуком, и не стойте на хайграунде без вижна.",
    "bristleback": "Против Бристлбека купите стик первым предметом и не бейте его в спину.",
    "axe": "Против Акса (Axe) не стойте в толпе своих крипов во время его захода с крутилкой и сбивайте Battle Hunger добиванием.",
    "timbersaw": "Против Тимбера держитесь дальше от деревьев и сбивайте ему пассивку издалека.",
}


class LaneMatchupSkill:
    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)

    def get_plan(
        self,
        position: int = 1,
        hero_name: str | None = None,
        enemy_hero: str | list[str] | None = None,
        is_turbo: bool = False,
        enemy_heroes: list[str] | None = None,
    ) -> str:
        """Возвращает краткий и практичный план на линию (3–4 предложения)."""
        pos_idx = position if 1 <= position <= 5 else 1
        pos_str = position_label(pos_idx)
        parts = [f"План на линию ({pos_str} Керри):" if pos_idx == 1 else f"План на линию ({pos_str}):"]

        if hero_name:
            parts.append(f"Играете за {hero_name}.")

        pos_tip = _POSITION_TIPS.get(pos_idx, _POSITION_TIPS[1])
        parts.append(pos_tip)

        enemies = []
        if enemy_heroes:
            enemies.extend(enemy_heroes)
        if isinstance(enemy_hero, list):
            enemies.extend(enemy_hero)
        elif isinstance(enemy_hero, str):
            enemies.append(enemy_hero)

        for eh in enemies:
            low_enemy = str(eh).lower()
            matched_enemy_tip = None
            for key, tip in _ENEMY_SPECIFIC_TIPS.items():
                if key in low_enemy:
                    matched_enemy_tip = tip
                    break
            if matched_enemy_tip:
                parts.append(matched_enemy_tip)
            else:
                parts.append(f"Против {eh} следите за откатом его ключевых способностей.")

        if is_turbo:
            parts.append("В Турбо вышки падают быстрее — не затягивайте лайнинг дольше 6-й минуты.")

        return " ".join(parts)

    get_lane_plan = get_plan

