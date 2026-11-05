"""Персональный тренер: промпты для LLM, запасные ответы без сети, профиль игрока.

Поддерживает два провайдера ИИ:
- ``ollama`` — локальный сервер Ollama (без GPU-нагрузки от облака);
- ``openai_compatible`` — любой OpenAI-совместимый API (DeepSeek, OpenRouter, Groq).

Всё, что делает тренер, учитывает три вещи: позицию (1–5), тайминг/режим
(All Pick vs Turbo) и вражеский драфт.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import beginner
from .coach_brain import (
    build_brief,
    detect_query_kind,
    make_system_prompt,
    render_brief,
    render_system_prompt,
    resolve_mentioned_hero,
    retrieve_knowledge,
    sanitize_answer,
)
from .coach_memory import CoachMemory
from .config import LlmConfig, OllamaConfig, PlayerProfile
from .context import (
    analyze_player_mistakes,
    build_gsi_context,
    collect_draft,
    current_hero_npc,
    format_gsi_summary,
    get_game_mode,
    get_live_gsi_snapshot,
    get_player_profile_summary,
    gsi_stale_hint,
    item_name,
    load_player_profile,
    update_player_profile_mistakes,
    _extract_allplayers,
)
from .dota_api import DotaApi
from .heroes import HeroCatalog, normalize
from .roles import normalize_position, position_label

log = logging.getLogger("aidota.coach")


def _supports_item_costs(func: Any, name: str = "item_costs") -> bool:
    """Появился ли у ``get_live_gsi_snapshot`` параметр цен предметов (контракт v1.2)."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


_LIVE_SNAPSHOT_HAS_ITEM_COSTS = _supports_item_costs(get_live_gsi_snapshot)

_COUNTER_HINTS: dict[str, str] = {
    "faceless_void": "Против Войда покупай Aeon Disk (поглотит урон в куполе), Ghost Scepter или Halberd (дизарм Войда снаружи), а саппортам — Force Staff и Glimmer для сейва союзников. БКБ от хроносферы не спасает!",
    "enigma": "Против Энигмы нужны дальний контроль (Silencer, Rubick, Scythe of Vyse) и Blink Dagger, чтобы сбивать Black Hole снаружи купола. БКБ от блэкхола не спасает!",
    "doom": "Против Дуума покупай Linken's Sphere (заблокирует ульт), Lotus Orb (вернёт Doom врагу) или Aeon Disk. БКБ от ультимейта Дума не защищает!",
    "legion_commander": "Против Легионки спасают Linken's Sphere, Lotus Orb, Halberd (дизармить до дуэли) и Aeon Disk. БКБ от дуэли не спасает!",
    "beastmaster": "Против Бистмастера бери Linken's Sphere, Lotus Orb или Aeon Disk. Его ультимейт Primal Roar пробивает БКБ!",
    "axe": "Против Акса держи дистанцию, собери Eul для сброса агра или выходи в Aeon Disk. Агр Акса пробивает БКБ!",
    "juggernaut": "Против Джаггернаута саппорту обязателен Ghost Scepter или Eul против Omnislash, а корам — Halberd или броня (Shiva, Cuirass).",
    "zuus": "Против Зевса купи Raindrops на линию, а дальше собирай Glimmer Cape, Pipe of Insight или BKB от магического урона.",
    "phantom_assassin": "Против Фантомки собирай Ghost Scepter, Solar Crest или выходи в Monkey King Bar от уклонений.",
    "bristleback": "Против Бристлбека возьми Magic Wand, Spirit Vessel и Silver Edge, чтобы отключать пассивку со спины.",
    "pudge": "Против Пуджа держи варды на хайграундах, купи Force Staff для разрыва дистанции и Linken's Sphere от Dismember.",
    "sniper": "Против Снайпера нужны Blink Dagger, Force Staff или Blade Mail, чтобы моментально сокращать дистанцию.",
    "antimage": "Против Антимага собирай Orchid Malevolence, Scythe of Vyse или Abyssal Blade для контроля сквозь щит.",
    "slark": "Против Сларка помогут Force Staff, Ghost Scepter, Crimson Guard и просветка Dust of Appearance.",
    "necrolyte": "Против Некрофоса обязательны Spirit Vessel для снижения хила, Glimmer Cape под косу и Nullifier.",
    "tinker": "Против Тинкера нужны Blink Dagger, Pipe of Insight, Lotus Orb и контроль в виде Scythe of Vyse.",
    "ursa": "Против Урсы собирай Ghost Scepter, Force Staff, Halberd или Eul, чтобы кайтить его под Enrage.",
    "viper": "Против Вайпера возьми фласки на линию, Force Staff для выхода из лужи и защитный Pipe of Insight.",
    "morphling": "Против Морфа нужны Spirit Vessel, Eye of Skadi, Shiva's Guard и взрывной сайленс или Hex.",
    "storm_spirit": "Против Шторма собирай Orchid Malevolence, Rod of Atos или Scythe of Vyse.",
    "witch_doctor": "Против Витч Доктора держи стан на его Death Ward и купи Glimmer Cape или Ghost Scepter.",
    "crystal_maiden": "Против ЦМки сбивай ультимейт станом или выходи из зоны Freezing Field через Force Staff.",
    "silencer": "Против Сайленсера собирай BKB, Manta Style, Eul's Scepter или Lotus Orb для сброса безмолвия.",
    "huskar": "Против Хускара собирай Spirit Vessel, Eye of Skadi, Shiva's Guard для среза регена, а корам — Silver Edge и Halberd.",
    "winter_wyvern": "Против Виверны покупай Aeon Disk, Linken's Sphere и держи позиционку — не стой рядом с союзниками.",
    "slardar": "Против Слардара держи дистанцию, покупай Ghost Scepter, Force Staff и броню (Shiva, Assault Cuirass). Снимай Corrosive Haze диспелом.",
    "tiny": "Против Тини держи позиционку, покупай Raindrops от прокаста, Aeon Disk и броневые ауры против его гигантского физ-урона с дерева.",
    "kunkka": "Против Кунки собирай Eul's Scepter (контрит X-Mark и комбу с Торрентом), Pipe of Insight от корабля и BKB.",
    "dragon_knight": "Против ДК бери Spirit Vessel против Dragon Blood регена, Silver Edge для отключения пассивки и контроль до включения БКБ.",
    "sven": "Против Свена бери Halberd (дизарм на 5 секунд), Ghost Scepter, Force Staff и Eul для кайта под его ультимейтом God's Strength.",
    "wraith_king": "Против ВК собирай Diffusal Blade / Mana Burn чтобы сжечь ману на Reincarnation, или Spirit Vessel против вампиризма.",
    "chaos_knight": "Против ЦК нужны АоЕ-урон и зачистка иллюзий: Shiva's Guard, Maelstrom/Mjollnir, Battle Fury или иллюзионный контрпик.",
    "lifestealer": "Против Гули держи дистанцию, покупай Force Staff, Ghost Scepter, Halberd и Eye of Skadi для среза регена с Open Wounds.",
    "alchemist": "Против Алхимика ставь варды в его стаки, прессуй в ранней игре, покупай Spirit Vessel и Eye of Skadi против химической ярости.",
    "night_stalker": "Против Баланара держи варды на высоких точках ночью, собирай Force Staff, Ghost Scepter и сейвы от его сайленса.",
    "spirit_breaker": "Против Бары носи с собой Eul's Scepter или стан для сбивания разгона (Charge), а также Linken's Sphere от Nether Strike.",
    "lycan": "Против Ликана покупай Ghost Scepter, Crimson Guard против волков, Force Staff для кайта в ультимейте и не ходи в одиночку.",
    "broodmother": "Против Бруды нужны Crimson Guard (блокирует весь урон паучков), Shiva's Guard, Dust of Appearance и АоЕ-способности.",
    "clinkz": "Против Клинкза всегда носи Dust of Appearance и Sentry, собирай Ghost Scepter и Blademail от взрывного урона со спины.",
    "weaver": "Против Вивера носи Dust of Appearance, собирай Orchid Malevolence, Rod of Atos или Scythe of Vyse для моментального контроля.",
    "riki": "Против Рики саппортам обязателен Force Staff (вытолкнуть из тучки), Dust of Appearance и Ghost Scepter от Tricks of the Trade.",
    "bounty_hunter": "Против БХ носи Dust, покупай Lotus Orb / Manta Style / Eul для сброса Track и держи саппортов под прикрытием.",
    "nyx_assassin": "Против Никса носи Sentry, держи Raindrops и Pipe of Insight от взрывного магического урона и не бей в Carapace.",
    "bloodseeker": "Против Сикера: при ультимейте Rupture просто стой на месте или используй телепорт на базу! Носи с собой свиток ТП.",
    "razor": "Против Разора покупай Force Staff, Pike или Eul, чтобы моментально разрывать дистанцию отсоса урона Static Link.",
    "venomancer": "Против Веномансера покупай Pipe of Insight от магии, Lotus Orb для снятия замедлений и BKB против яда.",
    "medusa": "Против Медузы собирай Diffusal Blade, Eye of Skadi, Shiva's Guard и отворачивайся от взгляда Stone Gaze.",
    "terrorblade": "Против ТБ собирай Linken's Sphere от Sunder, урон сквозь иллюзии (Shiva, Mjollnir) и взрывной магический урон.",
    "luna": "Против Луны покупай Blademail, Pipe of Insight от Eclipse, Ghost Scepter и держись рядом с крипами при ульте.",
    "phantom_lancer": "Против ПЛа обязательны Mjollnir, Shiva's Guard, Crimson Guard и постоянный АоЕ-урон. Одиночные таргеты против него бесполезны.",
    "meepo": "Против Мипо фокусите одного самого пробитого клона, собирайте AoE-контроль, Shiva's Guard и взрывной фокус.",
    "arc_warden": "Против Арка покупай Blink Dagger для быстрого сокращения дистанции внутрь купола Magnetic Field, и лови главную копию.",
    "ember_spirit": "Против Эмбера нужны Orchid Malevolence, Scythe of Vyse, Rod of Atos и физ-урон, пробивающий его Flame Guard.",
    "void_spirit": "Против Войд Спирита собирай сайленс (Orchid), Eul или Hex, чтобы не дать ему уйти через порталы и прыжки.",
    "earth_spirit": "Против Земели покупай BKB от бесконечного контроля и Lotus Orb для сброса Magnetize.",
    "leshrac": "Против Лешрака покупай Pipe of Insight, BKB, Mage Slayer и Spirit Vessel для снижения его вампиризма с Bloodstone.",
    "queenofpain": "Против Квопы покупай Orchid Malevolence для контроля прыжка, Raindrops на линии и Pipe of Insight от Sonic Wave.",
    "puck": "Против Пака собирай Orchid Malevolence, Scythe of Vyse или Rod of Atos. Не выходи из Coil без БКБ!",
    "windrunner": "Против ВРки собирай Blade Mail (убьёт её за секунду в Focus Fire), Ghost Scepter, Halberd и Monkey King Bar от Windrun.",
    "mirana": "Против Мираны ставь варды на реке, слушай звук стрелы и покупай Dust of Appearance против Moonlight Shadow.",
    "jakiro": "Против Джакиро покупай Force Staff для быстрого выхода из Macropyre и Ice Path, а также Pipe of Insight.",
    "disruptor": "Против Дизраптора собирай BKB (прожимать до Static Storm), Eul's Scepter для сейва союзника от Glimpse и Lotus Orb.",
    "dazzle": "Против Даззла первым убивай его в драке! Покупай Orchid / Hex, чтобы не дать нажать Shallow Grave на союзников.",
    "ancient_apparition": "Против Аппарата держи позиционку — уворачивайся от Ice Blast. Помни: хил под ультом АА заблокирован на 100%!",
    "dark_willow": "Против Виллоу покупай BKB, Blademail против Bedlam и AoE-способности, когда она сидит в Shadow Realm.",
    "hoodwink": "Против Белки топориком (Quelling Blade) срубай дерево с ее привязкой Bushwhack, покупай Blademail и Force Staff.",
    "marci": "Против Марси покупай Halberd (дизарм во время Sidekick и ульта), Ghost Scepter и кайти ее рывки.",
    "primal_beast": "Против Праймала собирай Eul's Scepter для сбивания разгона Onslaught и сбивания ультимейта Pulverize.",
    "muerta": "Против Муэрты покупай BKB (ее ультимейт Pierce the Veil наносит ТОЛЬКО магический урон) или Blade Mail.",
    "ringmaster": "Против Рингмастера не смотри лицом на его колесо Wheel of Wonder и покупай BKB от его магии и контроля.",
    "mars": "Против Марса покупай Force Staff / Hurricane Pike чтобы выпрыгивать из Арены, и не бей его спереди в Bulwark.",
    "snapfire": "Против Бабки покупай Blademail и BKB, или выходи из зоны бомбардировки Mortimer Kisses через Force Staff.",
    "timbersaw": "Против Тимбера собирай Spirit Vessel для снижения регена Reactive Armor, Silver Edge и чистый/магический урон.",
    "underlord": "Против Андерлорда собирай Force Staff для выхода из Pit of Malice и Pipe of Insight от Firestorm.",
    "ogre_magi": "Против Огра покупай Spirit Vessel против его огромного хп-пула и Lotus Orb для возвращения прокастов от мультикаста.",
}

