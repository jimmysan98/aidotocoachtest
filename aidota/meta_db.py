"""База знаний Dota 2 Meta DB: все герои, билды, теги угроз, контрпики и рекомендации по драфту.

Содержит:
- ``META_HEROES_DB`` — 127 героев Dota 2 (ключ — короткий id из data/heroes.json) с русским
  именем, атрибутом, позициями, тегами угроз (словарь A2), контрпиками в обе стороны,
  синергиями и эталонными сборками (минимум на одну роль, для популярных героев — на 2+);
- ``SITUATIONAL_COUNTER_RULES`` — правила «тег угрозы -> контр-предметы»;
- ``get_hero_build`` / ``get_hero_counters`` / ``get_skill_order`` / ``get_hero_threats`` /
  ``get_lane_tips`` — знания для тренера (билды, контрпики, прокачка, угрозы, линия);
- ``recommend_picks`` — умный подбор пиков с учётом контрпиков, контроля драфта,
  синергий, ролей и уже взятых/забаненных героев.

Поддерживаются все 5 позиций (1: Керри, 2: Мид, 3: Оффлейн, 4: Софт-саппорт,
5: Хард-саппорт). Данные статические: генерируются из heroes.json и обновляются вручную.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger("aidota.meta_db")

# Маппинг позиций
ROLE_NAMES: dict[int, str] = {
    1: "Керри (Safe Lane)",
    2: "Мидер (Mid Lane)",
    3: "Оффлейнер (Offlane)",
    4: "Софт-саппорт (4-ка)",
    5: "Хард-саппорт (5-ка)",
}

# ---------------------------------------------------------------------------
# A2. Словарь тегов угроз и контр-предметы под каждый тег
# ---------------------------------------------------------------------------
THREAT_TAGS: tuple[str, ...] = (
    "evasion",
    "heal_regen",
    "passives",
    "saves_buffs",
    "magic_burst_cc",
    "single_target_cc",
    "illusions_swarms",
    "right_clickers",
    "high_mobility",
    "invis",
    "summons",
    "silence",
    "global_presence",
    "split_push",
    "tanky",
    "burst",
)

# Тег -> предметы, которые закрывают эту угрозу (только реальные предметы Dota 2).
TAG_COUNTER_ITEMS: dict[str, tuple[str, ...]] = {
    "evasion": ("Monkey King Bar", "Bloodthorn"),
    "heal_regen": ("Spirit Vessel", "Eye of Skadi", "Shiva's Guard"),
    "passives": ("Silver Edge",),
    "saves_buffs": ("Nullifier",),
    "magic_burst_cc": ("Black King Bar", "Pipe of Insight", "Mage Slayer"),
    "single_target_cc": ("Linken's Sphere", "Lotus Orb"),
    "illusions_swarms": ("Mjollnir", "Shiva's Guard", "Battle Fury", "Crimson Guard"),
    "right_clickers": ("Heaven's Halberd", "Ghost Scepter", "Blade Mail"),
    "high_mobility": ("Orchid Malevolence", "Scythe of Vyse", "Rod of Atos", "Gleipnir"),
    "invis": ("Dust of Appearance", "Sentry Ward", "Gem of True Sight", "Silver Edge"),
    "summons": ("Mjollnir", "Crimson Guard", "Battle Fury", "Shiva's Guard"),
    "silence": ("Black King Bar", "Eul's Scepter of Divinity", "Lotus Orb", "Manta Style"),
    "global_presence": ("Black King Bar", "Pipe of Insight", "Lotus Orb"),
    "split_push": ("Boots of Travel", "Manta Style", "Battle Fury"),
    "tanky": ("Spirit Vessel", "Eye of Skadi", "Shiva's Guard", "Diffusal Blade"),
    "burst": ("Black King Bar", "Aeon Disk", "Lotus Orb", "Pipe of Insight"),
}

# Короткие русские описания тегов — для фраз тренера («чем опасен герой»).
TAG_THREAT_PHRASES: dict[str, str] = {
    "evasion": "уклонения и промахи",
    "heal_regen": "лечение и реген",
    "passives": "сильные пассивки",
    "saves_buffs": "сейвы и защитные баффы",
    "magic_burst_cc": "магический бурст и контроль",
    "single_target_cc": "таргетный контроль",
    "illusions_swarms": "иллюзии и толпа юнитов",
    "right_clickers": "сильный райтклик",
    "high_mobility": "мобильность и эскейпы",
    "invis": "невидимость",
    "summons": "саммоны и пуш крипами",
    "silence": "сайленс",
    "global_presence": "глобальное присутствие на карте",
    "split_push": "сплитпуш",
    "tanky": "танкованность и живучесть",
    "burst": "высокий бурст-урон",
}

# Ситуативные контр-предметы по типу угрозы
SITUATIONAL_COUNTER_RULES: list[dict[str, Any]] = [
    {
        "threat": "evasion",
        "description": "Уклонения (Phantom Assassin, Windranger, Butterfly, Radiance)",
        "items": ["Monkey King Bar", "Bloodthorn"],
        "tip": "MKB даёт гарантированное попадание сквозь промахи и увороты.",
    },
    {
        "threat": "heal_regen",
        "description": "Бешеный реген и вампиризм (Morphling, Alchemist, Huskar, Necrophos, Bristleback)",
        "items": ["Spirit Vessel", "Eye of Skadi", "Shiva's Guard"],
        "tip": "Срезает входящее исцеление и вампиризм на 35–45%.",
    },
    {
        "threat": "passives",
        "description": "Сильные пассивные способности (Bristleback, PA, Spectre, Timbersaw, Viper)",
        "items": ["Silver Edge"],
        "tip": "Silver Edge накладывает истощение (Break) на 5 секунд из невидимости.",
    },
    {
        "threat": "saves_buffs",
        "description": "Сейв-артефакты и баффы (Ghost Scepter, Aeon Disk, Glimmer Cape, Force Staff, Windrun)",
        "items": ["Nullifier"],
        "tip": "Nullifier непрерывно развеивает все положительные защитные баффы с цели.",
    },
    {
        "threat": "magic_burst_cc",
        "description": "Тяжелый магический урон и контроль (Lion, Lina, Zeus, Shaman, Jakiro)",
        "items": ["Black King Bar", "Pipe of Insight", "Mage Slayer"],
        "tip": "BKB даёт невосприимчивость к магии; Mage Slayer режет урон вражеского мага на 40%.",
    },
    {
        "threat": "single_target_cc",
        "description": "Направленный контроль и ультимейты (Doom, Legion Duel, Bane Grip, Batrider Lasso)",
        "items": ["Linken's Sphere", "Lotus Orb"],
        "tip": "Linken блокирует ульт, Lotus Orb отражает и снимает негативные эффекты.",
    },
    {
        "threat": "illusions_swarms",
        "description": "Иллюзии и саммоны (Phantom Lancer, Naga Siren, Chaos Knight, Broodmother)",
        "items": ["Mjollnir", "Shiva's Guard", "Battle Fury", "Crimson Guard"],
        "tip": "АоЕ молнии и срез урона нейтрализуют орды иллюзий и пауков.",
    },
    {
        "threat": "right_clickers",
        "description": "Мощные райткликеры (Sven, Ursa, Sniper, Troll Warlord)",
        "items": ["Heaven's Halberd", "Ghost Scepter", "Blade Mail"],
        "tip": "Halberd даёт 5 секунд обезоруживания (дизарма), которое невозможно снять BKB.",
    },
    {
        "threat": "high_mobility",
        "description": "Высокая мобильность и эскейпы (Storm Spirit, Puck, Ember Spirit, Weaver, Void Spirit)",
        "items": ["Orchid Malevolence", "Scythe of Vyse", "Rod of Atos", "Gleipnir"],
        "tip": "Моментальный сайленс или хекс не дают нажать прыжок или ульт.",
    },
    {
        "threat": "invis",
        "description": "Невидимость (Riki, Bounty Hunter, Clinkz, Weaver)",
        "items": ["Dust of Appearance", "Sentry Ward", "Gem of True Sight", "Silver Edge"],
        "tip": "Даст и сентори закрывают инвиз на линии и в драке, Gem — для контроля карты.",
    },
    {
        "threat": "summons",
        "description": "Саммоны и пуш крипами (Nature's Prophet, Broodmother, Beastmaster, Lycan)",
        "items": ["Mjollnir", "Crimson Guard", "Battle Fury", "Shiva's Guard"],
        "tip": "АоЕ-урон и блок физ-урона быстро чистят саммонов и иллюзии.",
    },
    {
        "threat": "silence",
        "description": "Сайленсы (Silencer, Death Prophet, Skywrath Mage, Night Stalker)",
        "items": ["Black King Bar", "Eul's Scepter of Divinity", "Lotus Orb", "Manta Style"],
        "tip": "Сброс сайленса Manta/Eul и BKB возвращают возможность кастовать.",
    },
    {
        "threat": "global_presence",
        "description": "Глобальные ультимейты (Zeus, Spectre, Nature's Prophet, Dawnbreaker)",
        "items": ["Black King Bar", "Pipe of Insight", "Lotus Orb"],
        "tip": "BKB и Pipe переживают глобальный магический урон, вижн — контроль карты.",
    },
    {
        "threat": "split_push",
        "description": "Сплитпуш и давление по линиям (Nature's Prophet, Lycan, Terrorblade, Clinkz)",
        "items": ["Boots of Travel", "Manta Style", "Battle Fury"],
        "tip": "Мобильность и скорость зачистки волн не дают врагу разменять вышки.",
    },
    {
        "threat": "tanky",
        "description": "Толстые танки и живучесть (Bristleback, Centaur Warrunner, Tidehunter)",
        "items": ["Spirit Vessel", "Eye of Skadi", "Shiva's Guard", "Diffusal Blade"],
        "tip": "Срез регена и замедление не дают танку бесконечно восстанавливаться в драке.",
    },
    {
        "threat": "burst",
        "description": "Высокий бурст-урон (Lina, Lion, Tiny, Morphling)",
        "items": ["Black King Bar", "Aeon Disk", "Lotus Orb", "Pipe of Insight"],
        "tip": "Aeon Disk и BKB переживают прокаст, после которого враг остаётся без кулдаунов.",
    },
]


# Детальные эталонные билды 15 популярных героев (сохранены из прежней базы).
# Остальные герои собираются ниже из _HERO_FACTS.
_DETAILED_HEROES_DB: dict[str, dict[str, Any]] = {
    # ------------------ КЕРРИ (ПОЗИЦИЯ 1) ------------------ #
    "antimage": {
        "id": "antimage",
        "name": "Anti-Mage",
        "ru_name": "Антимаг",
        "attr": "agi",
        "roles": [1],
        "tags": ["high_mobility", "split_push", "right_clickers"],
        "counters": ["medusa", "storm_spirit", "leshrac", "zuus", "queenofpain", "puck"],
        "countered_by": ["legion_commander", "bloodseeker", "slardar", "axe", "meepo", "night_stalker"],
        "synergies": ["magnataur", "grimstroke", "disruptor", "treant"],
        "builds": {
            1: {
                "starting": ["Tango", "Quelling Blade", "Circlet", "Slippers of Agility", "Iron Branch"],
                "early_game": ["Power Treads", "Wraith Band", "Magic Wand", "Cornucopia"],
                "core": [
                    {"item": "Battle Fury", "timing": "13-15 мин", "tip": "Основа фарма и сплитпуша"},
                    {"item": "Manta Style", "timing": "20-22 мин", "tip": "Сброс сайленса, сжигание маны"},
                    {"item": "Basher", "timing": "26-28 мин", "tip": "Контроль сквозь BKB"},
                    {"item": "Eye of Skadi", "timing": "32-35 мин", "tip": "Живучесть и замедление"},
                    {"item": "Butterfly / BKB", "timing": "38-40 мин", "tip": "Физ-урон или защита от магии"},
                ],
                "situational": [
                    {"item": "Black King Bar", "trigger": "Много магии/дизейблов", "why": "Когда враги фокусят в контроле"},
                    {"item": "Monkey King Bar", "trigger": "Уклонения у врагов (PA/Butterfly)", "why": "Гарантия попадания"},
                    {"item": "Nullifier", "trigger": "Ghost/Aeon Disk/Force", "why": "Снимает сейвы саппортов"},
                ],
                "shard": "Средний (после Манты) — улучшает Counterspell на отражение союзникам.",
                "scepter": "По ситуации — даёт второй заряд блинка и создает иллюзию при прыжке.",
                "lane_tips": "Фарми крипов под вышкой, следи за маной на блинк, на 6-м уровне делай килл ультом с саппортом.",
            }
        },
    },
    "phantom_assassin": {
        "id": "phantom_assassin",
        "name": "Phantom Assassin",
        "ru_name": "Фантомка",
        "attr": "agi",
        "roles": [1],
        "tags": ["evasion", "passives", "right_clickers", "burst"],
        "counters": ["sniper", "drow_ranger", "zuus", "crystal_maiden", "rubick", "clinkz"],
        "countered_by": ["axe", "razor", "morphling", "shredder", "doom_bringer", "spectre"],
        "synergies": ["magnataur", "slardar", "vengefulspirit", "ogre_magi"],
        "builds": {
            1: {
                "starting": ["Tango", "Quelling Blade", "Circlet", "Slippers of Agility", "Iron Branch"],
                "early_game": ["Power Treads", "Wraith Band", "Magic Wand", "Cornucopia"],
                "core": [
                    {"item": "Battle Fury", "timing": "13-16 мин", "tip": "Ускорение фарма стаков"},
                    {"item": "Desolator", "timing": "19-22 мин", "tip": "Снижение брони и ваншоты критом"},
                    {"item": "Black King Bar", "timing": "23-26 мин", "tip": "Иммунитет к магии для врыва"},
                    {"item": "Basher / Abyssal", "timing": "29-32 мин", "tip": "Стан сквозь иммунитет"},
                    {"item": "Nullifier / Satanic", "timing": "36-40 мин", "tip": "Снятие сейвов или вампиризм"},
                ],
                "situational": [
                    {"item": "Monkey King Bar", "trigger": "Вражеский MKB/увороты", "why": "Против бабочек и ВРки"},
                    {"item": "Satanic", "trigger": "Тяжелые затяжные драки", "why": "Отхил за 1 крит"},
                ],
                "shard": "Высокий (15:00) — Fan of Knives наносит урон от максимального HP и истощает пассивки.",
                "scepter": "Очень высокий (4-м слотом) — сбрасывает кулдауны при убийстве и даёт чистый Blur.",
                "lane_tips": "Добивай крипов кинжалом (Stifling Dagger), копи на БФ и не дерись до покупки БКБ.",
            }
        },
    },
    "juggernaut": {
        "id": "juggernaut",
        "name": "Juggernaut",
        "ru_name": "Джаггернаут",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "passives", "burst"],
        "counters": ["invoker", "tinker", "necrolyte", "pudge", "venomancer"],
        "countered_by": ["axe", "slardar", "windrunner", "ursa", "obsidian_destroyer"],
        "synergies": ["crystal_maiden", "shadow_shaman", "lion", "grimstroke"],
        "builds": {
            1: {
                "starting": ["Tango", "Quelling Blade", "Circlet", "Slippers of Agility", "Iron Branch"],
                "early_game": ["Phase Boots", "Magic Wand", "Wraith Band"],
                "core": [
                    {"item": "Maelstrom / Battle Fury", "timing": "12-15 мин", "tip": "Фарм крипов и молнии"},
                    {"item": "Manta Style / Sange & Yasha", "timing": "19-22 мин", "tip": "Мобильность и статы"},
                    {"item": "Aghanim's Scepter", "timing": "25-28 мин", "tip": "Swiftslash — мини-ульт каждые 15 сек"},
                    {"item": "Eye of Skadi / Butterfly", "timing": "31-35 мин", "tip": "Замедление или физ-урон"},
                    {"item": "Nullifier / Abyssal Blade", "timing": "38-42 мин", "tip": "Снятие Ghost Scepter"},
                ],
                "situational": [
                    {"item": "Black King Bar", "trigger": "Контроль сквозь крутилку", "why": "Если враги ловят вне крутилки"},
                    {"item": "Monkey King Bar", "trigger": "Уклонения", "why": "Для попадания в ультимейте"},
                ],
                "shard": "Высокий (15:00) — ускоряет Blade Fury и бьёт автоатаками.",
                "scepter": "Ключевой кор-предмет (3-4 слот) — Swiftslash гарантирует фраг одиночной цели.",
                "lane_tips": "Крутилка на 1-2 уровне с замедлением саппорта гарантирует убийство вражеской тройки.",
            }
        },
    },
    "faceless_void": {
        "id": "faceless_void",
        "name": "Faceless Void",
        "ru_name": "Войд",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "burst", "high_mobility"],
        "counters": ["storm_spirit", "weaver", "antimage", "queenofpain", "ember_spirit"],
        "countered_by": ["skeleton_king", "abaddon", "omniknight", "vengefulspirit", "pudge"],
        "synergies": ["invoker", "witch_doctor", "jakiro", "snapfire", "skywrath_mage"],
        "builds": {
            1: {
                "starting": ["Tango", "Quelling Blade", "Circlet", "Slippers of Agility", "Iron Branch"],
                "early_game": ["Power Treads", "Wraith Band", "Magic Wand", "Morbid Mask"],
                "core": [
                    {"item": "Maelstrom", "timing": "12-14 мин", "tip": "Скорость фарма и урон в куполе"},
                    {"item": "Manta Style / BKB", "timing": "20-23 мин", "tip": "Сейв от сайленса и дизейблов"},
                    {"item": "Mjollnir", "timing": "26-28 мин", "tip": "Максимальная скорость атаки и молнии"},
                    {"item": "Eye of Skadi / Butterfly", "timing": "32-35 мин", "tip": "Живучесть и урон"},
                    {"item": "Refresher Orb", "timing": "38-42 мин", "tip": "Два купола подряд выигрывают игру"},
                ],
                "situational": [
                    {"item": "Monkey King Bar", "trigger": "Бабочки и увороты", "why": "Удары сквозь промахи в куполе"},
                    {"item": "Nullifier", "trigger": "Aeon Disk у врагов", "why": "Снимает диск внутри хроносферы"},
                ],
                "shard": "Средний — обратный прыжок Time Walk для выживания.",
                "scepter": "Ситуативный — Time Dilation с наложением стана вокруг.",
                "lane_tips": "Используй Time Walk для отката урона после размена. Хроносферу ставь строго по комбе саппортов.",
            }
        },
    },
    "slark": {
        "id": "slark",
        "name": "Slark",
        "ru_name": "Сларк",
        "attr": "agi",
        "roles": [1],
        "tags": ["high_mobility", "passives", "right_clickers"],
        "counters": ["abyssal_underlord", "bristleback", "tidehunter", "shredder", "spectre"],
        "countered_by": ["bloodseeker", "disruptor", "grimstroke", "legion_commander", "faceless_void"],
        "synergies": ["omniknight", "wisp", "ancient_apparition", "dazzle"],
        "builds": {
            1: {
                "starting": ["Tango", "Quelling Blade", "Orb of Venom", "Slippers of Agility", "Iron Branch"],
                "early_game": ["Power Treads", "Magic Wand", "Falcon Blade / Wraith Band"],
                "core": [
                    {"item": "Diffusal Blade", "timing": "11-14 мин", "tip": "Замедление и сжигание маны"},
                    {"item": "Aghanim's Scepter", "timing": "18-21 мин", "tip": "Два длинных Pounce с дальностью"},
                    {"item": "Black King Bar / Manta", "timing": "24-27 мин", "tip": "Сейв от контроля"},
                    {"item": "Disperser / Eye of Skadi", "timing": "30-33 мин", "tip": "Апгрейд диффузов и статы"},
                    {"item": "Abyssal Blade / Butterfly", "timing": "37-40 мин", "tip": "Стан и выживаемость"},
                ],
                "situational": [
                    {"item": "Silver Edge", "trigger": "Пассивки у врагов", "why": "Отключение пассивок с критом"},
                    {"item": "Nullifier", "trigger": "Ghost/Force у саппортов", "why": "Саппорты не уйдут в астрал"},
                ],
                "shard": "Очень высокий (15:00) — Depth Shroud даёт союзникам невидимость и неуязвимость в тучке.",
                "scepter": "Один из лучших в игре: делает Сларка неуловимым с гигантской дальностью прыжков.",
                "lane_tips": "Держи пассивку на воровство ловкости, при опасности жми Dark Pact заранее для диспела стана.",
            }
        },
    },

    # ------------------ МИД (ПОЗИЦИЯ 2) ------------------ #
    "storm_spirit": {
        "id": "storm_spirit",
        "name": "Storm Spirit",
        "ru_name": "Шторм Спирит",
        "attr": "int",
        "roles": [2],
        "tags": ["high_mobility", "magic_burst_cc", "burst"],
        "counters": ["sniper", "tinker", "drow_ranger", "venomancer", "zuus"],
        "countered_by": ["antimage", "silencer", "nyx_assassin", "doom_bringer", "puck"],
        "synergies": ["life_stealer", "slardar", "spirit_breaker", "rattletrap"],
        "builds": {
            2: {
                "starting": ["Tango", "Faerie Fire", "Iron Branch", "Iron Branch", "Circlet"],
                "early_game": ["Bottle", "Power Treads", "Magic Wand", "Null Talisman"],
                "core": [
                    {"item": "Kaya & Sange / Witch Blade", "timing": "13-16 мин", "tip": "Снижение расхода маны"},
                    {"item": "Orchid Malevolence", "timing": "18-21 мин", "tip": "Сайленс и фраги по соло-целям"},
                    {"item": "Black King Bar", "timing": "24-27 мин", "tip": "Неуязвимость для врыва в драку"},
                    {"item": "Bloodstone / Parasma", "timing": "30-34 мин", "tip": "Манареген и вампиризм заклинаний"},
                    {"item": "Scythe of Vyse", "timing": "37-40 мин", "tip": "Моментальный хекс из тумана"},
                ],
                "situational": [
                    {"item": "Linken's Sphere", "trigger": "Направленный контроль (Doom/Duel)", "why": "Блокирует первый спелл"},
                    {"item": "Shiva's Guard", "trigger": "Много физ-урона у врагов", "why": "Броня и АоЕ замедление"},
                ],
                "shard": "Средний — даёт пассивку Overload союзникам рядом.",
                "scepter": "Высокий в лейте — притягивает всех врагов в радиусе ультимейта (черная дыра).",
                "lane_tips": "Контролируй руны воды и активные руны на 6-й минуте с Bottle. Фарми лесные кемпы ремнантами.",
            }
        },
    },
    "invoker": {
        "id": "invoker",
        "name": "Invoker",
        "ru_name": "Инвокер",
        "attr": "uni",
        "roles": [2],
        "tags": ["magic_burst_cc", "global_presence", "summons", "burst"],
        "counters": ["nevermore", "medusa", "skeleton_king", "drow_ranger"],
        "countered_by": ["nyx_assassin", "antimage", "silencer", "puck", "broodmother"],
        "synergies": ["faceless_void", "legion_commander", "axe", "spirit_breaker"],
        "builds": {
            2: {
                "starting": ["Tango", "Circlet", "Iron Branch", "Iron Branch", "Faerie Fire"],
                "early_game": ["Boots of Speed", "Urn of Shadows", "Magic Wand", "Null Talisman"],
                "core": [
                    {"item": "Spirit Vessel / Witch Blade", "timing": "12-15 мин", "tip": "Срез регена или яд"},
                    {"item": "Orchid / Black King Bar", "timing": "19-22 мин", "tip": "Сайленс или маг-иммунитет"},
                    {"item": "Aghanim's Shard", "timing": "22-25 мин", "tip": "Усиление спеллов"},
                    {"item": "Aghanim's Scepter / Octarine", "timing": "28-32 мин", "tip": "Катаклизм и КД спеллов"},
                    {"item": "Refresher Orb / Scythe of Vyse", "timing": "36-40 мин", "tip": "Двойной прокаст или хекс"},
                ],
                "situational": [
                    {"item": "Black King Bar", "trigger": "Много контроля", "why": "Чтобы успевать кастовать комбинации"},
                    {"item": "Linken's Sphere", "trigger": "Соло-таргет ультимейты", "why": "Сейв от инициации"},
                ],
                "shard": "Высокий — усиливает Chaos Meteor или EMP в зависимости от билда.",
                "scepter": "Ключевой — даёт Cataclysm (глобальный двойной Sun Strike по всем врагам).",
                "lane_tips": "Следи за станами союзников на боковых линиях для меткого Sun Strike. Держи сферу Quas для хила.",
            }
        },
    },
    "nevermore": {
        "id": "nevermore",
        "name": "Shadow Fiend",
        "ru_name": "СФ",
        "attr": "agi",
        "roles": [2, 1],
        "tags": ["magic_burst_cc", "burst", "right_clickers"],
        "counters": ["bloodseeker", "sniper", "terrorblade", "huskar"],
        "countered_by": ["tinker", "templar_assassin", "spirit_breaker", "storm_spirit", "ursa"],
        "synergies": ["magnataur", "sand_king", "tidehunter", "rattletrap"],
        "builds": {
            2: {
                "starting": ["Tango", "Faerie Fire", "Iron Branch", "Iron Branch", "Circlet"],
                "early_game": ["Bottle", "Power Treads", "Magic Wand", "Wraith Band"],
                "core": [
                    {"item": "Shadow Blade / Dragon Lance", "timing": "13-16 мин", "tip": "Инициация или дальность"},
                    {"item": "Black King Bar", "timing": "20-23 мин", "tip": "Обязателен для каста Requiem of Souls"},
                    {"item": "Silver Edge / Pike", "timing": "26-28 мин", "tip": "Криты и мобильность"},
                    {"item": "Eye of Skadi / Butterfly", "timing": "32-35 мин", "tip": "Живучесть и урон"},
                    {"item": "Satanic / Swift Blink", "timing": "38-42 мин", "tip": "Вампиризм и мгновенный врыв"},
                ],
                "situational": [
                    {"item": "Eul's Scepter", "trigger": "Магический билд", "why": "Ваншот ультом из ветра"},
                    {"item": "Monkey King Bar", "trigger": "Уклонения", "why": "Против бабочек"},
                ],
                "shard": "Средний — стаки душ наносят критический урон.",
                "scepter": "Высокий — увеличивает максимальное число душ и возвращает волны ульта.",
                "lane_tips": "Стакай койлы на врага. Добивай крипов рукой, а дальника забирай Shadowraze.",
            }
        },
    },

    # ------------------ ОФФЛЕЙН (ПОЗИЦИЯ 3) ------------------ #
    "axe": {
        "id": "axe",
        "name": "Axe",
        "ru_name": "Акс",
        "attr": "str",
        "roles": [3],
        "tags": ["tanky", "single_target_cc", "passives"],
        "counters": ["dazzle", "phantom_lancer", "meepo", "troll_warlord", "slark", "broodmother"],
        "countered_by": ["shredder", "viper", "obsidian_destroyer", "venomancer", "necrolyte"],
        "synergies": ["invoker", "skywrath_mage", "witch_doctor", "jakiro"],
        "builds": {
            3: {
                "starting": ["Tango", "Quelling Blade", "Ring of Protection", "Circlet", "Iron Branch"],
                "early_game": ["Phase Boots", "Magic Wand", "Bracer", "Chainmail"],
                "core": [
                    {"item": "Blink Dagger", "timing": "11-13 мин", "tip": "Главный артефакт для мгновенного Call"},
                    {"item": "Blade Mail", "timing": "15-18 мин", "tip": "Возврат урона во время агра"},
                    {"item": "Black King Bar", "timing": "22-25 мин", "tip": "Защита от сайленса и магии до врыва"},
                    {"item": "Aghanim's Shard", "timing": "25-27 мин", "tip": "Снижение урона врагов от крутилки"},
                    {"item": "Overwhelming Blink / Heart", "timing": "32-36 мин", "tip": "Огромный запас HP и АоЕ урон"},
                ],
                "situational": [
                    {"item": "Pipe of Insight", "trigger": "Сильная магия у врагов", "why": "Аура щита для всей команды"},
                    {"item": "Crimson Guard", "trigger": "Много физ-атак и иллюзий", "why": "Блокирует физ-урон союзникам"},
                ],
                "shard": "Высокий — крутилка снижает урон врагов и настакивает броню.",
                "scepter": "Средний — Berserker's Call накладывает Battle Hunger и даёт броню за каждого врага.",
                "lane_tips": "Агри вражеских крипов на себя рядом с вражеским керри — пассивка Counter Helix моментально снесет ему лицо.",
            }
        },
    },
    "centaur": {
        "id": "centaur",
        "name": "Centaur Warrunner",
        "ru_name": "Кентавр",
        "attr": "str",
        "roles": [3],
        "tags": ["tanky", "global_presence", "burst"],
        "counters": ["sniper", "drow_ranger", "arc_warden", "phantom_assassin"],
        "countered_by": ["life_stealer", "necrolyte", "shredder", "slark"],
        "synergies": ["witch_doctor", "crystal_maiden", "invoker", "pudge"],
        "builds": {
            3: {
                "starting": ["Tango", "Quelling Blade", "Bracer", "Iron Branch", "Iron Branch"],
                "early_game": ["Phase Boots", "Magic Wand", "Bracer", "Cloak"],
                "core": [
                    {"item": "Blink Dagger", "timing": "12-14 мин", "tip": "Врыв со станом Hoof Stomp"},
                    {"item": "Blade Mail / Pipe of Insight", "timing": "17-20 мин", "tip": "Защита и возврат урона"},
                    {"item": "Heart of Tarrasque", "timing": "25-28 мин", "tip": "Бешеный урон с Double Edge"},
                    {"item": "Aghanim's Scepter", "timing": "31-35 мин", "tip": "Hitch a Ride — спасение союзника в тележку"},
                    {"item": "Overwhelming Blink / Shiva", "timing": "38-42 мин", "tip": "Замедление и контроль"},
                ],
                "situational": [
                    {"item": "Lotus Orb", "trigger": "Направленный контроль", "why": "Снимает сайленс и станы"},
                    {"item": "Crimson Guard", "trigger": "Много физ-урона", "why": "Щит для всей команды"},
                ],
                "shard": "Высокий — Double Edge настакивает урон и силу.",
                "scepter": "Уникальный спасательный круг: сажает союзника в тележку, даруя неуязвимость к фокусу.",
                "lane_tips": "Используй Double Edge для добивания крипов и размена. Stampede жми для спасения команды или глобальной контратаки.",
            }
        },
    },
    "slardar": {
        "id": "slardar",
        "name": "Slardar",
        "ru_name": "Слардар",
        "attr": "str",
        "roles": [3],
        "tags": ["tanky", "single_target_cc", "right_clickers"],
        "counters": ["riki", "bounty_hunter", "clinkz", "antimage", "weaver"],
        "countered_by": ["tidehunter", "razor", "ursa", "shadow_demon"],
        "synergies": ["life_stealer", "phantom_assassin", "drow_ranger", "snapfire"],
        "builds": {
            3: {
                "starting": ["Tango", "Quelling Blade", "Bracer", "Iron Branch", "Iron Branch"],
                "early_game": ["Power Treads", "Magic Wand", "Bracer"],
                "core": [
                    {"item": "Blink Dagger", "timing": "11-13 мин", "tip": "Мгновенный Slithereen Crush со станом"},
                    {"item": "Aghanim's Scepter", "timing": "18-21 мин", "tip": "Лужи дают +брони, хп-реген и статус-резист"},
                    {"item": "Black King Bar", "timing": "24-27 мин", "tip": "Неуязвимость в драке"},
                    {"item": "Harpoon / Echo Sabre", "timing": "28-31 мин", "tip": "Быстрые 3 удара для гарантированного баша"},
                    {"item": "Assault Cuirass / Moon Shard", "timing": "35-39 мин", "tip": "Снижение брони и скорость атаки"},
                ],
                "situational": [
                    {"item": "Heart of Tarrasque", "trigger": "Тяжелый урон", "why": "Огромный запас выживаемости"},
                    {"item": "Nullifier", "trigger": "Ghost/Force у врагов", "why": "Враги не уйдут от башей"},
                ],
                "shard": "Высокий — Crush накладывает ульт Corrosive Haze на всех задетых врагов.",
                "scepter": "Один из сильнейших предметов: лужи делают Слардара невероятно живым танком.",
                "lane_tips": "Набивай 3 удара пассивки об крипа, затем делай рывок в героя и давай гарантированный баш с лужей.",
            }
        },
    },

    # ------------------ СОФТ-САППОРТ (ПОЗИЦИЯ 4) ------------------ #
    "rubick": {
        "id": "rubick",
        "name": "Rubick",
        "ru_name": "Рубик",
        "attr": "int",
        "roles": [4],
        "tags": ["magic_burst_cc", "single_target_cc"],
        "counters": ["enigma", "tidehunter", "magnataur", "sand_king", "witch_doctor"],
        "countered_by": ["riki", "clinkz", "storm_spirit", "night_stalker", "phantom_assassin"],
        "synergies": ["pudge", "mirana", "slardar", "centaur"],
        "builds": {
            4: {
                "starting": ["Tango", "Blood Grenade", "Faerie Fire", "Iron Branch", "Iron Branch", "Sentry Ward"],
                "early_game": ["Arcane Boots", "Magic Wand", "Wind Lace"],
                "core": [
                    {"item": "Blink Dagger", "timing": "14-17 мин", "tip": "Позиционка и кража ключевых ультимейтов"},
                    {"item": "Aether Lens", "timing": "19-22 мин", "tip": "Дальность применения способностей"},
                    {"item": "Aghanim's Shard", "timing": "23-25 мин", "tip": "Телекинез союзников для дальнего сейва"},
                    {"item": "Aghanim's Scepter", "timing": "29-33 мин", "tip": "Два украденных заклинания одновременно"},
                    {"item": "Glimmer Cape / Force Staff", "timing": "35-38 мин", "tip": "Сейв себя и команды"},
                ],
                "situational": [
                    {"item": "Aeon Disk", "trigger": "Вражеские прыжки и ваншоты", "why": "Сейв от смерти за 1 секунду"},
                    {"item": "Ghost Scepter", "trigger": "Физ-керри (PA/Clinkz)", "why": "Иммунитет к физ-атакам"},
                ],
                "shard": "Высокий — спасает союзника броском на 850 дальности.",
                "scepter": "Ключевой артефакт в мид-лейте: улучшает украденные спеллы до версии с аганимом.",
                "lane_tips": "Используй Fade Bolt для харасса и среза урона вражеского керри. Поднимай врага в Telekinesis на хайграунд.",
            }
        },
    },
    "spirit_breaker": {
        "id": "spirit_breaker",
        "name": "Spirit Breaker",
        "ru_name": "Бара",
        "attr": "str",
        "roles": [4, 3],
        "tags": ["high_mobility", "global_presence", "single_target_cc", "tanky"],
        "counters": ["tinker", "sniper", "furion", "zuus", "arc_warden"],
        "countered_by": ["abyssal_underlord", "rattletrap", "rubick", "shadow_demon"],
        "synergies": ["life_stealer", "invoker", "storm_spirit", "bloodseeker"],
        "builds": {
            4: {
                "starting": ["Tango", "Orb of Venom", "Iron Branch", "Iron Branch", "Blood Grenade"],
                "early_game": ["Phase Boots", "Magic Wand", "Urn of Shadows", "Wind Lace"],
                "core": [
                    {"item": "Spirit Vessel", "timing": "13-16 мин", "tip": "Срез регена и доп-урон в разгоне"},
                    {"item": "Shadow Blade", "timing": "19-22 мин", "tip": "Невидимый Charge of Darkness"},
                    {"item": "Black King Bar", "timing": "25-28 мин", "tip": "Беспрепятственный разгон в драке"},
                    {"item": "Aghanim's Scepter", "timing": "31-34 мин", "tip": "Разгон сквозь BKB с низким КД"},
                    {"item": "Octarine Core / Shiva", "timing": "37-41 мин", "tip": "Бесконечные чарджи и броня"},
                ],
                "situational": [
                    {"item": "Lotus Orb", "trigger": "Сайленс и таргетные спеллы", "why": "Сброс замедлений"},
                    {"item": "Blade Mail", "trigger": "Фокус в драке", "why": "Возврат урона"},
                ],
                "shard": "Высокий — Planar Pocket перенаправляет вражеский спелл в Бару и даёт сопротивление магии.",
                "scepter": "Превращает Charge of Darkness в пробивающий BKB инструмент инициации каждые 7 секунд.",
                "lane_tips": "Помогай керри на 1-2 уровне, а с получением 2 уровня разгоняйся на мид для неожиданного ганга.",
            }
        },
    },

    # ------------------ ХАРД-САППОРТ (ПОЗИЦИЯ 5) ------------------ #
    "crystal_maiden": {
        "id": "crystal_maiden",
        "name": "Crystal Maiden",
        "ru_name": "ЦМка",
        "attr": "int",
        "roles": [5],
        "tags": ["magic_burst_cc", "single_target_cc", "burst"],
        "counters": ["phantom_assassin", "slark", "terrorblade", "sven"],
        "countered_by": ["bounty_hunter", "nyx_assassin", "rattletrap", "earthshaker", "silencer"],
        "synergies": ["juggernaut", "ursa", "sven", "bristleback"],
        "builds": {
            5: {
                "starting": ["Tango", "Blood Grenade", "Sentry Ward", "Observer Ward", "Iron Branch", "Faerie Fire"],
                "early_game": ["Tranquil Boots", "Magic Wand", "Wind Lace", "Infused Raindrops"],
                "core": [
                    {"item": "Glimmer Cape", "timing": "13-16 мин", "tip": "Невидимость и маг-щит во время ульта"},
                    {"item": "Force Staff", "timing": "19-22 мин", "tip": "Сейв союзников и разрыв дистанции"},
                    {"item": "Blink Dagger / BKB", "timing": "25-28 мин", "tip": "Врыв или защита Freezing Field"},
                    {"item": "Aghanim's Shard", "timing": "29-32 мин", "tip": "Движение и каст спеллов во время ульта"},
                    {"item": "Aghanim's Scepter", "timing": "35-39 мин", "tip": "Frostbite по всем врагам в ультимейте"},
                ],
                "situational": [
                    {"item": "Ghost Scepter", "trigger": "Физ-убийцы (Clinkz/PA)", "why": "Иммунитет к автоатакам"},
                    {"item": "Aeon Disk", "trigger": "Внезапные прокасты", "why": "Спасает от мгновенной смерти"},
                ],
                "shard": "Очень высокий — позволяет медленно двигаться во время Freezing Field и кастовать спеллы.",
                "scepter": "Замораживает любого врага, находящегося в ультимейте более 1.75 секунд.",
                "lane_tips": "Frostbite на вражеского оффлейнера + Blood Grenade + тычки вашего керри — гарантированный First Blood.",
            }
        },
    },
    "witch_doctor": {
        "id": "witch_doctor",
        "name": "Witch Doctor",
        "ru_name": "Витч Доктор",
        "attr": "int",
        "roles": [5, 4],
        "tags": ["heal_regen", "single_target_cc", "burst"],
        "counters": ["spectre", "bristleback", "axe", "centaur", "pudge"],
        "countered_by": ["silencer", "rubick", "sniper", "rattletrap", "spirit_breaker"],
        "synergies": ["faceless_void", "magnataur", "slardar", "mars", "jakiro"],
        "builds": {
            5: {
                "starting": ["Tango", "Blood Grenade", "Sentry Ward", "Iron Branch", "Iron Branch", "Faerie Fire"],
                "early_game": ["Arcane Boots", "Magic Wand", "Wind Lace"],
                "core": [
                    {"item": "Glimmer Cape", "timing": "13-16 мин", "tip": "Невидимость при касте Death Ward"},
                    {"item": "Aghanim's Shard", "timing": "15-18 мин", "tip": "Voodoo Switcheroo — сейв и урон в астрале"},
                    {"item": "Aghanim's Scepter", "timing": "24-27 мин", "tip": "Death Ward отскакивает между всеми врагами"},
                    {"item": "Force Staff / Blink", "timing": "30-33 мин", "tip": "Позиционирование"},
                    {"item": "Black King Bar / Aeon Disk", "timing": "36-40 мин", "tip": "Несбиваемый ультимейт"},
                ],
                "situational": [
                    {"item": "Ghost Scepter", "trigger": "Физ-керри", "why": "Сейв от автоатак"},
                    {"item": "Spirit Vessel", "trigger": "Реген у врагов", "why": "Комбинируется с Maledict"},
                ],
                "shard": "Один из лучших в Dota 2: прячет Доктора в астрал на 3 секунды с атаками Death Ward.",
                "scepter": "Превращает ульт в чистое АоЕ уничтожение всей команды врага за 2 секунды.",
                "lane_tips": "Кидай банку Paralyzing Cask, когда рядом только 2 героя или герой и 1 крип для максимальных отскоков.",
            }
        },
    },
}

# Компактные факты по 112 героям без детального билда: имя, ru_name, атрибут,
# позиции, теги угроз (словарь A2), контрпики в обе стороны, синергии и совет на линию.
# Билды для них собираются шаблоном по роли/атрибуту в _build_compact_hero (contract A1).
_HERO_FACTS: dict[str, dict[str, Any]] = {
    "bane": {
        "name": "Bane",
        "ru_name": "Бейн",
        "attr": "uni",
        "roles": [1, 5],
        "tags": ["single_target_cc", "magic_burst_cc", "heal_regen"],
        "counters": ["storm_spirit", "ember_spirit", "void_spirit", "morphling"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["mirana", "pudge", "leshrac"],
        "lane_tip": "Держи Nightmare для сетапа на союзника-инициатора, а Brain Sap трать на размен — под стан не подставляйся.",
    },
    "bloodseeker": {
        "name": "Bloodseeker",
        "ru_name": "Бладсикер",
        "attr": "agi",
        "roles": [1, 2, 3],
        "tags": ["global_presence", "silence", "heal_regen"],
        "counters": ["antimage", "weaver", "slark", "storm_spirit", "queenofpain"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["spirit_breaker", "legion_commander", "axe"],
        "lane_tip": "Rupture вешай на мобильного героя до его прыжка, Bloodrage жми на себя перед разменом урона.",
    },
    "drow_ranger": {
        "name": "Drow Ranger",
        "ru_name": "Дровка",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "silence", "passives"],
        "counters": ["medusa", "sniper", "zuus", "crystal_maiden"],
        "countered_by": ["axe", "slardar", "spectre", "storm_spirit"],
        "synergies": ["crystal_maiden", "vengefulspirit", "magnataur"],
        "lane_tip": "Gust держи на случай прыжка на тебя, а Marksmanship включай только когда в драке есть прикрытие.",
    },
    "earthshaker": {
        "name": "Earthshaker",
        "ru_name": "Шейкер",
        "attr": "str",
        "roles": [3, 4, 5],
        "tags": ["magic_burst_cc", "single_target_cc", "burst"],
        "counters": ["meepo", "phantom_lancer", "naga_siren", "broodmother", "chaos_knight"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["dark_seer", "enigma", "faceless_void", "witch_doctor"],
        "lane_tip": "Fissure ставь так, чтобы отрезать путь отхода, а Echo Slam прибереги под толпу врагов рядом.",
    },
    "mirana": {
        "name": "Mirana",
        "ru_name": "Мирана",
        "attr": "agi",
        "roles": [1, 2, 4],
        "tags": ["invis", "single_target_cc", "high_mobility"],
        "counters": ["huskar", "spectre", "crystal_maiden", "zuus", "sniper"],
        "countered_by": ["axe", "slardar", "night_stalker", "bounty_hunter"],
        "synergies": ["sand_king", "bounty_hunter", "dark_willow"],
        "lane_tip": "Arrow бросай на звук и по краю экрана, а Leap оставляй на эскейп после размена.",
    },
    "morphling": {
        "name": "Morphling",
        "ru_name": "Морф",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["high_mobility", "tanky", "burst", "heal_regen"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["antimage", "axe", "invoker", "doom_bringer"],
        "synergies": ["earthshaker", "sand_king", "vengefulspirit"],
        "lane_tip": "Держи в запасе Strength-форму под фокус, а Waveform используй и для добивания, и для эскейпа.",
    },
    "phantom_lancer": {
        "name": "Phantom Lancer",
        "ru_name": "Фантом Лансер",
        "attr": "agi",
        "roles": [1],
        "tags": ["illusions_swarms", "evasion", "high_mobility"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["axe", "earthshaker", "sven", "magnataur"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Держи иллюзии между крипами, Doppelganger оставляй на момент, когда в тебя летит контроль.",
    },
    "puck": {
        "name": "Puck",
        "ru_name": "Пак",
        "attr": "int",
        "roles": [2],
        "tags": ["high_mobility", "magic_burst_cc", "silence"],
        "counters": ["sniper", "crystal_maiden", "zuus", "tinker", "lina"],
        "countered_by": ["bloodseeker", "silencer", "disruptor", "doom_bringer"],
        "synergies": ["faceless_void", "magnataur", "sand_king"],
        "lane_tip": "Orb используй и для фарма, и для эскейпа, а Waning Rift наказывай героев без магии.",
    },
    "pudge": {
        "name": "Pudge",
        "ru_name": "Пудж",
        "attr": "str",
        "roles": [3, 4, 5],
        "tags": ["single_target_cc", "tanky", "burst"],
        "counters": ["crystal_maiden", "lina", "zuus", "tinker", "sniper"],
        "countered_by": ["slardar", "ursa", "life_stealer", "doom_bringer"],
        "synergies": ["mirana", "sand_king", "jakiro"],
        "lane_tip": "Hook кидай в момент, когда враг идёт добивать крипа, а Rot держи включённым при размене вплотную.",
    },
    "razor": {
        "name": "Razor",
        "ru_name": "Разор",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["right_clickers", "tanky", "burst"],
        "counters": ["ursa", "slark", "troll_warlord", "phantom_assassin", "life_stealer"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["crystal_maiden", "shadow_demon", "grimstroke"],
        "lane_tip": "Static Link тяни керри до его удара, Plasma Field трать на добивание и харас на линии.",
    },
    "sand_king": {
        "name": "Sand King",
        "ru_name": "Сэнд Кинг",
        "attr": "uni",
        "roles": [3, 4, 5],
        "tags": ["magic_burst_cc", "invis", "burst"],
        "counters": ["phantom_lancer", "meepo", "broodmother", "naga_siren"],
        "countered_by": ["crystal_maiden", "zuus", "lina", "sniper"],
        "synergies": ["pudge", "mirana", "leshrac", "witch_doctor"],
        "lane_tip": "Sand Storm используй для фарма и отхода, а Burrowstrike ставь с расчётом на Epicenter под ним.",
    },
    "sven": {
        "name": "Sven",
        "ru_name": "Свен",
        "attr": "str",
        "roles": [1],
        "tags": ["right_clickers", "magic_burst_cc", "burst"],
        "counters": ["crystal_maiden", "lina", "zuus", "phantom_assassin", "storm_spirit"],
        "countered_by": ["axe", "razor", "slardar", "windrunner"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Storm Hammer бросай после врыва саппорта, а God's Strength держи до момента, когда есть цель бить.",
    },
    "tiny": {
        "name": "Tiny",
        "ru_name": "Тини",
        "attr": "str",
        "roles": [2, 3, 4],
        "tags": ["burst", "magic_burst_cc", "tanky"],
        "counters": ["sniper", "crystal_maiden", "lina", "zuus", "drow_ranger"],
        "countered_by": ["life_stealer", "ursa", "troll_warlord", "slark"],
        "synergies": ["wisp", "faceless_void", "sand_king"],
        "lane_tip": "Avalanche + Toss — твой главный размен, а Toss используй и для сейва союзника из фокуса.",
    },
    "vengefulspirit": {
        "name": "Vengeful Spirit",
        "ru_name": "Венга",
        "attr": "agi",
        "roles": [4, 5],
        "tags": ["single_target_cc", "magic_burst_cc", "saves_buffs"],
        "counters": ["storm_spirit", "puck", "ember_spirit", "void_spirit"],
        "countered_by": ["crystal_maiden", "zuus", "lina", "sniper"],
        "synergies": ["juggernaut", "sven", "drow_ranger", "luna"],
        "lane_tip": "Magic Missile береги на решающий момент, а Vengeful Aura включай в драках рядом с керри.",
    },
    "windrunner": {
        "name": "Windranger",
        "ru_name": "ВРка",
        "attr": "uni",
        "roles": [2, 3, 4],
        "tags": ["evasion", "high_mobility", "single_target_cc"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "lina", "zuus"],
        "countered_by": ["axe", "slardar", "spectre", "doom_bringer"],
        "synergies": ["axe", "sand_king", "faceless_void"],
        "lane_tip": "Windrun жми до размена автоатаками, а Shackleshot кидай так, чтобы зацепить второго героя.",
    },
    "zuus": {
        "name": "Zeus",
        "ru_name": "Зевс",
        "attr": "int",
        "roles": [2, 4],
        "tags": ["magic_burst_cc", "global_presence", "burst"],
        "counters": ["riki", "bounty_hunter", "clinkz", "weaver", "crystal_maiden"],
        "countered_by": ["antimage", "pudge", "night_stalker", "storm_spirit"],
        "synergies": ["faceless_void", "legion_commander", "sand_king"],
        "lane_tip": "Стакай Arc Lightning на линии и бей им по герою, а Thundergod's Wrath забирай добивания по всей карте.",
    },
    "kunkka": {
        "name": "Kunkka",
        "ru_name": "Кунка",
        "attr": "str",
        "roles": [2, 4, 5],
        "tags": ["magic_burst_cc", "burst", "saves_buffs"],
        "counters": ["sniper", "crystal_maiden", "zuus", "lina", "drow_ranger"],
        "countered_by": ["templar_assassin", "puck", "weaver", "storm_spirit"],
        "synergies": ["mirana", "shadow_demon", "magnataur"],
        "lane_tip": "Torrent ставь на путь отхода, а Tidebringer бей по крипам так, чтобы задеть героя.",
    },
    "lina": {
        "name": "Lina",
        "ru_name": "Лина",
        "attr": "int",
        "roles": [2, 4, 5],
        "tags": ["magic_burst_cc", "burst", "right_clickers"],
        "counters": ["crystal_maiden", "zuus", "sniper", "phantom_assassin", "tinker"],
        "countered_by": ["antimage", "storm_spirit", "puck", "weaver"],
        "synergies": ["sand_king", "magnataur", "faceless_void"],
        "lane_tip": "Light Strike Array ставь после стана союзника, а Dragon Slave используй для добивания и хараса.",
    },
    "lion": {
        "name": "Lion",
        "ru_name": "Лион",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "single_target_cc", "burst"],
        "counters": ["storm_spirit", "puck", "ember_spirit", "morphling", "crystal_maiden"],
        "countered_by": ["antimage", "storm_spirit", "slark", "weaver"],
        "synergies": ["sven", "juggernaut", "ursa"],
        "lane_tip": "Hex держи на мобильного героя, а Earth Spike — на инициатора, который заходит первым.",
    },
    "shadow_shaman": {
        "name": "Shadow Shaman",
        "ru_name": "Шаман",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["single_target_cc", "summons", "split_push"],
        "counters": ["crystal_maiden", "zuus", "sniper", "lina", "tinker"],
        "countered_by": ["antimage", "storm_spirit", "slark", "weaver"],
        "synergies": ["juggernaut", "ursa", "sven"],
        "lane_tip": "Hex + Shackles — гарантированный фраг под саппортом, а Mass Serpent Ward ставь на вышку после килла.",
    },
    "tidehunter": {
        "name": "Tidehunter",
        "ru_name": "Тайдхантер",
        "attr": "str",
        "roles": [3],
        "tags": ["tanky", "magic_burst_cc", "passives"],
        "counters": ["phantom_assassin", "ursa", "troll_warlord", "sniper", "sven"],
        "countered_by": ["slark", "axe", "razor"],
        "synergies": ["magnataur", "sand_king", "witch_doctor", "kunkka"],
        "lane_tip": "Kraken Shell держи как халявный диспел, а Ravage береги на 2-3 врага в куче — иначе просто зони.",
    },
    "lich": {
        "name": "Lich",
        "ru_name": "Лич",
        "attr": "int",
        "roles": [5],
        "tags": ["magic_burst_cc", "saves_buffs", "burst"],
        "counters": ["ursa", "troll_warlord", "sven", "phantom_assassin"],
        "countered_by": ["crystal_maiden", "zuus", "lina", "sniper"],
        "synergies": ["axe", "legion_commander", "juggernaut"],
        "lane_tip": "Frost Shield вешай на керри под фокус, а Frost Nova трать, чтобы разменивать ману на HP врага.",
    },
    "riki": {
        "name": "Riki",
        "ru_name": "Рики",
        "attr": "agi",
        "roles": [1, 4],
        "tags": ["invis", "right_clickers", "silence"],
        "counters": ["crystal_maiden", "zuus", "lina", "shadow_shaman", "witch_doctor"],
        "countered_by": ["slardar", "bounty_hunter", "bloodseeker", "zuus"],
        "synergies": ["magnataur", "dark_willow", "grimstroke"],
        "lane_tip": "Smoke Screen кидай до атаки, чтобы враг не успел нажать сейв, и следи за вижном у саппортов.",
    },
    "enigma": {
        "name": "Enigma",
        "ru_name": "Энигма",
        "attr": "uni",
        "roles": [3, 4, 5],
        "tags": ["summons", "single_target_cc", "split_push"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["silencer", "rubick", "disruptor", "crystal_maiden"],
        "synergies": ["faceless_void", "witch_doctor", "sand_king", "tidehunter"],
        "lane_tip": "Eidolons ставь на фарм и давление, а Black Hole придерживай строго под момент, когда BKB врагов на КД.",
    },
    "tinker": {
        "name": "Tinker",
        "ru_name": "Тинкер",
        "attr": "int",
        "roles": [1, 2],
        "tags": ["magic_burst_cc", "high_mobility", "split_push"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["antimage", "storm_spirit", "nyx_assassin", "rattletrap"],
        "synergies": ["faceless_void", "legion_commander", "axe"],
        "lane_tip": "Держи March of the Machines на фарме, а после Rearm и Blink не стой на одной точке — сразу уходи.",
    },
    "sniper": {
        "name": "Sniper",
        "ru_name": "Снайпер",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["right_clickers", "passives", "global_presence"],
        "counters": ["crystal_maiden", "zuus", "lina", "witch_doctor", "shadow_shaman"],
        "countered_by": ["axe", "slardar", "spectre", "storm_spirit", "phantom_assassin"],
        "synergies": ["crystal_maiden", "vengefulspirit", "juggernaut"],
        "lane_tip": "Держи дистанцию через Take Aim, а Shrapnel используй и для фарма, и для зонинга при отступлении.",
    },
    "necrolyte": {
        "name": "Necrophos",
        "ru_name": "Некрофос",
        "attr": "int",
        "roles": [1, 2, 3],
        "tags": ["heal_regen", "tanky", "magic_burst_cc"],
        "counters": ["sven", "troll_warlord", "ursa", "phantom_assassin", "life_stealer"],
        "countered_by": ["antimage", "silencer", "doom_bringer", "ancient_apparition"],
        "synergies": ["crystal_maiden", "grimstroke", "sand_king"],
        "lane_tip": "Death Pulse спамь и по крипам, и по героям — тебе важно лечиться от каждого попадания.",
    },
    "warlock": {
        "name": "Warlock",
        "ru_name": "Варлок",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["summons", "heal_regen", "magic_burst_cc"],
        "counters": ["phantom_lancer", "meepo", "broodmother", "naga_siren", "chaos_knight"],
        "countered_by": ["axe", "legion_commander", "antimage"],
        "synergies": ["faceless_void", "enigma", "sand_king"],
        "lane_tip": "Fatal Bonds кидай в начале драки, а Shadow Word держи на раненом керри, а не на себя.",
    },
    "beastmaster": {
        "name": "Beastmaster",
        "ru_name": "Бистмастер",
        "attr": "uni",
        "roles": [2, 3, 4],
        "tags": ["summons", "single_target_cc", "global_presence"],
        "counters": ["sniper", "drow_ranger", "windrunner", "crystal_maiden"],
        "countered_by": ["axe", "legion_commander", "slark", "shredder"],
        "synergies": ["crystal_maiden", "legion_commander", "magnataur"],
        "lane_tip": "Hawk отправляй на поиск вражеского леса, а Wild Axes бросай по крипам и герою одновременно.",
    },
    "queenofpain": {
        "name": "Queen of Pain",
        "ru_name": "Квопа",
        "attr": "int",
        "roles": [2],
        "tags": ["magic_burst_cc", "high_mobility", "burst"],
        "counters": ["crystal_maiden", "lina", "zuus", "phantom_assassin", "sniper"],
        "countered_by": ["antimage", "silencer", "nyx_assassin", "puck"],
        "synergies": ["faceless_void", "magnataur", "sand_king"],
        "lane_tip": "Blink используй агрессивно только когда у врага нет контроля, иначе держи его на эскейп.",
    },
    "venomancer": {
        "name": "Venomancer",
        "ru_name": "Веномансер",
        "attr": "uni",
        "roles": [3, 4, 5],
        "tags": ["summons", "split_push", "magic_burst_cc"],
        "counters": ["phantom_assassin", "life_stealer", "juggernaut", "sven", "ursa"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["crystal_maiden", "grimstroke", "sand_king"],
        "lane_tip": "Plague Wards ставь на пути отхода и у вышки, а Venomous Gale кидай первым — яд усилит весь урон.",
    },
    "skeleton_king": {
        "name": "Wraith King",
        "ru_name": "ВК",
        "attr": "str",
        "roles": [1, 3],
        "tags": ["right_clickers", "tanky", "summons"],
        "counters": ["crystal_maiden", "lina", "zuus", "lion", "windrunner"],
        "countered_by": ["antimage", "invoker", "puck", "storm_spirit"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Wraithfire Blast кидай на цель, которую фокусит команда, а скелетов используй для давления на вышки.",
    },
    "death_prophet": {
        "name": "Death Prophet",
        "ru_name": "Дез Профет",
        "attr": "uni",
        "roles": [2, 3],
        "tags": ["magic_burst_cc", "split_push", "heal_regen"],
        "counters": ["phantom_lancer", "meepo", "broodmother", "crystal_maiden", "sniper"],
        "countered_by": ["antimage", "silencer", "doom_bringer", "legion_commander"],
        "synergies": ["crystal_maiden", "sand_king", "jakiro"],
        "lane_tip": "Spirit Siphon ставь на героя перед разменом, а Exorcism активируй после закупа на живучесть.",
    },
    "pugna": {
        "name": "Pugna",
        "ru_name": "Пугна",
        "attr": "int",
        "roles": [2, 4, 5],
        "tags": ["magic_burst_cc", "split_push", "saves_buffs"],
        "counters": ["zuus", "lina", "crystal_maiden", "leshrac", "necrolyte"],
        "countered_by": ["antimage", "silencer", "pudge", "legion_commander"],
        "synergies": ["sand_king", "faceless_void", "grimstroke"],
        "lane_tip": "Nether Ward ставь прямо в замес под каст вражеского мага, а Decrepify используй для комбо по цели.",
    },
    "templar_assassin": {
        "name": "Templar Assassin",
        "ru_name": "Темпларка",
        "attr": "agi",
        "roles": [2, 1],
        "tags": ["right_clickers", "burst", "passives"],
        "counters": ["sniper", "crystal_maiden", "zuus", "lina", "drow_ranger"],
        "countered_by": ["axe", "razor", "viper", "shredder"],
        "synergies": ["dark_seer", "sand_king", "magnataur"],
        "lane_tip": "Refraction включай до того, как в тебя полетит магия, а Meld используй для минус брони на вышке.",
    },
    "viper": {
        "name": "Viper",
        "ru_name": "Вайпер",
        "attr": "agi",
        "roles": [2, 3, 1],
        "tags": ["passives", "right_clickers", "tanky"],
        "counters": ["ursa", "life_stealer", "juggernaut", "phantom_assassin", "troll_warlord"],
        "countered_by": ["antimage", "crystal_maiden", "storm_spirit", "sniper"],
        "synergies": ["crystal_maiden", "dark_seer", "sand_king"],
        "lane_tip": "На линии дави Poison Attack каждый раз, когда враг подходит добивать крипа, и не дерись в лейте без ульты.",
    },
    "luna": {
        "name": "Luna",
        "ru_name": "Луна",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "magic_burst_cc", "split_push"],
        "counters": ["crystal_maiden", "zuus", "sniper", "lina", "shadow_shaman"],
        "countered_by": ["axe", "slardar", "spectre", "storm_spirit"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Moon Glaives качай для фарма пачек, а Eclipse включай только когда в куче есть 2+ героя.",
    },
    "dragon_knight": {
        "name": "Dragon Knight",
        "ru_name": "ДК",
        "attr": "str",
        "roles": [2, 3, 1],
        "tags": ["tanky", "single_target_cc", "right_clickers"],
        "counters": ["sniper", "crystal_maiden", "lina", "zuus"],
        "countered_by": ["axe", "shredder", "viper", "slark"],
        "synergies": ["crystal_maiden", "jakiro", "witch_doctor"],
        "lane_tip": "Dragon Blood делает тебя неубиваемым на линии — разменивайся смело, а ульт держи на снос вышек.",
    },
    "dazzle": {
        "name": "Dazzle",
        "ru_name": "Дазл",
        "attr": "uni",
        "roles": [4, 5],
        "tags": ["saves_buffs", "heal_regen", "single_target_cc"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "phantom_assassin"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["juggernaut", "sven", "medusa"],
        "lane_tip": "Shallow Grave всегда держи для керри в фокусе, а Shadow Wave лечи и себя, и союзника рядом.",
    },
    "rattletrap": {
        "name": "Clockwerk",
        "ru_name": "Клокверк",
        "attr": "str",
        "roles": [3, 4, 5],
        "tags": ["single_target_cc", "high_mobility", "tanky"],
        "counters": ["crystal_maiden", "zuus", "lina", "tinker", "sniper"],
        "countered_by": ["axe", "razor", "shredder", "life_stealer"],
        "synergies": ["spirit_breaker", "bloodseeker", "legion_commander"],
        "lane_tip": "Battery Assault включай до Hookshot, а Power Cogs загоняй врага в кольцо — так он не добьёт крипа.",
    },
    "leshrac": {
        "name": "Leshrac",
        "ru_name": "Лешрак",
        "attr": "int",
        "roles": [2, 4, 5],
        "tags": ["magic_burst_cc", "split_push", "burst"],
        "counters": ["phantom_lancer", "meepo", "naga_siren", "broodmother", "crystal_maiden"],
        "countered_by": ["antimage", "nyx_assassin", "rattletrap", "pugna"],
        "synergies": ["sand_king", "jakiro", "crystal_maiden"],
        "lane_tip": "Держи Lightning Storm на фарме и харасе, а Diabolic Edict включай прямо под вышкой или в толпе.",
    },
    "furion": {
        "name": "Nature's Prophet",
        "ru_name": "Фурион",
        "attr": "uni",
        "roles": [1, 2, 4],
        "tags": ["summons", "split_push", "global_presence"],
        "counters": ["crystal_maiden", "zuus", "sniper", "lina", "tinker"],
        "countered_by": ["axe", "storm_spirit", "spirit_breaker", "slark"],
        "synergies": ["crystal_maiden", "legion_commander", "lycan"],
        "lane_tip": "Teleportation используй для давления на пустые линии, но не улетай далеко без вижна на карте.",
    },
    "life_stealer": {
        "name": "Lifestealer",
        "ru_name": "Лайфстилер",
        "attr": "str",
        "roles": [1, 3],
        "tags": ["right_clickers", "heal_regen", "saves_buffs"],
        "counters": ["bristleback", "tidehunter", "centaur", "spectre"],
        "countered_by": ["axe", "razor", "viper", "troll_warlord"],
        "synergies": ["spirit_breaker", "storm_spirit", "slardar"],
        "lane_tip": "Rage жми до стана врага, а Infest на нейтральном крипе держи как второй эскейп.",
    },
    "dark_seer": {
        "name": "Dark Seer",
        "ru_name": "Дарк Сир",
        "attr": "int",
        "roles": [3],
        "tags": ["illusions_swarms", "high_mobility", "tanky"],
        "counters": ["sniper", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "razor"],
        "synergies": ["earthshaker", "enigma", "leshrac"],
        "lane_tip": "Ion Shell вешай на своего инициатора, а Wall of Replica ставь так, чтобы враги пробегали сквозь него.",
    },
    "clinkz": {
        "name": "Clinkz",
        "ru_name": "Клинкз",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["invis", "right_clickers", "summons"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper", "windrunner"],
        "countered_by": ["axe", "slardar", "bounty_hunter", "bloodseeker"],
        "synergies": ["crystal_maiden", "magnataur", "grimstroke"],
        "lane_tip": "Skeleton Walk держи для подхода и эскейпа, а скелетов ставь на линии, которые враг не успевает защищать.",
    },
    "omniknight": {
        "name": "Omniknight",
        "ru_name": "Омник",
        "attr": "str",
        "roles": [4, 5],
        "tags": ["saves_buffs", "heal_regen", "tanky"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sniper"],
        "countered_by": ["axe", "silencer", "doom_bringer"],
        "synergies": ["juggernaut", "sven", "medusa"],
        "lane_tip": "Purification кидай на союзника, который идёт в размен, а Guardian Angel придерживай под их BKB-раш.",
    },
    "enchantress": {
        "name": "Enchantress",
        "ru_name": "Энчантресс",
        "attr": "int",
        "roles": [4, 5, 1],
        "tags": ["summons", "heal_regen", "right_clickers"],
        "counters": ["life_stealer", "ursa", "phantom_assassin", "troll_warlord"],
        "countered_by": ["axe", "legion_commander", "sniper"],
        "synergies": ["crystal_maiden", "legion_commander", "furion"],
        "lane_tip": "Enchant держи для крипа-танка на линии и для сетапа, а Untouchable используй в размене с керри.",
    },
    "huskar": {
        "name": "Huskar",
        "ru_name": "Хускар",
        "attr": "str",
        "roles": [2, 1, 3],
        "tags": ["heal_regen", "tanky", "right_clickers"],
        "counters": ["sniper", "crystal_maiden", "lina", "zuus", "phantom_assassin"],
        "countered_by": ["ancient_apparition", "axe", "doom_bringer", "necrolyte"],
        "synergies": ["dazzle", "oracle", "crystal_maiden"],
        "lane_tip": "Burning Spear спамь на линии и не бойся размена — ты сильнее почти любого на 1-3 уровне.",
    },
    "night_stalker": {
        "name": "Night Stalker",
        "ru_name": "Найт Сталкер",
        "attr": "str",
        "roles": [3, 1, 4],
        "tags": ["silence", "tanky", "burst"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper", "tinker"],
        "countered_by": ["axe", "shredder", "razor", "life_stealer"],
        "synergies": ["spirit_breaker", "bloodseeker", "legion_commander"],
        "lane_tip": "Ночь — твоё время: заходи на линию с Void, а днём фарми в лесу и не лезь в драки.",
    },
    "broodmother": {
        "name": "Broodmother",
        "ru_name": "Бруда",
        "attr": "agi",
        "roles": [3, 1],
        "tags": ["summons", "split_push", "heal_regen"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus"],
        "countered_by": ["axe", "legion_commander", "earthshaker", "shredder"],
        "synergies": ["crystal_maiden", "legion_commander", "lycan"],
        "lane_tip": "Паутины ставь по всей линии, чтобы быстро уходить от гангов, а пауков пускай на вышку.",
    },
    "bounty_hunter": {
        "name": "Bounty Hunter",
        "ru_name": "Баунти Хантер",
        "attr": "agi",
        "roles": [4, 5],
        "tags": ["invis", "right_clickers", "global_presence"],
        "counters": ["crystal_maiden", "zuus", "sniper", "lina", "tinker"],
        "countered_by": ["slardar", "bloodseeker", "zuus"],
        "synergies": ["spirit_breaker", "legion_commander", "slark"],
        "lane_tip": "Track вешай на вражеского керри перед фокусом — команда получит и золото, и вижн.",
    },
    "weaver": {
        "name": "Weaver",
        "ru_name": "Вивер",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["high_mobility", "invis", "saves_buffs"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper", "lich"],
        "countered_by": ["axe", "slardar", "bounty_hunter", "bloodseeker"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Shukuchi используй и для урона, и для ухода, а Time Lapse не трать зря — это твой второй шанс.",
    },
    "jakiro": {
        "name": "Jakiro",
        "ru_name": "Джакиро",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "split_push", "single_target_cc"],
        "counters": ["phantom_lancer", "meepo", "naga_siren", "broodmother"],
        "countered_by": ["crystal_maiden", "lina", "zuus", "sniper"],
        "synergies": ["sand_king", "crystal_maiden", "faceless_void"],
        "lane_tip": "Ice Path ставь перед собой при отступлении и под затяжной бой, а Macropyre — в узкий проход.",
    },
    "batrider": {
        "name": "Batrider",
        "ru_name": "Батрайдер",
        "attr": "uni",
        "roles": [3, 4, 5],
        "tags": ["single_target_cc", "high_mobility", "magic_burst_cc"],
        "counters": ["sniper", "drow_ranger", "crystal_maiden", "zuus"],
        "countered_by": ["axe", "life_stealer", "razor", "shredder"],
        "synergies": ["dark_seer", "magnataur", "sand_king"],
        "lane_tip": "Flaming Lasso — главный инструмент: вытащи керри из строя, а Firefly используй для подхода.",
    },
    "chen": {
        "name": "Chen",
        "ru_name": "Чен",
        "attr": "int",
        "roles": [5, 4],
        "tags": ["summons", "heal_regen", "global_presence"],
        "counters": ["sniper", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "legion_commander", "furion"],
        "lane_tip": "Забирай сильных нейтралов на линии и с ними дави вышку; Hand of God держи на спасение команды.",
    },
    "spectre": {
        "name": "Spectre",
        "ru_name": "Спектра",
        "attr": "agi",
        "roles": [1],
        "tags": ["illusions_swarms", "tanky", "global_presence"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper", "tinker"],
        "countered_by": ["axe", "slardar", "bounty_hunter", "bristleback"],
        "synergies": ["crystal_maiden", "grimstroke", "dark_seer"],
        "lane_tip": "Фарми безопасно и смотри на Haunt: появляйся в драке, когда враги уже потратили контроль.",
    },
    "ancient_apparition": {
        "name": "Ancient Apparition",
        "ru_name": "АА",
        "attr": "int",
        "roles": [5, 4],
        "tags": ["magic_burst_cc", "global_presence", "burst"],
        "counters": ["huskar", "alchemist", "morphling", "necrolyte", "life_stealer"],
        "countered_by": ["omniknight", "dazzle", "oracle", "abaddon"],
        "synergies": ["sand_king", "faceless_void", "enigma"],
        "lane_tip": "Ice Blast кидай на керри с лечением, а Cold Feet доводи до заморозки на линии.",
    },
    "doom_bringer": {
        "name": "Doom",
        "ru_name": "Дум",
        "attr": "str",
        "roles": [3, 1],
        "tags": ["single_target_cc", "tanky", "burst"],
        "counters": ["storm_spirit", "puck", "ember_spirit", "void_spirit", "crystal_maiden"],
        "countered_by": ["crystal_maiden", "silencer", "weaver", "sniper"],
        "synergies": ["spirit_breaker", "legion_commander", "night_stalker"],
        "lane_tip": "Devour забирай полезного крипа на линии, а Doom применяй точечно по вражескому керри или магу.",
    },
    "ursa": {
        "name": "Ursa",
        "ru_name": "Урса",
        "attr": "agi",
        "roles": [1, 2, 3],
        "tags": ["right_clickers", "passives", "tanky"],
        "counters": ["bristleback", "tidehunter", "centaur", "doom_bringer", "life_stealer"],
        "countered_by": ["axe", "razor", "viper", "troll_warlord"],
        "synergies": ["crystal_maiden", "dark_seer", "magnataur"],
        "lane_tip": "На линии ищи размен вплотную: Fury Swipes быстро стакается, а Overpower держи на летальный заход.",
    },
    "gyrocopter": {
        "name": "Gyrocopter",
        "ru_name": "Гиро",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "magic_burst_cc", "burst"],
        "counters": ["phantom_lancer", "meepo", "naga_siren", "crystal_maiden", "zuus"],
        "countered_by": ["axe", "slardar", "spectre", "storm_spirit"],
        "synergies": ["crystal_maiden", "magnataur", "enigma"],
        "lane_tip": "Rocket Barrage включай вплотную, а Flak Cannon — когда рядом много целей и крипов.",
    },
    "alchemist": {
        "name": "Alchemist",
        "ru_name": "Алхимик",
        "attr": "str",
        "roles": [1, 3],
        "tags": ["heal_regen", "tanky", "right_clickers"],
        "counters": ["crystal_maiden", "sniper", "lina", "zuus"],
        "countered_by": ["ancient_apparition", "axe", "slark", "doom_bringer"],
        "synergies": ["crystal_maiden", "dark_seer", "magnataur"],
        "lane_tip": "Greed и Acid Spray — твой фарм-двигатель: добивай крипов и не отвлекайся на драки без ульты.",
    },
    "silencer": {
        "name": "Silencer",
        "ru_name": "Сайленсер",
        "attr": "int",
        "roles": [4, 5, 2],
        "tags": ["silence", "global_presence", "right_clickers"],
        "counters": ["enigma", "faceless_void", "magnataur", "sand_king", "storm_spirit"],
        "countered_by": ["axe", "legion_commander", "doom_bringer", "ursa"],
        "synergies": ["crystal_maiden", "legion_commander", "axe"],
        "lane_tip": "Global Silence — главный козырь: включай его в момент, когда враг начинает комбо.",
    },
    "obsidian_destroyer": {
        "name": "Outworld Devourer",
        "ru_name": "ОДА",
        "attr": "int",
        "roles": [2, 3, 1],
        "tags": ["magic_burst_cc", "burst", "single_target_cc"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sven"],
        "countered_by": ["antimage", "silencer", "nyx_assassin", "crystal_maiden"],
        "synergies": ["dark_seer", "faceless_void", "enigma"],
        "lane_tip": "Astral Imprisonment используй для сейва и набора интеллекта, а Arcane Orb трать по цели без BKB.",
    },
    "lycan": {
        "name": "Lycan",
        "ru_name": "Ликан",
        "attr": "str",
        "roles": [1, 3],
        "tags": ["summons", "split_push", "right_clickers"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "tinker"],
        "countered_by": ["axe", "legion_commander", "shredder", "earthshaker"],
        "synergies": ["crystal_maiden", "legion_commander", "furion"],
        "lane_tip": "Волков пускай на линии и в лес, а Shapeshift активируй под снос вышки, пока враги далеко.",
    },
    "brewmaster": {
        "name": "Brewmaster",
        "ru_name": "Панда",
        "attr": "uni",
        "roles": [3, 1],
        "tags": ["tanky", "summons", "single_target_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "slardar", "shredder", "razor"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Thunder Clap даёт и урон, и замедление — начинай размен с него, а Primal Split береги под драку.",
    },
    "shadow_demon": {
        "name": "Shadow Demon",
        "ru_name": "ШД",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["saves_buffs", "illusions_swarms", "magic_burst_cc"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "slark"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["leshrac", "sand_king", "grimstroke"],
        "lane_tip": "Disruption используй как сейв от фокуса и для создания иллюзий перед вышкой врага.",
    },
    "lone_druid": {
        "name": "Lone Druid",
        "ru_name": "Лон Друид",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["right_clickers", "summons", "tanky"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "earthshaker", "shredder"],
        "synergies": ["crystal_maiden", "legion_commander", "furion"],
        "lane_tip": "Медведь — твой фронт: держи его между врагом и собой, а Rabid вешай перед разменом.",
    },
    "chaos_knight": {
        "name": "Chaos Knight",
        "ru_name": "ЦК",
        "attr": "str",
        "roles": [1],
        "tags": ["illusions_swarms", "right_clickers", "single_target_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "drow_ranger"],
        "countered_by": ["axe", "earthshaker", "legion_commander", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Chaos Bolt бросай с близкой дистанции ради максимального стана, а Phantasm — под снос вышки.",
    },
    "meepo": {
        "name": "Meepo",
        "ru_name": "Мипо",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["summons", "single_target_cc", "split_push"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "tinker"],
        "countered_by": ["axe", "earthshaker", "legion_commander", "sven"],
        "synergies": ["crystal_maiden", "legion_commander", "dark_seer"],
        "lane_tip": "Poof используй и для фарма, и для прыжка к союзнику, а клонов держи на разных линиях.",
    },
    "treant": {
        "name": "Treant Protector",
        "ru_name": "Трент",
        "attr": "str",
        "roles": [5, 4],
        "tags": ["saves_buffs", "single_target_cc", "global_presence"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sniper"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "juggernaut", "medusa"],
        "lane_tip": "Living Armor кидай на союзника под фокус даже через всю карту, а Overgrowth — по толпе врагов.",
    },
    "ogre_magi": {
        "name": "Ogre Magi",
        "ru_name": "Огр",
        "attr": "str",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "single_target_cc", "tanky"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["juggernaut", "sven", "legion_commander"],
        "lane_tip": "Fireblast + Ignite — твой размен на линии, а Bloodlust вешай на керри перед дракой.",
    },
    "undying": {
        "name": "Undying",
        "ru_name": "Андаинг",
        "attr": "str",
        "roles": [4, 3, 5],
        "tags": ["summons", "heal_regen", "tanky"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "phantom_assassin"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "death_prophet"],
        "lane_tip": "Tombstone ставь у линии перед дракой, а Decay спамь по герою — тебе нужно много силы.",
    },
    "disruptor": {
        "name": "Disruptor",
        "ru_name": "Дизраптор",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["silence", "magic_burst_cc", "single_target_cc"],
        "counters": ["storm_spirit", "ember_spirit", "void_spirit", "antimage", "puck"],
        "countered_by": ["axe", "legion_commander", "sniper"],
        "synergies": ["faceless_void", "sand_king", "grimstroke"],
        "lane_tip": "Glimpse возвращай врага после телепорта или прыжка, а Static Storm держи под момент комбо.",
    },
    "nyx_assassin": {
        "name": "Nyx Assassin",
        "ru_name": "Никс",
        "attr": "uni",
        "roles": [4, 3],
        "tags": ["invis", "burst", "single_target_cc"],
        "counters": ["storm_spirit", "queenofpain", "zuus", "lina", "tinker"],
        "countered_by": ["slardar", "bounty_hunter", "weaver", "bloodseeker"],
        "synergies": ["crystal_maiden", "legion_commander", "magnataur"],
        "lane_tip": "Выходи из Vendetta на мага без BKB, а Spiked Carapace включай прямо под вражеский прокаст.",
    },
    "naga_siren": {
        "name": "Naga Siren",
        "ru_name": "Нага",
        "attr": "agi",
        "roles": [1, 5],
        "tags": ["illusions_swarms", "single_target_cc", "saves_buffs"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "earthshaker", "legion_commander", "sven"],
        "synergies": ["crystal_maiden", "dark_seer", "enigma"],
        "lane_tip": "Иллюзиями дави линии, а Song of the Siren используй, чтобы спасти команду или начать бой.",
    },
    "keeper_of_the_light": {
        "name": "Keeper of the Light",
        "ru_name": "Котл",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "saves_buffs", "global_presence"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sniper"],
        "countered_by": ["axe", "legion_commander", "night_stalker"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Illuminate заряжай из тумана, а Chakra Magic отдавай инициатору под его ключевой скилл.",
    },
    "wisp": {
        "name": "Io",
        "ru_name": "Ио",
        "attr": "uni",
        "roles": [4, 5],
        "tags": ["heal_regen", "saves_buffs", "global_presence"],
        "counters": ["sniper", "crystal_maiden", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["tiny", "legion_commander", "life_stealer", "medusa"],
        "lane_tip": "Tether держи на керри и лечи его, а Relocate используй для ганга и срочного возврата.",
    },
    "visage": {
        "name": "Visage",
        "ru_name": "Визаж",
        "attr": "uni",
        "roles": [4, 5, 1],
        "tags": ["summons", "burst", "right_clickers"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "dark_seer", "magnataur"],
        "lane_tip": "Сукубы — главный урон: води их в фокус и не давай умирать, а Soul Assumption бей после их урона.",
    },
    "medusa": {
        "name": "Medusa",
        "ru_name": "Медуза",
        "attr": "agi",
        "roles": [1],
        "tags": ["tanky", "right_clickers", "single_target_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "tinker"],
        "countered_by": ["antimage", "invoker", "silencer", "slark"],
        "synergies": ["crystal_maiden", "dark_seer", "enigma"],
        "lane_tip": "Mystic Snake пускай по крипам в героя, а Stone Gaze включай, когда враги уже зашли на тебя.",
    },
    "troll_warlord": {
        "name": "Troll Warlord",
        "ru_name": "Тролль",
        "attr": "agi",
        "roles": [1],
        "tags": ["right_clickers", "passives", "single_target_cc"],
        "counters": ["bristleback", "tidehunter", "centaur", "medusa"],
        "countered_by": ["axe", "razor", "viper", "life_stealer"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Переключай стойки под дистанцию, а Battle Trance используй для сноса вышки или фокуса одной цели.",
    },
    "magnataur": {
        "name": "Magnus",
        "ru_name": "Магнус",
        "attr": "uni",
        "roles": [3, 4, 5],
        "tags": ["magic_burst_cc", "high_mobility", "burst"],
        "counters": ["phantom_lancer", "meepo", "naga_siren", "broodmother"],
        "countered_by": ["axe", "slark", "life_stealer", "shredder"],
        "synergies": ["faceless_void", "sand_king", "sven", "juggernaut"],
        "lane_tip": "Skewer затаскивай врага под своих, а Reverse Polarity — строго после BKB-раша врагов.",
    },
    "shredder": {
        "name": "Timbersaw",
        "ru_name": "Тимбер",
        "attr": "str",
        "roles": [3, 2, 1],
        "tags": ["tanky", "magic_burst_cc", "high_mobility"],
        "counters": ["phantom_assassin", "ursa", "troll_warlord", "sven", "life_stealer"],
        "countered_by": ["axe", "silencer", "doom_bringer", "night_stalker"],
        "synergies": ["crystal_maiden", "dark_seer", "grimstroke"],
        "lane_tip": "Reactive Armor стакай об крипов до размена, а Timber Chain используй и для эскейпа, и для врыва.",
    },
    "bristleback": {
        "name": "Bristleback",
        "ru_name": "Бристл",
        "attr": "str",
        "roles": [1, 3],
        "tags": ["tanky", "passives", "burst"],
        "counters": ["antimage", "phantom_assassin", "juggernaut", "life_stealer"],
        "countered_by": ["axe", "ursa", "ancient_apparition", "doom_bringer"],
        "synergies": ["crystal_maiden", "grimstroke", "sand_king"],
        "lane_tip": "Подставляй спину под урон и стакай Quill Spray — в затяжном размене рядом с крипами тебя не убить.",
    },
    "tusk": {
        "name": "Tusk",
        "ru_name": "Туск",
        "attr": "str",
        "roles": [4, 3, 5],
        "tags": ["single_target_cc", "magic_burst_cc", "saves_buffs"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Snowball начинай только с союзником внутри, а Ice Shards ставь так, чтобы запереть врага у крипов.",
    },
    "skywrath_mage": {
        "name": "Skywrath Mage",
        "ru_name": "Скай",
        "attr": "int",
        "roles": [4, 5, 2],
        "tags": ["magic_burst_cc", "silence", "burst"],
        "counters": ["crystal_maiden", "zuus", "lina", "sniper", "tinker"],
        "countered_by": ["axe", "legion_commander", "doom_bringer", "nyx_assassin"],
        "synergies": ["faceless_void", "legion_commander", "sand_king"],
        "lane_tip": "Arcane Bolt спамь с дальней дистанции, а Ancient Seal вешай на мобильного героя до его прыжка.",
    },
    "abaddon": {
        "name": "Abaddon",
        "ru_name": "Абаддон",
        "attr": "uni",
        "roles": [4, 5, 1, 3],
        "tags": ["saves_buffs", "heal_regen", "tanky"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sniper"],
        "countered_by": ["axe", "ancient_apparition", "doom_bringer", "silencer"],
        "synergies": ["juggernaut", "medusa", "crystal_maiden"],
        "lane_tip": "Aphotic Shield вешай на союзника в фокусе — он и лечит, и снимает контроль, а Borrowed Time включай до фокуса.",
    },
    "elder_titan": {
        "name": "Elder Titan",
        "ru_name": "Элдер Титан",
        "attr": "str",
        "roles": [4, 5, 3],
        "tags": ["magic_burst_cc", "tanky", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["faceless_void", "sand_king", "dark_seer"],
        "lane_tip": "Echo Stomp начинай с духом, а Natural Order включай до фокуса — снятая броня усилит всю команду.",
    },
    "legion_commander": {
        "name": "Legion Commander",
        "ru_name": "Легионка",
        "attr": "str",
        "roles": [3, 1, 4],
        "tags": ["single_target_cc", "right_clickers", "heal_regen"],
        "counters": ["storm_spirit", "ember_spirit", "void_spirit", "puck"],
        "countered_by": ["axe", "razor", "viper", "troll_warlord"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Duel всегда начинай после Overwhelming Odds, а Moment of Courage используй в размене с керри.",
    },
    "techies": {
        "name": "Techies",
        "ru_name": "Течис",
        "attr": "uni",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "burst", "split_push"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "grimstroke"],
        "lane_tip": "Мины ставь на путях отхода и у руны, а Blast Off! используй как инициацию только с прикрытием.",
    },
    "ember_spirit": {
        "name": "Ember Spirit",
        "ru_name": "Эмбер",
        "attr": "agi",
        "roles": [2, 1],
        "tags": ["high_mobility", "magic_burst_cc", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "tinker"],
        "countered_by": ["axe", "legion_commander", "silencer", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "dark_seer"],
        "lane_tip": "Fire Remnant держи на запасном пути, а Sleight of Fist используй для урона и уклонения.",
    },
    "earth_spirit": {
        "name": "Earth Spirit",
        "ru_name": "Эрт Спирит",
        "attr": "str",
        "roles": [4, 3],
        "tags": ["magic_burst_cc", "single_target_cc", "high_mobility"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Boulder Smash бей по камню, чтобы откинуть врага, а Rolling Boulder начинай с расчётом на камень.",
    },
    "abyssal_underlord": {
        "name": "Underlord",
        "ru_name": "Андерлорд",
        "attr": "str",
        "roles": [3],
        "tags": ["magic_burst_cc", "tanky", "global_presence"],
        "counters": ["phantom_lancer", "meepo", "naga_siren", "broodmother", "crystal_maiden"],
        "countered_by": ["axe", "slark", "legion_commander", "shredder"],
        "synergies": ["faceless_void", "sand_king", "enigma"],
        "lane_tip": "Atrophy Aura включай в размен у крипов — так ты быстрее набираешь урон, а Firestorm ставь под отход врага.",
    },
    "terrorblade": {
        "name": "Terrorblade",
        "ru_name": "ТБ",
        "attr": "agi",
        "roles": [1],
        "tags": ["illusions_swarms", "right_clickers", "saves_buffs"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "earthshaker", "legion_commander", "sven"],
        "synergies": ["crystal_maiden", "dark_seer", "magnataur"],
        "lane_tip": "Иллюзиями дави линии, а Sunder держи на спасение себя или добивание толстого врага.",
    },
    "phoenix": {
        "name": "Phoenix",
        "ru_name": "Феникс",
        "attr": "str",
        "roles": [4, 5, 3],
        "tags": ["heal_regen", "magic_burst_cc", "burst"],
        "counters": ["phantom_lancer", "meepo", "broodmother", "naga_siren"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["sand_king", "faceless_void", "enigma"],
        "lane_tip": "Fire Spirits вешай на врага перед разменом, а Supernova ставь так, чтобы яйцо прикрывали союзники.",
    },
    "oracle": {
        "name": "Oracle",
        "ru_name": "Оракл",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["saves_buffs", "heal_regen", "magic_burst_cc"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "sniper"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["crystal_maiden", "juggernaut", "medusa"],
        "lane_tip": "False Promise вешай на керри до фокуса, а Purifying Flames не спамь по своим без нужды.",
    },
    "winter_wyvern": {
        "name": "Winter Wyvern",
        "ru_name": "Виверна",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["saves_buffs", "heal_regen", "magic_burst_cc"],
        "counters": ["life_stealer", "ursa", "troll_warlord", "phantom_assassin"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "faceless_void"],
        "lane_tip": "Cold Embrace спасает от фокуса — держи на керри, а Winter's Curse ставь на жертву с большим уроном.",
    },
    "arc_warden": {
        "name": "Arc Warden",
        "ru_name": "Арк Варден",
        "attr": "uni",
        "roles": [1, 2],
        "tags": ["illusions_swarms", "split_push", "right_clickers"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "earthshaker", "legion_commander"],
        "synergies": ["crystal_maiden", "dark_seer", "magnataur"],
        "lane_tip": "Tempest Double пускай давить линию и фармить, а Flux вешай на цель, когда рядом никого нет.",
    },
    "monkey_king": {
        "name": "Monkey King",
        "ru_name": "МК",
        "attr": "agi",
        "roles": [1, 2, 4],
        "tags": ["right_clickers", "invis", "high_mobility"],
        "counters": ["sniper", "crystal_maiden", "lina", "zuus"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Boundless Strike бей по низкому HP, а Tree Dance используй для подхода через лес и эскейпа.",
    },
    "dark_willow": {
        "name": "Dark Willow",
        "ru_name": "Виллоу",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "single_target_cc", "burst"],
        "counters": ["storm_spirit", "ember_spirit", "void_spirit", "puck"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Cursed Crown задерживай до момента, когда враг не успеет выйти, а Bramble Maze ставь на отход.",
    },
    "pangolier": {
        "name": "Pangolier",
        "ru_name": "Панго",
        "attr": "uni",
        "roles": [2, 3, 1],
        "tags": ["tanky", "high_mobility", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Swashbuckle используй для размена и эскейпа, а Rolling Thunder — по толпе врагов в узком коридоре.",
    },
    "grimstroke": {
        "name": "Grimstroke",
        "ru_name": "Гримстроук",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "silence", "single_target_cc"],
        "counters": ["crystal_maiden", "sniper", "lina", "zuus", "life_stealer"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["sand_king", "faceless_void", "magnataur"],
        "lane_tip": "Phantom's Embrace вешай на цель без эскейпа, а Soulbind — до прокаста, чтобы связать двоих.",
    },
    "hoodwink": {
        "name": "Hoodwink",
        "ru_name": "Худвинк",
        "attr": "agi",
        "roles": [4, 5, 2],
        "tags": ["magic_burst_cc", "burst", "high_mobility"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "slark"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Bushwhack бросай по кустам и на путь отхода, а Acorn Shot связывай с ловушкой союзника.",
    },
    "void_spirit": {
        "name": "Void Spirit",
        "ru_name": "Войд Спирит",
        "attr": "uni",
        "roles": [2, 3],
        "tags": ["high_mobility", "magic_burst_cc", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina", "tinker"],
        "countered_by": ["axe", "legion_commander", "silencer", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Aether Remnant ставь на отход, а Astral Step используй и для врыва, и для эскейпа через врага.",
    },
    "snapfire": {
        "name": "Snapfire",
        "ru_name": "Снапфайр",
        "attr": "uni",
        "roles": [4, 5],
        "tags": ["magic_burst_cc", "saves_buffs", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Firesnap Cookie кидай союзнику в прыжок на врага или себе для эскейпа, а Mortimer Kisses — по куче.",
    },
    "mars": {
        "name": "Mars",
        "ru_name": "Марс",
        "attr": "str",
        "roles": [3],
        "tags": ["single_target_cc", "tanky", "magic_burst_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "shredder", "life_stealer", "razor"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Spear of Mars бросай в стену песка или в Arena of Blood, а God's Rebuke — по врагу у крипов.",
    },
    "ringmaster": {
        "name": "Ring Master",
        "ru_name": "Рингмастер",
        "attr": "int",
        "roles": [4, 5],
        "tags": ["single_target_cc", "saves_buffs", "magic_burst_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Держи контроль для сетапа на керри, а сейв-способность оставляй на момент фокуса по союзнику.",
    },
    "dawnbreaker": {
        "name": "Dawnbreaker",
        "ru_name": "Донбрейкер",
        "attr": "str",
        "roles": [3, 1, 4],
        "tags": ["heal_regen", "global_presence", "tanky"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Celestial Hammer ставь на путь отхода, а Solar Guardian — на спасение союзника в драке на другой линии.",
    },
    "marci": {
        "name": "Marci",
        "ru_name": "Марси",
        "attr": "uni",
        "roles": [4, 1, 3],
        "tags": ["single_target_cc", "right_clickers", "high_mobility"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Dispose швыряй врага под своих, а Sidekick вешай на керри перед фокусом.",
    },
    "primal_beast": {
        "name": "Primal Beast",
        "ru_name": "Праймал Бист",
        "attr": "str",
        "roles": [3, 4],
        "tags": ["single_target_cc", "tanky", "magic_burst_cc"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "shredder"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Trample включай до врыва, а Pulverize прибереги на момент, когда враг начинает каст.",
    },
    "muerta": {
        "name": "Muerta",
        "ru_name": "Муэрта",
        "attr": "int",
        "roles": [1, 2, 3],
        "tags": ["right_clickers", "magic_burst_cc", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Dead Shot используй для сетапа и эскейпа, а Pierce the Veil — когда враги без физиммунитета.",
    },
    "kez": {
        "name": "Kez",
        "ru_name": "Кез",
        "attr": "agi",
        "roles": [1, 2],
        "tags": ["right_clickers", "high_mobility", "burst"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "doom_bringer"],
        "synergies": ["crystal_maiden", "magnataur", "sand_king"],
        "lane_tip": "Переключай стойку под дистанцию до цели: в ближнем бою держи мобильность на эскейп.",
    },
    "largo": {
        "name": "Largo",
        "ru_name": "Ларго",
        "attr": "str",
        "roles": [4, 5, 3],
        "tags": ["tanky", "single_target_cc", "saves_buffs"],
        "counters": ["crystal_maiden", "sniper", "zuus", "lina"],
        "countered_by": ["axe", "legion_commander", "silencer"],
        "synergies": ["crystal_maiden", "sand_king", "magnataur"],
        "lane_tip": "Играй от контроля и сейва: держи способность для сетапа на керри и не заходи первым без прикрытия.",
    },
}

# A3: порядок прокачки — id: (что максить, что брать на 1-м уровне, совет).
SKILL_ORDERS: dict[str, tuple[str, str, str]] = {
    "antimage": ("Mana Break", "Mana Break", "Blink бери на 2-м уровне для эскейпа, дальше макси Mana Break."),
    "axe": ("Counter Helix", "Counter Helix", "Berserker's Call бери на 2-м, чтобы агрить крипов под пассивку."),
    "bane": ("Brain Sap", "Brain Sap", "Nightmare бери на 2-м для сетапа, Fiend's Grip — на 6-м."),
    "bloodseeker": ("Bloodrage", "Bloodrage", "Blood Rite качай вторым, Thirst помогает добивать убегающих."),
    "crystal_maiden": ("Frostbite", "Frostbite", "Frostbite на 1-м даёт самый сильный размен; Nova качай для хараса."),
    "drow_ranger": ("Multishot", "Frost Arrows", "Gust бери на 2-м для сайленса, Marksmanship — на 6-м."),
    "earthshaker": ("Fissure", "Fissure", "Enchant Totem качай после, Echo Slam — на 6-м под комбо."),
    "juggernaut": ("Blade Fury", "Blade Fury", "Blade Fury даёт и урон, и иммунитет; Blade Dance качай после."),
    "mirana": ("Starstorm", "Starstorm", "Sacred Arrow держи для сетапа, Leap качай для мобильности."),
    "morphling": ("Attribute Shift", "Waveform", "Waveform — для фарма и эскейпа, Morph качай вторым."),
    "nevermore": ("Shadowraze", "Shadowraze", "Три Shadowraze подряд бьют сильнее; Necromastery качай после."),
    "phantom_lancer": ("Spirit Lance", "Spirit Lance", "Doppelganger бери на 2-м для эскейпа, Juxtapose — на 6-м."),
    "puck": ("Illusory Orb", "Illusory Orb", "Waning Rift качай вторым для сайленса, Dream Coil — на 6-м."),
    "pudge": ("Meat Hook", "Meat Hook", "Rot качай вторым для урона, Dismember — на 6-м."),
    "razor": ("Static Link", "Static Link", "Plasma Field качай вторым, Eye of the Storm — на 6-м."),
    "sand_king": ("Burrowstrike", "Burrowstrike", "Sand Storm качай для фарма, Epicenter — на 6-м."),
    "storm_spirit": ("Static Remnant", "Static Remnant", "Overload качай вторым, Ball Lightning — на 6-м для мобильности."),
    "sven": ("Storm Hammer", "Storm Hammer", "Great Cleave качай вторым для фарма, God's Strength — на 6-м."),
    "tiny": ("Avalanche", "Avalanche", "Toss качай вторым под комбо Avalanche+Toss."),
    "vengefulspirit": ("Magic Missile", "Magic Missile", "Vengeful Aura качай вторым — она усиливает всю команду."),
    "windrunner": ("Powershot", "Powershot", "Windrun качай вторым для эскейпа, Focus Fire — на 6-м."),
    "zuus": ("Arc Lightning", "Arc Lightning", "Lightning Bolt качай вторым, Thundergod's Wrath — на 6-м."),
    "kunkka": ("Tidebringer", "Tidebringer", "Torrent качай вторым для контроля, Ghostship — на 6-м."),
    "lina": ("Dragon Slave", "Dragon Slave", "Light Strike Array качай вторым для стана, Laguna Blade — на 6-м."),
    "lion": ("Earth Spike", "Earth Spike", "Hex бери на 2-м для контроля, Finger of Death — на 6-м."),
    "shadow_shaman": ("Ether Shock", "Ether Shock", "Hex качай вторым, Mass Serpent Ward — на 6-м для пуша."),
    "slardar": ("Slithereen Crush", "Slithereen Crush", "Bash качай вторым, Corrosive Haze — на 6-м для фокуса цели."),
    "tidehunter": ("Anchor Smash", "Anchor Smash", "Kraken Shell качай для живучести, Ravage — на 6-м."),
    "witch_doctor": ("Paralyzing Cask", "Paralyzing Cask", "Voodoo Restoration качай вторым для сейва, Death Ward — на 6-м."),
    "lich": ("Frost Nova", "Frost Nova", "Frost Shield качай вторым для сейва союзника, Chain Frost — на 6-м."),
    "riki": ("Smoke Screen", "Smoke Screen", "Tricks of the Trade качай вторым для урона из инвиза."),
    "enigma": ("Malefice", "Malefice", "Eidolons качай для фарма, Black Hole — на 6-м для инициации."),
    "tinker": ("Heat-Seeking Missile", "Laser", "Rearm качай после основных скиллов, Blink даёт мобильность."),
    "sniper": ("Shrapnel", "Shrapnel", "Take Aim качай вторым для дальности, Assassinate — на 6-м."),
    "necrolyte": ("Death Pulse", "Death Pulse", "Heartstopper Aura качай для сустейна, Reaper's Scythe — на 6-м."),
    "warlock": ("Fatal Bonds", "Shadow Word", "Shadow Word качай для хила на линии, Chaotic Offering — на 6-м."),
    "beastmaster": ("Wild Axes", "Summon Boar", "Inner Beast качай вторым, Primal Roar — на 6-м."),
    "queenofpain": ("Shadow Strike", "Shadow Strike", "Scream of Pain качай вторым для фарма, Sonic Wave — на 6-м."),
    "venomancer": ("Venomous Gale", "Venomous Gale", "Plague Wards качай вторым для пуша, Poison Nova — на 6-м."),
    "faceless_void": ("Time Walk", "Time Walk", "Time Lock качай вторым, Chronosphere — на 6-м под комбо команды."),
    "skeleton_king": ("Wraithfire Blast", "Wraithfire Blast", "Skeleton Army качай для фарма, Reincarnation — на 6-м."),
    "death_prophet": ("Crypt Swarm", "Crypt Swarm", "Spirit Siphon качай вторым, Exorcism — на 6-м для пуша."),
    "phantom_assassin": ("Stifling Dagger", "Stifling Dagger", "Кинжал добивает на дистанции; Blur качай после."),
    "pugna": ("Nether Blast", "Nether Blast", "Decrepify качай вторым для комбо, Life Drain — на 6-м."),
    "templar_assassin": ("Refraction", "Refraction", "Meld качай вторым для минус брони, Psionic Trap — на 6-м."),
    "viper": ("Nethertoxin", "Poison Attack", "Poison Attack качай для хараса, Viper Strike — на 6-м."),
    "luna": ("Lucent Beam", "Lucent Beam", "Moon Glaives качай вторым для фарма, Eclipse — на 6-м."),
    "dragon_knight": ("Dragon Blood", "Breathe Fire", "Dragon Blood даёт реген и броню, Elder Dragon Form — на 6-м."),
    "dazzle": ("Shadow Wave", "Poison Touch", "Shallow Grave качай вторым для сейва, Bad Juju — на 6-м."),
    "rattletrap": ("Battery Assault", "Battery Assault", "Power Cogs качай вторым, Hookshot — на 6-м для инициации."),
    "leshrac": ("Lightning Storm", "Lightning Storm", "Diabolic Edict качай для урона по вышкам, Pulse Nova — на 6-м."),
    "furion": ("Nature's Call", "Sprout", "Sprout бери для сетапа, Wrath of Nature — на 6-м для гангов."),
    "life_stealer": ("Feast", "Rage", "Open Wounds бери на 2-м, Infest — на 6-м."),
    "dark_seer": ("Ion Shell", "Ion Shell", "Surge качай вторым для мобильности, Wall of Replica — на 6-м."),
    "clinkz": ("Strafe", "Strafe", "Skeleton Walk качай для инвиза и эскейпа, Burning Barrage — на 6-м."),
    "omniknight": ("Purification", "Purification", "Heavenly Grace качай вторым для сейва, Guardian Angel — на 6-м."),
    "enchantress": ("Impetus", "Untouchable", "Enchant бери для крипов на линии, Nature's Attendants качай для хила."),
    "huskar": ("Burning Spear", "Burning Spear", "Inner Fire бери для диспела, Life Break — на 6-м."),
    "night_stalker": ("Void", "Void", "Crippling Fear качай вторым для сайленса, Dark Ascension — на 6-м."),
    "broodmother": ("Spawn Spiderlings", "Spawn Spiderlings", "Silken Bola качай вторым, Spin Web — для мобильности."),
    "bounty_hunter": ("Shuriken Toss", "Jinada", "Shadow Walk бери на 2-м, Track — на 6-м для фарма команды."),
    "weaver": ("Shukuchi", "Shukuchi", "The Swarm качай вторым для минус брони, Time Lapse — на 6-м."),
    "jakiro": ("Dual Breath", "Dual Breath", "Ice Path качай вторым для контроля, Macropyre — на 6-м."),
    "batrider": ("Sticky Napalm", "Sticky Napalm", "Firefly бери на 2-м для мобильности, Flaming Lasso — на 6-м."),
    "chen": ("Holy Persuasion", "Holy Persuasion", "Penitence качай вторым, Hand of God — на 6-м для сейва."),
    "spectre": ("Desolate", "Spectral Dagger", "Dispersion качай для живучести, Haunt — на 6-м."),
    "ancient_apparition": ("Ice Vortex", "Cold Feet", "Ice Blast на 6-м блокирует лечение, Cold Feet доводи до заморозки."),
    "doom_bringer": ("Scorched Earth", "Devour", "Devour качай для фарма, Doom — на 6-м."),
    "ursa": ("Fury Swipes", "Earthshock", "Overpower качай вторым для бурста, Enrage — на 6-м."),
    "spirit_breaker": ("Charge of Darkness", "Charge of Darkness", "Greater Bash качай вторым, Nether Strike — на 6-м."),
    "gyrocopter": ("Rocket Barrage", "Rocket Barrage", "Flak Cannon качай вторым для фарма, Call Down — на 6-м."),
    "alchemist": ("Greed", "Acid Spray", "Acid Spray для линии, Chemical Rage — на 6-м."),
    "invoker": ("Exort в QE-билде", "Quas", "Для QE макси Exort, Sun Strike держи для добивания."),
    "silencer": ("Arcane Curse", "Last Word", "Global Silence — на 6-м, Glaives качай для урона."),
    "obsidian_destroyer": ("Arcane Orb", "Arcane Orb", "Astral Imprisonment качай для сейва и набора интеллекта."),
    "lycan": ("Summon Wolves", "Summon Wolves", "Feral Impulse качай вторым, Shapeshift — на 6-м для пуша."),
    "brewmaster": ("Thunder Clap", "Thunder Clap", "Drunken Brawler качай вторым, Primal Split — на 6-м."),
    "shadow_demon": ("Shadow Poison", "Shadow Poison", "Disruption качай вторым для сейва, Demonic Purge — на 6-м."),
    "lone_druid": ("Summon Spirit Bear", "Summon Spirit Bear", "Rabid качай для бафа медведя, True Form — на 6-м."),
    "chaos_knight": ("Chaos Bolt", "Chaos Bolt", "Reality Rift качай вторым, Phantasm — на 6-м."),
    "meepo": ("Poof", "Poof", "Ransack качай для сустейна, Divided We Stand — на 6-м."),
    "treant": ("Leech Seed", "Leech Seed", "Living Armor качай для сейва линий, Overgrowth — на 6-м."),
    "ogre_magi": ("Fireblast", "Fireblast", "Ignite качай вторым, Multicast — на 6-м."),
    "undying": ("Decay", "Decay", "Soul Rip качай вторым для хила, Tombstone — на 6-м."),
    "rubick": ("Fade Bolt", "Fade Bolt", "Telekinesis бери на 2-м для сейва, Spell Steal — на 6-м."),
    "disruptor": ("Thunder Strike", "Thunder Strike", "Glimpse бери на 2-м для возврата врагов, Static Storm — на 6-м."),
    "nyx_assassin": ("Impale", "Impale", "Mana Burn качай вторым, Vendetta — на 6-м для гангов."),
    "naga_siren": ("Mirror Image", "Mirror Image", "Ensnare качай вторым, Song of the Siren — на 6-м."),
    "keeper_of_the_light": ("Illuminate", "Illuminate", "Mana Leak бери против мобильных, Spirit Form — на 6-м."),
    "wisp": ("Spirits", "Tether", "Overcharge качай вторым для усиления союзника, Relocate — на 6-м."),
    "visage": ("Soul Assumption", "Grave Chill", "Gravekeeper's Cloak качай для танковости, Summon Familiars — на 6-м."),
    "slark": ("Dark Pact", "Dark Pact", "Pact жми заранее для диспела, Essence Shift качай после."),
    "medusa": ("Mystic Snake", "Mystic Snake", "Mana Shield качай вторым для живучести, Stone Gaze — на 6-м."),
    "troll_warlord": ("Fervor", "Berserker's Rage", "Whirling Axes качай вторым, Battle Trance — на 6-м."),
    "centaur": ("Hoof Stomp", "Hoof Stomp", "Double Edge качай вторым, Stampede — на 6-м для спасения."),
    "magnataur": ("Shockwave", "Shockwave", "Empower качай вторым для фарма, Reverse Polarity — на 6-м."),
    "shredder": ("Reactive Armor", "Whirling Death", "Timber Chain качай вторым для эскейпа, Chakram — на 6-м."),
    "bristleback": ("Quill Spray", "Quill Spray", "Bristleback качай для живучести, Viscous Nasal Goo — для слоу."),
    "tusk": ("Ice Shards", "Ice Shards", "Snowball качай вторым для инициации, Walrus Punch — на 6-м."),
    "skywrath_mage": ("Arcane Bolt", "Arcane Bolt", "Concussive Shot качай вторым, Mystic Flare — на 6-м."),
    "abaddon": ("Aphotic Shield", "Aphotic Shield", "Curse of Avernus качай вторым, Borrowed Time — на 6-м."),
    "elder_titan": ("Echo Stomp", "Echo Stomp", "Natural Order качай вторым, Earth Splitter — на 6-м."),
    "legion_commander": ("Overwhelming Odds", "Overwhelming Odds", "Moment of Courage качай вторым, Duel — на 6-м."),
    "techies": ("Sticky Bomb", "Sticky Bomb", "Blast Off! качай вторым для инициации, Remote Mines — на 6-м."),
    "ember_spirit": ("Searing Chains", "Searing Chains", "Sleight of Fist качай вторым, Fire Remnant — на 6-м."),
    "earth_spirit": ("Boulder Smash", "Boulder Smash", "Rolling Boulder качай для мобильности, Magnetize — на 6-м."),
    "abyssal_underlord": ("Firestorm", "Firestorm", "Atrophy Aura качай для урона, Dark Rift — на 6-м."),
    "terrorblade": ("Metamorphosis", "Conjure Image", "Reflection качай вторым для хараса, Sunder — на 6-м."),
    "phoenix": ("Fire Spirits", "Fire Spirits", "Icarus Dive качай вторым, Supernova — на 6-м."),
    "oracle": ("Purifying Flames", "Fortune's End", "Fate's Edict бери на 2-м для сейва, False Promise — на 6-м."),
    "winter_wyvern": ("Arctic Burn", "Arctic Burn", "Cold Embrace качай для сейва, Winter's Curse — на 6-м."),
    "arc_warden": ("Flux", "Flux", "Magnetic Field качай вторым, Tempest Double — на 6-м."),
    "monkey_king": ("Boundless Strike", "Boundless Strike", "Jingu Mastery качай вторым, Wukong's Command — на 6-м."),
    "dark_willow": ("Bramble Maze", "Bramble Maze", "Shadow Realm качай вторым для эскейпа, Bedlam — на 6-м."),
    "pangolier": ("Swashbuckle", "Swashbuckle", "Shield Crash качай вторым, Rolling Thunder — на 6-м."),
    "grimstroke": ("Stroke of Fate", "Stroke of Fate", "Phantom's Embrace качай вторым, Soulbind — на 6-м."),
    "hoodwink": ("Acorn Shot", "Acorn Shot", "Bushwhack качай вторым для сетапа, Sharpshooter — на 6-м."),
    "void_spirit": ("Resonant Pulse", "Resonant Pulse", "Aether Remnant качай вторым, Astral Step — на 6-м."),
    "snapfire": ("Scatterblast", "Scatterblast", "Firesnap Cookie качай вторым для сейва, Mortimer Kisses — на 6-м."),
    "mars": ("Spear of Mars", "Spear of Mars", "God's Rebuke качай вторым, Arena of Blood — на 6-м."),
    "ringmaster": ("ключевую способность контроля", "контроль на 1-м уровне", "Смотри, что лучше заходит в лейне, и макси это; ульт — на 6-м."),
    "dawnbreaker": ("Starbreaker", "Starbreaker", "Celestial Hammer качай вторым, Solar Guardian — на 6-м."),
    "marci": ("Dispose", "Dispose", "Sidekick качай вторым, Unleash — на 6-м для бурста."),
    "primal_beast": ("Trample", "Trample", "Uproar качай вторым, Pulverize — на 6-м."),
    "muerta": ("The Calling", "Dead Shot", "Gunslinger качай вторым, Pierce the Veil — на 6-м."),
    "kez": ("Echo Slash", "Echo Slash", "Grappling Claw качай для мобильности; переключай стойку под дистанцию."),
    "largo": ("ключевую способность контроля", "способность под лейн", "Макси то, чем чаще попадаешь; сейв оставляй на фокус по керри."),
}


# Алиасы id/старых названий героев (вход тренера может быть на любом языке).
_HERO_ID_ALIASES: dict[str, str] = {
    "shadow_fiend": "nevermore",
    "nevermore": "nevermore",
    "sf": "nevermore",
    "outworld_destroyer": "obsidian_destroyer",
    "outworld": "obsidian_destroyer",
    "od": "obsidian_destroyer",
    "timbersaw": "shredder",
    "timber": "shredder",
    "wraith_king": "skeleton_king",
    "wk": "skeleton_king",
    "underlord": "abyssal_underlord",
    "lifestealer": "life_stealer",
    "clockwerk": "rattletrap",
    "nature_prophet": "furion",
    "natures_prophet": "furion",
    "zeus": "zuus",
    "windranger": "windrunner",
    "magnus": "magnataur",
    "io": "wisp",
    "queen_of_pain": "queenofpain",
    "qop": "queenofpain",
    "anti_mage": "antimage",
    "am": "antimage",
    "cm": "crystal_maiden",
    "crystalmaiden": "crystal_maiden",
    "pa": "phantom_assassin",
    "phantomassassin": "phantom_assassin",
    "pl": "phantom_lancer",
    "potm": "mirana",
    "kotl": "keeper_of_the_light",
    "ns": "night_stalker",
    "bh": "bounty_hunter",
    "bs": "bloodseeker",
    "bristle": "bristleback",
    "skeletonking": "skeleton_king",
    "obsidian": "obsidian_destroyer",
    "furionn": "furion",
    "sb": "spirit_breaker",
    "wr": "windrunner",
    "lc": "legion_commander",
}


def _normalize_attr(attr: str | None) -> str:
    """Приводит атрибут к контракту A1: str / agi / int / uni."""
    value = str(attr or "").strip().lower()
    if value in ("all", "universal", "uni", "universal_attr"):
        return "uni"
    if value in ("str", "agi", "int"):
        return value
    return "str"


def _norm_hero_key(value: Any) -> str:
    """Нормализует имя/id героя: нижний регистр, без npc-префикса и знаков."""
    text = str(value or "").strip().lower().replace("npc_dota_hero_", "")
    text = text.replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


# ---------------------------------------------------------------------------
# A1. Шаблонные билды для героев без детальной сборки
# ---------------------------------------------------------------------------
_ROLE_SITUATIONAL: dict[int, tuple[tuple[str, str, str], ...]] = {
    1: (
        ("Black King Bar", "Много контроля и магии", "Позволяет бить цель без страха дизейбла"),
        ("Monkey King Bar", "Уклонения у врага", "Гарантированное попадание сквозь промахи"),
        ("Nullifier", "Ghost Scepter / Aeon Disk", "Снимает защитные баффы с цели"),
        ("Silver Edge", "Сильные пассивки", "Истощение (Break) на 5 секунд"),
        ("Linken's Sphere", "Одиночный контроль (Doom/Duel)", "Блокирует первый направленный спелл"),
    ),
    2: (
        ("Black King Bar", "Много контроля и магии", "Свободно кастовать комбо в драке"),
        ("Linken's Sphere", "Одиночный контроль (Doom/Duel)", "Блокирует первый направленный спелл"),
        ("Shiva's Guard", "Тяжелый физ-урон", "Броня и АоЕ-замедление"),
        ("Aeon Disk", "Ваншот-прокаст", "Сейв от смерти на 2.5 секунды"),
        ("Nullifier", "Сейвы у врага", "Развеивает Ghost/Force/Disk"),
    ),
    3: (
        ("Pipe of Insight", "Магия у врага", "Аура щита для всей команды"),
        ("Crimson Guard", "Много физ-урона", "Блок физ-урона команде"),
        ("Lotus Orb", "Таргетный контроль", "Отражение и снятие дебаффов"),
        ("Heaven's Halberd", "Сильный райтклик", "Дизарм, который не снимается BKB"),
        ("Nullifier", "Сейвы у врага", "Снимает Ghost/Force с цели"),
    ),
    4: (
        ("Ghost Scepter", "Физ-керри у врага", "Иммунитет к автоатакам"),
        ("Aeon Disk", "Ваншот-комбо", "Сейв от мгновенной смерти"),
        ("Lotus Orb", "Сайленс/таргетные спеллы", "Диспел и отражение"),
        ("Spirit Vessel", "Реген у врага", "Срезает лечение и вампиризм"),
        ("Eul's Scepter of Divinity", "Нужен сетап или диспел", "Подброс и сброс дебаффов"),
    ),
    5: (
        ("Ghost Scepter", "Физ-керри у врага", "Иммунитет к автоатакам"),
        ("Aeon Disk", "Ваншот-комбо", "Сейв от мгновенной смерти"),
        ("Lotus Orb", "Сайленс/таргетные спеллы", "Диспел и отражение"),
        ("Spirit Vessel", "Реген у врага", "Срезает лечение и вампиризм"),
        ("Eul's Scepter of Divinity", "Нужен сетап или диспел", "Подброс и сброс дебаффов"),
    ),
}

_ROLE_SHARD: dict[int, str] = {
    1: "Шард ситуативный: бери после 2-3 кор-предметов, если он усиливает твой фарм или урон.",
    2: "Шард полезен в мид-гейме: усиливает ключевую способность под твой билд.",
    3: "Шард бери после Blink и BKB, если он усиливает инициацию или живучесть.",
    4: "Шард — приоритетный слот на 15-й минуте: усиливает полезность в драках.",
    5: "Шард покупай на 15-20-й минуте, если он усиливает сейв или контроль.",
}

_ROLE_SCEPTER: dict[int, str] = {
    1: "Аганим ситуативный: бери в лейте, если усиленный ультимейт вписывается в драфт.",
    2: "Аганим ситуативный: бери после BKB и основного урона, если усиленный ульт даёт темп.",
    3: "Аганим в лейте: усиленный ультимейт меняет инициацию — бери под драфт.",
    4: "Аганим ситуативный: бери, если усиленный ульт даёт контроль или сейв команде.",
    5: "Аганим ситуативный: в лейте усиливает ульт для всей команды.",
}


def _materialize(items: Iterable[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [{"item": item, "timing": timing, "tip": tip} for item, timing, tip in items]


def _template_build(role: int, attr: str) -> dict[str, Any]:
    """Шаблонный билд по позиции и атрибуту (contract A1 разрешает для остальных героев)."""
    role_norm = max(1, min(5, int(role or 1)))
    attribute = _normalize_attr(attr)

    if role_norm == 1:
        starting = ["Tango", "Quelling Blade", "Circlet", "Gauntlets of Strength", "Iron Branch"]
        if attribute == "str":
            early_game = ["Phase Boots", "Bracer", "Magic Wand"]
            core = [
                ("Echo Sabre / Armlet of Mordiggian", "12-15 мин", "Урон и живучесть для ранних драк"),
                ("Sange and Yasha / Manta Style", "20-23 мин", "Статы, скорость и сброс дебаффов"),
                ("Black King Bar", "24-27 мин", "Защита от магии для врыва в драку"),
                ("Heart of Tarrasque / Assault Cuirass", "31-35 мин", "Живучесть и аура для команды"),
                ("Nullifier / Abyssal Blade", "38-42 мин", "Снятие сейвов и стан по цели"),
            ]
        elif attribute == "int":
            starting = ["Tango", "Quelling Blade", "Circlet", "Mantle of Intelligence", "Iron Branch"]
            early_game = ["Power Treads", "Null Talisman", "Magic Wand"]
            core = [
                ("Witch Blade / Falcon Blade", "12-15 мин", "Урон с руки и манареген"),
                ("Black King Bar", "20-23 мин", "Кастовать и бить без контроля"),
                ("Sange and Yasha / Manta Style", "26-29 мин", "Статы и сброс дебаффов"),
                ("Eye of Skadi / Shiva's Guard", "32-36 мин", "Срез регена и живучесть"),
                ("Nullifier / Abyssal Blade", "38-42 мин", "Снятие сейвов и контроль цели"),
            ]
        elif attribute == "uni":
            early_game = ["Power Treads", "Bracer", "Magic Wand"]
            core = [
                ("Falcon Blade / Echo Sabre", "12-15 мин", "Ранний урон и статы"),
                ("Black King Bar", "20-23 мин", "Защита от магии для врыва"),
                ("Sange and Yasha / Manta Style", "26-29 мин", "Статы и сброс дебаффов"),
                ("Aghanim's Scepter", "32-36 мин", "Усиление ключевой способности"),
                ("Nullifier / Abyssal Blade", "38-42 мин", "Снятие сейвов и контроль цели"),
            ]
        else:  # agi
            starting = ["Tango", "Quelling Blade", "Circlet", "Slippers of Agility", "Iron Branch"]
            early_game = ["Power Treads", "Wraith Band", "Magic Wand"]
            core = [
                ("Maelstrom / Battle Fury", "13-16 мин", "Скорость фарма и зачистка пачек"),
                ("Manta Style", "20-23 мин", "Сброс дебаффов и давление иллюзиями"),
                ("Black King Bar", "24-27 мин", "Защита от магии для врыва"),
                ("Eye of Skadi / Butterfly", "31-35 мин", "Срез регена или максимум урона"),
                ("Nullifier / Abyssal Blade", "38-42 мин", "Снятие сейвов и стан по цели"),
            ]
    elif role_norm == 2:
        starting = ["Tango", "Faerie Fire", "Iron Branch", "Iron Branch", "Circlet"]
        early_game = ["Bottle", "Power Treads", "Magic Wand"]
        if attribute == "int":
            early_game = ["Bottle", "Power Treads", "Null Talisman", "Magic Wand"]
            core = [
                ("Witch Blade / Kaya and Sange", "13-16 мин", "Усиление спеллов и манареген"),
                ("Orchid Malevolence", "19-22 мин", "Сайленс под одиночную цель"),
                ("Black King Bar", "24-27 мин", "Свободно кастовать комбо в драке"),
                ("Aghanim's Scepter / Scythe of Vyse", "29-33 мин", "Усиление ульта или хекс"),
                ("Shiva's Guard / Refresher Orb", "36-40 мин", "Броня и повторный прокаст"),
            ]
        elif attribute == "agi":
            early_game = ["Bottle", "Power Treads", "Wraith Band", "Magic Wand"]
            core = [
                ("Witch Blade / Dragon Lance", "13-16 мин", "Урон по способностям и дистанция"),
                ("Black King Bar", "20-23 мин", "Бить и кастовать без дизейбла"),
                ("Desolator / Manta Style", "26-29 мин", "Минус брони или сброс дебаффов"),
                ("Satanic / Butterfly", "33-37 мин", "Вампиризм или уклонение"),
                ("Nullifier / Abyssal Blade", "38-42 мин", "Снятие сейвов и контроль цели"),
            ]
        elif attribute == "str":
            early_game = ["Bottle", "Phase Boots", "Bracer", "Magic Wand"]
            core = [
                ("Echo Sabre / Armlet of Mordiggian", "12-15 мин", "Ранний урон и живучесть"),
                ("Black King Bar", "20-23 мин", "Инициация без страха контроля"),
                ("Blink Dagger", "26-29 мин", "Мгновенный врыв в драку"),
                ("Heart of Tarrasque / Shiva's Guard", "33-37 мин", "Танкование и аура"),
                ("Overwhelming Blink", "38-42 мин", "Инициация с уроном по площади"),
            ]
        else:  # uni
            early_game = ["Bottle", "Power Treads", "Bracer", "Magic Wand"]
            core = [
                ("Falcon Blade / Echo Sabre", "12-15 мин", "Ранний урон и статы"),
                ("Black King Bar", "20-23 мин", "Кастовать и бить без дизейбла"),
                ("Aghanim's Scepter", "26-29 мин", "Усиление ключевой способности"),
                ("Eye of Skadi / Shiva's Guard", "33-37 мин", "Живучесть и замедление"),
                ("Scythe of Vyse", "38-42 мин", "Хекс по ключевой цели"),
            ]
    elif role_norm == 3:
        starting = ["Tango", "Quelling Blade", "Bracer", "Iron Branch", "Iron Branch"]
        early_game = ["Phase Boots", "Magic Wand", "Bracer", "Chainmail"]
        core = [
            ("Blink Dagger", "11-14 мин", "Мгновенная инициация"),
            ("Blade Mail / Pipe of Insight", "16-19 мин", "Возврат урона или аура щита"),
            ("Black King Bar", "22-25 мин", "Врыв без страха контроля"),
            ("Heart of Tarrasque / Shiva's Guard", "29-33 мин", "Танкование урона команды"),
            ("Overwhelming Blink", "36-40 мин", "Инициация с АоЕ-уроном"),
        ]
    elif role_norm == 4:
        starting = ["Tango", "Blood Grenade", "Sentry Ward", "Observer Ward", "Iron Branch", "Faerie Fire"]
        early_game = ["Tranquil Boots", "Magic Wand", "Wind Lace", "Infused Raindrops"]
        core = [
            ("Glimmer Cape", "13-16 мин", "Сейв союзника невидимостью"),
            ("Force Staff", "19-22 мин", "Выталкивание из зоны опасности"),
            ("Aghanim's Shard", "22-25 мин", "Усиление полезности героя"),
            ("Black King Bar / Ghost Scepter", "28-32 мин", "Выживать под фокусом"),
            ("Aghanim's Scepter / Aeon Disk", "34-38 мин", "Усиление ульта или сейв"),
        ]
    else:  # 5
        starting = ["Tango", "Blood Grenade", "Sentry Ward", "Observer Ward", "Iron Branch", "Faerie Fire"]
        early_game = ["Arcane Boots", "Magic Wand", "Wind Lace", "Infused Raindrops"]
        core = [
            ("Glimmer Cape", "13-16 мин", "Сейв союзника невидимостью"),
            ("Force Staff", "19-22 мин", "Выталкивание из зоны опасности"),
            ("Aghanim's Shard", "24-27 мин", "Усиление полезности героя"),
            ("Aghanim's Scepter / Pipe of Insight", "30-34 мин", "Усиление ульта или аура щита"),
            ("Aeon Disk / Guardian Greaves", "36-40 мин", "Сейв и реген команде"),
        ]

    situational = [
        {"item": item, "trigger": trigger, "why": why}
        for item, trigger, why in _ROLE_SITUATIONAL[role_norm]
    ]
    return {
        "starting": list(starting),
        "early_game": list(early_game),
        "core": _materialize(core),
        "situational": situational,
        "shard": _ROLE_SHARD[role_norm],
        "scepter": _ROLE_SCEPTER[role_norm],
        "lane_tips": "",
    }


def _build_compact_hero(hero_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Собирает запись героя из компактных фактов и шаблонных билдов."""
    attr = _normalize_attr(facts.get("attr"))
    roles = [int(r) for r in (facts.get("roles") or [1]) if 1 <= int(r) <= 5] or [1]
    lane_tip = str(facts.get("lane_tip") or "").strip()
    builds: dict[int, dict[str, Any]] = {}
    for role in sorted(set(roles)):
        build = _template_build(role, attr)
        build["lane_tips"] = lane_tip
        builds[role] = build
    return {
        "id": hero_id,
        "name": str(facts.get("name") or hero_id),
        "ru_name": str(facts.get("ru_name") or hero_id),
        "attr": attr,
        "roles": sorted(set(roles)),
        "tags": list(facts.get("tags") or []),
        "counters": list(facts.get("counters") or []),
        "countered_by": list(facts.get("countered_by") or []),
        "synergies": list(facts.get("synergies") or []),
        "builds": builds,
    }


