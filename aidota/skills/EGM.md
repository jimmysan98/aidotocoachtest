# aidota/skills

## Overview
Modular tactical coaching skills package providing stateless lane-matchup advice and stateful GSI game-clock trackers for Roshan, enemy ultimates, and recurring macro objectives.

## Interface
- `[__init__.py:__all__]` — exports `MacroTimerSkill`, `CooldownTrackerSkill`, `LaneMatchupSkill`.
- `[cooldown_tracker.py:ULTIMATE_COOLDOWNS]` — lookup table mapping 7 canonical enemy ultimate keys (`ravage`, `chronosphere`, `black_hole`, `global_silence`, `doom`, `echo_slam`, `arena_of_blood`) to display names and cooldown durations in seconds.
- `[cooldown_tracker.py:UltEntry]` — `@dataclass` holding `key`, `name`, `start_sec`, `duration`, `alerted_30s`, and `is_ready_alerted`.
- `[cooldown_tracker.py:CooldownTrackerSkill]` — `__init__(enabled=True)`, `reset()`, `record_roshan(clock_seconds=None) -> str`, `record_ult(ability_key, clock_seconds=None) -> str` (alias `record_enemy_ult`), `check(clock_seconds=None) -> str | None` (alias `check_alerts`), `_parse_seconds(clock_seconds) -> float`.
- `[lane_matchup.py:LaneMatchupSkill]` — `__init__(enabled=True)`, `get_plan(position=1, hero_name=None, enemy_hero=None, is_turbo=False, enemy_heroes=None) -> str` (alias `get_lane_plan`); resolves role labels via `..roles.position_label` and matches enemy tips from `_POSITION_TIPS` and `_ENEMY_SPECIFIC_TIPS`.
- `[macro_timer.py:MacroTimerSkill]` — `__init__(enabled=True)`, `add_custom_timer(target_seconds, label) -> None`, `reset() -> None`, `check(clock_seconds, position=1, team_name=None, enemy_mid_level=None) -> str | None`.
- Subdirectories: none (leaf package).

## Invariants
- `[cooldown_tracker.py:CooldownTrackerSkill.check]` returns `None` immediately when `enabled` is `False`; if `clock_seconds < roshan_death_sec` (`elapsed < 0`), it assumes a new match started, calls `reset()`, and returns `None`.
- `[cooldown_tracker.py:CooldownTrackerSkill.record_ult]` maps hero aliases (`tidehunter`, `enigma`, `faceless_void`, `void`, `silencer`, `earthshaker`, `shaker`, `mars`) to `ULTIMATE_COOLDOWNS` keys, defaults falsy `ability_key` to `"ravage"`, and falls back to `120` seconds for unknown ability keys.
- `[cooldown_tracker.py:CooldownTrackerSkill.check]` triggers each Roshan alert at most once per death in fixed elapsed windows (`[270, 310)` Aegis -30s, `[450, 490)` min respawn -30s, `[630, 670)` max respawn -30s) and pops tracked ultimates once `elapsed < 0` or `elapsed >= duration`.
- `[cooldown_tracker.py:CooldownTrackerSkill._parse_seconds]` falls back to `time.time()` whenever `clock_seconds` is `None` or cannot be converted via `float()`.
- `[lane_matchup.py:LaneMatchupSkill.get_plan]` clamps any `position` outside `1..5` to `1` and matches `_ENEMY_SPECIFIC_TIPS` by substring against lowercased enemy hero strings before falling back to a generic cooldown reminder.
- `[macro_timer.py:MacroTimerSkill.check]` returns `None` if `enabled` is `False`, `clock_seconds` is `None`/non-integer-convertible, or `int(clock_seconds) < 0` (resetting tracked alert sets when `sec < 0` and prior alerts exist).
- `[macro_timer.py:MacroTimerSkill.check]` evaluates triggers in strict priority order and returns at most one message per tick: custom timers (`sec >= target_sec`, popped on trigger) -> enemy mid level 6 (`enemy_mid_level >= 6` or `329 <= sec <= 334`) -> Tormentor (`1179 <= sec <= 1182`) -> Wisdom runes (`sec >= 400`, `(sec + 20) % 420 in (0, 1)`) -> Lotuses (`sec >= 160`, `(sec + 20) % 180 in (0, 1)`) -> Pos 4/5 pulls (`60 <= sec <= 480`, `sec % 60 in (36, 37, 38)`) -> Neutral stacks (`50 <= sec <= 1500`, `sec % 60 in (52, 53)`).

## Side Effects
- `[cooldown_tracker.py:CooldownTrackerSkill]` mutates in-memory `roshan_death_sec`, `_roshan_*_alerted` flags, and `tracked_ults` dict, and emits INFO logs to logger `aidota.skills.cooldown_tracker`.
- `[macro_timer.py:MacroTimerSkill]` mutates in-memory alert tracking sets/booleans (`_alerted_wisdom`, `_alerted_lotus`, `_alerted_stacks`, `_alerted_pulls`, `_alerted_tormentor`, `_alerted_mid_lvl6`) and `_custom_timers` list, and emits INFO logs to logger `aidota.skills.macro_timer`.
- `[lane_matchup.py]` creates module logger `aidota.skills.lane_matchup`; `get_plan` performs no disk, network, or state mutations.

## Verification
- Run: `.venv\Scripts\python.exe tools/run_tests.py`
- Or: `.venv\Scripts\python.exe -m unittest discover -s tests`
