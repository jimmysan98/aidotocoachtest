use crate::gsi::model::GsiPayload;
use crate::state::AppState;
use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::post,
    Json, Router,
};
use parking_lot::RwLock;
use std::sync::Arc;
use tracing::{debug, warn};

pub fn gsi_router(state: Arc<RwLock<AppState>>) -> Router {
    Router::new()
        .route("/", post(handle_gsi))
        .with_state(state)
}

async fn handle_gsi(
    State(state): State<Arc<RwLock<AppState>>>,
    Json(payload): Json<GsiPayload>,
) -> impl IntoResponse {
    let auth_valid = {
        let guard = state.read();
        payload.is_auth_valid(&guard.auth_token)
    };

    if !auth_valid {
        warn!("GSI: Отклонён запрос с неверным токеном авторизации");
        return StatusCode::FORBIDDEN;
    }

    debug!(
        "GSI: Принят пакет матча. Герой: {}, Время: {:?}",
        payload.clean_hero_name(),
        payload.map.as_ref().and_then(|m| m.clock_time)
    );

    {
        let mut guard = state.write();
        guard.update_gsi(payload);
    }

    StatusCode::OK
}
