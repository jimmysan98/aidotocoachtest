"""aidota.item_validator — Двусторонний реестр предметов и детерминированный пост-валидатор (Fork E1).

Обеспечивает:
  1. Двусторонний маппинг русского сленга / названий / английских слагов <-> предметов Dota 2.
  2. Полный учёт владения предметами игроком: инвентарь (slot0..slot5), бэкпак (slot6..slot8),
     тайник (stash0..stash5), съеденные Aghanim's Scepter/Shard/Moon Shard и эквиваленты апгрейдов (_UPGRADE_EQUIVALENTS).
  3. Реестр удалённых предметов (Patch 7.33+ и устаревшие) с детерминированной заменой.
  4. Анализ пробелов драфта команды по 8 критериям: control, push, frontline, save, dispel, antiheal, break, true_strike.
  5. Детерминированный пост-валидатор (Zero-LLM rewrite) для структурированных рекомендаций и текста.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .live_advice import _ROLE_FALLBACK_ITEMS, _UPGRADE_EQUIVALENTS
from .meta_db import (
    META_HEROES_DB,
    _resolve_hero_id,
    get_hero_build,
)

log = logging.getLogger("aidota.item_validator")

# ---------------------------------------------------------------------------
# Реестр удалённых предметов Dota 2 -> их современные эквиваленты
# ---------------------------------------------------------------------------
REMOVED_ITEMS_REPLACEMENTS: dict[str, str] = {
    "poor_mans_shield": "vanguard",
    "pms": "vanguard",
    "iron_talon": "quelling_blade",
    "ring_of_aquila": "wraith_band",
    "necronomicon": "helm_of_the_overlord",
    "necronomicon_1": "helm_of_the_overlord",
    "necronomicon_2": "helm_of_the_overlord",
    "necronomicon_3": "helm_of_the_overlord",
    "hood_of_defiance": "pipe_of_insight",
    "medallion_of_courage": "solar_crest",
    "tome_of_knowledge": "wisdom_rune",
    "helm_of_the_dominator_2": "helm_of_the_overlord",
}

# ---------------------------------------------------------------------------
# Двусторонний словарь русского сленга <-> английский слаг и красивое имя
# ---------------------------------------------------------------------------
SLANG_TO_CANONICAL: dict[str, tuple[str, str]] = {
    # BKB
    "бэкабэ": ("black_king_bar", "Black King Bar"),
    "бекабе": ("black_king_bar", "Black King Bar"),
    "бкб": ("black_king_bar", "Black King Bar"),
    "bkb": ("black_king_bar", "Black King Bar"),
    "black king bar": ("black_king_bar", "Black King Bar"),
    # Manta
    "манта": ("manta_style", "Manta Style"),
    "манта стайл": ("manta_style", "Manta Style"),
    "manta": ("manta_style", "Manta Style"),
    "manta style": ("manta_style", "Manta Style"),
    # Eul / Wind Waker
    "эул": ("cyclone", "Eul's Scepter of Divinity"),
    "еул": ("cyclone", "Eul's Scepter of Divinity"),
    "еула": ("cyclone", "Eul's Scepter of Divinity"),
    "эула": ("cyclone", "Eul's Scepter of Divinity"),
    "eul": ("cyclone", "Eul's Scepter of Divinity"),
    "euls": ("cyclone", "Eul's Scepter of Divinity"),
    "винд вейкер": ("wind_waker", "Wind Waker"),
    "виндвейкер": ("wind_waker", "Wind Waker"),
    "wind waker": ("wind_waker", "Wind Waker"),
    # Sticks / Wand
    "стики": ("magic_wand", "Magic Wand"),
    "стик": ("magic_wand", "Magic Wand"),
    "палочки": ("magic_wand", "Magic Wand"),
    "вэнд": ("magic_wand", "Magic Wand"),
    "magic wand": ("magic_wand", "Magic Wand"),
    "magic stick": ("magic_stick", "Magic Stick"),
    # Aghanim
    "аганим": ("ultimate_scepter", "Aghanim's Scepter"),
    "септер": ("ultimate_scepter", "Aghanim's Scepter"),
    "скипетр": ("ultimate_scepter", "Aghanim's Scepter"),
    "scepter": ("ultimate_scepter", "Aghanim's Scepter"),
    "aghanim": ("ultimate_scepter", "Aghanim's Scepter"),
    "aghanims scepter": ("ultimate_scepter", "Aghanim's Scepter"),
    # Shard
    "шард": ("aghanims_shard", "Aghanim's Shard"),
    "осколок": ("aghanims_shard", "Aghanim's Shard"),
    "shard": ("aghanims_shard", "Aghanim's Shard"),
    "aghanims shard": ("aghanims_shard", "Aghanim's Shard"),
    # Blink Dagger
    "дагер": ("blink", "Blink Dagger"),
    "даггер": ("blink", "Blink Dagger"),
    "блинк": ("blink", "Blink Dagger"),
    "blink": ("blink", "Blink Dagger"),
    "blink dagger": ("blink", "Blink Dagger"),
    "овервелминг блинк": ("overwhelming_blink", "Overwhelming Blink"),
    "свифт блинк": ("swift_blink", "Swift Blink"),
    "аркан блинк": ("arcane_blink", "Arcane Blink"),
    # Hex
    "хекс": ("sheepstick", "Scythe of Vyse"),
    "гекс": ("sheepstick", "Scythe of Vyse"),
    "гуинсо": ("sheepstick", "Scythe of Vyse"),
    "hex": ("sheepstick", "Scythe of Vyse"),
    "scythe of vyse": ("sheepstick", "Scythe of Vyse"),
    # Blade Mail
    "блейдмейл": ("blade_mail", "Blade Mail"),
    "блейд мейл": ("blade_mail", "Blade Mail"),
    "бм": ("blade_mail", "Blade Mail"),
    "bm": ("blade_mail", "Blade Mail"),
    "blade mail": ("blade_mail", "Blade Mail"),
    # Diffusal / Disperser
    "диффуза": ("diffusal_blade", "Diffusal Blade"),
    "диффуз": ("diffusal_blade", "Diffusal Blade"),
    "дифуза": ("diffusal_blade", "Diffusal Blade"),
    "diffusal": ("diffusal_blade", "Diffusal Blade"),
    "diffusal blade": ("diffusal_blade", "Diffusal Blade"),
    "дисперсер": ("disperser", "Disperser"),
    "disperser": ("disperser", "Disperser"),
    # Force Staff / Hurricane Pike
    "форс": ("force_staff", "Force Staff"),
    "форстафф": ("force_staff", "Force Staff"),
    "форс стафф": ("force_staff", "Force Staff"),
    "force staff": ("force_staff", "Force Staff"),
    "пика": ("hurricane_pike", "Hurricane Pike"),
    "хурикан": ("hurricane_pike", "Hurricane Pike"),
    "хурикейн пайк": ("hurricane_pike", "Hurricane Pike"),
    "hurricane pike": ("hurricane_pike", "Hurricane Pike"),
    "драгон ленс": ("dragon_lance", "Dragon Lance"),
    "dragon lance": ("dragon_lance", "Dragon Lance"),
    # Basher / Abyssal
    "башер": ("basher", "Skull Basher"),
    "skull basher": ("basher", "Skull Basher"),
    "абиссал": ("abyssal_blade", "Abyssal Blade"),
    "абисал": ("abyssal_blade", "Abyssal Blade"),
    "abyssal": ("abyssal_blade", "Abyssal Blade"),
    "abyssal blade": ("abyssal_blade", "Abyssal Blade"),
    # Linken / Lotus
    "линка": ("sphere", "Linken's Sphere"),
    "линка сфер": ("sphere", "Linken's Sphere"),
    "линка сфера": ("sphere", "Linken's Sphere"),
    "сфера": ("sphere", "Linken's Sphere"),
    "linken": ("sphere", "Linken's Sphere"),
    "linkens sphere": ("sphere", "Linken's Sphere"),
    "лотус": ("lotus_orb", "Lotus Orb"),
    "лотус орб": ("lotus_orb", "Lotus Orb"),
    "lotus": ("lotus_orb", "Lotus Orb"),
    "lotus orb": ("lotus_orb", "Lotus Orb"),
    # Heart / Butterfly / Satanic
    "тараска": ("heart", "Heart of Tarrasque"),
    "сердце тарраски": ("heart", "Heart of Tarrasque"),
    "heart": ("heart", "Heart of Tarrasque"),
    "heart of tarrasque": ("heart", "Heart of Tarrasque"),
    "бабочка": ("butterfly", "Butterfly"),
    "butterfly": ("butterfly", "Butterfly"),
    "сатаник": ("satanic", "Satanic"),
    "satanic": ("satanic", "Satanic"),
    # Damage: Daedalus / Deso / MKB / Rapier / Nullifier / Bloodthorn / Orchid
    "криты": ("greater_crit", "Daedalus"),
    "даедалус": ("greater_crit", "Daedalus"),
    "дедалус": ("greater_crit", "Daedalus"),
    "буриза": ("greater_crit", "Daedalus"),
    "daedalus": ("greater_crit", "Daedalus"),
    "дезоль": ("desolator", "Desolator"),
    "дезолятор": ("desolator", "Desolator"),
    "desolator": ("desolator", "Desolator"),
    "мкб": ("monkey_king_bar", "Monkey King Bar"),
    "monkey king bar": ("monkey_king_bar", "Monkey King Bar"),
    "рапира": ("rapier", "Divine Rapier"),
    "divine rapier": ("rapier", "Divine Rapier"),
    "нулик": ("nullifier", "Nullifier"),
    "нуллифаер": ("nullifier", "Nullifier"),
    "нуллификатор": ("nullifier", "Nullifier"),
    "nullifier": ("nullifier", "Nullifier"),
    "бладторн": ("bloodthorn", "Bloodthorn"),
    "блад торн": ("bloodthorn", "Bloodthorn"),
    "bloodthorn": ("bloodthorn", "Bloodthorn"),
    "орчид": ("orchid", "Orchid Malevolence"),
    "орхид": ("orchid", "Orchid Malevolence"),
    "orchid": ("orchid", "Orchid Malevolence"),
    "orchid malevolence": ("orchid", "Orchid Malevolence"),
    # Skadi / Shiva
    "скади": ("skadi", "Eye of Skadi"),
    "глаз скади": ("skadi", "Eye of Skadi"),
    "skadi": ("skadi", "Eye of Skadi"),
    "eye of skadi": ("skadi", "Eye of Skadi"),
    "шива": ("shivas_guard", "Shiva's Guard"),
    "шивас": ("shivas_guard", "Shiva's Guard"),
    "shiva": ("shivas_guard", "Shiva's Guard"),
    "shivas guard": ("shivas_guard", "Shiva's Guard"),
    # Boots
    "пт": ("power_treads", "Power Treads"),
    "павер тредс": ("power_treads", "Power Treads"),
    "power treads": ("power_treads", "Power Treads"),
    "фейзы": ("phase_boots", "Phase Boots"),
    "фейз бутс": ("phase_boots", "Phase Boots"),
    "phase boots": ("phase_boots", "Phase Boots"),
    "арканы": ("arcane_boots", "Arcane Boots"),
    "аркан бутс": ("arcane_boots", "Arcane Boots"),
    "arcane boots": ("arcane_boots", "Arcane Boots"),
    "транквилы": ("tranquil_boots", "Tranquil Boots"),
    "tranquil boots": ("tranquil_boots", "Tranquil Boots"),
    "тревела": ("travel_boots", "Boots of Travel"),
    "boots of travel": ("travel_boots", "Boots of Travel"),
    "гривсы": ("guardian_greaves", "Guardian Greaves"),
    "guardian greaves": ("guardian_greaves", "Guardian Greaves"),
    "беаринги": ("boots_of_bearing", "Boots of Bearing"),
    "boots of bearing": ("boots_of_bearing", "Boots of Bearing"),
    # Lightning: Maelstrom / Mjollnir / Gleipnir
    "молнии": ("maelstrom", "Maelstrom"),
    "маелстром": ("maelstrom", "Maelstrom"),
    "maelstrom": ("maelstrom", "Maelstrom"),
    "мьёльнир": ("mjollnir", "Mjollnir"),
    "мьелнир": ("mjollnir", "Mjollnir"),
    "mjollnir": ("mjollnir", "Mjollnir"),
    "глейпнир": ("gungir", "Gleipnir"),
    "глепнир": ("gungir", "Gleipnir"),
    "gleipnir": ("gungir", "Gleipnir"),
    # Defensive: Pipe / Crimson / Mekansm / Urn / Vessel / Solar
    "пайп": ("pipe", "Pipe of Insight"),
    "пайп оф инсайт": ("pipe", "Pipe of Insight"),
    "pipe": ("pipe", "Pipe of Insight"),
    "pipe of insight": ("pipe", "Pipe of Insight"),
    "кримсон": ("crimson_guard", "Crimson Guard"),
    "кримсон гард": ("crimson_guard", "Crimson Guard"),
    "crimson guard": ("crimson_guard", "Crimson Guard"),
    "мека": ("mekansm", "Mekansm"),
    "мекансм": ("mekansm", "Mekansm"),
    "mekansm": ("mekansm", "Mekansm"),
    "урна": ("urn_of_shadows", "Urn of Shadows"),
    "urn of shadows": ("urn_of_shadows", "Urn of Shadows"),
    "вессель": ("spirit_vessel", "Spirit Vessel"),
    "спирит вессел": ("spirit_vessel", "Spirit Vessel"),
    "spirit vessel": ("spirit_vessel", "Spirit Vessel"),
    "солар": ("solar_crest", "Solar Crest"),
    "солар крест": ("solar_crest", "Solar Crest"),
    "solar crest": ("solar_crest", "Solar Crest"),
    # Farm & Specials: Radiance / BF / Armlet / Khanda / Phylactery / Parasma / Witch Blade
    "радик": ("radiance", "Radiance"),
    "радианс": ("radiance", "Radiance"),
    "radiance": ("radiance", "Radiance"),
    "бф": ("bfury", "Battle Fury"),
    "батлфури": ("bfury", "Battle Fury"),
    "батл фури": ("bfury", "Battle Fury"),
    "battle fury": ("bfury", "Battle Fury"),
    "армлет": ("armlet", "Armlet of Mordiggian"),
    "armlet": ("armlet", "Armlet of Mordiggian"),
    "канда": ("khanda", "Khanda"),
    "khanda": ("khanda", "Khanda"),
    "филактерия": ("phylactery", "Phylactery"),
    "филактери": ("phylactery", "Phylactery"),
    "phylactery": ("phylactery", "Phylactery"),
    "паразма": ("parasma", "Parasma"),
    "parasma": ("parasma", "Parasma"),
    "вичблейд": ("witch_blade", "Witch Blade"),
    "вич блейд": ("witch_blade", "Witch Blade"),
    "witch blade": ("witch_blade", "Witch Blade"),
    # Sange / Yasha / Kaya variants
    "яша": ("yasha", "Yasha"),
    "yasha": ("yasha", "Yasha"),
    "саша": ("sange", "Sange"),
    "sange": ("sange", "Sange"),
    "кая": ("kaya", "Kaya"),
    "kaya": ("kaya", "Kaya"),
    "саша яша": ("sange_and_yasha", "Sange and Yasha"),
    "сиша": ("sange_and_yasha", "Sange and Yasha"),
    "sny": ("sange_and_yasha", "Sange and Yasha"),
    "sange and yasha": ("sange_and_yasha", "Sange and Yasha"),
    "кая яша": ("kaya_and_yasha", "Kaya and Yasha"),
    "кня": ("kaya_and_yasha", "Kaya and Yasha"),
    "kaya and yasha": ("kaya_and_yasha", "Kaya and Yasha"),
    "саша кая": ("sange_and_kaya", "Sange and Kaya"),
    "sange and kaya": ("sange_and_kaya", "Sange and Kaya"),
    # Int / Cast range / Cdr
    "линза": ("aether_lens", "Aether Lens"),
    "аетер ленс": ("aether_lens", "Aether Lens"),
    "aether lens": ("aether_lens", "Aether Lens"),
    "октан": ("octarine_core", "Octarine Core"),
    "октарин": ("octarine_core", "Octarine Core"),
    "octarine core": ("octarine_core", "Octarine Core"),
    "этериал": ("ethereal_blade", "Ethereal Blade"),
    "эзериал": ("ethereal_blade", "Ethereal Blade"),
    "ethereal blade": ("ethereal_blade", "Ethereal Blade"),
    # Invisibility
    "лотар": ("invis_sword", "Shadow Blade"),
    "шадоу блейд": ("invis_sword", "Shadow Blade"),
    "shadow blade": ("invis_sword", "Shadow Blade"),
    "сильвер": ("silver_edge", "Silver Edge"),
    "сильвер эдж": ("silver_edge", "Silver Edge"),
    "silver edge": ("silver_edge", "Silver Edge"),
    "глиммер": ("glimmer_cape", "Glimmer Cape"),
    "глимер": ("glimmer_cape", "Glimmer Cape"),
    "glimmer cape": ("glimmer_cape", "Glimmer Cape"),
    # Consumables / Utility
    "капли": ("infused_raindrop", "Infused Raindrop"),
    "raindrop": ("infused_raindrop", "Infused Raindrop"),
    "боттл": ("bottle", "Bottle"),
    "бутылка": ("bottle", "Bottle"),
    "bottle": ("bottle", "Bottle"),
    "коррозия": ("orb_of_corrosion", "Orb of Corrosion"),
    "orb of corrosion": ("orb_of_corrosion", "Orb of Corrosion"),
    "вейл": ("veil_of_discord", "Veil of Discord"),
    "veil of discord": ("veil_of_discord", "Veil of Discord"),
    "дагон": ("dagon", "Dagon"),
    "dagon": ("dagon", "Dagon"),
    "танго": ("tango", "Tango"),
    "tango": ("tango", "Tango"),
    "фласка": ("flask", "Healing Salve"),
    "healing salve": ("flask", "Healing Salve"),
    "кларити": ("clarity", "Clarity"),
    "clarity": ("clarity", "Clarity"),
    "манго": ("enchanted_mango", "Enchanted Mango"),
    "mango": ("enchanted_mango", "Enchanted Mango"),
    "даст": ("dust", "Dust of Appearance"),
    "дасты": ("dust", "Dust of Appearance"),
    "пыль": ("dust", "Dust of Appearance"),
    "dust": ("dust", "Dust of Appearance"),
    "смок": ("smoke_of_deceit", "Smoke of Deceit"),
    "smoke": ("smoke_of_deceit", "Smoke of Deceit"),
    "сентри": ("ward_sentry", "Sentry Ward"),
    "обсервер": ("ward_observer", "Observer Ward"),
    "варды": ("ward_observer", "Observer Ward"),
    "тп": ("tpscroll", "Town Portal Scroll"),
    "телепорт": ("tpscroll", "Town Portal Scroll"),
}

# Обратный маппинг: slug -> (canonical_name, [aliases])
CANONICAL_SLUG_TO_NAMES: dict[str, str] = {}
for _alias, (_slug, _dname) in SLANG_TO_CANONICAL.items():
    if _slug not in CANONICAL_SLUG_TO_NAMES:
        CANONICAL_SLUG_TO_NAMES[_slug] = _dname


# ---------------------------------------------------------------------------
# Извлечение всех предметов, которыми владеет игрок
# ---------------------------------------------------------------------------

def extract_player_owned_items(payload: dict[str, Any] | None) -> set[str]:
    """Возвращает множество канонических слагов предметов игрока.

    Учитывает:
      - Основной инвентарь (slot0..slot5)
      - Бэкпак (slot6..slot8)
      - Тайник (stash0..stash5)
      - Телепорт
      - Съеденные постоянные баффы (Aghanim's Scepter / Shard / Moon Shard)
      - Покрытые базовые компоненты для уже собранных апгрейдов (_UPGRADE_EQUIVALENTS)
    """
    owned: set[str] = set()
    if not isinstance(payload, dict):
        return owned

    hero_data = payload.get("hero") or {}
    items_data = hero_data.get("items") or {}

    # 1. Проверяем слоты hero.items
    if isinstance(items_data, dict):
        for _slot_key, item_info in items_data.items():
            if not isinstance(item_info, dict):
                continue
            raw_name = str(item_info.get("name") or "").strip().lower()
            if not raw_name or raw_name == "empty" or raw_name.startswith("item_empty"):
                continue
            if raw_name.startswith("item_"):
                raw_name = raw_name[5:]
            slug = re.sub(r"[^a-z0-9_]+", "_", raw_name).strip("_")
            if slug:
                owned.add(slug)

    # 2. Проверяем stash_items, если они вынесены отдельно
    stash_data = hero_data.get("stash_items") or hero_data.get("stash")
    if isinstance(stash_data, dict):
        for _k, s_info in stash_data.items():
            if not isinstance(s_info, dict):
                continue
            s_name = str(s_info.get("name") or "").strip().lower()
            if not s_name or s_name == "empty" or s_name.startswith("item_empty"):
                continue
            if s_name.startswith("item_"):
                s_name = s_name[5:]
            slug = re.sub(r"[^a-z0-9_]+", "_", s_name).strip("_")
            if slug:
                owned.add(slug)

    # 3. Проверяем съеденные баффы и флаги героя
    # Aghanim's Scepter
    if hero_data.get("aghanims_scepter_consumed") or hero_data.get("has_scepter"):
        owned.add("ultimate_scepter")
        owned.add("scepter")
    # Aghanim's Shard
    if hero_data.get("aghanims_shard_consumed") or hero_data.get("has_shard"):
        owned.add("aghanims_shard")
        owned.add("shard")
    # Moon Shard
    if hero_data.get("moon_shard_consumed") or hero_data.get("has_moon_shard"):
        owned.add("moon_shard")

    # Проверяем массив buffs
    buffs = hero_data.get("buffs") or []
    if isinstance(buffs, list):
        for buff in buffs:
            b_name = str(buff.get("name") if isinstance(buff, dict) else buff).lower()
            if "scepter" in b_name or "ultimate_scepter" in b_name:
                owned.add("ultimate_scepter")
            if "shard" in b_name or "aghanims_shard" in b_name:
                owned.add("aghanims_shard")
            if "moon_shard" in b_name:
                owned.add("moon_shard")

    # 4. Расширяем эквивалентами апгрейдов (_UPGRADE_EQUIVALENTS)
    # Если у игрока hurricane_pike -> force_staff и dragon_lance тоже считаются покрытыми
    expanded_owned = set(owned)
    for upgrade_name, base_tuple in _UPGRADE_EQUIVALENTS.items():
        clean_up = upgrade_name.lower().replace(" ", "_").replace("'", "")
        if clean_up in owned or upgrade_name in owned:
            for base in base_tuple:
                clean_base = base.lower().replace(" ", "_").replace("'", "")
                expanded_owned.add(clean_base)
                expanded_owned.add(base.lower())

    # Специальные псевдонимы
    if "ultimate_scepter" in expanded_owned:
        expanded_owned.add("scepter")
        expanded_owned.add("aghanims_scepter")
    if "aghanims_shard" in expanded_owned:
        expanded_owned.add("shard")
    if "black_king_bar" in expanded_owned:
        expanded_owned.add("bkb")
    if "cyclone" in expanded_owned:
        expanded_owned.add("eul")
        expanded_owned.add("euls")
        expanded_owned.add("euls_scepter_of_divinity")
    if "sheepstick" in expanded_owned:
        expanded_owned.add("hex")
        expanded_owned.add("scythe_of_vyse")

    return expanded_owned


# ---------------------------------------------------------------------------
# Резолвер и валидатор кандидатов в предметы
# ---------------------------------------------------------------------------

_SLUG_ALIASES: dict[str, set[str]] = {
    "bfury": {"bfury", "battle_fury", "battlefury", "бф", "батлфури"},
    "invis_sword": {"invis_sword", "shadow_blade", "lotar", "лотар", "шадоу_блейд"},
    "greater_crit": {"greater_crit", "daedalus", "криты", "даедалус", "буриза"},
    "cyclone": {"cyclone", "eul", "euls", "euls_scepter_of_divinity", "эул", "еул"},
    "ultimate_scepter": {"ultimate_scepter", "scepter", "aghanims_scepter", "аганим"},
    "aghanims_shard": {"aghanims_shard", "shard", "шард"},
    "sheepstick": {"sheepstick", "scythe_of_vyse", "hex", "хекс", "гуинсо"},
    "gungir": {"gungir", "gleipnir", "глейпнир"},
    "sphere": {"sphere", "linken", "linkens_sphere", "линка"},
    "shivas_guard": {"shivas_guard", "shiva", "шива"},
    "black_king_bar": {"black_king_bar", "bkb", "бкб", "бэкабэ", "бекабе"},
    "blade_mail": {"blade_mail", "bm", "бм", "блейдмейл"},
    "magic_wand": {"magic_wand", "magic_stick", "wand", "stick", "стики"},
    "power_treads": {"power_treads", "pt", "пт"},
    "phase_boots": {"phase_boots", "phase", "фейзы"},
    "arcane_boots": {"arcane_boots", "arcane", "арканы"},
    "tranquil_boots": {"tranquil_boots", "tranquil", "транквилы"},
    "travel_boots": {"travel_boots", "travel", "тревела"},
    "guardian_greaves": {"guardian_greaves", "greaves", "гривсы"},
    "boots_of_bearing": {"boots_of_bearing", "bearing", "беаринги"},
    "force_staff": {"force_staff", "force", "форс", "форстафф"},
    "hurricane_pike": {"hurricane_pike", "pike", "пика", "хурикан"},
    "dragon_lance": {"dragon_lance", "lance"},
    "diffusal_blade": {"diffusal_blade", "diffusal", "диффуза"},
    "disperser": {"disperser", "дисперсер"},
    "silver_edge": {"silver_edge", "сильвер"},
    "pipe": {"pipe", "pipe_of_insight", "пайп"},
    "crimson_guard": {"crimson_guard", "crimson", "кримсон"},
    "spirit_vessel": {"spirit_vessel", "vessel", "вессель"},
    "urn_of_shadows": {"urn_of_shadows", "urn", "урна"},
    "solar_crest": {"solar_crest", "solar", "солар"},
    "radiance": {"radiance", "радик"},
    "armlet": {"armlet", "армлет"},
    "khanda": {"khanda", "канда"},
    "phylactery": {"phylactery", "филактерия"},
    "parasma": {"parasma", "паразма"},
    "witch_blade": {"witch_blade", "вичблейд"},
    "basher": {"basher", "skull_basher", "башер"},
    "abyssal_blade": {"abyssal_blade", "abyssal", "абиссал"},
    "heart": {"heart", "heart_of_tarrasque", "тараска"},
    "butterfly": {"butterfly", "бабочка"},
    "satanic": {"satanic", "сатаник"},
    "monkey_king_bar": {"monkey_king_bar", "mkb", "мкб"},
    "desolator": {"desolator", "дезоль"},
    "nullifier": {"nullifier", "нулик"},
    "bloodthorn": {"bloodthorn", "бладторн"},
    "orchid": {"orchid", "orchid_malevolence", "орчид"},
    "octarine_core": {"octarine_core", "octarine", "октан"},
    "aether_lens": {"aether_lens", "lens", "линза"},
    "glimmer_cape": {"glimmer_cape", "glimmer", "глиммер"},
}


def resolve_item_slug_and_name(raw_name_or_slang: str) -> tuple[str, str, bool]:
    """Разрешает текстовый токен или сленг в (slug, display_name, is_removed)."""
    raw_str = raw_name_or_slang.strip()
    norm = re.sub(r"['`]+", "", raw_str.lower())
    if norm.startswith("item_"):
        norm = norm[5:]

    norm_space = re.sub(r"[^a-z0-9а-яё]+", " ", norm).strip()
    norm_underscore = re.sub(r"[^a-z0-9а-яё]+", "_", norm).strip("_")

    # 1. Проверяем список удалённых предметов
    for cand in (norm, norm_space, norm_underscore):
        if cand in REMOVED_ITEMS_REPLACEMENTS:
            repl_slug = REMOVED_ITEMS_REPLACEMENTS[cand]
            repl_name = CANONICAL_SLUG_TO_NAMES.get(repl_slug, repl_slug.replace("_", " ").title())
            return repl_slug, repl_name, True

    # 2. Проверяем прямой сленг / алиасы
    for cand in (norm, norm_space, norm_underscore):
        if cand in SLANG_TO_CANONICAL:
            slug, dname = SLANG_TO_CANONICAL[cand]
            return slug, dname, False

    # 3. Чистый слаг
    clean_slug = norm_underscore
    if clean_slug in REMOVED_ITEMS_REPLACEMENTS:
        repl_slug = REMOVED_ITEMS_REPLACEMENTS[clean_slug]
        repl_name = CANONICAL_SLUG_TO_NAMES.get(repl_slug, repl_slug.replace("_", " ").title())
        return repl_slug, repl_name, True

    if clean_slug in CANONICAL_SLUG_TO_NAMES:
        return clean_slug, CANONICAL_SLUG_TO_NAMES[clean_slug], False

    # Поиск по подстроке
    for alias, (slug, dname) in SLANG_TO_CANONICAL.items():
        if alias in (norm, norm_space, norm_underscore):
            return slug, dname, False

    # Неизвестный предмет
    return clean_slug, raw_str, False


def is_item_owned_or_covered(item_slug: str, owned_items: set[str]) -> bool:
    """Проверяет, куплен ли предмет или его старший апгрейд."""
    clean = item_slug.lower().replace("item_", "").replace(" ", "_").replace("'", "")
    aliases = _SLUG_ALIASES.get(clean, {clean})
    if aliases.intersection(owned_items):
        return True
    for own in owned_items:
        clean_own = own.lower().replace("item_", "").replace(" ", "_").replace("'", "")
        if clean_own == clean or clean_own in aliases:
            return True
        own_aliases = _SLUG_ALIASES.get(clean_own, {clean_own})
        if clean in own_aliases or aliases.intersection(own_aliases):
            return True

    # Проверка эквивалентов апгрейдов (_UPGRADE_EQUIVALENTS)
    for upgrade_name, bases in _UPGRADE_EQUIVALENTS.items():
        clean_up = upgrade_name.lower().replace(" ", "_").replace("'", "")
        up_aliases = _SLUG_ALIASES.get(clean_up, {clean_up})
        is_up_owned = bool(up_aliases.intersection(owned_items))
        if not is_up_owned:
            is_up_owned = any(own in up_aliases or clean_up == own.lower().replace(" ", "_").replace("'", "") for own in owned_items)
        if is_up_owned:
            for b in bases:
                clean_b = b.lower().replace(" ", "_").replace("'", "")
                base_aliases = _SLUG_ALIASES.get(clean_b, {clean_b})
                if clean in base_aliases or aliases.intersection(base_aliases):
                    return True
    return False


def validate_item_candidate(
    raw_item: str,
    owned_items: set[str],
) -> tuple[bool, str, str | None]:
    """Валидирует кандидата в предмет.

    Возвращает (is_valid, resolved_name, rejection_reason).
    """
    slug, name, is_removed = resolve_item_slug_and_name(raw_item)
    if is_removed:
        return False, name, f"Предмет {raw_item} удалён из текущего патча Dota 2."
    if is_item_owned_or_covered(slug, owned_items):
        return False, name, f"Предмет {name} уже куплен или покрыт апгрейдом игрока."
    return True, name, None


# ---------------------------------------------------------------------------
# Детерминированный пост-валидатор (Zero-LLM Rewrite)
# ---------------------------------------------------------------------------

def pick_deterministic_fallback_item(
    hero_name: str,
    role: int,
    owned_items: set[str],
    enemy_heroes: list[str] | None = None,
) -> tuple[str, str]:
    """Детерминированно подбирает следующий валидный предмет (Zero-LLM fallback).

    Возвращает (item_name, reason).
    """
    # 1. Проверяем эталонный билд героя из meta_db
    try:
        build = get_hero_build(hero_name, role=role, enemy_heroes=enemy_heroes)
        candidates: list[tuple[str, str]] = []
        for slot in (build.get("core") or []):
            if isinstance(slot, dict) and slot.get("item"):
                candidates.append((str(slot["item"]), f"Ключевой кор-слот по эталонному таймингу ({slot.get('timing') or 'мидгейм'})"))
        for slot in (build.get("situational") or []):
            if isinstance(slot, dict) and slot.get("item"):
                candidates.append((str(slot["item"]), str(slot.get("why") or "Ситуативный слот под драфт врага")))

        for item_name, reason in candidates:
            valid, resolved_name, _ = validate_item_candidate(item_name, owned_items)
            if valid:
                return resolved_name, reason
    except Exception:  # noqa: BLE001
        pass

    # 2. Проверяем универсальные слоты по роли (_ROLE_FALLBACK_ITEMS)
    role_fallbacks = _ROLE_FALLBACK_ITEMS.get(role, _ROLE_FALLBACK_ITEMS[1])
    for item_name, reason in role_fallbacks:
        valid, resolved_name, _ = validate_item_candidate(item_name, owned_items)
        if valid:
            return resolved_name, reason

    # 3. Железный абсолютный детерминированный сейв-слот
    for universal in ("Black King Bar", "Blink Dagger", "Force Staff", "Ghost Scepter", "Glimmer Cape"):
        valid, resolved_name, _ = validate_item_candidate(universal, owned_items)
        if valid:
            return resolved_name, "Универсальный защитный слот для командных драк."

    return "Town Portal Scroll", "Всегда держи ТП наготове для сейва и ротаций."


def sanitize_structured_item_advice(
    advice_list: list[dict[str, Any]],
    owned_items: set[str],
    hero_name: str = "",
    role: int = 1,
    enemy_heroes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Пост-валидирует структурированные DTO рекомендаций предметов.

    Удаляет или детерминированно заменяет удалённые/уже купленные предметы
    без повторного вызова LLM.
    """
    sanitized: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for entry in advice_list:
        raw_name = str(entry.get("item") or entry.get("name") or "").strip()
        if not raw_name:
            continue
        valid, resolved_name, _ = validate_item_candidate(raw_name, owned_items)
        slug, _, _ = resolve_item_slug_and_name(raw_name)
        if valid and slug not in seen_slugs:
            seen_slugs.add(slug)
            new_entry = dict(entry)
            new_entry["item"] = resolved_name
            sanitized.append(new_entry)

    # Если после валидации список оказался пуст — детерминированно добавляем замену
    if not sanitized:
        fallback_name, fallback_reason = pick_deterministic_fallback_item(
            hero_name, role, owned_items, enemy_heroes
        )
        sanitized.append({
            "item": fallback_name,
            "why": fallback_reason,
            "cost": 0,
            "priority": "HIGH",
        })

    return sanitized


_APPROX_ITEM_COSTS: dict[str, int] = {
    "black_king_bar": 4050,
    "blink": 2250,
    "overwhelming_blink": 6800,
    "swift_blink": 6800,
    "arcane_blink": 6800,
    "bfury": 4100,
    "desolator": 3500,
    "manta_style": 4600,
    "manta": 4600,
    "orchid": 3275,
    "bloodthorn": 6625,
    "sheepstick": 5200,
    "aghanims_shard": 1400,
    "aghs_shard": 1400,
    "ultimate_scepter": 4200,
    "satanic": 5050,
    "skadi": 5300,
    "butterfly": 5450,
    "greater_crit": 5150,
    "silver_edge": 5450,
    "radiance": 4700,
    "shivas_guard": 5175,
    "assault": 5125,
    "heart": 5100,
    "monkey_king_bar": 4700,
    "sphere": 4800,
    "ethereal_blade": 5200,
    "octarine_core": 4800,
    "guardian_greaves": 5250,
    "pipe_of_insight": 3725,
    "crimson_guard": 3600,
    "lotus_orb": 3850,
    "hurricane_pike": 4450,
    "boots_of_bearing": 4225,
    "mjollnir": 5500,
    "gleipnir": 5650,
    "diffusal_blade": 2500,
    "disperser": 6100,
    "nullifier": 4375,
    "khanda": 5000,
    "parasma": 5575,
    "cyclone": 2625,
    "force_staff": 2200,
    "glimmer_cape": 2150,
    "aether_lens": 2275,
    "spirit_vessel": 2780,
    "solar_crest": 2600,
    "sange_and_yasha": 4100,
    "kaya_and_yasha": 4100,
    "sange_and_kaya": 4100,
    "blade_mail": 2100,
    "armlet": 2500,
    "dragon_lance": 1900,
    "kaya": 2100,
    "yasha": 2100,
    "sange": 2100,
    "magic_wand": 450,
}

_CONSUMABLE_OR_UTILITY_SLUGS: frozenset[str] = frozenset({
    "tpscroll",
    "dust",
    "ward_observer",
    "ward_sentry",
    "smoke_of_deceit",
    "magic_stick",
    "magic_wand",
    "infused_raindrop",
    "tango",
    "flask",
    "clarity",
    "enchanted_mango",
    "faerie_fire",
    "bottle",
})


def select_prevalidated_item_candidates(
    payload_or_owned: dict[str, Any] | set[str],
    *,
    hero_name: str = "",
    role: int = 1,
    enemy_heroes: list[str] | None = None,
    item_costs: dict[str, int] | None = None,
    gold: int | None = None,
    limit: int = 3,
) -> list[tuple[str, str, int]]:
    """Детерминированно отбирает 2–3 допустимых кандидата ДО генерации LLM.

    Возвращает [(display_name, reason, gold_needed)]. Все кандидаты уже проверены
    на отсутствие в инвентаре/стэше/поглощённых баффах и реестре удалённых предметов.
    """
    if isinstance(payload_or_owned, dict):
        owned_items = extract_player_owned_items(payload_or_owned)
        if gold is None:
            pl = payload_or_owned.get("player")
            raw_g = pl.get("gold") if isinstance(pl, dict) else 0
            try:
                gold = int(raw_g or 0)
            except (TypeError, ValueError):
                gold = 0
    else:
        owned_items = set(payload_or_owned)
        gold = 0 if gold is None else int(gold)

    eff_limit = max(1, min(5, int(limit)))
    raw_candidates: list[tuple[str, str]] = []
    if hero_name:
        try:
            build = get_hero_build(hero_name, role=role, enemy_heroes=enemy_heroes)
            for slot in (build.get("core") or ()):
                if isinstance(slot, dict) and slot.get("item"):
                    timing = str(slot.get("timing") or "мидгейм")
                    why = str(slot.get("why") or f"кор-слот к {timing}")
                    raw_candidates.append((str(slot["item"]), why))
            for slot in (build.get("situational") or ()):
                if isinstance(slot, dict) and slot.get("item"):
                    why = str(slot.get("why") or "ситуативный слот под драфт")
                    raw_candidates.append((str(slot["item"]), why))
        except Exception:  # noqa: BLE001
            pass

    for item_nm, reason in _ROLE_FALLBACK_ITEMS.get(role, _ROLE_FALLBACK_ITEMS[1]):
        raw_candidates.append((item_nm, reason))
    for universal in ("Black King Bar", "Blink Dagger", "Force Staff", "Glimmer Cape"):
        raw_candidates.append((universal, "ключевой сейв/инициация под драки"))

    out: list[tuple[str, str, int]] = []
    seen_slugs: set[str] = set()
    for raw_name, reason in raw_candidates:
        valid, resolved_name, _ = validate_item_candidate(raw_name, owned_items)
        if not valid:
            continue
        slug, _, _ = resolve_item_slug_and_name(resolved_name)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        cost = 0
        if item_costs:
            cost = int(item_costs.get(resolved_name.lower()) or item_costs.get(slug) or 0)
        if cost <= 0:
            cost = int(_APPROX_ITEM_COSTS.get(slug, 0))
        gold_needed = max(0, cost - gold) if cost > 0 else 0
        out.append((resolved_name, reason[:95], gold_needed))
        if len(out) >= eff_limit:
            break

    if not out:
        fb_name, fb_why = pick_deterministic_fallback_item(hero_name, role, owned_items, enemy_heroes)
        out.append((fb_name, fb_why[:95], 0))
    return out


def format_allowed_item_candidates(candidates: list[tuple[str, str, int]]) -> list[str]:
    """Форматирует предвалидированных кандидатов для вставки в Segment 2."""
    lines: list[str] = []
    for name, reason, gold_needed in candidates[:3]:
        if gold_needed > 0:
            lines.append(f"{name} (нужно ~{gold_needed} золота): {reason}")
        else:
            lines.append(f"{name}: {reason}")
    return lines


def sanitize_voice_or_chat_advice_text(
    text: str,
    owned_items: set[str],
    hero_name: str = "",
    role: int = 1,
    enemy_heroes: list[str] | None = None,
    *,
    reject_incoherent: bool = False,
    proactive: bool = False,
) -> str:
    """Проверяет короткие голосовые фразы / ответы LLM на предмет галлюцинаций.

    При ``reject_incoherent=False`` сохраняет обратную совместимость с legacy-форматом
    (пометка ``(Поправка: ...)``). При ``reject_incoherent=True`` никогда не склеивает
    противоречивый текст: в проактивном режиме отбрасывает (``""``), иначе чисто
    заменяет на детерминированный валидный слот.
    """
    if not text:
        return text

    invalid_found: list[tuple[str, str]] = []
    text_lower = text.lower()

    # Проверяем намерение рекомендации покупки
    has_buy_intent = any(w in text_lower for w in ("купи", "собер", "бери", "возьми", "делай", "нужен", "покупай", "добира"))
    if not has_buy_intent:
        return text

    # Проверяем, упоминается ли какой-либо удалённый предмет
    for rem_key in REMOVED_ITEMS_REPLACEMENTS:
        rem_display = rem_key.replace("_", " ")
        if rem_key in text_lower or rem_display in text_lower:
            _, repl_name, _ = resolve_item_slug_and_name(rem_key)
            invalid_found.append((repl_name, f"предмет {rem_display} удалён из игры"))

    # Проверяем, упоминается ли какой-либо предмет из SLANG_TO_CANONICAL, который уже куплен игроком
    seen_invalid_slugs: set[str] = set()
    for alias, (slug, dname) in SLANG_TO_CANONICAL.items():
        if len(alias) >= 2 and slug not in seen_invalid_slugs:
            if re.search(r"\b" + re.escape(alias) + r"\b", text_lower):
                if is_item_owned_or_covered(slug, owned_items):
                    seen_invalid_slugs.add(slug)
                    invalid_found.append((dname, "уже куплен"))

    if not invalid_found:
        return text

    if reject_incoherent:
        if proactive:
            return ""
        fb_name, fb_why = pick_deterministic_fallback_item(hero_name, role, owned_items, enemy_heroes)
        return f"Следующим нужным слотом собирай {fb_name} — {fb_why}"

    # Legacy-режим для совместимости со старыми контрактами тестов
    fb_name, fb_why = pick_deterministic_fallback_item(hero_name, role, owned_items, enemy_heroes)
    corrections = [f"{name} ({reason})" for name, reason in invalid_found[:2]]
    clarification = f" (Поправка: {', '.join(corrections)}. Следующий нужный слот: {fb_name} — {fb_why})"
    return f"{text}{clarification}"


def validate_llm_item_reply(
    text: str,
    owned_items: set[str],
    *,
    allowed_candidates: list[str] | None = None,
    strict_allowlist: bool = False,
    hero_name: str = "",
    role: int = 1,
    enemy_heroes: list[str] | None = None,
    proactive: bool = False,
) -> tuple[str, bool]:
    """Жёсткий пост-валидатор: не склеивает "(Поправка: ...)", проверяет уже купленные/удалённые слоты."""
    if not text or not text.strip():
        return "", False

    cleaned = sanitize_voice_or_chat_advice_text(
        text,
        owned_items,
        hero_name=hero_name,
        role=role,
        enemy_heroes=enemy_heroes,
        reject_incoherent=True,
        proactive=proactive,
    )
    if not cleaned:
        return "", False
    if cleaned != text:
        return cleaned, False

    if strict_allowlist and allowed_candidates:
        text_lower = cleaned.lower()
        has_buy_intent = any(w in text_lower for w in ("купи", "собер", "бери", "возьми", "добира", "покупай"))
        if has_buy_intent:
            allowed_slugs: set[str] = set()
            for cand in allowed_candidates:
                c_slug, _, _ = resolve_item_slug_and_name(cand)
                if c_slug:
                    allowed_slugs.add(c_slug)
            mentioned_major_slugs: list[str] = []
            for alias, (slug, _) in SLANG_TO_CANONICAL.items():
                if len(alias) >= 2 and slug not in _CONSUMABLE_OR_UTILITY_SLUGS:
                    if re.search(r"\b" + re.escape(alias) + r"\b", text_lower):
                        mentioned_major_slugs.append(slug)
            if mentioned_major_slugs and not any(s in allowed_slugs for s in mentioned_major_slugs):
                if proactive:
                    return "", False
                fb_name, fb_why = pick_deterministic_fallback_item(hero_name, role, owned_items, enemy_heroes)
                return f"Под текущий драфт лучше взять {fb_name} — {fb_why}", False

    return cleaned, True


# ---------------------------------------------------------------------------
# Анализ драфта и пробелов команды (8 атрибутов + Pos 1-5)
# ---------------------------------------------------------------------------

DRAFT_ATTRIBUTES: dict[str, dict[str, Any]] = {
    "control": {
        "title": "Контроль / станы",
        "tags": frozenset({"single_target_cc", "magic_burst_cc", "silence"}),
        "desc": "Надёжная фиксация целей для инициации и прерывания телепортов.",
        "counter_items": ("Eul's Scepter of Divinity", "Scythe of Vyse", "Abyssal Blade"),
    },
    "push": {
        "title": "Осадный потенциал / пуш",
        "tags": frozenset({"split_push", "summons"}),
        "desc": "Быстрое взятие вышек и реализация выбитых фрагов.",
        "counter_items": ("Meteor Hammer", "Desolator", "Assault Cuirass"),
    },
    "frontline": {
        "title": "Фронтлайн / впитывание урона",
        "tags": frozenset({"tanky"}),
        "desc": "Живой инициатор, принимающий первый удар и открывающий вижн.",
        "counter_items": ("Crimson Guard", "Pipe of Insight", "Heart of Tarrasque"),
    },
    "save": {
        "title": "Сейв союзников",
        "tags": frozenset({"saves_buffs", "heal_regen"}),
        "desc": "Спасение ключевых коров от фокуса и прокаста.",
        "counter_items": ("Force Staff", "Glimmer Cape", "Lotus Orb"),
    },
    "dispel": {
        "title": "Развеивание дебаффов",
        "tags": frozenset({"dispel", "saves_buffs"}),
        "desc": "Сброс сайленсов, рутов, замедлений и дебаффов врага.",
        "counter_items": ("Eul's Scepter of Divinity", "Lotus Orb", "Manta Style", "Guardian Greaves"),
    },
    "antiheal": {
        "title": "Снижение лечения / регенерации",
        "tags": frozenset({"antiheal"}),
        "desc": "Контр-пик/предмет против Alchemist, Morphling, Necrophos, Dazzle, Huskar.",
        "counter_items": ("Spirit Vessel", "Eye of Skadi", "Shiva's Guard"),
    },
    "break": {
        "title": "Истощение пассивок (Break)",
        "tags": frozenset({"break"}),
        "desc": "Отключение критических пассивок Bristleback, Spectre, PA, Viper, Huskar.",
        "counter_items": ("Silver Edge",),
    },
    "true_strike": {
        "title": "Точный удар против уклонения (True Strike)",
        "tags": frozenset({"true_strike"}),
        "desc": "Попадание по Phantom Assassin, Windranger, бабочке и Radiance.",
        "counter_items": ("Monkey King Bar", "Bloodthorn"),
    },
}


def analyze_team_draft_gaps(
    my_picks: list[str],
    enemy_picks: list[str],
    player_role: int = 1,
) -> dict[str, Any]:
    """Анализирует пробелы драфта команды по 8 критериям и роли игрока."""
    ally_tags: set[str] = set()
    for ally in my_picks:
        h_id = _resolve_hero_id(ally)
        if h_id and h_id in META_HEROES_DB:
            ally_tags.update(META_HEROES_DB[h_id].get("tags") or ())

    enemy_tags: set[str] = set()
    enemy_names: list[str] = []
    for enemy in enemy_picks:
        h_id = _resolve_hero_id(enemy)
        if h_id and h_id in META_HEROES_DB:
            enemy_tags.update(META_HEROES_DB[h_id].get("tags") or ())
            enemy_names.append(str(META_HEROES_DB[h_id].get("name") or enemy))

    missing_gaps: list[dict[str, Any]] = []

    for attr_key, attr_data in DRAFT_ATTRIBUTES.items():
        # Проверяем, покрыт ли атрибут союзниками
        has_attr = bool(ally_tags.intersection(attr_data["tags"]))
        
        # Специфические триггеры от вражеского драфта:
        urgent = False
        why_urgent = ""
        if attr_key == "break":
            passives = {"bristleback", "spectre", "phantom_assassin", "huskar", "viper", "tidehunter", "dragon_knight"}
            if any(e.lower().replace(" ", "_") in passives for e in enemy_picks):
                urgent = True
                why_urgent = "У врагов сильные пассивки — жизненно необходим Break (Silver Edge)."
        elif attr_key == "true_strike":
            evasion = {"phantom_assassin", "windranger", "brewmaster"}
            if any(e.lower().replace(" ", "_") in evasion for e in enemy_picks):
                urgent = True
                why_urgent = "У врагов встроенное уклонение — потребуется True Strike (MKB / Bloodthorn)."
        elif attr_key == "antiheal":
            healers = {"alchemist", "morphling", "necrophos", "dazzle", "huskar", "slark", "abaddon"}
            if any(e.lower().replace(" ", "_") in healers for e in enemy_picks):
                urgent = True
                why_urgent = "У врагов колоссальное лечение/реген — необходим Spirit Vessel, Skadi или Shiva."

        if not has_attr or urgent:
            missing_gaps.append({
                "key": attr_key,
                "title": attr_data["title"],
                "urgent": urgent,
                "reason": why_urgent or attr_data["desc"],
                "recommended_items": list(attr_data["counter_items"]),
            })

    return {
        "missing_gaps": missing_gaps,
        "player_role": player_role,
        "ally_count": len(my_picks),
        "enemy_count": len(enemy_picks),
    }
