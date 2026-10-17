"""Персональный тренер: промпты для LLM, запасные ответы без сети, профиль игрока.

Поддерживает два провайдера ИИ:
- ``ollama`` — локальный сервер Ollama (без GPU-нагрузки от облака);
- ``openai_compatible`` — любой OpenAI-совместимый API (DeepSeek, OpenRouter, Groq).

Всё, что делает тренер, учитывает три вещи: позицию (1–5), тайминг/режим
(All Pick vs Turbo) и вражеский драфт.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from .config import LlmConfig, OllamaConfig, PlayerProfile
from .context import (
    analyze_player_mistakes,
    build_gsi_context,
    collect_draft,
    current_hero_npc,
    get_game_mode,
    gsi_stale_hint,
    item_name,
)
from .dota_api import DotaApi
from .heroes import HeroCatalog, normalize
from .roles import normalize_position, position_label

log = logging.getLogger("aidota.coach")

SYSTEM_PROMPT = (
    "Ты — персональный тренер по Dota 2 с рейтингом 7000+ MMR и помогаешь игроку "
    "прямо во время матча. Правила ответа: говори по-русски; строго 3–5 конкретных "
    "предложений (чтобы чтение занимало 10–20 секунд); без воды, общих фраз и markdown; "
    "называй предметы и способности так, как они называются в русском клиенте. "
    "Всегда учитывай: роль игрока (позиция 1–5), текущую минуту и режим (All Pick или Turbo) "
    "и вражеский драфт. Если данных не хватает — прямо скажи, чего именно не хватает, "
    "и дай один короткий универсальный совет."
)


class OllamaError(RuntimeError):
    """LLM недоступна или модель не готова. Текст исключения — для пользователя."""


# Подсказки по предметам против конкретных вражеских героев (для fallback без LLM).
_ENEMY_ITEM_HINTS: tuple[tuple[str, str], ...] = (
    ("silencer", "BKB / Manta / Eul / Lotus — много сайленсов"),
    ("huskar", "Spirit Vessel / Skadi / Shiva — срез регена"),
    ("necrophos", "Spirit Vessel / Skadi — срез регена"),
    ("morphling", "Spirit Vessel / Skadi / Silence"),
    ("alchemist", "Spirit Vessel / Skadi / Shiva"),
    ("timbersaw", "Spirit Vessel / Shiva"),
    ("phantom_assassin", "MKB — против уклонения"),
    ("brewmaster", "MKB — против уклонения"),
    ("faceless_void", "Force Staff / Eul / BKB — от хроносферы"),
    ("enigma", "BKB / Force — против блэкхола"),
    ("legion_commander", "Linken / BKB / сейв — от дуэли"),
    ("beastmaster", "Manta / BKB — от рулета"),
    ("warlock", "BKB / Force Staff — от Апхевола"),
    ("doom", "Linken / BKB — от дуума"),
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
        memory_size: int = 6,
        session: Any | None = None,
    ):
        self.api = api
        self.catalog = catalog
        self.gsi = gsi
        self.llm = self._coerce_llm(llm)
        self.profile = player_profile if player_profile is not None else PlayerProfile()
        self._memory: deque[dict[str, str]] = deque(maxlen=max(1, memory_size))
        self._lock = threading.RLock()
        self._models_cache: tuple[float, list[str], str | None] = (0.0, [], None)
        self.last_error: str | None = None
        self._position: int | None = None  # переопределение на время матча

        if session is None:
            import requests

            session = requests.Session()
        self._session = session

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
        npc = current_hero_npc(self.gsi_payload())
        if not npc:
            return None
        return self.catalog.by_npc(npc)

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
            model = (self.llm.model or "").strip()
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

    def chat(self, user_prompt: str, system: str | None = None) -> str:
        model = self.resolve_model()
        if model is None:
            raise OllamaError(self.last_error or "LLM недоступна")
        system = system or SYSTEM_PROMPT
        if self.provider == "openai_compatible":
            return self._openai_chat(model, user_prompt, system)
        return self._ollama_chat(model, user_prompt, system)

    def _ollama_chat(self, model: str, user_prompt: str, system: str) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": self.llm.temperature},
        }
        url = f"{self.llm.base_url.rstrip('/')}/api/chat"
        try:
            response = self._session.post(url, json=payload, timeout=self.llm.timeout)
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Не удалось обратиться к Ollama: {exc}") from exc
        if getattr(response, "status_code", 0) != 200:
            raise OllamaError(f"Ollama вернула HTTP {getattr(response, 'status_code', '?')}")
        try:
            data = response.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Некорректный ответ Ollama: {exc}") from exc
        if not text:
            raise OllamaError("Ollama вернула пустой ответ")
        return text

    def _openai_chat(self, model: str, user_prompt: str, system: str) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.llm.temperature,
        }
        headers = {"Authorization": f"Bearer {self.llm.api_key}"} if self.llm.api_key else {}
        url = f"{self.llm.base_url.rstrip('/')}/chat/completions"
        try:
            response = self._session.post(url, json=payload, headers=headers, timeout=self.llm.timeout)
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Не удалось обратиться к {self.llm.base_url}: {exc}") from exc
        if getattr(response, "status_code", 0) != 200:
            raise OllamaError(f"API вернул HTTP {getattr(response, 'status_code', '?')}")
        try:
            data = response.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Некорректный ответ API: {exc}") from exc
        if not text:
            raise OllamaError("API вернул пустой ответ")
        return text

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

    def _enemy_hints(self) -> list[str]:
        draft = collect_draft(self.gsi_payload(), self.catalog)
        hints: list[str] = []
        seen: set[str] = set()
        for enemy in draft.enemy_picks:
            key = normalize(enemy)
            for fragment, hint in _ENEMY_ITEM_HINTS:
                if fragment in key and hint not in seen:
                    hints.append(f"{enemy} → {hint}")
                    seen.add(hint)
        return hints

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
    def advice(self, hero: dict, screen_context: str = "") -> str:
        if not hero:
            return "Не понял, для какого героя совет. Назовите героя или наведите на него курсор."
        meta_lines, _ = self._hero_meta_lines(hero)
        gsi_text = self.gsi_context()
        history = self.memory_context()
        prompt = "\n".join(
            [
                self._role_block(),
                f"Игрок спрашивает про героя {hero.get('localized_name')}.",
                "",
                "Данные о герое:",
                *meta_lines,
                "",
                "Текущая ситуация в игре (GSI):",
                gsi_text,
                "",
                *([f"Контекст с экрана: {screen_context}", ""] if screen_context else []),
                *([f"Недавняя история диалога:\n{history}", ""] if history else []),
                "Дай: 1) 2–3 предмета, которые купить сейчас, с коротким обоснованием; "
                "2) порядок прокачки способностей; 3) один тактический приоритет именно на эту минуту.",
            ]
        )
        try:
            answer = self.chat(prompt)
            self.remember(f"Совет для {hero.get('localized_name')}", answer)
            return answer
        except OllamaError as exc:
            log.info("LLM недоступна, использую запасной ответ: %s", exc)
            self.last_error = str(exc)
            return self._fallback_advice(hero, meta_lines, gsi_text, str(exc))

    def _fallback_advice(
        self, hero: dict, meta_lines: list[str], gsi_text: str, reason: str
    ) -> str:
        meta_lines, _ = self._hero_meta_lines(hero, include_items=False)
        popularity = self.api.item_popularity(int(hero.get("id", 0)))
        item_lines: list[str] = []
        if popularity:
            phases = (
                ("Старт", "start_game_items"),
                ("Ранняя игра", "early_game_items"),
                ("Мид", "mid_game_items"),
                ("Лейт", "late_game_items"),
            )
            for label, key in phases:
                values = self.api.top_items(popularity, key, 5)
                if values:
                    item_lines.append(f"{label}: {', '.join(values)}.")
        parts = [
            f"LLM недоступна ({reason}), поэтому — краткая шпаргалка по {hero.get('localized_name')}:",
            *meta_lines,
            *item_lines,
            "Порядок прокачки: ориентируйтесь на популярные сборки выше и максите навык, "
            "который нужен для текущей линии.",
            f"Ситуация в игре: {gsi_text}",
        ]
        return "\n".join(part for part in parts if part)

    def game_analysis(self) -> str:
        payload = self.gsi_payload()
        if payload is None:
            return (
                "Данных GSI нет: не вижу игру. Проверьте параметр -gamestateintegration "
                "и cfg-файл, затем перезапустите Dota 2."
            )
        mistakes = self.mistakes()
        context = build_gsi_context(
            payload, self.catalog, self.api.items(), age_seconds=self.gsi_age(),
            position=self.position, mistakes=mistakes,
        )
        prompt = "\n".join(
            [
                self._role_block(),
                "Сделай глубокий разбор текущей игры.",
                "",
                "Ситуация (GSI, включая найденные детектором ошибки):",
                context,
                "",
                "Дай: 1) что прямо сейчас идёт не так; 2) конкретный план на ближайшие "
                "2–3 минуты; 3) одну привычку, которую исправить. Кратко, по-русски.",
            ]
        )
        try:
            answer = self.chat(prompt)
            self.remember("Разбор игры", answer)
            return answer
        except OllamaError as exc:
            self.last_error = str(exc)
            lines = [f"LLM недоступна ({exc}). Что видит детектор:"]
            lines.extend("• " + m for m in (mistakes or ["явных ошибок не найдено"]))
            lines.append("Ситуация: " + context)
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
        prompt = "\n".join(
            [
                self._role_block(),
                f"Герой: {hero_name or 'не определён'}.",
                f"Текущий инвентарь: {', '.join(inventory) or 'не виден'}.",
                f"Золото: {gold if gold is not None else 'неизвестно'}.",
                f"Пики врага: {', '.join(enemy_picks) or 'не видны'}.",
                *([f"Вражеские контр-предметы: {', '.join(hints)}.", ""] if hints else []),
                "Ситуация (GSI):",
                context,
                "",
                "Назови 1–2 конкретных следующих предмета с объяснением «почему» "
                "(учитывай вражеский драфт: BKB/Manta/Eul/Lotus против сайленсов, "
                "Spirit Vessel/Skadi/Shiva против регена, MKB против уклонения и т.д.).",
            ]
        )
        try:
            answer = self.chat(prompt)
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
        prompt = "\n".join(
            [
                self._role_block(),
                f"Мой пул на эту позицию: {', '.join(pool_names)}.",
                *([f"Союзники уже: {', '.join(draft.my_picks)}.", ""] if draft.my_picks else []),
                *([f"Враги: {', '.join(draft.enemy_picks)}.", ""] if draft.enemy_picks else []),
                *([f"Баны: {', '.join(draft.bans[:10])}.", ""] if draft.bans else []),
                "Ситуация (GSI):",
                context,
                "",
                "Выбери лучшего героя СТРОГО из моего пула (обоснуй 1 фразой), и если весь "
                "пул жёстко законтрен — назови 1 метовую альтернативу вне пула. Кратко.",
            ]
        )
        try:
            answer = self.chat(prompt)
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

        prompt = "\n".join(
            [
                self._role_block(),
                "Проанализируй драфт Dota 2.",
                *structured,
                "",
                "Ситуация (GSI):",
                self.gsi_context(),
                "",
                "Дай: 1) на какие линии и кем лучше встать; 2) кого стоит банить/опасаться; "
                "3) сильные и слабые стороны драфта. Кратко, по-русски.",
            ]
        )
        try:
            answer = self.chat(prompt)
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