_HERO_SKILL_TIPS: dict[str, str] = {
    "oracle": "На линии для Oracle макси третью способность Purifying Flames для урона и хила, а на 1-м уровне возьми Fortune's End для диспела.",
    "juggernaut": "Для Juggernaut макси Blade Fury для урона и сейва, на единицу возьми Healing Ward, а следом докачивай Blade Dance.",
    "pudge": "Для Pudge макси Rot для замедления и фарма, на 1-м уровне бери Meat Hook, а пассивку качай на один раз.",
    "antimage": "Для Anti-Mage качай Blink и пассивку на магию, а к 7–8 уровню макси Blink для быстрого фарма.",
    "crystal_maiden": "Для Crystal Maiden макси Crystal Nova для харасса на линии, на 1-м уровне бери Frostbite, а ауру качай на два раза.",
    "zuus": "Для Zeus макси Arc Lightning для фарма и харасса, а Lightning Bolt прокачивай следом.",
    "faceless_void": "Для Faceless Void макси Time Walk для выживаемости на линии, на 1-м уровне бери прыжок, а Time Lock докачивай следом.",
    "invoker": "Для Invoker на линии бери Exort и Quas, макси Exort для максимального урона от Sun Strike и фарма.",
    "phantom_assassin": "Для Phantom Assassin макси Stifling Dagger для фарма и харасса на линии, а прыжок качай следом.",
    "shadow_fiend": "Для Shadow Fiend макси Shadowraze для прокаста и зачистки пачек крипов, а пассивку на души бери на 2-м уровне.",
    "sniper": "Для Sniper макси Shrapnel для контроля линии и фарма, а Headshot и Take Aim прокачивай следом.",
    "axe": "Для Axe макси Counter Helix для крутилки в пачках крипов, а Battle Hunger и Berserker's Call докачивай на линии.",
    "bristleback": "Для Bristleback макси Quill Spray для стаков колючек, на 1-м бери иголки, а пассивку со спины держи на 2-м уровне.",
    "slark": "Для Slark макси Dark Pact для диспела и фарма, на 1-м уровне бери Pounce для эскейпа, а пассивку бери на 2-м.",
    "ursa": "Для Ursa макси Earthshock для замедления или Overpower для урона, на 1-м уровне бери Fury Swipes.",
    "witch_doctor": "Для Witch Doctor макси Paralyzing Cask или Maledict для киллов на линии, а на 1-м бери банку (Cask).",
    "chen": "На линии для Chen на 1-м уровне качай Holy Persuasion, приручи лесного крипа с аурой или станом и прессуй линию вместе с керри.",
    "drow_ranger": "Для Drow Ranger макси Multishot для урона и зачистки, а Frost Arrows прокачивай для замедления и фарма.",
    "shadow_shaman": "Для Shadow Shaman макси Ether Shock для харасса и пуша, а на 1-м бери Hex или Shackles для сейва и фрагов.",
    "lion": "Для Lion макси Earth Spike для стана и Mana Drain для бесконечной маны, на 1-м уровне бери стан.",
    "lina": "Для Lina макси Dragon Slave и пассивку Fiery Soul для скорости атаки, на 1-м уровне бери первую способность.",
    "earthshaker": "Для Earthshaker макси Fissure для контроля и блокировки проходов, на 1-м уровне бери Fissure.",
    "tidehunter": "Для Tidehunter макси Anchor Smash и Kraken Shell для непробиваемой брони, на 1-м бери якорь.",
    "rubick": "Для Rubick макси Fade Bolt для снижения атаки врагов и харасса, на 1-м уровне бери Fade Bolt или Telekinesis.",
    "slardar": "Для Slardar макси Bash of the Deep и Slithereen Crush для постоянного стана, на 1-м бери баш.",
    "tiny": "Для Tiny макси Avalanche и Toss для сокрушительного прокаста, на 1-м бери бросок или лавину.",
    "kunkka": "Для Kunkka макси Tidebringer для зонирования врагов на линии, на 1-м уровне бери Torrent или удар сплешем.",
    "dragon_knight": "Для Dragon Knight макси Breathe Fire для снижения атаки врагов и фарми Dragon Blood для пассивной брони.",
    "sven": "Для Sven макси Great Cleave для быстрого фарма стаков, на 1-м бери Storm Hammer для сейва или первого фрага.",
    "wraith_king": "Для Wraith King макси Mortal Strike для критов и вампиризм, на 1-м бери стан Wraithfire Blast.",
    "chaos_knight": "Для Chaos Knight макси Chaos Bolt и Chaos Strike для критов и отжора, на 1-м бери стан.",
    "lifestealer": "Для Lifestealer макси Feast для урона от здоровья врагов, на 1-м бери Rage для бесплатного БКБ.",
    "alchemist": "Для Alchemist макси Greevil's Greed для удвоения золота и Acid Spray для зачистки стаков.",
    "night_stalker": "Для Night Stalker макси Void для урона и замедления, бери на единицу Hunter in the Night к первой ночи.",
    "spirit_breaker": "Для Spirit Breaker макси Greater Bash для урона с разгона, на 1-м бери Charge of Darkness.",
    "lycan": "Для Lycan макси Summon Wolves и Feral Impulse для урона волков и регена, на 1-м бери волков.",
    "broodmother": "Для Broodmother макси Spawn Spiderlings для зачистки лагерей и пуша, на 1-м бери паутину Spin Web.",
    "clinkz": "Для Clinkz макси Tar Bomb для огромного физ-урона на линии и Strafe для скорости атаки.",
    "weaver": "Для Weaver макси Shukuchi для мобильности и урона сквозь врагов, на 1-м бери Shukuchi.",
    "riki": "Для Riki макси Cloak and Dagger и Blink Strike для ударов со спины, на 1-м бери блинк.",
    "bounty_hunter": "Для Bounty Hunter макси Jinada для воровства золота с каждой тычки, на 1-м бери Jinada.",
    "nyx_assassin": "Для Nyx макси Impale и Mind Flare для взрывного урона по магам, на 1-м бери стан Impale.",
    "bloodseeker": "Для Bloodseeker макси Blood Rite для контроля крипов и Thirst для скорости и вижена раненых.",
    "razor": "Для Razor макси Static Link для кражи урона у вражеского керри, на 1-м бери Static Link или Plasma Field.",
    "venomancer": "Для Venomancer макси Poison Sting и Plague Ward для контроля всей территории линии.",
    "medusa": "Для Medusa макси Mystic Snake для отсоса маны и харасса, а Mana Shield держи включенным.",
    "terrorblade": "Для Terrorblade макси Metamorphosis и Conjure Image для мощного пуша и фарма иллюзиями.",
    "luna": "Для Luna макси Lucent Beam и Moon Blessings для дополнительного урона с руки и подготовки к Eclipse.",
    "phantom_lancer": "Для Phantom Lancer макси Spirit Lance для харасса и замедления, на 1-м бери копье.",
    "meepo": "Для Meepo макси Poof для моментальной зачистки пачек и лесных лагерей, на 1-м бери сетку Earthbind.",
    "arc_warden": "Для Arc Warden макси Spark Wraith и Flux для одиночного убийства отбившихся врагов.",
    "storm_spirit": "Для Storm Spirit макси Static Remnant и Overload для фарма и взрывного харасса, на 1-м бери ремнант.",
    "ember_spirit": "Для Ember Spirit макси Flame Guard и Sleight of Fist для урона и щита от магии.",
    "void_spirit": "Для Void Spirit макси Resonant Pulse для щита и Aether Remnant для контроля.",
    "earth_spirit": "Для Earth Spirit макси Rolling Boulder для быстрого отката камня и Smash для стана.",
    "leshrac": "Для Leshrac макси Lightning Storm для харасса на линии и Split Earth для стана.",
    "queenofpain": "Для Queen of Pain макси Shadow Strike для замедления и тикающего урона, на 1-м бери кинжал.",
    "puck": "Для Puck макси Illusory Orb для урона и Waning Rift для сайленса, на 1-м бери Orb.",
    "windrunner": "Для Windranger макси Powershot для добивания крипов и врагов на дистанции, на 1-м бери Powershot.",
    "mirana": "Для Mirana макси Starstorm для зачистки крипов на линии, на 1-м бери Sacred Arrow.",
    "jakiro": "Для Jakiro макси Dual Breath и Liquid Fire для бесплатного харасса вышки и врагов без траты маны.",
    "disruptor": "Для Disruptor макси Thunder Strike для вижена и урона, на 1-м бери первую способность.",
    "dazzle": "Для Dazzle макси Poison Touch для замедления и Shadow Wave для хила в пачке крипов.",
    "ancient_apparition": "Для Ancient Apparition макси Chilling Touch для огромного урона с автоатаки на линии.",
    "dark_willow": "Для Dark Willow макси Bramble Maze и Shadow Realm для дальнего сейва и урона.",
    "hoodwink": "Для Hoodwink макси Acorn Shot и Bushwhack для быстрой связки с деревом.",
    "marci": "Для Marci макси Rebound для прыжков и Dispose для переворачивания врагов под союзника.",
    "primal_beast": "Для Primal Beast макси Trample для сокрушительного урона при беге, на 1-м бери Onslaught.",
    "muerta": "Для Muerta макси Dead Shot для рикошета и The Calling для контроля и сайленса.",
    "ringmaster": "Для Ringmaster макси Tame the Beasts для урона кнутом и Escape Act для сейва союзников.",
    "mars": "Для Mars макси Spear of Mars для стана об деревья и God's Rebuke для крита со щита.",
    "snapfire": "Для Snapfire макси Scatterblast для урона в упор и Firesnap Cookie для прыжка со станом.",
    "timbersaw": "Для Timbersaw макси Whirling Death для срезки атрибутов врагов и Reactive Armor для регена.",
    "underlord": "Для Underlord макси Firestorm для процента урона от макс хп и Atrophy Aura для урона с руки.",
    "ogre_magi": "Для Ogre Magi макси Ignite для замедления и поджога, на 1-м бери Fireblast со станом.",
}

# Промпт личности тренера живёт в coach_brain (там же правила и защита от
# галлюцинаций). Здесь оставляем публичное имя для обратной совместимости.
SYSTEM_PROMPT = make_system_prompt("Астра")


COACH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_live_gsi_state",
            "description": "Получить детальный снимок текущего состояния игры из Dota 2 GSI (минута, счет, KDA, нетворс, предметы, уровень).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_opendota_meta",
            "description": "Получить статистику OpenDota по герою: винрейт, популярные предметы по стадиям и роли.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hero_name": {"type": "string", "description": "Имя героя (на русском или английском)"},
                },
                "required": ["hero_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hero_counters",
            "description": "Получить лучшие контр-предметы и контр-пики против конкретного вражеского героя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hero_name": {"type": "string", "description": "Имя вражеского героя"},
                },
                "required": ["hero_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_last_match",
            "description": "Запустить детальный разбор последнего завершённого матча игрока (статистика, ошибки, рекомендации).",
            "parameters": {
                "type": "object",
                "properties": {
                    "match_id": {"type": "string", "description": "Опциональный числовой ID матча"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_game_timer",
            "description": "Установить пользовательский голосовой таймер на N секунд со звуковым напоминанием.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "Количество секунд от текущего момента"},
                    "label": {"type": "string", "description": "Название напоминания (например, 'проверить Рошана', 'забрать руну мудрости')"},
                },
                "required": ["seconds", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_draft_picks",
            "description": "Подобрать лучшие контрпики и синергичные варианты на указанную позицию (1-5) против врагов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "enemy_heroes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список вражеских героев (на русском или английском)",
                    },
                    "role": {
                        "type": "integer",
                        "description": "Позиция игрока: 1 (керри), 2 (мид), 3 (оффлейн), 4 (софт-саппорт), 5 (хард-саппорт)",
                    },
                },
                "required": ["role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hero_item_build",
            "description": "Получить полный гайд по закупке на героя (старт, лайнинг, кор-билд с таймингами, ситуативные контр-артефакты, шард) для конкретной роли.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hero_name": {"type": "string", "description": "Имя героя"},
                    "role": {"type": "integer", "description": "Позиция (1-5)"},
                },
                "required": ["hero_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hero_skill_order",
            "description": "Получить оптимальный порядок прокачки способностей героя: что максить и что брать на 1-м уровне.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hero_name": {"type": "string", "description": "Имя героя"},
                },
                "required": ["hero_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_coach_knowledge",
            "description": "Найти в базе знаний тренера факты по вопросу игрока: билды, контр-предметы, прокачка, лейн, драфт.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Вопрос или ключевые слова игрока"},
                    "hero_name": {"type": "string", "description": "Герой игрока (необязательно)"},
                },
                "required": ["query"],
            },
        },
    },
]



class OllamaError(RuntimeError):
    """LLM недоступна или модель не готова. Текст исключения — для пользователя."""


# Подсказки по предметам против конкретных вражеских героев (для fallback без LLM).
_ENEMY_ITEM_HINTS: tuple[tuple[str, str], ...] = (
    ("faceless_void", "Aeon Disk / Ghost Scepter / Halberd / Force Staff — сейв от хроносферы (БКБ не спасает!)"),
    ("enigma", "Aeon Disk / Scythe of Vyse / дальний контроль — сбивать блэкхол снаружи (БКБ не спасает!)"),
    ("legion_commander", "Linken's Sphere / Lotus Orb / Heaven's Halberd / Aeon Disk — от Duel (BKB не спасает)"),
    ("doom", "Linken's Sphere / Lotus Orb / Aeon Disk — от Doom (BKB не спасает)"),
    ("beastmaster", "Linken's Sphere / Lotus Orb / Aeon Disk — от Primal Roar (BKB не спасает)"),
    ("magnus", "Aeon Disk / Scythe of Vyse / правильная позиция — от RP (БКБ не спасает!)"),
    ("axe", "Force Staff / Eul's Scepter / Aeon Disk — от Berserker's Call (пробивает BKB)"),
    ("silencer", "BKB / Manta / Eul / Lotus — сброс сайленсов"),
    ("huskar", "Spirit Vessel / Skadi / Shiva / Silver Edge — срез регена и отключение пассивки"),
    ("necrophos", "Spirit Vessel / Skadi / Glimmer Cape / Pipe — срез хила и сейв от косы"),
    ("morphling", "Spirit Vessel / Skadi / Shiva / Orchid — срез перекачки и сайленс"),
    ("alchemist", "Spirit Vessel / Skadi / Shiva — срез регена под ультом"),
    ("timbersaw", "Spirit Vessel / Shiva / Silver Edge — срез регена и брейк реактора"),
    ("phantom_assassin", "MKB / Ghost Scepter / Solar Crest — пробивать уклонение и сейв"),
    ("brewmaster", "MKB / Eul — против промахов и кайтинг"),
    ("warlock", "Force Staff / Glimmer / BKB — выход из замедления и сейв"),
    ("bane", "Linken's Sphere / Lotus Orb / Aeon Disk — от Fiend's Grip (BKB не спасает)"),
    ("pudge", "Linken's Sphere / Lotus Orb / Force Staff / Pipe — от хука и Dismember"),
    ("winter_wyvern", "Aeon Disk / Linken's Sphere — не кучковаться с союзниками"),
    ("juggernaut", "Ghost Scepter / Eul's Scepter / Aeon Disk — уйти от Omnislash; BKB не спасает от физического Omnislash; Linken's его НЕ блокирует"),
    ("bristleback", "Silver Edge / Spirit Vessel / Skadi — отключение пассивки со спины"),
)


def _g(data: Any, *keys: str, default: Any = None) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


