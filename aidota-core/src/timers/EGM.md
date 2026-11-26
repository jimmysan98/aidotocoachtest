# Timers (`aidota-core/src/timers`)

## Overview
**FROZEN LEGACY MODULE (Phase 4 / Fork F2):** Deterministic Dota 2 macro objective timer and alert calculator kept only for Rust C-ABI / `cargo test` compatibility. The canonical single source of truth for map timers, neutral item schedules (7/15/25/35/60 min + Turbo), Roshan windows, and edge-triggered alerts is `aidota/map_timers.py`. Do not add new game rules here.

## Interface
- `[mod.rs:macro_timer]` — Public submodule exposing macro timer calculations.
- `[macro_timer.rs:MacroTimers]` — `Debug`, `Clone`, `Serialize`, `Deserialize`, `Default` struct with fields `clock_time: i64`, `is_daytime: bool`, `day_night_remaining_sec: i64`, `wisdom_runes_sec: i64`, `wisdom_runes_next_min: i64`, `water_runes_sec: Option<i64>`, `power_runes_sec: i64`, `bounty_runes_sec: i64`, `lotus_pool_sec: i64`, `tormentor_sec: Option<i64>`, `stack_pull_sec: i64`, `roshan_status: String`, and `active_alerts: Vec<String>`.
- `[macro_timer.rs:MacroTimers::compute]` — `pub fn compute(clock_time: i64, roshan_killed_at: Option<i64>) -> Self`.

## Invariants
- `[macro_timer.rs:MacroTimers::compute]` stores the raw `clock_time` in `MacroTimers.clock_time`, while clamping negative pre-horn values (`clock_time < 0`) to `0` (`normalized_time`) for day/night (`300s`), wisdom (`420s`), bounty (`180s`), and stack/pull (`:53` per minute) modulo arithmetic.
- `[macro_timer.rs:MacroTimers::compute]` evaluates water runes (`120s` and `240s`, `None` for `clock_time >= 240`), power runes (starts at `360s`, then every `120s`), lotus pool (`180 - clock_time` before `180s`, else `180 - (clock_time % 180)`), and Tormentor (`1200s`, `None` for `clock_time >= 1200`) directly against `clock_time`.
- `[macro_timer.rs:MacroTimers::compute]` appends alert strings to `active_alerts` only within fixed trigger thresholds: day/night (`day_night_remaining_sec <= 20`), wisdom runes (`wisdom_runes_sec <= 30 && clock_time > 0`), water runes (`sec <= 20`), power runes (`power_runes_sec <= 25 && clock_time >= 300`), and Tormentor (`(1170..=1200).contains(&clock_time)`).
- `[macro_timer.rs:MacroTimers::compute]` formats `roshan_status` from `elapsed = clock_time - kill_time` using a `480s` minimum respawn and `660s` maximum respawn window; if `roshan_killed_at` is `None` or `elapsed > 660`, Roshan is reported alive.

## Side Effects
- None (pure deterministic calculation; no I/O, locks, or state mutations).

## Verification
- Run: `cargo test` (in `aidota-core/`)