def _assemble_meta_db() -> dict[str, dict[str, Any]]:
    database: dict[str, dict[str, Any]] = {}
    for hero_id, meta in _DETAILED_HEROES_DB.items():
        database[hero_id] = meta
    for hero_id, facts in _HERO_FACTS.items():
        database[hero_id] = _build_compact_hero(hero_id, facts)
    return database


META_HEROES_DB: dict[str, dict[str, Any]] = _assemble_meta_db()
_ALL_HERO_IDS: frozenset[str] = frozenset(META_HEROES_DB)


def _finalize_meta_db(database: dict[str, dict[str, Any]]) -> None:
    """Нормализует ссылки, добивает билды на все роли и раскрывает контрпики симметрично."""
    for hero_id, meta in database.items():
        meta["attr"] = _normalize_attr(meta.get("attr"))

        roles: list[int] = []
        for raw_role in meta.get("roles") or [1]:
            try:
                role = int(raw_role)
            except (TypeError, ValueError):
                continue
            if 1 <= role <= 5 and role not in roles:
                roles.append(role)
        meta["roles"] = sorted(roles) or [1]

        for field_name in ("counters", "countered_by", "synergies"):
            cleaned: list[str] = []
            for raw_ref in meta.get(field_name) or []:
                ref = _HERO_ID_ALIASES.get(str(raw_ref), str(raw_ref))
                if ref in _ALL_HERO_IDS and ref != hero_id and ref not in cleaned:
                    cleaned.append(ref)
            meta[field_name] = cleaned

        builds: dict[int, dict[str, Any]] = {}
        for raw_key, build in (meta.get("builds") or {}).items():
            try:
                builds[int(raw_key)] = build
            except (TypeError, ValueError):
                continue
        base_tip = ""
        if builds:
            base_tip = str(next(iter(builds.values())).get("lane_tips") or "").strip()
        for role in meta["roles"]:
            if role not in builds:
                build = _template_build(role, meta["attr"])
                build["lane_tips"] = base_tip or (
                    "Держи тайминги ключевых предметов и не дерись без прикрытия."
                )
                builds[role] = build
        if not builds:
            builds[1] = _template_build(1, meta["attr"])
        meta["builds"] = dict(sorted(builds.items()))

    # Контрпики в обе стороны: если A контрит B, то B получает A в countered_by.
    for hero_id, meta in database.items():
        for enemy_id in meta["counters"]:
            target = database[enemy_id]
            if hero_id not in target["countered_by"]:
                target["countered_by"].append(hero_id)
    # Синергии тоже раскрываем симметрично.
    for hero_id, meta in database.items():
        for ally_id in meta["synergies"]:
            target = database[ally_id]
            if hero_id not in target["synergies"]:
                target["synergies"].append(hero_id)

    for meta in database.values():
        meta["countered_by"] = meta["countered_by"][:10]
        meta["synergies"] = meta["synergies"][:8]


