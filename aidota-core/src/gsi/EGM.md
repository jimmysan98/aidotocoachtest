# GSI (`aidota-core/src/gsi`)

## Overview
Dota 2 Game State Integration (GSI) telemetry model and Axum HTTP ingestion router. Deserializes incoming JSON match payloads, validates the configured auth token, and updates shared `AppState`.

## Interface
- `[mod.rs:model]` — Public submodule defining GSI JSON payload types and helper methods.
- `[mod.rs:server]` — Public submodule providing the GSI HTTP router.
- `[model.rs:Auth]`, `[model.rs:Provider]`, `[model.rs:MapState]`, `[model.rs:PlayerState]`, `[model.rs:HeroState]`, `[model.rs:ItemSlot]`, `[model.rs:AbilitySlot]` — `Debug`, `Clone`, `Serialize`, `Deserialize`, `Default` schema structs with `Option<...>` fields modeling Dota 2 GSI JSON sections.
- `[model.rs:GsiPayload]` — Root telemetry payload struct containing `auth`, `provider`, `map`, `player`, `hero`, `abilities: Option<HashMap<String, AbilitySlot>>`, `items: Option<HashMap<String, ItemSlot>>`, and `draft: Option<serde_json::Value>`.
- `[model.rs:GsiPayload::is_auth_valid]` — `pub fn is_auth_valid(&self, expected_token: &str) -> bool`.
- `[model.rs:GsiPayload::clean_hero_name]` — `pub fn clean_hero_name(&self) -> String`.
- `[server.rs:gsi_router]` — `pub fn gsi_router(state: Arc<RwLock<AppState>>) -> Router` serving `POST /` via `[server.rs:handle_gsi]`.

## Invariants
- `[model.rs:GsiPayload]` and all nested GSI model structs declare every field as `Option<...>` and derive `Default`, tolerating partial payloads.
- `[model.rs:GsiPayload::is_auth_valid]` returns `true` unconditionally when `expected_token.is_empty()`; otherwise requires `auth.token` to match `expected_token` exactly.
- `[model.rs:GsiPayload::clean_hero_name]` strips `"npc_dota_hero_"` if present, lowercases the hero name, and returns `"none"` if `hero` or `hero.name` is missing.
- `[server.rs:handle_gsi]` acquires a read lock solely to evaluate `is_auth_valid`, drops the read guard before acquiring a write lock, returns `StatusCode::FORBIDDEN` (403) without mutating state when unauthorized, and returns `StatusCode::OK` (200) after invoking `AppState::update_gsi`.

## Side Effects
- `[server.rs:handle_gsi]` acquires `parking_lot::RwLock` read and write locks on `Arc<RwLock<AppState>>`.
- `[server.rs:handle_gsi]` mutates `AppState` via `AppState::update_gsi` (updating `last_gsi` and `live_snapshot`, and broadcasting JSON over `tx_broadcast`).
- `[server.rs:handle_gsi]` logs warnings (`tracing::warn!`) for rejected tokens and debug logs (`tracing::debug!`) for accepted payloads.

## Verification
- Run: `cargo test` (in `aidota-core/`)
