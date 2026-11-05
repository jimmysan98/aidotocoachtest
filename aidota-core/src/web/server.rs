use crate::state::AppState;
use axum::{
    extract::{
        ws::{Message, WebSocket},
        State, WebSocketUpgrade,
    },
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use parking_lot::RwLock;
use serde_json::json;
use std::sync::Arc;
use tower_http::cors::CorsLayer;
use tower_http::services::ServeDir;
use tracing::info;

pub fn web_router(state: Arc<RwLock<AppState>>) -> Router {
    let (web_dir, assets_dir, hero_icons_dir) = {
        let guard = state.read();
        let base = guard.web_dir.parent().unwrap_or(&guard.web_dir).to_path_buf();
        (
            guard.web_dir.clone(),
            base.join("assets"),
            base.join("hero_icons"),
        )
    };

    Router::new()
        .route("/api/status", get(handle_status))
        .route("/api/live", get(handle_live))
        .route("/api/ws", get(handle_ws))
        .route("/api/roshan/kill", post(handle_roshan_kill))
        .nest_service("/assets", ServeDir::new(assets_dir))
        .nest_service("/hero_icons", ServeDir::new(hero_icons_dir))
        .fallback_service(ServeDir::new(web_dir))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

async fn handle_status() -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "core": "aidota-core (Rust)",
        "version": env!("CARGO_PKG_VERSION"),
        "performance": "zero-gc, native-simd"
    }))
}

async fn handle_live(
    State(state): State<Arc<RwLock<AppState>>>,
) -> impl IntoResponse {
    let snapshot = {
        let guard = state.read();
        guard.live_snapshot.clone()
    };

    match snapshot {
        Some(s) => (StatusCode::OK, Json(json!(s))),
        None => (
            StatusCode::OK,
            Json(json!({
                "status": "waiting_for_match",
                "message": "Dota 2 GSI активен. Ожидание подключения матча..."
            })),
        ),
    }
}

async fn handle_roshan_kill(
    State(state): State<Arc<RwLock<AppState>>>,
) -> impl IntoResponse {
    let mut guard = state.write();
    let current_clock = guard
        .last_gsi
        .as_ref()
        .and_then(|g| g.map.as_ref())
        .and_then(|m| m.clock_time)
        .unwrap_or(0);

    guard.roshan_kill_time = Some(current_clock);
    info!("Рошан убит на тайминге: {} сек", current_clock);

    Json(json!({
        "status": "success",
        "roshan_killed_at": current_clock,
        "window_min_sec": current_clock + 480,
        "window_max_sec": current_clock + 660
    }))
}

async fn handle_ws(
    ws: WebSocketUpgrade,
    State(state): State<Arc<RwLock<AppState>>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

async fn handle_socket(mut socket: WebSocket, state: Arc<RwLock<AppState>>) {
    let mut rx = {
        let guard = state.read();
        guard.tx_broadcast.subscribe()
    };

    // При подключении сразу отправляем текущий снапшот
    let initial_msg = {
        let guard = state.read();
        guard.live_snapshot.as_ref().and_then(|s| serde_json::to_string(s).ok())
    };

    if let Some(msg) = initial_msg {
        if socket.send(Message::Text(msg)).await.is_err() {
            return;
        }
    }

    // Слушаем широковещательный канал и пересылаем в веб-сокет
    while let Ok(msg) = rx.recv().await {
        if socket.send(Message::Text(msg)).await.is_err() {
            break;
        }
    }
}