_finalize_meta_db(META_HEROES_DB)


# ---------------------------------------------------------------------------
# A1/A3. Шаблон fallback-билда (публичный, сохранён для обратной совместимости)
# ---------------------------------------------------------------------------
def generate_fallback_build(hero_name: str = "Герой", role: int = 1, primary_attr: str = "str") -> dict[str, Any]:
    """Возвращает шаблонный билд для героя без детальной сборки."""
    role_norm = max(1, min(5, int(role or 1)))
    build = _template_build(role_norm, primary_attr)
    build["lane_tips"] = (
        f"Точной статистики по {hero_name} в базе нет: играй от своих сильных сторон, "
        "держи тайминги ключевых предметов и не дерись без прикрытия."
    )
    return build


# ---------------------------------------------------------------------------
# Поиск героя: id / английское / русское имя, опечатки и прозвища
# ---------------------------------------------------------------------------
_RU_ALIAS_INDEX: dict[str, str] | None = None


def _ru_alias_index() -> dict[str, str]:
    """Лениво строит индекс русских прозвищ из aidota.heroes.RU_ALIASES."""
    global _RU_ALIAS_INDEX
    if _RU_ALIAS_INDEX is not None:
        return _RU_ALIAS_INDEX
    index: dict[str, str] = {}
    try:
        from .heroes import RU_ALIASES  # ленивый импорт: meta_db не должен зависеть от каталога
    except Exception:  # noqa: BLE001
        RU_ALIASES = {}
    for short_id, variants in RU_ALIASES.items():
        target = _HERO_ID_ALIASES.get(short_id, short_id)
        if target not in META_HEROES_DB:
            continue
        for variant in variants:
            key = _norm_hero_key(variant)
            if key:
                index.setdefault(key, target)
    _RU_ALIAS_INDEX = index
    return index


