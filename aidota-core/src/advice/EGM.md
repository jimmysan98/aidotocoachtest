# Advice (`aidota-core/src/advice`)

## Overview
Rule-based counter-item recommendation engine for Dota 2 matches. Evaluates the player's current gold, owned inventory, and enemy hero list to emit prioritized `ItemAdvice` suggestions.

## Interface
- `[mod.rs:counter_items]` — Public submodule exposing counter-item structures and recommendation logic.
- `[counter_items.rs:ItemAdvice]` — Serializable (`Debug`, `Clone`, `Serialize`, `Deserialize`) recommendation record with fields `item_name: String`, `display_name: String`, `cost: u32`, `gold_needed: u32`, `reason: String`, and `priority: u8`.
- `[counter_items.rs:AdviceEngine]` — Unit struct namespace for static counter-item evaluation.
- `[counter_items.rs:AdviceEngine::build_advice]` — `pub fn build_advice(_hero_name: &str, current_gold: u32, owned_items: &[String], enemy_heroes: &[String]) -> Vec<ItemAdvice>`.

## Invariants
- `[counter_items.rs:AdviceEngine::build_advice]` normalizes `owned_items` and `enemy_heroes` to lowercase `HashSet<String>` values for case-insensitive matching; `_hero_name` is unused.
- `[counter_items.rs:AdviceEngine::build_advice]` matches enemy heroes by lowercase substring against five hardcoded rules:
  - `item_black_king_bar` (`Black King Bar`, cost `4050`, priority `95`) when enemies contain `"lion"`, `"lina"`, `"invoker"`, or `"storm"` and `"item_black_king_bar"` is not owned.
  - `item_ghost` (`Ghost Scepter`, cost `1500`, priority `90`) when enemies contain `"juggernaut"` or `"phantom_assassin"` and neither `"item_ghost"` nor `"item_ethereal_blade"` is owned.
  - `item_sphere` (`Linken's Sphere`, cost `4600`, priority `88`) when enemies contain `"doom"`, `"legion"`, or `"bane"` and `"item_sphere"` is not owned.
  - `item_monkey_king_bar` (`Monkey King Bar`, cost `4900`, priority `85`) when enemies contain `"phantom_assassin"` or `"windrunner"` and `"item_monkey_king_bar"` is not owned.
  - `item_spirit_vessel` (`Spirit Vessel`, cost `2980`, priority `82`) when enemies contain `"necrolyte"`, `"alchemist"`, `"morphling"`, or `"bristleback"` and `"item_spirit_vessel"` is not owned.
- `[counter_items.rs:AdviceEngine::build_advice]` computes `gold_needed` via `cost.saturating_sub(current_gold)` so remaining gold never underflows.
- `[counter_items.rs:AdviceEngine::build_advice]` returns recommendations sorted in descending `priority` order.

## Side Effects
- None (pure in-memory computation; no I/O, locks, or state mutations).

## Verification
- Run: `cargo test` (in `aidota-core/`)
