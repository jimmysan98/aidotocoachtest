# aidota-core

## Overview
Cargo crate root for the **frozen legacy** AIDOTA Rust engine (`[Cargo.toml:package]` `name = "aidota-core"`, `version = "1.3.0"`, `edition = "2021"`), grouping the C-ABI library target, standalone async GSI/Web server binary, and domain submodules under `./src/`.

> **Phase 4 / Fork F2 Freeze Notice:** Canonical map-timer and counter-item tactical logic now lives strictly in Python (`aidota/map_timers.py`, `aidota/live_advice.py`, `aidota/meta_db.py`). `aidota-core/` tactical duplicates (`src/timers/macro_timer.rs`, `src/advice/counter_items.rs`) are officially frozen for compatibility/build stability only and must not evolve independently.

### Cargo Targets & Dependencies
- **Package** (`[Cargo.toml:package]`): `aidota-core` `v1.3.0` (`edition = "2021"`, `authors = ["AIDOTA Team"]`) — "High-performance Rust core and GSI engine for AIDOTA".
- **Library Target** (`[Cargo.toml:lib]`): `name = "aidota_rust"`, `crate-type = ["cdylib", "rlib"]` (default root `src/lib.rs`), exporting C-ABI JSON functions (`aidota_compute_timers_json`, `aidota_build_advice_json`, `aidota_free_string`) and public Rust modules.
- **Binary Target** (`[Cargo.toml:bin]`): `name = "aidota-core"`, `path = "src/main.rs"`, running the Tokio runtime with the Dota 2 GSI listener (`127.0.0.1:3000`) and HUD Web/WebSocket dashboard server (`127.0.0.1:8080`).
- **Crate Dependencies** (`[Cargo.toml:dependencies]`): `tokio` (`1.40`, `full`), `axum` (`0.7`, `ws`), `tower-http` (`0.5`, `fs`, `cors`), `serde` (`1.0`, `derive`), `serde_json` (`1.0`), `tracing` (`0.1`), `tracing-subscriber` (`0.3`, `env-filter`), `parking_lot` (`0.12`), and `chrono` (`0.4`).

## Subdirectories & Boundaries
- `src`: [./src/EGM.md](./src/EGM.md) - Core Rust source package containing the C-ABI library entrypoint (`lib.rs`), binary entrypoint (`main.rs`), shared `AppState` / `LiveSnapshot` state coordinator (`state.rs`), and domain submodules (`advice`, `gsi`, `timers`, `web`).

## Verification
- Run: `cargo test` (in `aidota-core/`)