def get_hero_meta_entry(hero_name: str) -> dict[str, Any] | None:
    """Ищет героя в базе по id, английскому или русскому имени (и прозвищам)."""
    raw = str(hero_name or "").strip()
    if not raw:
        return None
    lower = raw.lower().replace("npc_dota_hero_", "").strip()
    if lower in META_HEROES_DB:
        return META_HEROES_DB[lower]
    alias_id = _HERO_ID_ALIASES.get(lower)
    if alias_id in META_HEROES_DB:
        return META_HEROES_DB[alias_id]

    key = _norm_hero_key(raw)
    if not key:
        return None
    for hero_id, meta in META_HEROES_DB.items():
        if key == _norm_hero_key(hero_id):
            return meta
        if key == _norm_hero_key(meta.get("name")):
            return meta
        if key == _norm_hero_key(meta.get("ru_name")):
            return meta

    alias_target = _ru_alias_index().get(key)
    if alias_target:
        return META_HEROES_DB[alias_target]

    if len(key) >= 4:
        for meta in META_HEROES_DB.values():
            for candidate in (_norm_hero_key(meta.get("name")), _norm_hero_key(meta.get("ru_name"))):
                if candidate and len(candidate) >= 4 and (key in candidate or candidate in key):
                    return meta
    return None


