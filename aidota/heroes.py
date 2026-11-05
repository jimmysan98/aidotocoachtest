"""Каталог героев Dota 2: поиск по имени, русские прозвища, транслитерация.

OpenDota отдаёт только английские имена (Anti-Mage, Pudge, ...).
Vosk же распознаёт русскую речь, поэтому для голосовых команд нужен
нечёткий поиск: «антимаг» -> Anti-Mage, «пудж» -> Pudge и т.д.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

Hero = dict[str, Any]

# Транслитерация нужна, чтобы сравнивать русское слово с английским именем.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "e", "ґ": "g",
}

# Русские прозвища и сокращения. Ключ — короткое имя NPC-героя.
RU_ALIASES: dict[str, tuple[str, ...]] = {
    "antimage": ("антимаг", "антимаги", "am"),
    "axe": ("акс", "аксе"),
    "bane": ("бейн",),
    "bloodseeker": ("блудсикер", "блуд", "бс"),
    "crystal_maiden": ("цм", "цмка", "кристалка", "кристал мейден"),
    "drow_ranger": ("дровка", "дров", "дроу"),
    "earthshaker": ("шейкер", "ершейкер", "эршейкер"),
    "juggernaut": ("джаггернаут", "джага", "джаггер", "джаг"),
    "mirana": ("мирана", "потма", "потм"),
    "morphling": ("морф", "морфлинг"),
    "nevermore": ("сф", "шадоу фиенд", "шедоу фиенд", "невермор"),
    "phantom_lancer": ("пл", "фантом лансер", "лансер", "ланцер"),
    "puck": ("пак", "пук"),
    "pudge": ("пудж", "пуджик", "пуджа"),
    "razor": ("разор",),
    "sand_king": ("сенд кинг", "сэнд кинг", "сэндкинг", "песочный"),
    "storm_spirit": ("шторм", "шторм спирит"),
    "sven": ("свен",),
    "tiny": ("тини",),
    "vengefulspirit": ("венга", "венга", "венджфул"),
    "windrunner": ("врка", "врки", "врку", "врке", "вркой", "виндрейнджер", "виндрангер", "винд"),
    "zuus": ("зевс", "зус"),
    "kunkka": ("кунка", "кункка"),
    "lina": ("лина",),
    "lion": ("лион",),
    "shadow_shaman": ("шаман", "шадоу шаман", "растаман"),
    "slardar": ("слардар",),
    "tidehunter": ("тайд", "тайдхантер"),
    "witch_doctor": ("вд", "витч доктор", "доктор"),
    "life_stealer": ("лайфстилер", "найкс", "лайф"),
    "night_stalker": ("найт сталкер", "нс", "сталкер"),
    "broodmother": ("бруда", "брудаматер", "бруда"),
    "bounty_hunter": ("бх", "баунти", "баунти хантер"),
    "weaver": ("вивер", "вив", "уивер"),
    "jakiro": ("джакиро", "джакира"),
    "batrider": ("батрайдер", "батрайдер", "бат"),
    "chen": ("чен",),
    "spectre": ("спектра", "спектр"),
    "ancient_apparition": ("аа", "апарат", "древний"),
    "doom_bringer": ("дум", "дум брингер", "думка"),
    "ursa": ("урса", "урса"),
    "spirit_breaker": ("бара", "спирит брейкер", "спирит"),
    "gyrocopter": ("гиро", "гирокоптер", "гира"),
    "alchemist": ("алхимик", "алх"),
    "invoker": ("инвокер", "вокер", "инвок"),
    "silencer": ("сайленсер", "силенсер", "молчун"),
    "obsidian_destroyer": ("ода", "обсидиан", "аутворлд"),
    "lycan": ("ликан",),
    "brewmaster": ("брюмастер", "панда", "брю"),
    "shadow_demon": ("шедоу демон", "шд", "демон"),
    "lone_druid": ("лон друид", "мишка", "друид"),
    "chaos_knight": ("цк", "хаос найт", "хаос"),
    "meepo": ("мипо",),
    "treant": ("трент", "трент протектор", "дерево"),
    "ogre_magi": ("огр", "огр маг", "огрик"),
    "undying": ("андаинг", "зомби", "андай"),
    "rubick": ("рубик",),
    "disruptor": ("дизраптор", "дизрапт"),
    "nyx_assassin": ("никс", "никс ассасин", "никса"),
    "naga_siren": ("нага", "нага сирена"),
    "keeper_of_the_light": ("котл", "кипер", "кипер оф лайт"),
    "wisp": ("ио", "висп"),
    "visage": ("визаж", "визаг"),
    "slark": ("сларк",),
    "medusa": ("медуза", "дуза"),
    "troll_warlord": ("тролль", "тролл", "тролль варлорд"),
    "centaur": ("кентавр", "центавр", "цента"),
    "magnataur": ("магнус", "магнус"),
    "shredder": ("тимбер", "таймбер", "тимберсо"),
    "bristleback": ("бристл", "бристлбек", "бристлбэк"),
    "tusk": ("туск",),
    "skywrath_mage": ("скай", "скайврат", "скайврат маг"),
    "abaddon": ("абаддон", "абадон", "аба"),
    "elder_titan": ("титан", "елдер титан", "элдер титан"),
    "legion_commander": ("легионка", "легион", "легион командор", "лк"),
    "techies": ("течис", "течас", "минер", "мина"),
    "ember_spirit": ("эмбер", "эмбер спирит", "эмбера"),
    "earth_spirit": ("эрт спирит", "эрт", "земляной"),
    "abyssal_underlord": ("андерлорд", "андер", "пит лорд"),
    "terrorblade": ("террорблейд", "тб", "террор"),
    "phoenix": ("феникс",),
    "oracle": ("оракл", "оракул"),
    "winter_wyvern": ("виверна", "винтер", "винтер виверн"),
    "arc_warden": ("арк", "арк варден", "арка"),
    "monkey_king": ("мк", "манки кинг", "манки", "обезьяна"),
    "dark_willow": ("виллоу", "дарк виллоу", "вилла"),
    "pangolier": ("панго", "панголиер", "пангольер"),
    "grimstroke": ("гримстроук", "грим", "гримка"),
    "hoodwink": ("худвинк", "худ", "белка"),
    "void_spirit": ("войд спирит", "войд", "войда"),
    "snapfire": ("снап", "снапфайр", "бабка"),
    "mars": ("марс",),
    "ringmaster": ("рингмастер", "ринг", "цирк"),
    "dawnbreaker": ("донбрейкер", "дон", "донбрейк"),
    "marci": ("марси",),
    "primal_beast": ("праймал", "примал бист", "праймал бист", "зверь"),
    "muerta": ("муэрта", "муерта"),
    "kez": ("кез",),
    # Герои, которых чаще зовут по английскому имени:
    "queenofpain": ("квопа", "квин", "квин оф пейн", "королева боли"),
    "phantom_assassin": ("па", "фантомка", "фантом ассасин"),
    "faceless_void": ("войд", "фейслес войд", "фейслес"),
    "templar_assassin": ("та", "темпларка", "темплар ассасин"),
    "viper": ("вайпер",),
    "venomancer": ("веном", "веномансер"),
    "warlock": ("варлок",),
    "furion": ("фурион", "нп", "нэйчурс профит", "профет"),
    "skeleton_king": ("вк", "врайт кинг", "скелет кинг", "рейс"),
    "death_prophet": ("дп", "дез профет", "смерть"),
    "dragon_knight": ("дк", "драгон найт", "дракон"),
    "riki": ("рики",),
    "tinker": ("тинкер", "тк"),
    "pugna": ("пугна",),
    "necrolyte": ("некр", "некрофос", "некролитом"),
    "clinkz": ("клинкз", "клинк"),
    "huskar": ("хускар", "хуск"),
    "omniknight": ("омник", "омнинайт", "омникнайт"),
    "beastmaster": ("бистмастер", "бист"),
    "dark_seer": ("дарк сир", "дс", "сир"),
    "enchantress": ("энчантресс", "энча", "энчант"),
    "enigma": ("энигма", "енигма"),
}

# Служебные слова, которые не должны приниматься за имя героя.
_STOPWORDS = {
    "что", "чтобы", "как", "какой", "какие", "какую", "какого", "каком", "какому", "каких", "какая", "какое",
    "кого", "кем", "кому", "ком", "чем", "чему", "чего", "кто", "где", "куда", "когда", "откуда", "зачем", "почему", "сколько",
    "мне", "меня", "мой", "моя", "моего", "надо", "нужно", "можно", "дай",
    "скажи", "пожалуйста", "плиз", "пж", "ок", "окей", "да", "нет",
    "купить", "предмет", "предметы", "сборка", "сборку", "закуп", "билд",
    "собери", "посоветуй", "подскажи", "совет", "разбери", "анализ",
    "герой", "героя", "герои", "героем", "героев", "играть", "играю", "сыграть",
    "игра", "игре", "игру", "матч", "матче", "партия", "партию",
    "лучше", "сейчас", "потом", "тогда", "самый", "самые", "сильные",
    "мета", "топ", "драфт", "драфту", "пики", "пик", "баны", "бан",
    "банить", "пикнуть", "контрпик", "статус", "счёт", "счет", "время",
    "таймер", "помощь", "команды", "повтори", "выход", "пока", "стоп",
    "возьми", "берем", "берём", "это", "этот", "этого", "там", "тут", "вот",
    "за", "на", "по", "и", "в", "с", "к", "у", "о", "об", "не", "ни",
    "или", "а", "но", "ну", "же", "ли", "бы", "уж", "ты", "вы", "я", "мы",
    "он", "она", "они", "оно", "про", "для", "от", "до", "при", "над",
    "под", "без", "если", "то", "так", "assistant", "dota", "дота", "доту",
    "доте", "дота2", "dota2",
    # Игровые термины, скиллы, уровни, линии, отводы, чтобы не путать с героями
    "скилл", "скиллы", "скилы", "скил", "скрил", "скрилы", "скилла", "скиллу", "скиллом", "скиллами", "скиллах",
    "прокачать", "прокачай", "прокачка", "прокачки", "прокачке", "качать", "качай", "максить", "макси",
    "способность", "способности", "способностей", "способностям", "способностями", "умение", "умения", "умений",
    "лвл", "уровень", "уровня", "уровне", "уровнем",
    "против", "противника", "враг", "врага", "враги", "врагов", "вражеский", "вражеского", "контр",
    "линия", "линии", "линию", "лайн", "лайна", "лайне",
    "отвод", "отводы", "отвода", "отводов", "отводе", "пулл", "стак", "стаки", "стакать", "стака", "стаков",
    "мидер", "мидера", "мидеру", "мидере", "мидером", "мид", "миду", "шестой",
}

_STOPWORDS_NORMALIZED = {
    re.sub(r"[^0-9a-z]+", "", "".join(_TRANSLIT.get(ch, ch) for ch in w.lower().replace("ё", "е")))
    for w in _STOPWORDS
}
_STOPWORDS_NORMALIZED.discard("")


def translit(text: str) -> str:
    """Русский текст -> латиница (для нечёткого сравнения)."""
    out = []
    for ch in text.lower():
        out.append(_TRANSLIT.get(ch, ch))
    return "".join(out)


def normalize(text: str) -> str:
    """Оставляет только буквы/цифры и транслитерирует кириллицу."""
    cleaned = re.sub(r"[^0-9a-zа-яёіїєґ]+", "", text.lower().replace("ё", "е"))
    return translit(cleaned)


def _filter_tokens(text: str) -> list[str]:
    """Разбивает текст на слова и отфильтровывает стоп-слова ДО и ПОСЛЕ транслитерации."""
    raw_tokens = [t for t in re.split(r"[^0-9a-zа-яёіїєґ]+", text.lower()) if t]
    filtered: list[str] = []
    for t in raw_tokens:
        if t in _STOPWORDS:
            continue
        norm_t = normalize(t)
        if not norm_t or norm_t in _STOPWORDS or norm_t in _STOPWORDS_NORMALIZED:
            continue
        filtered.append(norm_t)
    return filtered


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from rapidfuzz import fuzz

        return float(fuzz.ratio(a, b)) / 100.0
    except ImportError:
        return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class HeroMatch:
    hero: Hero
    score: float
    method: str

    @property
    def name(self) -> str:
        return self.hero.get("localized_name") or self.hero.get("name", "?")


class HeroCatalog:
    """Индексы и нечёткий поиск по героям."""

    def __init__(self, heroes: Iterable[Hero], aliases: dict[str, tuple[str, ...]] | None = None):
        self.heroes: list[Hero] = sorted(
            (h for h in heroes if h.get("name")), key=lambda h: h.get("localized_name", "")
        )
        self._by_id: dict[int, Hero] = {}
        self._by_npc: dict[str, Hero] = {}
        self._exact: dict[str, Hero] = {}
        self._aliases: dict[str, Hero] = {}
        aliases = RU_ALIASES if aliases is None else aliases

        for hero in self.heroes:
            npc = str(hero["name"]).lower()
            short = npc.replace("npc_dota_hero_", "")
            hero_id = hero.get("id")
            if isinstance(hero_id, int):
                self._by_id[hero_id] = hero
            for key in (npc, short):
                self._by_npc[key] = hero
            for key in (
                normalize(hero.get("localized_name", "")),
                normalize(short.replace("_", " ")),
            ):
                if key:
                    self._exact.setdefault(key, hero)
            for alias in aliases.get(short, ()):  # русские прозвища
                key = normalize(alias)
                if key:
                    self._aliases.setdefault(key, hero)

    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.heroes)

    def __bool__(self) -> bool:
        return bool(self.heroes)

    @property
    def available(self) -> bool:
        return bool(self.heroes)

    def by_id(self, hero_id: Any) -> Hero | None:
        try:
            return self._by_id.get(int(hero_id))
        except (TypeError, ValueError):
            return None

    def by_npc(self, npc_name: str | None) -> Hero | None:
        if not npc_name:
            return None
        key = str(npc_name).lower()
        return self._by_npc.get(key) or self._by_npc.get(key.replace("npc_dota_hero_", ""))

    def display_name(self, npc_name: str | None) -> str | None:
        hero = self.by_npc(npc_name)
        return (hero.get("localized_name") if hero else None) or None

    # ------------------------------------------------------------------ #
    def find(self, text: str, threshold: float = 0.7) -> HeroMatch | None:
        """Ищет героя по свободной фразе («что купить на антимаге»)."""
        if not text:
            return None
        norm = normalize(text)
        if not norm or norm in _STOPWORDS or norm in _STOPWORDS_NORMALIZED:
            return None

        tokens = _filter_tokens(text)

        # 1) точное совпадение всего текста/токена с алиасом или именем
        for candidate in (norm, *tokens):
            if candidate in _STOPWORDS or candidate in _STOPWORDS_NORMALIZED:
                continue
            hero = self._aliases.get(candidate)
            if hero is not None:
                return HeroMatch(hero, 1.0, "alias")

        # 2) вхождение имени/алиаса в текст (для фраз целиком)
        best: HeroMatch | None = None
        for key, hero in self._exact.items():
            if len(key) >= 5 and key in norm:
                best = _better(best, HeroMatch(hero, 0.97, "substring"))
        for key, hero in self._aliases.items():
            if len(key) >= 4 and key in norm:
                best = _better(best, HeroMatch(hero, 0.98, "alias-substring"))

        # 3) нечёткое сравнение с именем и алиасами
        for key, hero in self._exact.items():
            if len(key) <= 3:
                continue
            score = _similarity(norm, key)
            if score >= threshold:
                best = _better(best, HeroMatch(hero, score, "fuzzy-name"))
        for key, hero in self._aliases.items():
            if len(key) <= 2:
                continue  # совсем короткие аббревиатуры (am, sf, cm, vk...) сравниваем только точно
            score = _similarity(norm, key)
            # Падежи и опечатки: «врки» -> «врка», «пуджа» -> «пудж».
            if (
                len(key) >= 3
                and len(norm) >= 3
                and abs(len(norm) - len(key)) <= 1
                and score >= 0.74
                and (norm.startswith(key[:3]) or key.startswith(norm[:3]))
            ):
                score = max(score, 0.85)
            if score >= threshold:
                best = _better(best, HeroMatch(hero, score, "fuzzy-alias"))

        return best

    def find_in_tokens(self, text: str, threshold: float = 0.78) -> HeroMatch | None:
        """Ищет героя среди отдельных слов фразы (устойчивее к лишним словам)."""
        tokens = _filter_tokens(text)
        best: HeroMatch | None = None
        for token in tokens:
            if len(token) < 2:
                continue
            if token in self._aliases:
                return HeroMatch(self._aliases[token], 1.0, "alias-exact")
            match = self.find(token, threshold=threshold)
            if match is not None:
                best = _better(best, match)
        return best


def _better(current: HeroMatch | None, candidate: HeroMatch) -> HeroMatch:
    if current is None or candidate.score > current.score:
        return candidate
    return current
