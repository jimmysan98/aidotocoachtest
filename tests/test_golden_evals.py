"""tests.test_golden_evals — Эталонные оффлайн-фикстуры (Golden Evals) Фазы 3.

Покрывает:
  1. Драфт с анализом пробелов команды (8 критериев: control, push, frontline, save, dispel, antiheal, break, true_strike).
  2. Трёхзначная модель EnemyState (confirmed | inferred | unknown) и строгие эпистемические маркеры (туман войны не выдаётся за факт).
  3. 0% утечки галлюцинированных, удалённых и уже купленных (stash*, backpack, съеденный Scepter/Shard, апгрейды) предметов.
  4. Детерминированный пост-валидатор (Zero-LLM rewrite) без повторных сетевых вызовов.
  5. Обратная совместимость фасадных хуков Coach и 127 ключей META_HEROES_DB.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from aidota.coach import Coach
from aidota.config import Config
from aidota.enemy_state import (
    STATE_CONFIRMED,
    STATE_INFERRED,
    STATE_UNKNOWN,
    EnemyStateTracker,
    format_enemy_state_prompt_block,
    format_epistemic_item_notice,
)
from aidota.heroes import HeroCatalog
from aidota.item_validator import (
    analyze_team_draft_gaps,
    extract_player_owned_items,
    is_item_owned_or_covered,
    pick_deterministic_fallback_item,
    resolve_item_slug_and_name,
    sanitize_structured_item_advice,
    sanitize_voice_or_chat_advice_text,
    validate_item_candidate,
)
from aidota.meta_db import META_HEROES_DB


class DraftTeamGapsGoldenEvals(unittest.TestCase):
    """Эталонные тесты анализа пробелов драфта команды."""

    def test_missing_control_and_frontline(self) -> None:
        """Если у союзников нет контроля и танков — пробелы детектируются."""
        allies = ["Anti-Mage", "Sniper"]
        enemies = ["Anti-Mage", "Queen of Pain"]
        gaps = analyze_team_draft_gaps(allies, enemies, player_role=3)
        missing_keys = {g["key"] for g in gaps["missing_gaps"]}
        self.assertIn("control", missing_keys)
        self.assertIn("frontline", missing_keys)

    def test_urgent_break_vs_bristleback(self) -> None:
        """Против Bristleback детектируется срочная потребность в Break (Silver Edge)."""
        allies = ["Crystal Maiden", "Juggernaut"]
        enemies = ["Bristleback", "Rubick"]
        gaps = analyze_team_draft_gaps(allies, enemies, player_role=1)
        break_gap = next((g for g in gaps["missing_gaps"] if g["key"] == "break"), None)
        self.assertIsNotNone(break_gap)
        self.assertTrue(break_gap["urgent"])
        self.assertIn("Silver Edge", break_gap["recommended_items"])

    def test_urgent_true_strike_vs_phantom_assassin(self) -> None:
        """Против Phantom Assassin детектируется срочная потребность в True Strike (MKB/Bloodthorn)."""
        allies = ["Centaur Warrunner", "Lina"]
        enemies = ["Phantom Assassin", "Shadow Demon"]
        gaps = analyze_team_draft_gaps(allies, enemies, player_role=1)
        ts_gap = next((g for g in gaps["missing_gaps"] if g["key"] == "true_strike"), None)
        self.assertIsNotNone(ts_gap)
        self.assertTrue(ts_gap["urgent"])
        self.assertIn("Monkey King Bar", ts_gap["recommended_items"])

    def test_urgent_antiheal_vs_necrophos_morphling(self) -> None:
        """Против Morphling/Necrophos детектируется потребность в антихиле."""
        allies = ["Sven", "Tidehunter"]
        enemies = ["Morphling", "Necrophos"]
        gaps = analyze_team_draft_gaps(allies, enemies, player_role=2)
        antiheal_gap = next((g for g in gaps["missing_gaps"] if g["key"] == "antiheal"), None)
        self.assertIsNotNone(antiheal_gap)
        self.assertTrue(antiheal_gap["urgent"])
        self.assertIn("Spirit Vessel", antiheal_gap["recommended_items"])


class EnemyStateEpistemicGoldenEvals(unittest.TestCase):
    """Эталонные тесты трёхзначной модели EnemyState и эпистемических маркеров."""

    def setUp(self) -> None:
        self.tracker = EnemyStateTracker()

    def test_unknown_in_fog_of_war_never_claims_absence(self) -> None:
        """В начале игры туман войны не объявляет, что у врага нет предметов."""
        payload = {
            "map": {"clock_time": 120, "matchid": "999001"},
            "draft": {"enemy_picks": ["Axe", "Phantom Assassin"]},
        }
        self.tracker.sync_gsi(payload, clock_sec=120, enemy_picks=["Axe", "Phantom Assassin"])
        axe_state = self.tracker.get_hero_state("Axe")
        self.assertIsNotNone(axe_state)
        # До тайминга Даггера (540s) предметы Axe в тумане
        notice = format_epistemic_item_notice("Axe", "Blink Dagger", STATE_UNKNOWN)
        self.assertIn("тумане войны", notice)
        self.assertIn("не считай, что предметов нет", notice)

    def test_inferred_timing_window_triggers_probabilistic_warning(self) -> None:
        """По достижении тайминга (11 мин = 660с) Даггер на Аксе становится inferred (не факт!)."""
        payload = {
            "map": {"clock_time": 660, "matchid": "999002"},
            "draft": {"enemy_picks": ["Axe"]},
        }
        self.tracker.sync_gsi(payload, clock_sec=660, enemy_picks=["Axe"])
        axe_state = self.tracker.get_hero_state("Axe")
        self.assertIsNotNone(axe_state)
        inferred = axe_state.inferred_items()
        self.assertTrue(any(it.item_slug == "blink" for it in inferred))
        
        notice = format_epistemic_item_notice("Axe", "Blink Dagger", STATE_INFERRED)
        self.assertIn("возможен Blink Dagger", notice)
        self.assertIn("проверь слоты", notice)
        # Категорически не должно звучать как подтверждённый факт!
        self.assertNotIn("подтверждён", notice)

    def test_confirmed_player_callout_overrides_unknown_and_inferred(self) -> None:
        """Голосовая фиксация игроком помечает предмет как CONFIRMED."""
        payload = {
            "map": {"clock_time": 600, "matchid": "999003"},
            "draft": {"enemy_picks": ["Sven"]},
        }
        self.tracker.sync_gsi(payload, clock_sec=600, enemy_picks=["Sven"])
        # Игрок говорит: "У Свена БКБ"
        ok = self.tracker.confirm_enemy_item("Sven", "бэкабэ", clock_sec=600, source="player_callout")
        self.assertTrue(ok)

        sven_state = self.tracker.get_hero_state("Sven")
        self.assertIsNotNone(sven_state)
        conf = sven_state.confirmed_items()
        self.assertTrue(any(it.item_slug == "black_king_bar" for it in conf))

        notice = format_epistemic_item_notice("Sven", "Black King Bar", STATE_CONFIRMED)
        self.assertIn("подтверждён Black King Bar", notice)

    def test_spectator_allplayers_marks_items_confirmed(self) -> None:
        """В спектаторском GSI все видимые предметы врагов confirmed."""
        payload = {
            "map": {"clock_time": 1200, "matchid": "999004"},
            "allplayers": {
                "player_5": {
                    "team": "dire",
                    "hero_name": "npc_dota_hero_pudge",
                    "items": {
                        "slot0": {"name": "item_blink"},
                        "slot1": {"name": "item_pipe"},
                    },
                }
            },
        }
        self.tracker.sync_gsi(payload, clock_sec=1200, enemy_picks=["Pudge"])
        pudge_state = self.tracker.get_hero_state("Pudge")
        self.assertIsNotNone(pudge_state)
        conf_slugs = {it.item_slug for it in pudge_state.confirmed_items()}
        self.assertIn("blink", conf_slugs)
        self.assertIn("pipe", conf_slugs)

    def test_prompt_block_separates_fact_timing_and_fog(self) -> None:
        """Форматирование Seg 2 промпта чётко делит ФАКТ, ТАЙМИНГ и ТУМАН."""
        payload = {
            "map": {"clock_time": 700, "matchid": "999005"},
            "draft": {"enemy_picks": ["Axe", "Sven"]},
        }
        self.tracker.sync_gsi(payload, clock_sec=700, enemy_picks=["Axe", "Sven"])
        self.tracker.confirm_enemy_item("Sven", "bkb", clock_sec=700)
        block = self.tracker.build_prompt_context(clock_sec=700)
        self.assertIn("ФАКТ: Black King Bar", block)
        self.assertIn("ТАЙМИНГ (проверь): Blink Dagger", block)


class ItemLeakageZeroToleranceGoldenEvals(unittest.TestCase):
    """0% утечки: проверка тайника (stash), бэкпака, съеденных баффов, апгрейдов и удалённых предметов."""

    def test_stash_items_prevent_duplicate_recommendation(self) -> None:
        """Предмет в тайнике (stash0..stash5) исключает совет о повторной покупке."""
        payload = {
            "hero": {
                "items": {
                    "slot0": {"name": "item_power_treads"},
                    "slot1": {"name": "item_magic_wand"},
                    "stash0": {"name": "item_black_king_bar"},
                }
            }
        }
        owned = extract_player_owned_items(payload)
        self.assertIn("black_king_bar", owned)

        valid, name, reason = validate_item_candidate("Black King Bar", owned)
        self.assertFalse(valid)
        self.assertIn("уже куплен", str(reason))

    def test_backpack_items_prevent_duplicate_recommendation(self) -> None:
        """Предмет в бэкпаке (slot6..slot8) исключает совет о покупке."""
        payload = {
            "hero": {
                "items": {
                    "slot0": {"name": "item_boots"},
                    "slot6": {"name": "item_blink"},
                }
            }
        }
        owned = extract_player_owned_items(payload)
        self.assertIn("blink", owned)

        valid, _, _ = validate_item_candidate("Blink Dagger", owned)
        self.assertFalse(valid)

    def test_consumed_scepter_and_shard_prevent_recommendation(self) -> None:
        """Съеденный Scepter / Shard (флаг или бафф) исключает совет о покупке."""
        payload = {
            "hero": {
                "items": {"slot0": {"name": "item_boots"}},
                "aghanims_scepter_consumed": 1,
                "aghanims_shard_consumed": True,
            }
        }
        owned = extract_player_owned_items(payload)
        self.assertIn("ultimate_scepter", owned)
        self.assertIn("aghanims_shard", owned)

        valid_scepter, _, _ = validate_item_candidate("Aghanim's Scepter", owned)
        valid_shard, _, _ = validate_item_candidate("Aghanim's Shard", owned)
        valid_slang_aganim, _, _ = validate_item_candidate("аганим", owned)
        valid_slang_shard, _, _ = validate_item_candidate("шард", owned)

        self.assertFalse(valid_scepter)
        self.assertFalse(valid_shard)
        self.assertFalse(valid_slang_aganim)
        self.assertFalse(valid_slang_shard)

    def test_upgrade_equivalents_suppress_base_components(self) -> None:
        """При наличии Hurricane Pike базовые Force Staff и Dragon Lance не советуются."""
        payload = {
            "hero": {
                "items": {
                    "slot0": {"name": "item_hurricane_pike"},
                }
            }
        }
        owned = extract_player_owned_items(payload)
        self.assertTrue(is_item_owned_or_covered("force_staff", owned))
        self.assertTrue(is_item_owned_or_covered("dragon_lance", owned))
        self.assertTrue(is_item_owned_or_covered("форс", owned))

        valid_fs, _, _ = validate_item_candidate("Force Staff", owned)
        valid_dl, _, _ = validate_item_candidate("Dragon Lance", owned)
        self.assertFalse(valid_fs)
        self.assertFalse(valid_dl)

    def test_silver_edge_suppresses_shadow_blade(self) -> None:
        """Silver Edge подавляет Shadow Blade / лотар."""
        payload = {
            "hero": {
                "items": {
                    "slot0": {"name": "item_silver_edge"},
                }
            }
        }
        owned = extract_player_owned_items(payload)
        self.assertTrue(is_item_owned_or_covered("invis_sword", owned))
        self.assertTrue(is_item_owned_or_covered("shadow_blade", owned))

        valid, _, _ = validate_item_candidate("лотар", owned)
        self.assertFalse(valid)

    def test_removed_items_are_rejected_and_mapped(self) -> None:
        """Удалённые предметы (PMS, Necronomicon, Hood) блокируются и маппятся."""
        owned: set[str] = set()
        for removed in ("poor_mans_shield", "necronomicon", "hood_of_defiance", "medallion_of_courage"):
            slug, name, is_rem = resolve_item_slug_and_name(removed)
            self.assertTrue(is_rem)
            valid, _, reason = validate_item_candidate(removed, owned)
            self.assertFalse(valid)
            self.assertIn("удалён", str(reason))


class DeterministicZeroLLMFallbackGoldenEvals(unittest.TestCase):
    """Детерминированная замена невалидных рекомендаций (0 LLM retries)."""

    def test_pick_deterministic_fallback_returns_unowned_valid_item(self) -> None:
        """Детерминированный подбор возвращает реально непокупленный предмет."""
        owned = {"power_treads", "black_king_bar", "battle_fury"}
        item, why = pick_deterministic_fallback_item("Anti-Mage", role=1, owned_items=owned)
        self.assertNotIn(item.lower().replace(" ", "_"), owned)
        self.assertTrue(len(item) > 2)
        self.assertTrue(len(why) > 5)

    def test_sanitize_structured_advice_removes_owned_and_removed(self) -> None:
        """Структурированные DTO очищаются от уже купленных и удалённых предметов."""
        owned = {"black_king_bar", "force_staff"}
        raw_advice = [
            {"item": "Black King Bar", "why": "против магии", "priority": "HIGH"},
            {"item": "Force Staff", "why": "разрыв дистанции", "priority": "MEDIUM"},
            {"item": "Poor Man's Shield", "why": "блок урона", "priority": "LOW"},
            {"item": "Manta Style", "why": "сброс сайленса", "priority": "HIGH"},
        ]
        sanitized = sanitize_structured_item_advice(
            raw_advice, owned, hero_name="Anti-Mage", role=1
        )
        items = [s["item"] for s in sanitized]
        self.assertNotIn("Black King Bar", items)
        self.assertNotIn("Force Staff", items)
        self.assertNotIn("Poor Man's Shield", items)
        self.assertIn("Manta Style", items)

    def test_sanitize_structured_advice_empty_adds_deterministic_replacement(self) -> None:
        """Если все рекомендации отсеялись — детерминированно подставляется замена без LLM."""
        owned = {"black_king_bar", "manta_style", "butterfly", "satanic"}
        raw_advice = [
            {"item": "Black King Bar", "why": "защита"},
            {"item": "Manta Style", "why": "диспел"},
        ]
        sanitized = sanitize_structured_item_advice(
            raw_advice, owned, hero_name="Phantom Assassin", role=1
        )
        self.assertTrue(len(sanitized) > 0)
        self.assertNotIn(sanitized[0]["item"], ["Black King Bar", "Manta Style"])

    def test_sanitize_voice_advice_rewrites_hallucination_without_llm(self) -> None:
        """Короткая голосовая фраза 'Купи бэкабэ', когда BKB в стэше, детерминированно исправляется."""
        owned = {"black_king_bar"}
        text = "Купи бэкабэ чтобы пережить фокус."
        sanitized = sanitize_voice_or_chat_advice_text(
            text, owned, hero_name="Sven", role=1
        )
        self.assertIn("Поправка:", sanitized)
        self.assertIn("уже куплен", sanitized)
        self.assertIn("Следующий нужный слот:", sanitized)


class BackwardsCompatibilityAndContractGoldenEvals(unittest.TestCase):
    """Проверка сохранности 127 ключей базы героев и хуков Coach."""

    def test_meta_heroes_db_contains_at_least_127_heroes(self) -> None:
        """База данных META_HEROES_DB сохраняет все 127 героев Dota 2."""
        self.assertGreaterEqual(len(META_HEROES_DB), 127)

    def test_coach_facade_hooks_and_tools_exist(self) -> None:
        """Все фасадные методы Coach сохраняют сигнатуры и доступность."""
        api_mock = MagicMock()
        api_mock.heroes = []
        api_mock.items.return_value = {}
        api_mock.item_cost.return_value = 0
        catalog = HeroCatalog([])
        cfg = Config()

        coach = Coach(api_mock, catalog, None, cfg.llm, cfg.player_profile)
        self.assertTrue(hasattr(coach, "_knowledge_fallback"))
        self.assertTrue(hasattr(coach, "_fallback_match_analysis"))
        self.assertTrue(hasattr(coach, "_free_pollinations_chat"))
        self.assertTrue(hasattr(coach, "execute_tool"))
        self.assertTrue(hasattr(coach, "enemy_state_tracker"))

        # Проверка вызова confirm_enemy_item через execute_tool
        res = coach.execute_tool("confirm_enemy_item", {"hero": "Axe", "item": "Blink Dagger"})
        self.assertIn("подтверждён", res)

        # Проверка get_enemy_states через execute_tool
        states_text = coach.execute_tool("get_enemy_states", {})
        self.assertIn("Axe", states_text)

        # Проверка dismiss_enemy_item через execute_tool
        dismiss_res = coach.execute_tool("dismiss_enemy_item", {"hero": "Axe", "item": "Blink Dagger"})
        self.assertIn("Сброшено", dismiss_res)

        coach.close()


if __name__ == "__main__":
    unittest.main()