def _as_name_list(value: Any) -> list[str]:
    """Аккуратно превращает вход тренера в список имён (строка/список/None)."""
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        return []


def _resolve_hero_id(value: Any) -> str | None:
    """Имя/id героя -> канонический короткий id из META_HEROES_DB."""
    if isinstance(value, dict):
        value = value.get("id") or value.get("name") or ""
    meta = get_hero_meta_entry(str(value or ""))
    return meta["id"] if meta else None


def _resolve_hero_ids(values: Any) -> list[str]:
    result: list[str] = []
    for name in _as_name_list(values):
        hero_id = _resolve_hero_id(name)
        if hero_id and hero_id not in result:
            result.append(hero_id)
    return result


# ---------------------------------------------------------------------------
# A3: публичные функции знаний для тренера
# ---------------------------------------------------------------------------
def _counter_items_for_tags(tags: Iterable[str], limit: int = 6) -> list[str]:
    items: list[str] = []
    for tag in tags:
        for item in TAG_COUNTER_ITEMS.get(tag, ()):
            if item not in items:
                items.append(item)
            if len(items) >= limit:
                return items
    return items


def get_hero_counters(hero_name: str) -> dict[str, Any] | None:
    """Контр-предметы и контр-герои против героя (A3). None для неизвестного героя."""
    meta = get_hero_meta_entry(hero_name)
    if not meta:
        return None
    tags = [tag for tag in (meta.get("tags") or []) if tag in TAG_COUNTER_ITEMS]
    counter_items = _counter_items_for_tags(tags, limit=6)
    counter_heroes = [
        META_HEROES_DB[cid]["name"]
        for cid in meta.get("countered_by") or []
        if cid in META_HEROES_DB
    ][:5]
    threat_bits = [TAG_THREAT_PHRASES[tag] for tag in tags[:2] if tag in TAG_THREAT_PHRASES]
    threat_text = ", ".join(threat_bits) or "сильная сторона героя"
    if counter_items:
        tip = f"{threat_text.capitalize()}: бери {', '.join(counter_items[:3])}."
    else:
        tip = f"{threat_text.capitalize()} — играй аккуратнее."
    if counter_heroes:
        tip += f" Опаснее всего {counter_heroes[0]}."
    return {
        "hero_id": meta["id"],
        "name": meta["name"],
        "ru_name": meta["ru_name"],
        "tags": tags,
        "counter_items": counter_items,
        "counter_heroes": counter_heroes,
        "tip": tip,
    }