class Coach:
    def __init__(
        self,
        api: DotaApi,
        catalog: HeroCatalog,
        gsi: Any | None,
        llm: LlmConfig | OllamaConfig,
        player_profile: PlayerProfile | None = None,
        *,
        coach_name: str = "Астра",
        memory_size: int = 20,
        session: Any | None = None,
        memory_path: Path | str | None = None,
    ):
        self.api = api
        self.catalog = catalog
        self.gsi = gsi
        self.llm = self._coerce_llm(llm)
        self.profile = player_profile if player_profile is not None else PlayerProfile()
        self.coach_name = coach_name
        self._memory: deque[dict[str, str]] = deque(maxlen=max(1, memory_size))
        self._lock = threading.RLock()
        self._models_cache: tuple[float, list[str], str | None] = (0.0, [], None)
        self.last_error: str | None = None
        self._position: int | None = None  # переопределение на время матча
        self._manual_hero: dict | None = None  # герой, выбранный вручную или из чата

        if memory_path is None:
            data_dir = getattr(api, "data_dir", None)
            memory_path = Path(data_dir) / "coach_memory.json" if data_dir is not None else None
        self.long_memory = CoachMemory(memory_path)

        if session is None:
            import requests

            session = requests.Session()
        self._session = session

        from .skills import CooldownTrackerSkill, LaneMatchupSkill, MacroTimerSkill

        self.macro_timer = MacroTimerSkill()
        self.cooldown_tracker = CooldownTrackerSkill()
        self.lane_matchup = LaneMatchupSkill()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _coerce_llm(config: Any) -> LlmConfig:
        """Принимает LlmConfig или старый OllamaConfig (обратная совместимость)."""
        if isinstance(config, LlmConfig) or hasattr(config, "provider"):
            return config
        return LlmConfig(
            provider="ollama",
            base_url=getattr(config, "host", "http://127.0.0.1:11434"),
            model=getattr(config, "model", "gemma3"),
            temperature=getattr(config, "temperature", 0.4),
            timeout=getattr(config, "timeout", 120.0),
        )

    # ------------------------------------------------------------------ #
    # Тактический мозг: бриф, знания, память, тонкая настройка LLM
    # ------------------------------------------------------------------ #
    _FACTUAL_KINDS = frozenset(
        {"build", "counter", "skill", "pick", "lane", "timing", "analysis", "why", "match", "draft", "explain"}
    )

    def enemy_picks(self) -> list[str]:
        """Пики врага из текущего драфта GSI (без падений при отсутствии данных)."""
        try:
            return list(collect_draft(self.gsi_payload(), self.catalog).enemy_picks)
        except Exception:  # noqa: BLE001 — брифинг не должен ронять ответ
            return []

    def _profile_summary(self) -> str:
        """Компактная сводка профиля игрока и долговременной памяти."""
        parts = [f"Основная роль: Pos {self.position}"]
        mmr = str(self.profile.mmr_bracket or "").strip()
        if mmr:
            parts.append(f"ранг: {mmr}")
        pool_parts: list[str] = []
        hero_pool = self.profile.hero_pool or {}
        for pos in sorted(hero_pool, key=lambda value: str(value)):
            names = [str(n).strip() for n in (hero_pool.get(pos) or []) if str(n).strip()]
            if names:
                pool_parts.append(f"поз.{pos}: {', '.join(names[:5])}")
        if pool_parts:
            parts.append("пул героев — " + "; ".join(pool_parts[:5]))
        signatures = [str(h) for h in (self.profile.signature_heroes or []) if str(h).strip()]
        if signatures:
            parts.append("сигнатурные герои: " + ", ".join(signatures[:5]))
        mistakes = [str(m) for m in (self.profile.common_mistakes or []) if str(m).strip()]
        if mistakes:
            parts.append("частые ошибки: " + "; ".join(mistakes[:3]))
        memory_summary = self.long_memory.summary(lessons_limit=3, turns_limit=2)
        if memory_summary:
            parts.append(memory_summary)
        return "Профиль игрока: " + ". ".join(parts) + "."

    def _temperature_for(self, kind: str = "chat") -> float:
        """Фактические вопросы — холоднее (меньше выдумок), диалог — живее."""
        base = float(getattr(self.llm, "temperature", 0.4) or 0.4)
        if kind in self._FACTUAL_KINDS:
            return max(0.15, min(0.35, base))
        return max(0.35, min(0.6, base))

    def _max_tokens_for(self, kind: str = "chat") -> int:
        configured = int(getattr(self.llm, "max_tokens", 350) or 350)
        if kind == "match":
            return max(configured, 550)
        if kind in ("analysis", "draft"):
            return max(configured, 420)
        if kind in ("why", "build", "counter", "pick"):
            return max(90, min(configured, 260))
        # Опытным игрокам в чат-режиме можно ответить развёрнуто (до 500 символов).
        if kind in ("chat", "explain") and self.skill_level in ("advanced", "expert"):
            return max(configured, 480)
        return max(80, min(configured, 200))

    def _ask_llm(
        self,
        prompt: str,
        *,
        system: str | None = None,
        kind: str = "chat",
        max_sentences: int = 2,
        max_chars: int = 240,
        allow_retry: bool = True,
    ) -> str:
        """Запрос к LLM с «холодной» температурой и чисткой ответа для озвучки."""
        system = system or make_system_prompt(self.coach_name, skill_level=self.skill_level)
        if kind in ("analysis", "draft", "match"):
            system += (
                "\n\nДЛЯ РАЗБОРА: допустимо 3–4 коротких предложения — сначала главная проблема, "
                "затем конкретный план действий. Без markdown, списков и нумерации."
            )
        elif kind == "why":
            system += "\n\nДЛЯ ОБЪЯСНЕНИЯ: допустимо 2–3 коротких предложения, но без лекций."
        temperature = self._temperature_for(kind)
        max_tokens = self._max_tokens_for(kind)
        raw = self.chat(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            include_dynamic=False,
        )
        clean = sanitize_answer(
            raw,
            max_chars=max_chars,
            max_sentences=max_sentences,
            skill_level=self.skill_level,
            kind=kind,
        )
        if not clean and allow_retry:
            retry_system = (
                system
                + "\n\nВАЖНО: ответ должен быть одним-двумя живыми предложениями по-русски, "
                "без списков, markdown и вводных фраз."
            )
            raw = self.chat(
                prompt,
                system=retry_system,
                temperature=temperature,
                max_tokens=max_tokens,
                include_dynamic=False,
            )
            clean = sanitize_answer(
            raw,
            max_chars=max_chars,
            max_sentences=max_sentences,
            skill_level=self.skill_level,
            kind=kind,
        )
        return clean

    def _tactical_context(self, query: str, *, hero: dict | None = None) -> tuple[str, list[str], str]:
        """Собирает бриф и релевантные знания под конкретный вопрос.

        Если игрок назвал героя в вопросе («что собрать против фантомки»,
        «что качать на пудже»), знания берутся именно по этому герою, а не
        только по герою из GSI.
        Возвращает (brief_text, knowledge, kind).
        """
        kind = detect_query_kind(query)
        payload = self.gsi_payload()
        enemies = self.enemy_picks()
        brief = build_brief(
            payload,
            self.catalog,
            self.api.items(),
            position=self.position,
            profile=self.profile,
            hero=hero,
            item_costs=self.item_costs() or None,
        )

        knowledge_hero = hero
        knowledge_enemies = list(enemies)
        mentioned = resolve_mentioned_hero(query, self.catalog)
        if mentioned is not None:
            mentioned_name = str(mentioned.get("localized_name") or mentioned.get("name") or "").strip()
            current_name = str((hero or {}).get("localized_name") or "").strip()
            if mentioned_name and normalize(mentioned_name) != normalize(current_name):
                if kind in ("build", "skill", "lane", "why", "timing", "matchup"):
                    knowledge_hero = mentioned
                if kind in ("counter", "pick", "matchup") or "против" in query.lower():
                    knowledge_enemies = [mentioned_name] + [
                        enemy for enemy in knowledge_enemies
                        if normalize(enemy) != normalize(mentioned_name)
                    ]

        lane_plan = ""
        if kind == "lane":
            try:
                lane_plan = self.lane_plan()
            except Exception:  # noqa: BLE001
                lane_plan = ""
        knowledge = retrieve_knowledge(
            query,
            hero=knowledge_hero,
            position=self.position,
            enemy_heroes=knowledge_enemies,
            legacy_counter_hints=_COUNTER_HINTS,
            legacy_skill_tips=_HERO_SKILL_TIPS,
            lane_plan=lane_plan,
            catalog=self.catalog,
        )
        # Точечные подсказки там, где общие теги врут (BKB не спасает от
        # Omnislash/Duel/Doom и т.п.) — попадают в блок «ЗНАНИЯ ПО ВОПРОСУ».
        if kind in ("counter", "why", "build", "chat") and knowledge_enemies:
            precise = self._enemy_item_hints_for(knowledge_enemies, limit=2)
            if precise:
                knowledge = list(knowledge) + precise
        return render_brief(brief), knowledge, kind

    # ------------------------------------------------------------------ #
    # Позиция и профиль
    # ------------------------------------------------------------------ #
    @property
    def position(self) -> int:
        pos = self._position if self._position is not None else self.profile.default_position
        return normalize_position(pos) or 1

    def set_position(self, position: Any) -> int:
        normalized = normalize_position(position)
        if normalized is not None:
            self._position = normalized
        return self.position

    def reset_position(self) -> int:
        self._position = None
        return self.position

    # ------------------------------------------------------------------ #
    # Уровень игрока: beginner / amateur / advanced / expert
    # ------------------------------------------------------------------ #
    @property
    def skill_level(self) -> str:
        """Текущий уровень игрока из профиля (по умолчанию «новичок»)."""
        return beginner.normalize_skill_level(getattr(self.profile, "skill_level", "beginner"))

    def set_skill_level(self, level: Any) -> str:
        """Нормализует и запоминает уровень игрока в профиле. Возвращает код уровня."""
        normalized = beginner.normalize_skill_level(level)
        try:
            setattr(self.profile, "skill_level", normalized)
        except Exception as exc:  # noqa: BLE001 — профиль не должен ронять настройку
            log.debug("Не удалось сохранить skill_level в профиль: %s", exc)
        return normalized

    @property
    def level_label(self) -> str:
        """Человеческое название уровня игрока."""
        return beginner.level_label(self.skill_level)

    @property
    def provider(self) -> str:
        return (self.llm.provider or "ollama").strip().lower()

    def pool_heroes(self, position: Any = None) -> list[dict]:
        """Герои из пула игрока на указанную позицию, сопоставленные с каталогом."""
        pos = normalize_position(position) if position is not None else self.position
        names = self.profile.hero_pool.get(str(pos), [])
        heroes: list[dict] = []
        for name in names:
            match = self.catalog.find(name) or self.catalog.find_in_tokens(name)
            if match is not None:
                heroes.append(match.hero)
        return heroes

    # ------------------------------------------------------------------ #
    # GSI
    # ------------------------------------------------------------------ #
    def gsi_payload(self) -> dict | None:
        if self.gsi is None:
            return None
        try:
            return self.gsi.snapshot()
        except Exception:  # noqa: BLE001 — GSI не должен ронять ответ
            return None

    def gsi_age(self) -> float | None:
        if self.gsi is None:
            return None
        try:
            return self.gsi.age()
        except Exception:  # noqa: BLE001
            return None

    def item_costs(self) -> dict[str, int]:
        """Цены предметов из ``DotaApi.item_costs`` (пустой dict, если API их не умеет).

        Нужны, чтобы советы по закупкам показывали цену и нехватку золота,
        а нетворс при отсутствии ``player.net_worth`` считался по предметам.
        """
        getter = getattr(self.api, "item_costs", None)
        if not callable(getter):
            return {}
        try:
            costs = getter()
        except Exception:  # noqa: BLE001 — цены не должны ронять бриф
            return {}
        if not isinstance(costs, dict):
            return {}
        result: dict[str, int] = {}
        for name, value in costs.items():
            try:
                result[str(name)] = int(value)
            except (TypeError, ValueError):
                continue
        return result

    def _live_snapshot(self, payload: dict | None) -> dict[str, Any]:
        """``get_live_gsi_snapshot`` с ценами предметов (параметр появился в v1.2)."""
        kwargs: dict[str, Any] = {
            "position": self.position,
            "mmr_bracket": self.profile.mmr_bracket,
        }
        costs = self.item_costs()
        if costs and _LIVE_SNAPSHOT_HAS_ITEM_COSTS:
            kwargs["item_costs"] = costs
        try:
            snapshot = get_live_gsi_snapshot(payload, self.catalog, self.api.items(), **kwargs)
        except TypeError:
            kwargs.pop("item_costs", None)
            snapshot = get_live_gsi_snapshot(payload, self.catalog, self.api.items(), **kwargs)
        return snapshot if isinstance(snapshot, dict) else {}

    def gsi_context(self, position: Any = None) -> str:
        pos = normalize_position(position) if position is not None else self.position
        return build_gsi_context(
            self.gsi_payload(),
            self.catalog,
            self.api.items(),
            age_seconds=self.gsi_age(),
            position=pos,
        )

    def mistakes(self) -> list[str]:
        payload = self.gsi_payload()
        return analyze_player_mistakes(payload, self.position, get_game_mode(payload))

    def current_hero(self) -> dict | None:
        payload = self.gsi_payload()
        state = _g(payload, "map", "game_state")
        if state in ("DOTA_GAMERULES_STATE_INIT", "DOTA_GAMERULES_STATE_HERO_SELECTION"):
            self._manual_hero = None
        npc = current_hero_npc(payload)
        if not npc:
            return self._manual_hero
        hero = self.catalog.by_npc(npc)
        if hero:
            self._manual_hero = hero
        return hero or self._manual_hero

    def set_manual_hero(self, hero: dict | None) -> None:
        self._manual_hero = hero

    def set_coach_name(self, name: str) -> str:
        """Мгновенно переключает активного тренера (Астра / Джарвис)."""
        if name and name.strip():
            self.coach_name = name.strip()
        return self.coach_name

    def record_roshan(self) -> str:
        """Запускает отсчёт таймера Рошана."""
        payload = self.gsi_payload()
        clock = _g(payload, "map", "clock_time")
        return self.cooldown_tracker.record_roshan(clock)

    def record_ult(self, ability_key: str | None) -> str:
        """Запускает отсчёт перезарядки ультимейта врага."""
        payload = self.gsi_payload()
        clock = _g(payload, "map", "clock_time")
        return self.cooldown_tracker.record_ult(ability_key, clock)

    def lane_plan(self, enemy_hero: str | None = None) -> str:
        """Возвращает тактический план на линию."""
        payload = self.gsi_payload()
        hero_dict = self.current_hero()
        hero_name = hero_dict.get("localized_name") if hero_dict else None
        turbo = (get_game_mode(payload) == 23)
        return self.lane_matchup.get_plan(
            position=self.position,
            hero_name=hero_name,
            enemy_hero=enemy_hero,
            is_turbo=turbo,
        )

    def check_skills_alerts(self) -> str | None:
        """Проверяет макро-таймеры и кулдауны по текущему времени GSI."""
        payload = self.gsi_payload()
        if not payload:
            return None
        clock = _g(payload, "map", "clock_time")
        if clock is None:
            return None

        team_name = _dig(payload, "player", "team_name") or _dig(payload, "provider", "team_name")
        if isinstance(team_name, str):
            team_name = team_name.lower()
        else:
            team_name = None

        enemy_mid_lvl = None
        players = _extract_allplayers(payload)
        for p in players:
            p_team = p.get("team")
            if team_name and p_team and p_team != team_name:
                lvl = p.get("level")
                if lvl:
                    enemy_mid_lvl = max(enemy_mid_lvl or 0, lvl)

        alert = self.macro_timer.check(
            clock,
            position=self.position,
            team_name=team_name,
            enemy_mid_level=enemy_mid_lvl,
        )
        if alert:
            return alert
        return self.cooldown_tracker.check(clock)

    # ------------------------------------------------------------------ #
    # LLM: модели и запрос
    # ------------------------------------------------------------------ #
    def llm_models(self, *, max_age: float = 15.0) -> list[str]:
        now = time.time()
        with self._lock:
            cached_at, models, error = self._models_cache
            if now - cached_at < max_age:
                self.last_error = error
                return list(models)
        try:
            if self.provider == "openai_compatible":
                models = self._openai_models()
            else:
                models = self._ollama_models()
            error = None
        except Exception as exc:  # noqa: BLE001
            models = []
            error = f"LLM не отвечает ({self.provider}): {exc}"
            log.debug(error)
        with self._lock:
            self._models_cache = (now, models, error)
            self.last_error = error
        return models

    def _ollama_models(self) -> list[str]:
        url = f"{self.llm.base_url.rstrip('/')}/api/tags"
        response = self._session.get(url, timeout=4)
        if getattr(response, "status_code", 0) != 200:
            raise OllamaError(f"HTTP {getattr(response, 'status_code', '?')}")
        data = response.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def _openai_models(self) -> list[str]:
        url = f"{self.llm.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {self.llm.api_key}"} if self.llm.api_key else {}
        response = self._session.get(url, timeout=4, headers=headers)
        if getattr(response, "status_code", 0) != 200:
            raise OllamaError(f"HTTP {getattr(response, 'status_code', '?')}")
        data = response.json()
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def resolve_model(self) -> str | None:
        if self.provider == "openai_compatible":
            base_url_lower = (self.llm.base_url or "").lower()
            is_keyless = "pollinations" in base_url_lower
            if not is_keyless and not (self.llm.api_key or "").strip():
                self.last_error = (
                    "API-ключ не указан. Откройте Настройки (кнопка ⚙ вверху окна) и введите API-ключ."
                )
                return None
            model = (self.llm.model or "").strip()
            if "openrouter.ai" in base_url_lower:
                if not model or model.lower() in ("openai", "default", "gpt", "") or "/" not in model:
                    model = "openai/gpt-4o-mini"
                    self.llm.model = model
            elif "generativelanguage.googleapis.com" in base_url_lower:
                if not model:
                    model = "gemini-2.5-flash"
                    self.llm.model = model
            elif is_keyless and not model:
                model = "gemini" if "gemini" in base_url_lower else "openai"
                self.llm.model = model
            if not model:
                self.last_error = "Для openai_compatible укажите llm.model в config.json"
                return None
            return model
        return self._resolve_ollama_model()

    def _resolve_ollama_model(self) -> str | None:
        models = self.llm_models()
        if not models:
            self.last_error = (
                f"Ollama недоступна по адресу {self.llm.base_url}. "
                "Запустите Ollama (ollama serve) или переключитесь на облачную модель."
            )
            return None
        configured = (self.llm.model or "").strip()
        if configured:
            base = configured.split(":")[0].lower()
            for model in models:
                if model == configured or model.split(":")[0].lower() == base:
                    return model
        chosen = models[0]
        if configured:
            self.last_error = (
                f"Модель «{configured}» не установлена. Использую «{chosen}». "
                f"Установить: ollama pull {configured}"
            )
            log.warning(self.last_error)
        return chosen

    def get_dynamic_context(self) -> str:
        """Динамический контекст для системного промпта LLM.

        Включает живой GSI-снимок, тактический бриф по текущей минуте/роли,
        профиль игрока и долговременную память прошлых матчей.
        """
        parts: list[str] = []
        payload = self.gsi_payload()
        item_costs = self.item_costs()
        snapshot = self._live_snapshot(payload)
        if snapshot.get("available"):
            parts.append(f"ТЕКУЩАЯ СИТУАЦИЯ В МАТЧЕ (GSI Live):\n{format_gsi_summary(snapshot)}")
        try:
            brief = build_brief(
                payload,
                self.catalog,
                self.api.items(),
                position=self.position,
                profile=self.profile,
                hero=self.current_hero() if snapshot.get("available") else None,
                item_costs=item_costs or None,
            )
            brief_text = render_brief(brief)
            if brief_text:
                parts.append(f"ТАКТИЧЕСКИЙ БРИФ:\n{brief_text}")
        except Exception as exc:  # noqa: BLE001 — бриф не должен ронять промпт
            log.debug("Не удалось собрать тактический бриф: %s", exc)
        try:
            parts.append(f"ПРОФИЛЬ ИГРОКА:\n{self._profile_summary()}")
        except Exception:  # noqa: BLE001
            summary = get_player_profile_summary()
            if summary:
                parts.append(f"ПРОФИЛЬ ИГРОКА:\n{summary}")
        return "\n\n".join(parts)

    def _build_messages(
        self, user_prompt: str, system: str, *, include_dynamic: bool = True
    ) -> list[dict[str, Any]]:
        dyn = self.get_dynamic_context() if include_dynamic else ""
        if dyn and dyn not in system:
            full_system = f"{system}\n\n{dyn}"
        else:
            full_system = system

        messages: list[dict[str, Any]] = [{"role": "system", "content": full_system}]
        with self._lock:
            history = list(self._memory)
        for entry in history:
            q = entry.get("q", "").strip()
            a = entry.get("a", "").strip()
            if q and a:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _legacy_counter_hint(self, hero_name: str) -> str | None:
        """Старая ручная подсказка по контр-предметам (для героев с особыми фишками)."""
        name = str(hero_name or "").strip()
        if not name:
            return None
        if self.catalog.available:
            match = self.catalog.find(name) or self.catalog.find_in_tokens(name)
            if match is not None:
                npc = str(match.hero.get("name", "")).replace("npc_dota_hero_", "").lower()
                hint = _COUNTER_HINTS.get(npc)
                if hint:
                    return hint
        key = normalize(name)
        for fragment, hint in _COUNTER_HINTS.items():
            if fragment and fragment in key:
                return hint
        return None

    def execute_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        """Выполняет вызов зарегистрированного инструмента (Tool Calling)."""
        args = args or {}
        if name == "get_live_gsi_state":
            snapshot = self._live_snapshot(self.gsi_payload())
            return format_gsi_summary(snapshot)

        if name == "get_opendota_meta":
            hero_name = str(args.get("hero_name", "")).strip()
            match = self.catalog.find(hero_name) or self.catalog.find_in_tokens(hero_name) if self.catalog.available else None
            if match:
                lines, _ = self._hero_meta_lines(match.hero, include_items=True)
                return "\n".join(lines)
            return f"Герой «{hero_name}» не найден в каталоге."

        if name == "get_hero_counters":
            hero_name = str(args.get("hero_name", "")).strip()
            # 1) Полная база знаний (все герои); 2) старые подсказки; 3) каталог.
            try:
                from .meta_db import get_hero_counters as db_counters

                db = db_counters(hero_name)
            except Exception:  # noqa: BLE001 — мета-база не должна ронять инструмент
                db = None
            if isinstance(db, dict):
                items = ", ".join(str(i) for i in (db.get("counter_items") or [])[:4])
                name_ru = db.get("ru_name") or db.get("name") or hero_name
                if items:
                    parts = [f"{name_ru} — контр-предметы: {items}."]
                    tip = str(db.get("tip") or "").strip().rstrip(".")
                    if tip:
                        parts.append(tip + ".")
                    legacy = self._legacy_counter_hint(hero_name)
                    if legacy:
                        parts.append(f"Классические варианты: {legacy.rstrip('.')}.")
                    return " ".join(parts)
            match = self.catalog.find(hero_name) or self.catalog.find_in_tokens(hero_name) if self.catalog.available else None
            if match:
                npc = str(match.hero.get("name", "")).replace("npc_dota_hero_", "").lower()
                hint = _COUNTER_HINTS.get(npc)
                if hint:
                    return hint
                for frag, item_hint in _ENEMY_ITEM_HINTS:
                    if frag in npc:
                        return f"Против {match.hero.get('localized_name')}: {item_hint}"
                return f"Рекомендуются предметы на сейв и контроль против {match.hero.get('localized_name')}."
            return f"Герой «{hero_name}» не найден."

        if name == "get_hero_skill_order":
            hero_name = str(args.get("hero_name", "")).strip()
            try:
                from .meta_db import get_skill_order

                skill = get_skill_order(hero_name)
            except Exception:  # noqa: BLE001
                skill = None
            if isinstance(skill, dict):
                bits: list[str] = []
                if skill.get("priority"):
                    bits.append(f"максить: {str(skill['priority']).rstrip('.')}")
                if skill.get("level1"):
                    bits.append(f"на 1-м уровне: {str(skill['level1']).rstrip('.')}")
                if skill.get("tip"):
                    bits.append(str(skill["tip"]).rstrip("."))
                if bits:
                    return f"Прокачка {skill.get('ru_name') or hero_name}: " + "; ".join(bits) + "."
            match = self.catalog.find(hero_name) or self.catalog.find_in_tokens(hero_name) if self.catalog.available else None
            if match:
                npc = str(match.hero.get("name", "")).replace("npc_dota_hero_", "").lower()
                tip = _HERO_SKILL_TIPS.get(npc)
                if tip:
                    return tip
            return f"Нет точной прокачки для «{hero_name}»: макси основной боевой навык, сейв — в единицу."

        if name == "search_coach_knowledge":
            query = str(args.get("query", "")).strip()
            hero_name = str(args.get("hero_name", "")).strip()
            hero = None
            if hero_name and self.catalog.available:
                match = self.catalog.find(hero_name) or self.catalog.find_in_tokens(hero_name)
                hero = match.hero if match else None
            hero = hero or self.current_hero()
            try:
                # Используем тот же пайплайн, что и coach_chat: он сам определит
                # тип вопроса и подставит героя, упомянутого в тексте
                # («что купить против фантомки» → знания по Phantom Assassin).
                _, snippets, _ = self._tactical_context(query, hero=hero)
            except Exception:  # noqa: BLE001
                snippets = []
            if not snippets:
                return "В базе знаний нет точных данных по этому вопросу — отвечай по общим принципам роли."
            return "\n".join(f"- {s}" for s in snippets)

        if name == "analyze_last_match":
            match_id = args.get("match_id")
            return self.analyze_match(match_id=match_id)

        if name == "set_game_timer":
            seconds = int(args.get("seconds", 60))
            label = str(args.get("label", "напоминание"))
            clock = _g(self.gsi_payload(), "map", "clock_time")
            if clock is not None:
                if hasattr(self.macro_timer, "add_custom_timer"):
                    self.macro_timer.add_custom_timer(clock + seconds, label)
                return f"Таймер установлен на {seconds} сек ({label})."
            return f"Таймер «{label}» на {seconds} сек принят."

        if name == "recommend_draft_picks":
            from .meta_db import recommend_picks
            raw_enemies = args.get("enemy_heroes") or []
            if isinstance(raw_enemies, str):
                enemies = [e.strip() for e in raw_enemies.split(",") if e.strip()]
            else:
                enemies = list(raw_enemies)
            if not enemies:
                enemies = self.enemy_picks()
            role = int(args.get("role", self.position or 1))
            picks = recommend_picks(enemies, role=role, top_k=3)
            if not picks:
                return "Рекомендуются универсальные герои: Juggernaut, Axe или Centaur."
            lines = []
            for p in picks:
                c_str = ", ".join(p.get("core_items", [])[:3])
                strong = (p.get("strong_against") or ["стабильный лайнинг"])[0]
                lines.append(
                    f"{p['ru_name']} ({p['score']}%): {strong}. Сборка: {c_str}. {p['advice']}"
                )
            return "\n".join(lines)

        if name == "get_hero_item_build":
            from .meta_db import get_hero_build
            hero_name = str(args.get("hero_name", "")).strip()
            role = int(args.get("role", self.position or 1))
            enemies = self.enemy_picks()
            build = get_hero_build(hero_name, role=role, enemy_heroes=enemies)
            start = ", ".join(build.get("starting", []))
            core = ", ".join([f"{c['item']} ({c['timing']})" for c in build.get("core", [])[:3]])
            sit = ", ".join([f"{s['item']} ({s.get('trigger', '')})" for s in build.get("situational", [])[:2]])
            return (
                f"Гайд на {build.get('hero_ru_name', hero_name)} (поз. {role}):\n"
                f"Старт: {start}.\n"
                f"Основные: {core}.\n"
                f"Ситуативно: {sit}.\n"
                f"Шард: {str(build.get('shard') or '').rstrip('.')}.\n"
                f"Совет: {str(build.get('lane_tips') or '').rstrip('.')}"
            )

        return f"Неизвестный инструмент: {name}"

    def chat(
        self,
        user_prompt: str,
        system: str | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        include_dynamic: bool = True,
    ) -> str:
        """Запрос к LLM с возможностью тонкой настройки и аварийным failover.

        ``include_dynamic=False`` используется, когда Coach уже вложил
        тактический бриф в системный промпт и дублировать GSI-снимок не нужно.
        """
        system = system or make_system_prompt(self.coach_name, skill_level=self.skill_level)
        try:
            model = self.resolve_model()
            if model is None:
                raise OllamaError(self.last_error or "LLM недоступна")
            if self.provider == "openai_compatible":
                return self._openai_chat(
                    model,
                    user_prompt,
                    system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    include_dynamic=include_dynamic,
                )
            return self._ollama_chat(
                model,
                user_prompt,
                system,
                temperature=temperature,
                max_tokens=max_tokens,
                include_dynamic=include_dynamic,
            )
        except OllamaError as exc:
            if not self._should_failover(exc):
                raise
            try:
                log.warning("Основная LLM недоступна (%s), пробую бесплатный резерв", exc)
                return self._free_pollinations_chat(
                    user_prompt, system, include_dynamic=include_dynamic
                )
            except OllamaError:
                raise exc from None

    @staticmethod
    def _should_failover(exc: Exception) -> bool:
        """Failover только для сетевых/временных сбоев, не для проблем с ключом."""
        text = str(exc).lower()
        if not text:
            return False
        if any(
            marker in text
            for marker in (
                "401", "403", "api-ключ", "api ключ", "api key", "apikey",
                "ключ не", "ключ истёк", "expired", "недействителен",
            )
        ):
            return False
        if any(marker in text for marker in ("pollinations", "бесплатный ии")):
            return False
        return True

    def _ollama_chat(
        self,
        model: str,
        user_prompt: str,
        system: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        include_dynamic: bool = True,
    ) -> str:
        temp = float(temperature) if temperature is not None else float(getattr(self.llm, "temperature", 0.4) or 0.4)
        temp = max(0.05, min(0.95, temp))
        options: dict[str, Any] = {"temperature": temp}
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        messages = self._build_messages(user_prompt, system, include_dynamic=include_dynamic)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
            "tools": COACH_TOOLS,
        }
        url = f"{self.llm.base_url.rstrip('/')}/api/chat"

        def post() -> Any:
            try:
                return self._session.post(url, json=payload, timeout=self.llm.timeout)
            except Exception as exc:  # noqa: BLE001
                raise OllamaError(f"Не удалось обратиться к Ollama: {exc}") from exc

        tool_rounds_left = 2
        executed: set[str] = set()
        while True:
            response = post()
            # Старые сборки Ollama/модели могут не знать про tools — повтор без них.
            if getattr(response, "status_code", 0) != 200 and "tools" in payload:
                payload.pop("tools", None)
                response = post()
            if getattr(response, "status_code", 0) != 200:
                raise OllamaError(f"Ollama вернула HTTP {getattr(response, 'status_code', '?')}")
            try:
                data = response.json()
                message = data.get("message") or {}
            except Exception as exc:  # noqa: BLE001
                raise OllamaError(f"Некорректный ответ Ollama: {exc}") from exc
            text = (message.get("content") or "").strip()
            tool_calls = message.get("tool_calls") or []
            if text:
                return text
            if not tool_calls:
                raise OllamaError("Ollama вернула пустой ответ")
            if tool_rounds_left <= 0:
                raise OllamaError("Ollama вернула пустой ответ")
            messages.append(message)
            for tool_call in tool_calls:
                fn = (tool_call or {}).get("function") or {}
                fn_name = str(fn.get("name") or "")
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:  # noqa: BLE001
                        parsed_args = {}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}
                call_key = fn_name + ":" + json.dumps(parsed_args, ensure_ascii=False, sort_keys=True)
                if call_key in executed:
                    tool_result = "Этот инструмент уже вызывался — используй данные выше."
                else:
                    executed.add(call_key)
                    try:
                        tool_result = self.execute_tool(fn_name, parsed_args)
                    except Exception as exc:  # noqa: BLE001
                        tool_result = f"Ошибка инструмента {fn_name}: {exc}"
                messages.append({"role": "tool", "content": str(tool_result)[:1800]})
            payload["messages"] = messages
            tool_rounds_left -= 1

    def _free_pollinations_chat(
        self, user_prompt: str, system: str, *, include_dynamic: bool = True
    ) -> str:
        """Бесплатный резервный ИИ без необходимости вводить API-ключ."""
        messages = self._build_messages(user_prompt, system, include_dynamic=include_dynamic)
        payload = {
            "model": "openai",
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 300,
        }
        try:
            resp = self._session.post(
                "https://text.pollinations.ai/openai/chat/completions",
                json=payload,
                timeout=25.0,
            )
            if getattr(resp, "status_code", 0) == 200:
                data = resp.json()
                text = (data["choices"][0]["message"]["content"] or "").strip()
                if text:
                    return text
        except Exception as exc:  # noqa: BLE001
            log.warning("Pollinations failover exception: %s", exc)
        raise OllamaError("Бесплатный ИИ временно недоступен")

    @staticmethod
    def _api_error_text(response: Any) -> str:
        detail = ""
        try:
            err_json = response.json()
            if isinstance(err_json, dict) and "error" in err_json:
                error = err_json["error"]
                message = error.get("message") if isinstance(error, dict) else str(error)
                if message:
                    detail = f": {message}"
        except Exception:  # noqa: BLE001
            detail = f": {getattr(response, 'text', '')[:120]}"
        return detail

    def _openai_chat(
        self,
        model: str,
        user_prompt: str,
        system: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        include_dynamic: bool = True,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        base_url = (self.llm.base_url or "").rstrip("/")
        if "openrouter.ai" in base_url.lower():
            if not model or model.strip().lower() in ("openai", "default", "gpt", "") or "/" not in model:
                model = "openai/gpt-4o-mini"
        temp = float(temperature) if temperature is not None else float(getattr(self.llm, "temperature", 0.4) or 0.4)
        temp = max(0.05, min(0.95, temp))
        configured_tokens = int(getattr(self.llm, "max_tokens", 350) or 350)
        tokens = int(max_tokens) if max_tokens is not None else configured_tokens
        tokens = max(60, min(tokens, 900))
        messages = self._build_messages(user_prompt, system, include_dynamic=include_dynamic)
        headers = {"Authorization": f"Bearer {self.llm.api_key}"} if self.llm.api_key else {}
        url = f"{base_url}/chat/completions"

        tool_defs = COACH_TOOLS if tools is None else tools
        if "pollinations" in base_url.lower():
            tool_defs = []
            headers.clear()

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }
        if tool_defs:
            payload["tools"] = tool_defs
            payload["tool_choice"] = "auto"

        max_tool_rounds = 2
        tool_rounds_left = max_tool_rounds
        executed: set[str] = set()
        forced_final = False

        def post(payload_data: dict[str, Any]) -> Any:
            try:
                return self._session.post(url, json=payload_data, headers=headers, timeout=self.llm.timeout)
            except Exception as exc:  # noqa: BLE001
                raise OllamaError(f"Не удалось обратиться к {self.llm.base_url}: {exc}") from exc

        while True:
            response = post(payload)
            # Некоторые модели не поддерживают tools (HTTP 400) — повторяем без них.
            if getattr(response, "status_code", 0) == 400 and "tools" in payload:
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                tool_defs = []
                response = post(payload)
            if getattr(response, "status_code", 0) != 200:
                status_code = getattr(response, "status_code", 0)
                err_detail = self._api_error_text(response)
                if status_code in (401, 403) or "expired" in err_detail.lower():
                    raise OllamaError("API-ключ недействителен или истёк (HTTP 401)")
                raise OllamaError(f"API вернул HTTP {status_code}{err_detail}")
            try:
                data = response.json()
                msg = data["choices"][0]["message"]
            except Exception as exc:  # noqa: BLE001
                raise OllamaError(f"Некорректный ответ API: {exc}") from exc

            tool_calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip()
            if text:
                return text
            if not tool_calls:
                raise OllamaError("API вернул пустой ответ")

            if tool_rounds_left <= 0 and not forced_final:
                # Принудительно просим финальный ответ без инструментов.
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                messages.append(msg)
                messages.append({
                    "role": "user",
                    "content": "Ответь на вопрос игрока финальным ответом без вызова инструментов.",
                })
                payload["messages"] = messages
                forced_final = True
                continue
            if forced_final:
                raise OllamaError("API вернул пустой ответ")

            messages.append(msg)
            for tool_call in tool_calls:
                fn = tool_call.get("function", {}) or {}
                fn_name = str(fn.get("name", ""))
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:  # noqa: BLE001
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
                call_key = fn_name + ":" + json.dumps(parsed_args, ensure_ascii=False, sort_keys=True)
                if call_key in executed:
                    tool_result = "Этот инструмент уже вызывался — используй данные выше."
                else:
                    executed.add(call_key)
                    try:
                        tool_result = self.execute_tool(fn_name, parsed_args)
                    except Exception as exc:  # noqa: BLE001 — ошибка инструмента не валит диалог
                        tool_result = f"Ошибка инструмента {fn_name}: {exc}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", "call_1"),
                    "name": fn_name,
                    "content": str(tool_result)[:1800],
                })
            tool_rounds_left -= 1
            payload["messages"] = messages
            if not tool_defs:
                payload.pop("tools", None)

    # Обратная совместимость со старым именем метода.
    def llm(self, user_prompt: str) -> str:
        return self.chat(user_prompt)

    def ollama_models(self, *, max_age: float = 15.0) -> list[str]:
        return self.llm_models(max_age=max_age)

    # ------------------------------------------------------------------ #
    # Смена модели / пресета
    # ------------------------------------------------------------------ #
    def switch_model(self, target: str | None) -> tuple[bool, str]:
        """Переключает модель или пресет. Возвращает (изменено, сообщение)."""
        target = (target or "").strip()
        presets = self.llm.presets or {}
        if target and target in presets:
            preset = presets[target]
            for key in ("provider", "base_url", "api_key", "model", "temperature", "timeout"):
                if key in preset and preset[key] is not None:
                    setattr(self.llm, key, preset[key])
            self._models_cache = (0.0, [], None)
            return True, f"Пресет «{target}» активен: {self.llm.provider} / {self.llm.model}."
        if target:
            self.llm.model = target
            self._models_cache = (0.0, [], None)
            return True, f"Модель переключена на «{target}» (провайдер {self.llm.provider})."
        available = self.llm_models()
        return False, self.model_status() + (f" Доступны: {', '.join(available[:6])}." if available else "")

    def model_status(self) -> str:
        return f"Сейчас: {self.llm.provider} / {self.llm.model}."

    # ------------------------------------------------------------------ #
    # Память
    # ------------------------------------------------------------------ #
    def remember(self, question: str, answer: str) -> None:
        with self._lock:
            self._memory.append({"q": question[:200], "a": answer[:600]})
        try:
            self.long_memory.remember(question, answer)
        except Exception as exc:  # noqa: BLE001 — память не должна ронять ответ
            log.debug("Не удалось записать реплику в долговременную память: %s", exc)

    def memory_context(self, limit: int = 3) -> str:
        with self._lock:
            entries = list(self._memory)[-limit:]
        return "\n".join(f"Вопрос: {m['q']}\nОтвет: {m['a']}" for m in entries)

    # ------------------------------------------------------------------ #
    # Общие вспомогательные
    # ------------------------------------------------------------------ #
    def _role_block(self) -> str:
        parts = [f"Позиция игрока: {position_label(self.position)}."]
        if self.profile.mmr_bracket:
            parts.append(f"Ранг: {self.profile.mmr_bracket}.")
        return " ".join(parts)

    def _enemy_item_hints_for(self, enemies: Any, *, limit: int = 5) -> list[str]:
        """Точечные подсказки по закупке против конкретных врагов (без дублей)."""
        hints: list[str] = []
        seen: set[str] = set()
        for enemy in enemies or []:
            text = str(enemy or "").strip()
            if not text:
                continue
            key = normalize(text)
            for fragment, hint in _ENEMY_ITEM_HINTS:
                if fragment in key and hint not in seen:
                    hints.append(f"{text} → {hint}")
                    seen.add(hint)
            if len(hints) >= limit:
                break
        return hints[:limit]

    def _enemy_hints(self) -> list[str]:
        draft = collect_draft(self.gsi_payload(), self.catalog)
        return self._enemy_item_hints_for(list(draft.enemy_picks))

    def _hero_meta_lines(
        self, hero: dict, *, include_items: bool = True
    ) -> tuple[list[str], dict[str, dict[str, int]]]:
        pct, kind, games = DotaApi.winrate(hero)
        kind_label = {"pro": "про-игры", "pub": "паблики", "none": "нет данных"}[kind]
        roles = ", ".join(hero.get("roles") or []) or "не указаны"
        lines = [
            f"Герой: {hero.get('localized_name')} ({hero.get('name', '')}).",
            f"Роли: {roles}.",
            f"Винрейт: {pct:.1f}% ({kind_label}, игр: {games})." if kind != "none" else "Винрейт: нет данных.",
        ]
        popularity = self.api.item_popularity(int(hero.get("id", 0)))
        if popularity and include_items:
            lines.append(
                "Популярные предметы: "
                f"старт — {self._csv(self.api.top_items(popularity, 'start_game_items', 5))}; "
                f"ранняя игра — {self._csv(self.api.top_items(popularity, 'early_game_items', 5))}; "
                f"мид — {self._csv(self.api.top_items(popularity, 'mid_game_items', 5))}; "
                f"лейт — {self._csv(self.api.top_items(popularity, 'late_game_items', 5))}."
            )
        return lines, popularity

    @staticmethod
    def _csv(values: list[str]) -> str:
        return ", ".join(v for v in values if v) or "нет данных"

    # ------------------------------------------------------------------ #
    # Ответы
    # ------------------------------------------------------------------ #
    def _chat_limits(self, kind: str, *, voice: bool = False) -> tuple[int, int]:
        """Лимиты ответа для диалога: (max_sentences, max_chars) под уровень игрока.

        Новичок получает короткие ответы (до 3 предложений / 320 символов).
        Опытный игрок в чат-режиме — до 5 предложений / 500 символов с
        конкретикой, но голосовые ответы остаются короткими.
        """
        level = self.skill_level
        if level == "beginner" and kind in ("explain", "chat"):
            return 3, 320
        if level in ("advanced", "expert") and kind in ("explain", "chat") and not voice:
            return 5, 500
        if kind in ("why", "analysis"):
            return 3, 300
        return 2, 240

    def coach_chat(self, user_query: str, *, voice: bool = False) -> str:
        """Интерактивный диалог с тренером (Астра / Джарвис) по любому вопросу.

        Под капотом: определение типа вопроса → тактический бриф по GSI →
        подбор релевантных знаний из meta_db → «холодная» генерация →
        чистка ответа для озвучки → запись в долговременную память.

        Для новичка объяснение термина («что такое бкб») отдаётся из глоссария
        без обращения к LLM — быстрый и гарантированно простой ответ.
        ``voice=True`` (ответ уйдёт в озвучку) держит ответ коротким даже у
        опытных игроков.
        """
        clean_query = " ".join((user_query or "").split())
        level = self.skill_level
        if not clean_query:
            if level == "beginner":
                return beginner.freshman_greeting(self.coach_name)
            return "Да, я на связи. Что подсказать по игре?"

        kind_hint = detect_query_kind(clean_query)
        if level == "beginner" and kind_hint == "explain":
            explanation = beginner.explain_term(clean_query)
            if explanation:
                self.remember(clean_query, explanation)
                return explanation

        hero = self.current_hero()
        brief_text, knowledge, kind = self._tactical_context(clean_query, hero=hero)
        try:
            profile_text = self._profile_summary()
        except Exception:  # noqa: BLE001
            profile_text = ""
        system = render_system_prompt(
            self.coach_name,
            brief_text,
            knowledge,
            kind=kind,
            profile_text=profile_text,
            skill_level=level,
        )
        prompt = "\n".join(
            [
                f"Вопрос или реплика игрока: «{clean_query}»",
                f"Ответь как {self.coach_name}:",
            ]
        )

        max_sentences, max_chars = self._chat_limits(kind, voice=voice)
        try:
            answer = self._ask_llm(
                prompt,
                system=system,
                kind=kind,
                max_sentences=max_sentences,
                max_chars=max_chars,
            )
            if not answer:
                answer = self._knowledge_fallback(clean_query, hero, kind, knowledge)
            self.remember(clean_query, answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            log.warning("Ошибка LLM при ответе тренера: %s", exc)
            error_text = str(exc).lower()
            if "401" in error_text or "истёк" in error_text:
                return "⚠️ Ваш API-ключ недействителен или истёк (HTTP 401). Обновите ключ в Настройках (кнопка ⚙ вверху) или напишите: «апи ключ <ваш_новый_ключ>»."
            if "402" in error_text or "429" in error_text or "rate limit" in error_text:
                return "Бесплатный ИИ сейчас перегружен (лимит запросов). Подожди полминуты и спроси ещё раз."
            if kind == "explain" and level == "beginner":
                return (
                    "Пока не знаю такого термина — спроси иначе или назови другой термин из Dota 2, "
                    "и я объясню простыми словами."
                )
            return f"Не удалось связаться с ИИ ({exc}). Проверьте API-ключ в настройках."

    def _knowledge_fallback(
        self, query: str, hero: dict | None, kind: str, knowledge: list[str]
    ) -> str:
        """Ответ из базы знаний, если LLM вернула «воду»/отказ."""
        if knowledge:
            text = sanitize_answer(knowledge[0], max_chars=260, max_sentences=2)
            if text:
                return text
        hero_name = (hero or {}).get("localized_name")
        if hero_name:
            return (
                f"По {hero_name} играй от роли: держи темп фарма, не дерись без вижна "
                "и выходи на ключевой слот по таймингу."
            )
        return (
            "Нет данных для точного совета: играй по таймингам — контроль рун, вижн "
            "перед дракой и ключевой предмет к 15–20 минуте."
        )

    def hero_intro_advice(self, hero: dict, user_query: str = "") -> str:
        """Автономный стартовый совет от ИИ-тренера при выборе или упоминании героя."""
        if not hero:
            return "Не понял героя. Назовите героя ещё раз."
        self._manual_hero = hero
        hero_name = hero.get("localized_name") or hero.get("name")
        pos_str = position_label(self.position)
        short_npc = str(hero.get("name", "")).replace("npc_dota_hero_", "").lower()

        popularity = self.api.item_popularity(int(hero.get("id", 0)))
        top_start = self.api.top_items(popularity, "start_game_items", 4) if popularity else []
        skill_tip = _HERO_SKILL_TIPS.get(short_npc)
        hero_key = str(hero.get("localized_name") or hero.get("name") or "")
        knowledge: list[str] = []
        try:
            from .meta_db import get_hero_build, get_skill_order

            build = get_hero_build(hero_key, role=self.position)
            starting = ", ".join(str(i) for i in (build.get("starting") or [])[:5])
            if starting:
                knowledge.append(f"Стартовый закуп по базе: {starting}.")
            if build.get("lane_tips"):
                knowledge.append(f"Линия: {str(build['lane_tips']).rstrip('.')}")
            skill = get_skill_order(hero_key)
            if isinstance(skill, dict):
                bits = []
                if skill.get("priority"):
                    bits.append(f"максить: {skill['priority']}")
                if skill.get("level1"):
                    bits.append(f"на 1-м: {skill['level1']}")
                if bits:
                    knowledge.append("Прокачка: " + "; ".join(bits) + ".")
        except Exception:  # noqa: BLE001 — база знаний не должна ронять совет
            knowledge = []

        level = self.skill_level
        prompt_lines = [
            self._role_block(),
            f"Игрок берёт героя {hero_name} на позицию {pos_str}.",
            f"Популярные стартовые предметы: {', '.join(top_start) if top_start else 'стандартные расходники'}.",
            *([f"Знания по герою: {' '.join(knowledge)}"] if knowledge else []),
            *(
                [f"План тренировки на роль (для новичка): {beginner.role_training_plan(self.position)}"]
                if level == "beginner"
                else []
            ),
            f"Реплика игрока: «{user_query or hero_name}».",
            "Дай чёткий, уверенный стартовый совет от тренера (строго 1–2 предложения, до 25 слов): "
            "что купить на первые 600 золота, какую способность взять на 1-м уровне и что делать в начале линии. "
            "Отвечай живо, разнообразно, подчеркни ключевую фишку героя на линии. "
            "Категорический запрет на фразы вроде «я запомнил»! Отвечай сразу живым полезным советом по игре.",
        ]
        if level == "beginner":
            prompt_lines.append(
                "Игрок — новичок: если называешь незнакомый термин, поясни его в скобках. "
                "Тон — поддерживающий, без снобизма."
            )
        prompt = "\n".join(prompt_lines)

        fallback_parts = [f"Принято, {hero_name} ({pos_str})."]
        if top_start:
            fallback_parts.append(f"На старте возьми: {', '.join(top_start[:3])}.")
        if skill_tip:
            fallback_parts.append(skill_tip)
        else:
            fallback_parts.append("На 1-м уровне бери основной навык для контроля или харасса и помогай союзнику держать линию.")
        if level == "beginner":
            try:
                first_step = beginner.role_training_steps(self.position)[0].rstrip(".")
                fallback_parts.append(f"Тренировка: начни с простого — {first_step}.")
            except Exception:  # noqa: BLE001 — план не должен ронять совет
                pass
        fallback = " ".join(fallback_parts)

        try:
            answer = self._ask_llm(
                prompt,
                kind="build",
                max_sentences=3,
                max_chars=280,
            )
            if not answer:
                answer = fallback
            self.remember(f"Выбор героя {hero_name}", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            return fallback

    def start_items(self, hero: dict | None = None, user_query: str = "") -> str:
        """Краткий ответ о стартовом закупе на первые 600 золота без лейт-гейма."""
        hero = hero or self.current_hero()
        if not hero:
            return "Не понял, для какого героя стартовый закуп. Назовите героя или выберите его в игре."

        hero_name = hero.get("localized_name") or hero.get("name")
        popularity = self.api.item_popularity(int(hero.get("id", 0)))
        top_start = self.api.top_items(popularity, "start_game_items", 5) if popularity else []
        meta_starting: list[str] = []
        try:
            from .meta_db import get_hero_build

            build = get_hero_build(str(hero_name or ""), role=self.position)
            meta_starting = [str(i) for i in (build.get("starting") or [])][:5]
        except Exception:  # noqa: BLE001
            meta_starting = []

        prompt_lines = [
            self._role_block(),
            f"Игрок на герое {hero_name} спрашивает: «{user_query or 'Что купить на старте?'}»",
            f"Популярные стартовые предметы из статистики: {', '.join(top_start) if top_start else 'нет данных'}.",
            *(
                [f"Эталонный стартовый закуп: {', '.join(meta_starting)}."]
                if meta_starting
                else []
            ),
            "Назови ТОЛЬКО 3–4 конкретных стартовых предмета на первые 600 золота для линии.",
            "СТРОЖАЙШИЙ ЗАПРЕТ: не упоминай мид-гейм, лейт-гейм, способности, прокачку или рассуждения! Ответь ровно в одном-двух коротких предложениях.",
        ]
        prompt = "\n".join(prompt_lines)
        try:
            answer = self._ask_llm(prompt, kind="build", max_sentences=2, max_chars=220)
            if not answer:
                raise OllamaError("пустой ответ")
            self.remember(user_query or f"Стартовый закуп {hero_name}", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            items_fallback = meta_starting or top_start
            if items_fallback:
                return f"На старте для {hero_name} возьми: {', '.join(items_fallback[:4])}."
            return f"На старте для {hero_name} возьми пачку Танго, две ветки, цирклет и вард."

    def skills_order(self, hero: dict | None = None, user_query: str = "") -> str:
        """Краткий ответ по прокачке способностей на линию (1–2 предложения)."""
        hero = hero or self.current_hero()
        if not hero:
            return "Не понял, для какого героя прокачка. Назовите героя или выберите его в игре."
        hero_name = hero.get("localized_name") or hero.get("name")
        short_npc = str(hero.get("name", "")).replace("npc_dota_hero_", "").lower()

        fallback = _HERO_SKILL_TIPS.get(short_npc)
        meta_skill: list[str] = []
        try:
            from .meta_db import get_skill_order

            skill = get_skill_order(str(hero_name or ""))
            if isinstance(skill, dict):
                if skill.get("priority"):
                    meta_skill.append(f"максить: {skill['priority']}")
                if skill.get("level1"):
                    meta_skill.append(f"на 1-м уровне: {skill['level1']}")
                if skill.get("tip"):
                    meta_skill.append(str(skill["tip"]).rstrip("."))
        except Exception:  # noqa: BLE001
            meta_skill = []
        if not fallback:
            if meta_skill:
                fallback = f"На линии для {hero_name}: " + "; ".join(meta_skill).rstrip(".") + "."
            else:
                fallback = f"На линии для {hero_name} макси основной боевой навык для харасса или зачистки, а сейв держи в единицу."

        prompt_lines = [
            self._role_block(),
            f"Игрок на герое {hero_name} спрашивает: «{user_query or 'Что качать? Какой скилл максить?'}»",
            *([f"Знания по прокачке: {'; '.join(meta_skill)}."] if meta_skill else []),
            "Ответь в одном-двух коротких предложениях: какую способность максить первой на линии и что взять на 1-м уровне. Без списков и воды.",
        ]
        prompt = "\n".join(prompt_lines)
        try:
            answer = self._ask_llm(prompt, kind="skill", max_sentences=2, max_chars=220)
            if not answer:
                answer = fallback
            self.remember(user_query or f"Прокачка {hero_name}", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            return fallback

    def counter_advice(
        self, enemy_hero: dict | None, player_hero: dict | None = None, user_query: str = ""
    ) -> str:
        """Рекомендация контр-предметов против конкретного вражеского героя (1–2 предложения)."""
        if not enemy_hero:
            return "Не понял, против какого героя совет. Назовите героя или спросите: «что собрать против Джаггернаута»."

        enemy_name = enemy_hero.get("localized_name") or enemy_hero.get("name")
        enemy_npc = str(enemy_hero.get("name", "")).replace("npc_dota_hero_", "").lower()

        player_hero = player_hero or self.current_hero()
        player_name = player_hero.get("localized_name") if player_hero else None
        pos_str = position_label(self.position)

        fallback = _COUNTER_HINTS.get(enemy_npc)
        meta_counters: list[str] = []
        meta_tip = ""
        try:
            from .meta_db import get_hero_counters

            db = get_hero_counters(str(enemy_name or ""))
            if isinstance(db, dict):
                meta_counters = [str(i) for i in (db.get("counter_items") or [])]
                meta_tip = str(db.get("tip") or "").strip()
        except Exception:  # noqa: BLE001
            meta_counters = []
        if not fallback:
            if meta_counters:
                fallback = (
                    f"Против {enemy_name}: {', '.join(meta_counters[:3])}."
                    + (f" {meta_tip}" if meta_tip else "")
                )
            else:
                fallback = f"Против {enemy_name} для позиции {pos_str} собирай предметы на сейв (Ghost Scepter, Force Staff) или контроль (Orchid, Scythe of Vyse)."

        prompt_lines = [
            self._role_block(),
            f"Игрок на герое {player_name or 'саппорте/коре'} ({pos_str}) спрашивает: «{user_query or f'Что купить против {enemy_name}?'}»",
            f"Вражеский герой: {enemy_name}.",
            *(
                [f"Контр-предметы из базы знаний: {', '.join(meta_counters[:4])}."
                 + (f" {meta_tip}" if meta_tip else "")]
                if meta_counters
                else []
            ),
            "Назови 2–3 лучших конкретных контр-предмета против него под роль игрока.",
            "СТРОЖАЙШЕ: ровно 1–2 коротких предложения (до 20–25 слов, 4–6 секунд озвучки). Без списков, нумерации и воды.",
        ]
        prompt = "\n".join(prompt_lines)

        try:
            answer = self._ask_llm(prompt, kind="counter", max_sentences=2, max_chars=220)
            if not answer:
                answer = fallback
            self.remember(user_query or f"Контр-предметы против {enemy_name}", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            return fallback

    def pull_instructions(self, team_name: str | None = None) -> str:
        """Подсказка по времени отводов для текущей роли и команды."""
        team_clean = (team_name or "").strip().lower()
        team_label = "Свет" if "radiant" in team_clean else ("Тьма" if "dire" in team_clean else "")
        if self.position == 5:
            prefix = f"За {team_label}" if team_label else "Для пятёрки"
            return f"{prefix} на лёгкой линии: делай отвод на малый лагерь на :17 или :47 секунде, а на :53 секунде стакай малый лагерь."
        if self.position == 4:
            prefix = f"За {team_label}" if team_label else "Для четвёрки"
            return f"{prefix} на сложной линии: делай отвод большого лагеря на :17 или :47 секунде, когда линия пропушена вперёд."
        return "Отводы делают саппорты: малый лагерь на :17 и :47 секунде, большой лагерь на :17 и :47, а стак на :53."

    def advice(self, hero: dict, screen_context: str = "", user_query: str = "") -> str:
        if not hero:
            return "Не понял, для какого героя совет. Назовите героя или наведите на него курсор."
        meta_lines, popularity = self._hero_meta_lines(hero, include_items=False)
        gsi_text = self.gsi_context()
        history = self.memory_context()
        brief_text, knowledge, kind = self._tactical_context(
            user_query or "что купить дальше", hero=hero
        )

        # Проверяем тайминг матча
        clock = _g(self.gsi_payload(), "map", "clock_time")
        is_pre_game = (clock is None or clock <= 60)
        minute_num = clock // 60 if clock else 0
        hints = self._enemy_hints()
        threats_str = f"Вражеские угрозы: {', '.join(hints)}." if hints else ""

        if user_query:
            guidance = (
                f"Ответь конкретно, свежо и живо на вопрос игрока: «{user_query}». "
                f"Текущая минута игры: {minute_num}. {threats_str} "
                "Дай точный тактический совет (способности, позиционирование, тайминги, взаимодействие или ключевой предмет). "
                "Опирайся на тактический бриф и знания по вопросу; не выдумывай предметы и тайминги. "
                "Строго 1–2 предложения (до 25 слов) для быстрой озвучки голосом."
            )
        elif is_pre_game:
            top_start = self.api.top_items(popularity, "start_game_items", 5) if popularity else []
            guidance = (
                f"Игра только начинается (минута 0). Популярный старт: {', '.join(top_start) if top_start else 'стандартные расходники'}. "
                "Назови 3–4 стартовых расходника и главную задачу на первые секунды линии. "
                "Отвечай живо и разнообразно, строго 1–2 предложения, без воды и списков."
            )
        else:
            guidance = (
                f"Текущая минута: {minute_num}. {threats_str} "
                "Дай актуальный тактический фокус для этой минуты игры (предмет против врага, позиционка, сплит-пуш или командный файт). "
                "Опирайся на тактический бриф; варьируй формулировки и говори как живой тиммейт с 7000+ MMR. Строго 1–2 предложения (до 25 слов)."
            )

        level = self.skill_level
        if level == "beginner":
            guidance += (
                " Игрок — новичок: объясни простыми словами, назови одно конкретное действие "
                "и коротко скажи, зачем оно нужно."
            )
        elif level in ("advanced", "expert"):
            guidance += (
                " Игрок опытный: без объяснения базовых терминов — конкретика по таймингам "
                "паверспайков, темпу карты и контролю объектов."
            )

        prompt_parts = [
            self._role_block(),
            f"Игрок на герое {hero.get('localized_name')}.",
        ]
        if user_query:
            prompt_parts.append(f"Конкретный вопрос игрока: «{user_query}».")
        prompt_parts.extend(meta_lines)
        if brief_text:
            prompt_parts.append("Тактический бриф:\n" + brief_text)
        if knowledge:
            prompt_parts.append("Знания по вопросу:\n" + "\n".join(f"- {k}" for k in knowledge))
        prompt_parts.append(f"Ситуация в игре (GSI): {gsi_text}")
        if screen_context:
            prompt_parts.append(f"Контекст с экрана: {screen_context}")
        if history:
            prompt_parts.append(f"Недавний диалог:\n{history}")
        prompt_parts.append(guidance)

        prompt = "\n".join(prompt_parts)
        try:
            answer = self._ask_llm(
                prompt,
                kind=kind if user_query else "build",
                max_sentences=2,
                max_chars=240,
            )
            if not answer:
                answer = self._fallback_advice(hero, meta_lines, gsi_text, "пустой ответ LLM")
            self.remember(user_query or f"Совет для {hero.get('localized_name')}", answer)
            return answer
        except OllamaError as exc:
            log.info("LLM недоступна, использую запасной ответ: %s", exc)
            self.last_error = str(exc)
            return self._fallback_advice(hero, meta_lines, gsi_text, str(exc))

    def _fallback_advice(
        self, hero: dict, meta_lines: list[str], gsi_text: str, reason: str
    ) -> str:
        popularity = self.api.item_popularity(int(hero.get("id", 0)))
        hero_name = hero.get("localized_name")

        if "401" in reason or "истёк" in reason.lower():
            err_note = "API-ключ истёк или не указан"
        else:
            err_note = f"LLM недоступна ({reason})"

        if popularity:
            start_items = self.api.top_items(popularity, "start_game_items", 3)
            core_items = self.api.top_items(popularity, "early_game_items", 3) or self.api.top_items(popularity, "mid_game_items", 3)
            parts = []
            if start_items:
                parts.append(f"Старт: {', '.join(start_items)}.")
            if core_items:
                parts.append(f"Дальше: {', '.join(core_items)}.")
            if parts:
                return f"{err_note}. {hero_name} — {' '.join(parts)}"

        return f"{err_note}. Для {hero_name}: собирай предметы по позиции {position_label(self.position)}."

    def game_analysis(self) -> str:
        payload = self.gsi_payload()
        if payload is None:
            return (
                "Данных GSI нет: не вижу игру. Проверьте параметр -gamestateintegration "
                "и cfg-файл, затем перезапустите Dota 2."
            )
        level = self.skill_level
        mistakes = self.mistakes()
        context = build_gsi_context(
            payload, self.catalog, self.api.items(), age_seconds=self.gsi_age(),
            position=self.position, mistakes=mistakes,
        )
        hero = self.current_hero()
        brief_text, knowledge, _ = self._tactical_context("разбери игру", hero=hero)
        if level == "beginner":
            task_lines = [
                "Дай разбор для новичка в формате:",
                "Что получилось: ...",
                "Главная ошибка: ...",
                "Упражнение: ...",
                "Тон поддерживающий, без ругани и сложных терминов; термины поясняй в скобках.",
            ]
            max_sentences, max_chars = 4, 520
        elif level in ("advanced", "expert"):
            task_lines = [
                "Сделай глубокий разбор текущей игры, но не объясняй базовые термины и прописные истины.",
                "Оцени решения: тайминги паверспайков, темп карты, окна драфта, лейн-равновесие, "
                "тайминги вижна и смоков, экономику байбека и контроль объектов.",
                "Дай: 1) где потерян темп; 2) конкретное решение на ближайшие 2–3 минуты; "
                "3) одну привычку для следующего матча.",
            ]
            max_sentences, max_chars = 4, 560
        else:
            task_lines = [
                "Дай: 1) что прямо сейчас идёт не так; 2) конкретный план на ближайшие "
                "2–3 минуты; 3) одну привычку, которую исправить. Кратко, по-русски.",
            ]
            max_sentences, max_chars = 4, 520
        prompt = "\n".join(
            [
                self._role_block(),
                "Сделай глубокий разбор текущей игры.",
                "",
                "Ситуация (GSI, включая найденные детектором ошибки):",
                context,
                *(["Тактический бриф:\n" + brief_text] if brief_text else []),
                *(["Знания по драфту:\n" + "\n".join(f"- {k}" for k in knowledge)] if knowledge else []),
                "",
                *task_lines,
            ]
        )
        try:
            answer = self._ask_llm(
                prompt,
                kind="analysis",
                max_sentences=max_sentences,
                max_chars=max_chars,
            )
            if not answer:
                raise OllamaError("пустой ответ")
            self.remember("Разбор игры", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            if level == "beginner":
                return self._fallback_game_analysis_beginner(context, mistakes, hero, str(exc))
            lines = [f"LLM недоступна ({exc}). Что видит детектор:"]
            lines.extend("• " + m for m in (mistakes or ["явных ошибок не найдено"]))
            lines.append("Ситуация: " + context)
            return "\n".join(lines)

    def _fallback_game_analysis_beginner(
        self,
        context: str,
        mistakes: list[str],
        hero: dict | None,
        reason: str,
    ) -> str:
        """Разбор текущей игры без LLM в дружелюбном формате для новичка."""
        hero_name = (hero or {}).get("localized_name")
        main_mistake = (mistakes[0] if mistakes else "").strip().rstrip(".")
        if not main_mistake:
            main_mistake = "не хватает стабильного вижна и спокойного фарма без лишних смертей"
        drill = beginner.next_drill(self.position, seed_text=main_mistake)
        got_right = "ты играешь и уже разбираешь свои действия — это самый полезный шаг."
        if hero_name:
            got_right = f"ты играешь на {hero_name} и уже разбираешь свои действия — это самый полезный шаг."
        position_part = position_label(self.position)
        lines = [
            f"LLM недоступна ({reason}). Показываю разбор по данным детектора:",
            f"Что получилось: {got_right}",
            f"Главная ошибка: {main_mistake}. Для роли {position_part} это поправимо без сложных приёмов.",
            f"Упражнение: {drill}",
        ]
        return "\n".join(lines)

    def buy_next(self, hero: dict | None = None) -> str:
        payload = self.gsi_payload()
        if payload is None:
            return "Нет данных GSI: не вижу инвентарь и золото. Проверьте интеграцию с игрой."
        hero = hero or self.current_hero()
        hero_name = hero.get("localized_name") if hero else None

        raw_items = _g(payload, "hero", "items")
        inventory = []
        if isinstance(raw_items, dict):
            for slot in sorted(raw_items):
                if str(slot).startswith("slot"):
                    name = item_name(raw_items[slot], self.api.items())
                    if name and name != "?" and not name.lower().startswith("empty"):
                        inventory.append(name)
        gold = _g(payload, "player", "gold")
        enemy_picks = collect_draft(payload, self.catalog).enemy_picks
        hints = self._enemy_hints()
        context = build_gsi_context(
            payload, self.catalog, self.api.items(), age_seconds=self.gsi_age(),
            position=self.position,
        )
        build_knowledge: list[str] = []
        if hero_name:
            try:
                from .meta_db import get_hero_build

                build = get_hero_build(str(hero_name), role=self.position, enemy_heroes=enemy_picks)
                core = ", ".join(
                    f"{c.get('item')} ({c.get('timing')})"
                    for c in (build.get("core") or [])[:4]
                    if isinstance(c, dict)
                )
                if core:
                    build_knowledge.append(f"Эталонные слоты: {core}.")
                if build.get("shard"):
                    build_knowledge.append(f"Шард: {build['shard']}")
                situational = ", ".join(
                    str(s.get("item"))
                    for s in (build.get("situational") or [])[:3]
                    if isinstance(s, dict) and s.get("item")
                )
                if situational:
                    build_knowledge.append(f"Ситуативные предметы под драфт: {situational}.")
            except Exception:  # noqa: BLE001
                build_knowledge = []
        prompt = "\n".join(
            [
                self._role_block(),
                f"Герой: {hero_name or 'не определён'}.",
                f"Текущий инвентарь: {', '.join(inventory) or 'не виден'}.",
                f"Золото: {gold if gold is not None else 'неизвестно'}.",
                f"Пики врага: {', '.join(enemy_picks) or 'не видны'}.",
                *([f"Вражеские контр-предметы: {', '.join(hints)}.", ""] if hints else []),
                *([f"База билдов: {' '.join(build_knowledge)}"] if build_knowledge else []),
                "Ситуация (GSI):",
                context,
                "",
                "Назови 1–2 конкретных следующих предмета с объяснением «почему» "
                "(учитывай вражеский драфт: BKB/Manta/Eul/Lotus против сайленсов, "
                "Spirit Vessel/Skadi/Shiva против регена, MKB против уклонения и т.д.).",
            ]
        )
        try:
            answer = self._ask_llm(prompt, kind="build", max_sentences=2, max_chars=240)
            if not answer:
                raise OllamaError("пустой ответ")
            self.remember("Что купить дальше", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            lines = [f"LLM недоступна ({exc}). Быстрая подсказка по предметам:"]
            if hints:
                lines.extend("• " + h for h in hints)
            else:
                lines.append("• Универсально: добей ключевой слот по роли и держи BKB/сейв по вражескому драфту.")
            lines.append("Инвентарь: " + (", ".join(inventory) or "не виден"))
            return "\n".join(lines)

    def pick_advice(self) -> str:
        payload = self.gsi_payload()
        draft = collect_draft(payload, self.catalog)
        if payload is None:
            return (
                "Данных GSI нет: не вижу драфт. Проверьте параметр -gamestateintegration "
                "и cfg-файл Dota 2."
            )
        pool = self.pool_heroes()
        if not pool:
            return (
                f"В пуле героев нет никого на позицию {self.position}. "
                "Заполните player_profile.hero_pool в config.json."
            )

        context = build_gsi_context(
            payload, self.catalog, self.api.items(), age_seconds=self.gsi_age(), position=self.position
        )
        pool_names = [h.get("localized_name") for h in pool]
        meta_picks: list[str] = []
        try:
            from .meta_db import recommend_picks

            picks = recommend_picks(draft.enemy_picks, role=self.position, top_k=4)
            for pick in picks:
                if not isinstance(pick, dict):
                    continue
                cores = ", ".join(str(i) for i in (pick.get("core_items") or [])[:2])
                meta_picks.append(
                    f"{pick.get('ru_name')} ({pick.get('score')}%)"
                    + (f" — {cores}" if cores else "")
                )
        except Exception:  # noqa: BLE001
            meta_picks = []
        prompt = "\n".join(
            [
                self._role_block(),
                f"Мой пул на эту позицию: {', '.join(pool_names)}.",
                *([f"Союзники уже: {', '.join(draft.my_picks)}.", ""] if draft.my_picks else []),
                *([f"Враги: {', '.join(draft.enemy_picks)}.", ""] if draft.enemy_picks else []),
                *([f"Баны: {', '.join(draft.bans[:10])}.", ""] if draft.bans else []),
                *(
                    ["Контрпики из базы знаний: " + "; ".join(meta_picks) + "."]
                    if meta_picks
                    else []
                ),
                "Ситуация (GSI):",
                context,
                "",
                "Выбери лучшего героя СТРОГО из моего пула (обоснуй 1 фразой), и если весь "
                "пул жёстко законтрен — назови 1 метовую альтернативу вне пула. Кратко.",
            ]
        )
        try:
            answer = self._ask_llm(prompt, kind="pick", max_sentences=2, max_chars=240)
            if not answer:
                answer = self._fallback_pick(pool, draft, "пустой ответ LLM")
            self.remember("Кого пикнуть", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            return self._fallback_pick(pool, draft, str(exc))

    def _fallback_pick(self, pool: list[dict], draft: Any, reason: str) -> str:
        taken = set(draft.my_picks) | set(draft.enemy_picks) | set(draft.bans)
        ranked = sorted(
            (h for h in pool if h.get("localized_name") not in taken),
            key=lambda h: DotaApi.winrate(h)[0],
            reverse=True,
        )
        chosen = ranked[0] if ranked else pool[0]
        meta = self.api.meta(n=1)
        meta_name = meta[0]["hero"]["localized_name"] if meta else "?"
        return (
            f"LLM недоступна ({reason}). По винрейту из вашего пула — {chosen.get('localized_name')} "
            f"(вне пула метовая альтернатива: {meta_name})."
        )

    def draft_advice(self) -> str:
        payload = self.gsi_payload()
        if payload is None:
            return (
                "Данных о драфте нет: GSI не получает состояние игры. "
                "Проверьте cfg-файл и параметр -gamestateintegration, затем перезапустите Dota 2."
            )
        draft = collect_draft(payload, self.catalog)
        if draft.empty:
            return (
                "Драфт пока пуст: GSI не прислал пики и баны. "
                "Это нормально вне стадии выбора героев."
            )
        structured = []
        if draft.my_picks:
            structured.append("Ваши пики: " + ", ".join(draft.my_picks) + ".")
        if draft.enemy_picks:
            structured.append("Пики врага: " + ", ".join(draft.enemy_picks) + ".")
        if draft.bans:
            structured.append("Баны: " + ", ".join(draft.bans[:10]) + ".")
        knowledge: list[str] = []
        try:
            from .meta_db import recommend_picks

            picks = recommend_picks(
                draft.enemy_picks,
                ally_heroes=draft.my_picks,
                role=self.position,
                top_k=4,
            )
            if picks:
                knowledge.append(
                    "База знаний — контрпики на поз. "
                    + str(self.position)
                    + ": "
                    + "; ".join(
                        f"{p.get('ru_name')} ({p.get('score')}%)"
                        for p in picks
                        if isinstance(p, dict)
                    )
                    + "."
                )
        except Exception:  # noqa: BLE001
            knowledge = []
        try:
            brief = self._tactical_context("analyze draft", hero=None)
            if brief[0]:
                knowledge.append("Бриф: " + brief[0])
        except Exception:  # noqa: BLE001
            pass

        prompt = "\n".join(
            [
                self._role_block(),
                "Проанализируй драфт Dota 2.",
                *structured,
                *([f"Знания по драфту: {' '.join(knowledge)}"] if knowledge else []),
                "",
                "Ситуация (GSI):",
                self.gsi_context(),
                "",
                "Дай: 1) на какие линии и кем лучше встать; 2) кого стоит банить/опасаться; "
                "3) сильные и слабые стороны драфта. Кратко, по-русски.",
            ]
        )
        try:
            answer = self._ask_llm(prompt, kind="draft", max_sentences=4, max_chars=420)
            if not answer:
                raise OllamaError("пустой ответ")
            self.remember("Анализ драфта", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            return (
                "LLM недоступна (" + str(exc) + "). Что видно по GSI:\n"
                + "\n".join(structured)
                + "\nСильные стороны и контрпики лучше разобрать с работающей LLM."
            )

    def meta(self, n: int = 5) -> str:
        if not self.api.heroes:
            return "Нет данных о героях: отсутствует data/heroes.json и нет связи с OpenDota."
        top = self.api.meta(n=n)
        if not top:
            return "Не удалось посчитать мету: в данных OpenDota нет матчей."
        lines = []
        for item in top:
            hero = item["hero"]
            kind = "про" if item["kind"] == "pro" else "пабы"
            lines.append(
                f"{hero.get('localized_name')} — {item['winrate']:.1f}% ({kind}, {item['games']} игр)"
            )
        return "Сильнейшие герои по винрейту:\n" + "\n".join(f"• {line}" for line in lines)

    def status(self) -> str:
        payload = self.gsi_payload()
        context = build_gsi_context(
            payload, self.catalog, self.api.items(), age_seconds=self.gsi_age(),
            position=self.position,
        )
        hint = gsi_stale_hint(self.gsi_age())
        if hint and payload is not None:
            context = context + "\n" + hint
        return context

    # ------------------------------------------------------------------ #
    def describe_hero_from_text(self, text: str) -> dict | None:
        if not text:
            return None
        match = self.catalog.find(normalize(text)) if self.catalog.available else None
        return match.hero if match else None

    # ------------------------------------------------------------------ #
    # Анализ сыгранных матчей
    # ------------------------------------------------------------------ #
    def analyze_match(
        self,
        match_id: int | str | None = None,
        account_id: int | None = None,
    ) -> str:
        """Анализирует завершённый матч игрока (по ID матча или аккаунту) через OpenDota и LLM."""
        payload = self.gsi_payload()

        # 1. Определяем account_id
        target_account = account_id
        if target_account is None and self.profile.account_id is not None:
            target_account = self.profile.account_id
        if target_account is None and payload is not None:
            gsi_acc = _g(payload, "player", "accountid")
            if gsi_acc and int(gsi_acc) > 0:
                target_account = int(gsi_acc)
                self.profile.account_id = target_account

        # 2. Определяем match_id
        target_match = match_id
        if target_match is None and payload is not None:
            gsi_match = _g(payload, "map", "matchid")
            if gsi_match and str(gsi_match).isdigit() and int(gsi_match) > 0:
                target_match = int(gsi_match)

        if target_match is None and target_account is not None:
            recent = self.api.recent_matches(target_account, limit=1)
            if recent and recent[0].get("match_id"):
                target_match = recent[0]["match_id"]

        if target_match is None:
            return (
                "Не удалось определить матч для разбора.\n"
                "• Назовите номер матча (например: «разбор матча 7900000000»);\n"
                "• Либо укажите ваш Dota 2 Steam32 Account ID в Настройках (кнопка ⚙ вверху окна);\n"
                "• Убедитесь, что в Dota 2 включена опция «Общедоступная история матчей»."
            )

        match_data = self.api.match_details(target_match)
        if not match_data:
            return (
                f"Не удалось получить данные о матче #{target_match} с OpenDota.\n"
                "Возможно, матч ещё обрабатывается серверами (обычно требуется 1–2 минуты) "
                "или в профиле Dota 2 скрыта история матчей (Настройки -> Сообщество -> Общедоступная история матчей)."
            )

        summary = self.api.parse_match_analysis(match_data, account_id=target_account)
        if not summary:
            return f"Матч #{target_match} получен, но не удалось извлечь статистику игрока."

        # Формируем промпт для разбора
        bm = summary.get("benchmarks", {})
        bench_info = (
            f"GPM: лучше {bm.get('gpm_pct', 0)}% игроков, "
            f"XPM: лучше {bm.get('xpm_pct', 0)}%, "
            f"урон по героям: лучше {bm.get('hero_damage_pct', 0)}%, "
            f"урон по строениям: лучше {bm.get('tower_damage_pct', 0)}%, "
            f"ластхиты: лучше {bm.get('last_hits_pct', 0)}%, "
            f"смерти: лучше {bm.get('deaths_pct', 0)}%"
        )
        timings_str = ", ".join(summary.get("key_timings", [])) or "нет данных"
        items_str = ", ".join(summary.get("items", [])) or "нет предметов"
        allies_str = ", ".join(summary.get("allies", [])) or "неизвестно"
        enemies_str = ", ".join(summary.get("enemies", [])) or "неизвестно"

        # Сравнение закупа с эталонным билдом и контр-предметами под вражеский драфт
        build_reference = ""
        counter_reference = ""
        try:
            from .meta_db import get_hero_build, get_hero_threats

            build = get_hero_build(str(summary.get("hero_name") or ""), role=self.position)
            core = ", ".join(
                f"{c.get('item')} ({c.get('timing')})"
                for c in (build.get("core") or [])[:4]
                if isinstance(c, dict)
            )
            if core:
                build_reference = f"Эталонные слоты на {summary.get('hero_name')}: {core}."
            threats = get_hero_threats([str(e) for e in (summary.get("enemies") or [])])
            if isinstance(threats, list) and threats:
                counter_reference = "Контр-предметы по драфту: " + "; ".join(
                    f"{t.get('ru_name')}: {', '.join((t.get('counter_items') or [])[:2])}"
                    for t in threats[:3]
                    if isinstance(t, dict)
                ) + "."
        except Exception:  # noqa: BLE001 — база знаний не должна ронять разбор
            build_reference = counter_reference = ""

        history_lessons = ""
        try:
            lessons = self.long_memory.lessons(limit=3)
            if lessons:
                history_lessons = "Прошлые уроки: " + "; ".join(lessons) + "."
        except Exception:  # noqa: BLE001
            history_lessons = ""

        level = self.skill_level
        if level == "beginner":
            match_system_prompt = (
                "Ты — дружелюбный тренер по Dota 2 для новичка. "
                "Твоя цель — поддержать игрока и сделать понятный разбор сыгранного матча без ругани. "
                "Опирайся только на статистику матча и базу билдов, не выдумывай события и цифры. "
                "Говори по-русски, простыми словами, термины поясняй в скобках. "
                "Формат ответа: «Что получилось: ...», «Главная ошибка: ...», «Упражнение: ...»."
            )
        elif level in ("advanced", "expert"):
            match_system_prompt = (
                "Ты — аналитик Dota 2 высокого уровня и разбираешь решения опытного игрока. "
                "Не объясняй базовые термины и основы: анализируй тайминги паверспайков, темп карты, "
                "окна драфта, лейн-равновесие, тайминги вижна и смоков, экономику байбека и контроль объектов. "
                "Опирайся только на статистику матча и базу билдов, не выдумывай события и цифры. "
                "Говори по-русски, структурированно, без общих шаблонных фраз и воды. "
                "Называй предметы и способности так, как они называются в русском клиенте Dota 2."
            )
        else:
            match_system_prompt = (
                "Ты — строгий, профессиональный тренер по Dota 2 с 7000+ MMR. "
                "Твоя цель — сделать глубокий, честный и полезный разбор сыгранного матча для игрока. "
                "Опирайся только на статистику матча и базу билдов, не выдумывай события и цифры. "
                "Говори по-русски, структурированно, без общих шаблонных фраз и воды. "
                "Называй предметы и способности так, как они называются в русском клиенте Dota 2."
            )

        if level == "beginner":
            task_lines = [
                "Дай разбор в формате для новичка:",
                "Что получилось: что удалось и за что можно себя похвалить.",
                "Главная ошибка: одна самая важная ошибка простыми словами, без ругани.",
                "Упражнение: одно конкретное упражнение на следующую игру.",
            ]
        elif level in ("advanced", "expert"):
            task_lines = [
                "Разбери решения, а не основы:",
                "1. Где потерян темп карты и почему (тайминги паверспайков, фарм, окна драфта).",
                "2. Тайминги вижна и смоков, лейн-равновесие и агро крипов, экономика байбека.",
                "3. Контроль объектов (Рошан, аегида, вышки) и три решения на следующую игру.",
            ]
        else:
            task_lines = [
                "Дай конкретный разбор по 4 пунктам:",
                "1. Оценка выступления и темпа (был ли достаточный фарм, темп и участие в ключевых драках).",
                "2. Анализ сборки предметов против вражеского драфта (чего не хватило: BKB, срез регена, сейв, MKB, контроль).",
                "3. Главные допущенные ошибки (смерти, позиционирование, пуш вышек).",
                "4. 3 конкретных совета для следующей игры.",
            ]

        prompt = "\n".join(
            [
                f"Разбери сыгранный матч #{summary['match_id']} по Dota 2.",
                f"Результат: {summary['result_str']} ({summary['duration_str']}), сторона: {summary['side']}.",
                f"Герой игрока: {summary['hero_name']}.",
                f"K/D/A: {summary['kills']}/{summary['deaths']}/{summary['assists']} (KDA {summary['kda_ratio']:.2f}).",
                f"Фарм: GPM {summary['gpm']}, XPM {summary['xpm']}, крипов добито/заденаено: {summary['last_hits']}/{summary['denies']}, Net Worth: {summary['net_worth']}.",
                f"Урон: по героям {summary['hero_damage']}, по строениям {summary['tower_damage']}, лечение {summary['hero_healing']}.",
                f"Сравнение с другими игроками на {summary['hero_name']}: {bench_info}.",
                f"Сборка предметов: {items_str}.",
                f"Тайминги ключевых покупок: {timings_str}.",
                *([build_reference] if build_reference else []),
                *([counter_reference] if counter_reference else []),
                *([history_lessons] if history_lessons else []),
                f"Союзники: {allies_str}.",
                f"Вражеский драфт: {enemies_str}.",
                f"Итоговый счёт команд: Radiant {summary['radiant_score']} : Dire {summary['dire_score']}.",
                "",
                *task_lines,
            ]
        )

        header = (
            f"📊 РАЗБОР МАТЧА #{summary['match_id']} — {summary['result_str'].upper()} ({summary['duration_str']})\n"
            f"Герой: {summary['hero_name']} | KDA: {summary['kills']}/{summary['deaths']}/{summary['assists']} "
            f"| GPM: {summary['gpm']} | XPM: {summary['xpm']}\n"
            f"Предметы: {items_str}"
        )

        lesson = (
            f"{summary['result_str']} на {summary['hero_name']}: "
            f"KDA {summary['kills']}/{summary['deaths']}/{summary['assists']}, "
            f"GPM {summary['gpm']} ({bm.get('gpm_pct', 0)}-й перцентиль)"
        )
        try:
            answer = self.chat(
                prompt,
                system=match_system_prompt,
                temperature=0.3,
                max_tokens=max(int(getattr(self.llm, "max_tokens", 350) or 350), 550),
                include_dynamic=False,
            )
            cleaned = sanitize_answer(answer, max_chars=1600, max_sentences=12, kind="match")
            if cleaned and cleaned != answer:
                log.debug("Ответ разбора матча очищен от markdown")
            self.remember(f"Разбор матча #{summary['match_id']}", answer)
            self._record_match_lesson(lesson, summary)
            return f"{header}\n\n{answer}"
        except OllamaError as exc:
            self.last_error = str(exc)
            self._record_match_lesson(lesson + " (без LLM)", summary)
            fallback = self._fallback_match_analysis(summary, reason=str(exc))
            return f"{header}\n\n{fallback}"

    def _record_match_lesson(self, lesson: str, summary: dict[str, Any]) -> None:
        """Сохраняет урок матча в долговременную память и профиль игрока."""
        try:
            self.long_memory.remember_lesson(lesson, match_id=summary.get("match_id"))
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось запомнить урок матча: %s", exc)
        bm = summary.get("benchmarks", {}) or {}
        mistakes: list[str] = []
        if int(summary.get("deaths") or 0) > 6:
            mistakes.append("Слишком много смертей: играть от позиционки и вижна")
        if int(bm.get("gpm_pct") or 0) < 40:
            mistakes.append("Низкий темп фарма: держать 60+ ластхитов за 10 минут")
        if int(bm.get("tower_damage_pct") or 0) < 35:
            mistakes.append("Мало урона по строениям: конвертировать драки в объективы")
        try:
            data_dir = getattr(self.api, "data_dir", None)
            if data_dir is not None and mistakes:
                update_player_profile_mistakes(
                    data_dir,
                    mistakes=mistakes,
                    hero_name=str(summary.get("hero_name") or ""),
                    account_id=summary.get("account_id"),
                )
        except Exception as exc:  # noqa: BLE001 — профиль не должен ронять разбор
            log.debug("Не удалось обновить профиль игрока: %s", exc)

    def _fallback_match_analysis(self, summary: dict[str, Any], reason: str) -> str:
        """Эвристический разбор сыгранного матча без обращения к LLM."""
        bm = summary.get("benchmarks", {})
        hero = summary.get("hero_name", "Герой")
        deaths = summary.get("deaths", 0)
        gpm = summary.get("gpm", 0)
        gpm_pct = bm.get("gpm_pct", 50)
        tower_damage = summary.get("tower_damage", 0)
        items = [i.lower() for i in summary.get("items", [])]
        enemies = [e.lower() for e in summary.get("enemies", [])]

        lines = [f"⚠️ LLM недоступна ({reason}). Статистический разбор от AIDOTA:"]

        if self.skill_level == "beginner":
            if deaths <= 3:
                positive = "мало смертей — хорошее позиционирование."
            elif summary.get("kda_ratio", 0.0) >= 3.0:
                positive = "неплохая реализация фрагов в драках."
            elif gpm_pct >= 50:
                positive = "нормальный темп фарма для этой игры."
            else:
                positive = "ты доиграл матч до конца и получил опыт — это уже результат."
            if deaths > 6:
                main_mistake = "слишком много смертей: играй от вижна и позиции"
            elif gpm_pct < 40:
                main_mistake = "мало фарма: держи 60+ ластхитов за 10 минут"
            elif tower_damage < 1200:
                main_mistake = "драки не превращались в снос вышек и объективы"
            else:
                main_mistake = "мало вижна перед драками"
            drill = beginner.next_drill(
                self.position, seed_text=f"{hero}:{deaths}:{gpm}:{tower_damage}"
            )
            return "\n".join(
                [
                    f"⚠️ LLM недоступна ({reason}). Статистический разбор от AIDOTA:",
                    f"Что получилось: {positive}",
                    f"Главная ошибка: {main_mistake}.",
                    f"Упражнение: {drill}",
                ]
            )

        # 1. Темп и фарм
        if gpm_pct >= 75:
            farm_eval = f"• Темп фарма: Отличный! GPM {gpm} (топ-{100 - gpm_pct}% среди игроков на {hero})."
        elif gpm_pct >= 40:
            farm_eval = f"• Темп фарма: Средний (GPM {gpm}, {summary.get('last_hits', 0)} ластхитов за {summary.get('duration_str', '?')})."
        else:
            farm_eval = f"• Темп фарма: Ниже среднего (GPM {gpm}, {summary.get('last_hits', 0)} ластхитов). Не хватало фарма в мидгейме."
        lines.append(farm_eval)

        # 2. Выживаемость
        if deaths <= 3:
            death_eval = f"• Выживаемость: Отличная! Всего {deaths} смертей — грамотное позиционирование."
        elif deaths <= 7:
            death_eval = f"• Выживаемость: Приемлемая ({deaths} смертей), но часть смертей стоила преимущества."
        else:
            death_eval = f"• Выживаемость: Критическая ({deaths} смертей)! Слишком много лишних смертей без размена."
        lines.append(death_eval)

        # 2b. Вклад в урон и бенчмарки
        hero_damage = int(summary.get("hero_damage") or 0)
        hero_damage_pct = bm.get("hero_damage_pct", 50)
        if hero_damage_pct >= 70:
            lines.append(
                f"• Вклад в урон: высокий — по героям {hero_damage} урона (лучше {hero_damage_pct}% игроков на герое)."
            )
        elif hero_damage_pct < 35:
            lines.append(
                f"• Вклад в урон: низкий — {hero_damage} урона по героям ({hero_damage_pct}-й перцентиль)."
            )
        kda = summary.get("kda_ratio", 0.0)
        if kda >= 4.0:
            lines.append(f"• KDA {kda:.2f}: сильная реализация фрагов.")
        elif kda < 2.0:
            lines.append(f"• KDA {kda:.2f}: мало реализации киллов — старайся участвовать в драках по таймингам.")

        # 3. Пуш и строения
        if tower_damage >= 3000:
            lines.append(f"• Осада строений: Отлично! {tower_damage} урона по вышкам.")
        elif tower_damage < 800:
            lines.append(f"• Осада строений: Слабо ({tower_damage} урона по строениям). Драки не конвертировались в снос вышек.")

        # 4. Проверка контр-предметов против врагов
        counter_tips = []
        for enemy_sub, tip in _ENEMY_ITEM_HINTS:
            if any(enemy_sub in e for e in enemies):
                # Проверяем, есть ли нужные предметы
                key_words = [w.strip().lower() for w in tip.split("—")[0].split("/") if w.strip()]
                has_any = any(any(kw in it for it in items) for kw in key_words)
                if not has_any:
                    counter_tips.append(f"Против {enemy_sub.capitalize()}: рекомендуется {tip}")

        if counter_tips:
            lines.append("• Предметы против врагов:")
            for ct in counter_tips[:3]:
                lines.append(f"  - {ct}")

        # 5. Рекомендации
        lines.append("\nСоветы на следующую игру:")
        if deaths > 5:
            lines.append("1. Ставьте защитный вижн и покупайте BKB / защитный слот раньше при появлении опасности.")
        else:
            lines.append("1. Держите высокий темп фарма между перемещениями команды.")
        if tower_damage < 1200:
            lines.append("2. После каждой выигранной драки сразу жмите ближайшую башню, а не уходите в лес.")
        else:
            lines.append("2. Контролируйте Рошана и ключевые тайминги рун мудрости и лотосов.")
        lines.append("3. Адаптируйте закуп под контроль и типы урона в драфте противника.")

        return "\n".join(lines)
