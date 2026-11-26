# Source Core (`aidota-core/src`)

## Overview
Root Rust source tree for the **frozen legacy** `aidota-core` crate, providing both the C-ABI library crate (`lib.rs`, compiled as `aidota_rust`) and the standalone async GSI/Web server binary (`main.rs`, compiled as `aidota-core`), coordinated around shared `AppState` and `LiveSnapshot` state management (`state.rs`) and four domain submodules. Tactical duplicates in `timers/macro_timer.rs` and `advice/counter_items.rs` are officially frozen per Phase 4 / Fork F2 (`aidota/map_timers.py`, `aidota/live_advice.py`, and `aidota/meta_db.py` are the SSOT).

## Interface
- `[lib.rs:advice]`, `[lib.rs:gsi]`, `[lib.rs:state]`, `[lib.rs:timers]`, `[lib.rs:web]` — Public module exports exposed by the library crate (`lib.rs`); `main.rs` declares the same five modules privately for the binary target.
- `[lib.rs:aidota_compute_timers_json]` — `#[no_mangle] pub extern "C" fn aidota_compute_timers_json(clock_time: i64, roshan_kill_time: i64) -> *mut c_char`. Computes `MacroTimers` and returns a heap-allocated NUL-terminated JSON C string.
- `[lib.rs:aidota_build_advice_json]` — `#[no_mangle] pub extern "C" fn aidota_build_advice_json(hero_name: *const c_char, gold: u32, items_json: *const c_char, enemies_json: *const c_char) -> *mut c_char`. Computes `Vec<ItemAdvice>` from JSON array inputs and returns a heap-allocated NUL-terminated JSON C string.
- `[lib.rs:aidota_free_string]` — `#[no_mangle] pub extern "C" fn aidota_free_string(s: *mut c_char)`. Reclaims and frees strings previously returned by the C-ABI exports.
- `[state.rs:LiveSnapshot]` — `Debug`, `Clone`, `Serialize` struct representing the computed match snapshot (`hero_name`, `hero_level`, `kills`, `deaths`, `assists`, `gold`, `net_worth`, `last_hits`, `denies`, `clock_time`, `is_daytime`, `radiant_score`, `dire_score`, `roshan_status`, `timers: MacroTimers`, `advice: Vec<ItemAdvice>`, `alerts: Vec<String>`).
- `[state.rs:AppState]` — Shared runtime state holding `auth_token: String`, `web_dir: PathBuf`, `last_gsi: Option<GsiPayload>`, `roshan_kill_time: Option<i64>`, `live_snapshot: Option<LiveSnapshot>`, and `tx_broadcast: broadcast::Sender<String>`.
- `[state.rs:AppState::new]` — `pub fn new(auth_token: String, web_dir: PathBuf) -> (Arc<RwLock<Self>>, broadcast::Sender<String>)`.
- `[state.rs:AppState::update_gsi]` — `pub fn update_gsi(&mut self, payload: GsiPayload)`.
- `[main.rs:main]` — Async Tokio entrypoint configuring `tracing_subscriber`, resolving `web_dir`, initializing `AppState`, and running the GSI and Web Axum servers concurrently.
- Subdirectories:
  - [Advice (`./advice/EGM.md`)](./advice/EGM.md) — Counter-item recommendation types and `AdviceEngine::build_advice`.
  - [GSI (`./gsi/EGM.md`)](./gsi/EGM.md) — `GsiPayload` telemetry schema and `POST /` ingestion router (`gsi_router`).
  - [Timers (`./timers/EGM.md`)](./timers/EGM.md) — `MacroTimers::compute` deterministic objective countdowns and alerts.
  - [Web (`./web/EGM.md`)](./web/EGM.md) — `web_router` HTTP API (`/api/status`, `/api/live`, `/api/ws`, `/api/roshan/kill`) and static asset serving.

## Invariants
- `[lib.rs:aidota_compute_timers_json]` maps negative `roshan_kill_time` (`< 0`) to `None` and `>= 0` to `Some(roshan_kill_time)`, falling back to `"{}"` on JSON serialization failure.
- `[lib.rs:aidota_build_advice_json]` treats null `hero_name`, `items_json`, or `enemies_json` pointers (and invalid UTF-8 or malformed JSON arrays) as `""` or empty `Vec<String>`, falling back to `"[]"`; non-null pointers must point to valid NUL-terminated C strings.
- `[lib.rs:aidota_free_string]` must be called by FFI callers to free non-null pointers returned by `aidota_compute_timers_json` and `aidota_build_advice_json`, and must never be called twice on the same pointer; passing null is a safe no-op.
- `[state.rs:AppState::new]` initializes `tx_broadcast` with capacity `100`; slow WebSocket subscribers that lag beyond 100 unread snapshots drop intermediate frames.
- `[state.rs:AppState::update_gsi]` defaults missing `clock_time` and `gold` to `0`, missing `hero_level` to `1`, missing `net_worth` to `current_gold`, filters out `"empty"` item slots, and currently passes an empty `&[]` slice for `enemy_heroes` to `AdviceEngine::build_advice`.
- `[main.rs:main]` resolves `web_dir` by probing `./aidota/web`, `./web`, and `../aidota/web` before defaulting to `"web"`, reads `AIDOTA_TOKEN` (default `"aidota-local-token"`), and binds both servers strictly to loopback `127.0.0.1` (GSI on port `3000`, Web UI/API on port `8080`).
- `[main.rs:main]` shuts down when either spawned server task terminates or on `Ctrl+C` via `tokio::select!`.

## Side Effects
- `[lib.rs:aidota_compute_timers_json]` and `[lib.rs:aidota_build_advice_json]` allocate heap memory via `CString::into_raw` (`[lib.rs:aidota_free_string]` deallocates via `CString::from_raw`).
- `[state.rs:AppState::update_gsi]` mutates `live_snapshot` and `last_gsi`, and publishes serialized JSON snapshots onto `tx_broadcast` (ignoring errors when no subscribers are connected).
- `[main.rs:main]` initializes global `tracing_subscriber`, probes the filesystem for `web_dir`, reads `AIDOTA_TOKEN`, binds TCP listeners on `127.0.0.1:3000` and `127.0.0.1:8080`, and on Windows (`#[cfg(target_os = "windows")]`) spawns `cmd /C start http://127.0.0.1:8080`.

## Verification
- Run: `cargo test` (in `aidota-core/`)