def get_skill_order(hero_name: str) -> dict[str, Any] | None:
    """Порядок прокачки героя (A3). None для неизвестного героя."""
    meta = get_hero_meta_entry(hero_name)
    if not meta:
        return None
    skill = SKILL_ORDERS.get(meta["id"])
    if skill:
        priority, level1, tip = skill
    else:
        priority = "ключевую способность урона или контроля"
        level1 = "способность, которая помогает на линии"
        tip = "Макси то, чем чаще попадаешь по врагу, а ультимейт бери на 6-м уровне."
    return {
        "hero_id": meta["id"],
        "ru_name": meta["ru_name"],
        "priority": priority,
        "level1": level1,
        "tip": tip,
    }


def get_hero_threats(hero_names: list[str]) -> list[dict[str, Any]]:
    """Короткие угрозы по списку вражеских героев (A3), порядок сохраняется."""
    result: list[dict[str, Any]] = []
    for name in _as_name_list(hero_names):
        meta = get_hero_meta_entry(name)
        if not meta:
            continue
        tags = [tag for tag in (meta.get("tags") or []) if tag in TAG_THREAT_PHRASES]
        threat = ", ".join(TAG_THREAT_PHRASES[tag] for tag in tags[:2]) or "сильная сторона героя"
        result.append({
            "hero_id": meta["id"],
            "ru_name": meta["ru_name"],
            "tags": tags,
            "threat": threat,
            "counter_items": _counter_items_for_tags(tags, limit=5),
        })
    return result


