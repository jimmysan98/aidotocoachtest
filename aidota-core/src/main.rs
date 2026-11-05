mod advice;
mod gsi;
mod state;
mod timers;
mod web;

use crate::state::AppState;
use std::net::SocketAddr;
use std::path::PathBuf;
use tracing::info;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 1. Инициализация красивого логирования
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "aidota_core=info,tower_http=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    println!(
        r#"
    ╔═══════════════════════════════════════════════════════════╗
    ║       ⚡ AIDOTA CORE (RUST ENGINE v1.3.0) ⚡               ║
    ║   High-Performance Zero-GC Dota 2 Coach & GSI Backend    ║
    ╚═══════════════════════════════════════════════════════════╝
    "#
    );

    // 2. Определение путей к веб-интерфейсу (HTML/CSS)
    let current_dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let web_dir = if current_dir.join("aidota").join("web").exists() {
        current_dir.join("aidota").join("web")
    } else if current_dir.join("web").exists() {
        current_dir.join("web")
    } else if current_dir.parent().map(|p| p.join("aidota").join("web").exists()).unwrap_or(false) {
        current_dir.parent().unwrap().join("aidota").join("web")
    } else {
        PathBuf::from("web")
    };

    info!("Директория веб-интерфейса (HTML/CSS): {:?}", web_dir);

    // 3. Создание общего состояния приложения
    let auth_token = std::env::var("AIDOTA_TOKEN").unwrap_or_else(|_| "aidota-local-token".to_string());
    let (state, _) = AppState::new(auth_token, web_dir);

    // 4. Запуск Dota 2 GSI сервера (порт 3000)
    let gsi_addr = SocketAddr::from(([127, 0, 0, 1], 3000));
    let gsi_app = gsi::server::gsi_router(state.clone());
    info!("🚀 Dota 2 GSI Сервер запущен на http://{}", gsi_addr);

    let gsi_handle = tokio::spawn(async move {
        let listener = tokio::net::TcpListener::bind(gsi_addr).await.expect("Не удалось привязать GSI порт 3000");
        axum::serve(listener, gsi_app).await.expect("Ошибка в работе GSI сервера");
    });

    // 5. Запуск Веб-интерфейса (HTML/CSS Dashboard на порту 8080)
    let web_addr = SocketAddr::from(([127, 0, 0, 1], 8080));
    let web_app = web::server::web_router(state.clone());
    info!("🌐 Веб-интерфейс (HUD Дашборд) доступен: http://{}", web_addr);

    let web_handle = tokio::spawn(async move {
        let listener = tokio::net::TcpListener::bind(web_addr).await.expect("Не удалось привязать веб-порт 8080");
        axum::serve(listener, web_app).await.expect("Ошибка в работе Веб сервера");
    });

    // 6. Автоматическое открытие интерфейса в браузере
    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("cmd")
            .args(["/C", "start", "http://127.0.0.1:8080"])
            .spawn();
    }

    println!("\n  >>> Интерфейс открыт на http://127.0.0.1:8080 (Ctrl+C для выхода)\n");

    // Ожидание завершения
    tokio::select! {
        _ = gsi_handle => {},
        _ = web_handle => {},
        _ = tokio::signal::ctrl_c() => {
            info!("Получен сигнал завершения. Остановка AIDOTA Core...");
        }
    }

    Ok(())
}
