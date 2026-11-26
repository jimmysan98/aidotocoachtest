# Web (`aidota-core/src/web`)

## Overview
Axum HTTP, WebSocket, and static file server powering the HUD dashboard API and asset delivery. Exposes health status, live match snapshots, manual Roshan kill registration, WebSocket snapshot streaming, and static UI directories with permissive CORS.

## Interface
- `[mod.rs:server]` — Public submodule exposing the web server router.
- `[server.rs:web_router]` — `pub fn web_router(state: Arc<RwLock<AppState>>) -> Router` configuring:
  - `GET /api/status` (`[server.rs:handle_status]`)
  - `GET /api/live` (`[server.rs:handle_live]`)
  - `GET /api/ws` (`[server.rs:handle_ws]` upgrading to `[server.rs:handle_socket]`)
  - `POST /api/roshan/kill` (`[server.rs:handle_roshan_kill]`)
  - Static directory services at `/assets`, `/hero_icons`, and fallback `/`
  - `CorsLayer::permissive()` middleware

## Invariants
- `[server.rs:web_router]` derives the asset base path from `guard.web_dir.parent().unwrap_or(&guard.web_dir)` under a short-lived read lock, mounting `base.join("assets")` at `/assets`, `base.join("hero_icons")` at `/hero_icons`, and `web_dir` as the fallback `ServeDir`.
- `[server.rs:handle_status]` always responds with `200 OK` JSON containing `status: "ok"`, `core: "aidota-core (Rust)"`, and `version: env!("CARGO_PKG_VERSION")`.
- `[server.rs:handle_live]` clones `live_snapshot` under a read lock and returns `200 OK` with either the snapshot JSON or `{"status": "waiting_for_match", ...}` when `live_snapshot` is `None`.
- `[server.rs:handle_roshan_kill]` reads `last_gsi.map.clock_time` (defaulting to `0` if absent), sets `guard.roshan_kill_time = Some(current_clock)`, and returns `window_min_sec = current_clock + 480` and `window_max_sec = current_clock + 660`; it does not immediately recompute `live_snapshot` or push to `tx_broadcast` (the updated kill timestamp takes effect on the next `AppState::update_gsi` call).
- `[server.rs:handle_socket]` subscribes to `tx_broadcast`, immediately pushes the current `live_snapshot` JSON (if `Some`) on connect, and forwards subsequent broadcast messages as `Message::Text` frames until a send failure or broadcast receiver error ends the loop.

## Side Effects
- `[server.rs:web_router]`, `[server.rs:handle_live]`, and `[server.rs:handle_socket]` acquire `parking_lot::RwLock` read locks on `AppState`.
- `[server.rs:handle_roshan_kill]` acquires a `parking_lot::RwLock` write lock on `AppState` to set `roshan_kill_time` and logs via `tracing::info!`.
- `[server.rs:web_router]` reads static files from disk (`web_dir`, `../assets`, `../hero_icons`) via `tower_http::services::ServeDir`.
- `[server.rs:handle_ws]` / `[server.rs:handle_socket]` upgrades HTTP connections to WebSockets and transmits `Message::Text` frames from `tx_broadcast`.

## Verification
- Run: `cargo test` (in `aidota-core/`)