def get_lane_tips(hero_name: str, role: int, enemy_heroes: list[str] | None = None) -> str:
    """1-2 предложения тактики на линию под роль и врагов (A3); никогда не пустая строка."""
    try:
        role_norm = max(1, min(5, int(role or 1)))
    except (TypeError, ValueError):
        role_norm = 1
    meta = get_hero_meta_entry(hero_name)
    if not meta:
        return (
            "Точных данных по этому герою нет: добивай крипов безопасно, "
            "держи дистанцию и не дерись без прикрытия саппорта."
        )
    builds = meta.get("builds") or {}
    build = builds.get(role_norm) or next(iter(builds.values()), {})
    base = str(build.get("lane_tips") or "").strip()
    if not base:
        base = (
            f"Играй от сильных сторон {meta['ru_name']}: добивай крипов, "
            "разменивайся по кулдаунам и следи за вижном."
        )
    threats = get_hero_threats(enemy_heroes or [])
    threat = threats[0] if threats else None
    if threat:
        enemy = META_HEROES_DB.get(threat["hero_id"])
        enemy_display = enemy["name"] if enemy else threat["ru_name"]
        if threat.get("counter_items"):
            extra = f"Против {enemy_display} держи наготове {threat['counter_items'][0]}."
        else:
            extra = f"Против {enemy_display} играй аккуратнее: {threat['threat']}."
        if _norm_hero_key(enemy_display) not in _norm_hero_key(base):
            base = f"{base} {extra}"
    return base.strip()


# ---------------------------------------------------------------------------
# A1: детальный билд героя с адаптацией под врагов
# ---------------------------------------------------------------------------
def _enemy_meta_list(enemy_heroes: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in _as_name_list(enemy_heroes):
        meta = get_hero_meta_entry(name)
        if meta:
            result.append(meta)
    return result


def get_hero_build(hero_name: str, role: int = 1, enemy_heroes: list[str] | None = None) -> dict[str, Any]:
    """Возвращает детальный билд на героя с учетом позиции и вражеских угроз."""
    role_norm = max(1, min(5, int(role or 1)))
    meta = get_hero_meta_entry(hero_name)

    if meta and meta.get("builds"):
        build = meta["builds"].get(role_norm) or next(iter(meta["builds"].values()))
        result: dict[str, Any] = {
            "starting": list(build.get("starting") or []),
            "early_game": list(build.get("early_game") or []),
            "core": [dict(item) for item in build.get("core") or []],
            "situational": [dict(item) for item in build.get("situational") or []],
            "shard": build.get("shard", ""),
            "scepter": build.get("scepter", ""),
            "lane_tips": build.get("lane_tips", ""),
            "hero_name": meta["name"],
            "hero_ru_name": meta["ru_name"],
            "role": role_norm,
            "role_name": ROLE_NAMES.get(role_norm, f"Позиция {role_norm}"),
        }
    else:
        clean_name = str(hero_name or "Герой").replace("npc_dota_hero_", "").capitalize()
        result = generate_fallback_build(clean_name, role_norm)
        result["hero_name"] = clean_name
        result["hero_ru_name"] = clean_name
        result["role"] = role_norm
        result["role_name"] = ROLE_NAMES.get(role_norm, f"Позиция {role_norm}")

    # Динамическая адаптация ситуативных предметов под вражеских героев.
    if enemy_heroes:
        adapted_situational = list(result.get("situational") or [])
        enemy_str = " ".join(str(e).lower() for e in _as_name_list(enemy_heroes))

        # 1) Прежние точечные триггеры (сохранены ради обратной совместимости).
        if any(h in enemy_str for h in ("phantom_assassin", "фантом", "windrunner", "вр")):
            adapted_situational.insert(0, {
                "item": "Monkey King Bar",
                "trigger": "Уклонения у врагов",
                "why": "ОБЯЗАТЕЛЬНО против Phantom Assassin / Windranger для пробития уворотов!",
                "priority": "HIGH",
            })
        if any(h in enemy_str for h in ("morph", "морф", "huskar", "хускар", "alchemist", "алхимик", "bristle", "бристл")):
            adapted_situational.insert(0, {
                "item": "Spirit Vessel / Eye of Skadi",
                "trigger": "Высокий хил/реген врагов",
                "why": "Срезает лечение и вампиризм вражеских танков и керри на 40%+!",
                "priority": "HIGH",
            })
        if any(h in enemy_str for h in ("bristle", "бристл", "spectre", "спектр", "shredder", "timbersaw", "тимбер")):
            adapted_situational.insert(0, {
                "item": "Silver Edge",
                "trigger": "Ключевые пассивки",
                "why": "Истощает защиту спины Бристлбека или дисперсию Спектры на 5 секунд!",
                "priority": "HIGH",
            })
        if any(h in enemy_str for h in ("doom", "дум", "legion", "легион", "дуэль", "bane")):
            adapted_situational.insert(0, {
                "item": "Linken's Sphere",
                "trigger": "Направленный контроль",
                "why": "Блокирует смертельный ульт (Duel / Doom / Fiend's Grip)!",
                "priority": "HIGH",
            })

        # 2) Универсальная адаптация по тегам угроз A2.
        existing_items = " ".join(str(item.get("item", "")).lower() for item in adapted_situational)
        added = 0
        for enemy_meta in _enemy_meta_list(enemy_heroes):
            if added >= 5:
                break
            for tag in enemy_meta.get("tags") or []:
                for item in TAG_COUNTER_ITEMS.get(tag, ())[:2]:
                    if added >= 5:
                        break
                    if item.lower() in existing_items:
                        continue
                    adapted_situational.insert(0, {
                        "item": item,
                        "trigger": f"{TAG_THREAT_PHRASES.get(tag, tag)} ({enemy_meta['ru_name']})",
                        "why": f"Против {enemy_meta['name']} закрывает {TAG_THREAT_PHRASES.get(tag, tag)}.",
                        "priority": "HIGH",
                    })
                    existing_items += f" {item.lower()}"
                    added += 1
        result["situational"] = adapted_situational[:10]

    return result


# ---------------------------------------------------------------------------
# A4: умный рекомендатор пиков
# ---------------------------------------------------------------------------
_DRAFT_NEED_TAGS: dict[str, tuple[str, ...]] = {
    "control": ("single_target_cc", "magic_burst_cc", "silence"),
    "save": ("saves_buffs", "heal_regen"),
    "push": ("split_push", "summons"),
    "frontline": ("tanky", "saves_buffs"),
}


def _draft_needs(ally_ids: list[str]) -> set[str]:
    """Каких тегов не хватает драфту союзников (A4: покрытие ролей/тегов)."""
    if not ally_ids:
        return set(_DRAFT_NEED_TAGS)
    ally_tags: set[str] = set()
    for ally_id in ally_ids:
        ally = META_HEROES_DB.get(ally_id)
        if ally:
            ally_tags.update(ally.get("tags") or ())
    needs: set[str] = set()
    for need, tag_keys in _DRAFT_NEED_TAGS.items():
        if not ally_tags.intersection(tag_keys):
            needs.add(need)
    return needs


def recommend_picks(
    enemy_heroes: list[str],
    ally_heroes: list[str] | None = None,
    role: int = 1,
    top_k: int = 4,
    banned_heroes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Умный подбор пиков: контрпики, синергии, покрытие тегов драфта и роли.

    Скоринг (A4): базовые 50, жёсткий контрпик +30, враг контрит героя −25,
    синергия с союзником +20, покрытие нужного драфту тега +10 (максимум +20).
    Уже взятые (враги/союзники) и забаненные герои в рекомендации не попадают.
    Поля прежнего ответа сохранены, добавлен ``countered_by_preview``.
    """
    try:
        role_norm = max(1, min(5, int(role or 1)))
    except (TypeError, ValueError):
        role_norm = 1
    try:
        count = max(1, int(top_k or 4))
    except (TypeError, ValueError):
        count = 4

    enemy_ids = _resolve_hero_ids(enemy_heroes)
    ally_ids = _resolve_hero_ids(ally_heroes)
    banned_ids = _resolve_hero_ids(banned_heroes)
    excluded = set(enemy_ids) | set(ally_ids) | set(banned_ids)
    needs = _draft_needs(ally_ids)

    candidates: list[tuple[float, dict[str, Any]]] = []

    for hero_id, meta in META_HEROES_DB.items():
        if hero_id in excluded:
            continue
        if role_norm not in (meta.get("roles") or []):
            continue

        counter_ids = set(meta.get("counters") or [])
        countered_by_ids = set(meta.get("countered_by") or [])
        synergy_ids = set(meta.get("synergies") or [])
        candidate_tags = set(meta.get("tags") or ())

        score = 50.0
        strong_against: list[str] = []
        weak_against: list[str] = []
        combos: list[str] = []

        for enemy_id in enemy_ids:
            enemy = META_HEROES_DB.get(enemy_id)
            if not enemy:
                continue
            if enemy_id in counter_ids:
                score += 30
                strong_against.append(f"Хорош против {enemy['name']}")
            if enemy_id in countered_by_ids:
                score -= 25
                weak_against.append(f"Слаб против {enemy['name']}")

        for ally_id in ally_ids:
            ally = META_HEROES_DB.get(ally_id)
            if not ally or ally_id not in synergy_ids:
                continue
            score += 20
            combos.append(f"Синергия с {ally['name']}")

        covered_needs = [
            need
            for need, tag_keys in _DRAFT_NEED_TAGS.items()
            if need in needs and candidate_tags.intersection(tag_keys)
        ]
        if covered_needs:
            score += min(20, 10 * len(covered_needs))

        danger_ids: list[str] = []
        for enemy_id in enemy_ids:
            if enemy_id in countered_by_ids and enemy_id not in danger_ids:
                danger_ids.append(enemy_id)
        for counter_id in meta.get("countered_by") or []:
            if len(danger_ids) >= 3:
                break
            if counter_id not in danger_ids:
                danger_ids.append(counter_id)
        countered_by_preview = [
            META_HEROES_DB[cid]["name"] for cid in danger_ids[:3] if cid in META_HEROES_DB
        ]

        matchup_pct = int(max(1, min(99, round(score))))
        build_info = (meta.get("builds") or {}).get(role_norm, {})
        core_preview = [str(item.get("item", "")) for item in (build_info.get("core") or [])[:3]]

        candidates.append((score, {
            "hero_id": hero_id,
            "name": meta["name"],
            "ru_name": meta["ru_name"],
            "role": role_norm,
            "role_name": ROLE_NAMES.get(role_norm, f"Позиция {role_norm}"),
            "score": matchup_pct,
            "strong_against": strong_against or ["Стабильный лайнинг и урон"],
            "weak_against": weak_against,
            "combos": combos,
            "core_items": core_preview,
            "shard": build_info.get("shard", "По таймингу"),
            "advice": build_info.get("lane_tips", "Играйте по таймингам ключевых артефактов."),
            "tags": sorted(candidate_tags),
            "countered_by_preview": countered_by_preview,
        }))

    # Сортировка по «сырому» скору (до округления), иначе топ упирается в 99%.
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [payload for _, payload in candidates[:count]]
