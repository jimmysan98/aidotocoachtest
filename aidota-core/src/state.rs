use crate::advice::counter_items::ItemAdvice;
use crate::gsi::model::GsiPayload;
use crate::timers::macro_timer::MacroTimers;
use parking_lot::RwLock;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::broadcast;

#[derive(Debug, Clone, Serialize)]
pub struct LiveSnapshot {
    pub hero_name: String,
    pub hero_level: u32,
    pub kills: u32,
    pub deaths: u32,
    pub assists: u32,
    pub gold: u32,
    pub net_worth: u32,
    pub last_hits: u32,
    pub denies: u32,
    pub clock_time: i64,
    pub is_daytime: bool,
    pub radiant_score: u32,
    pub dire_score: u32,
    pub roshan_status: String,
    pub timers: MacroTimers,
    pub advice: Vec<ItemAdvice>,
    pub alerts: Vec<String>,
}

pub struct AppState {
    pub auth_token: String,
    pub web_dir: PathBuf,
    pub last_gsi: Option<GsiPayload>,
    pub roshan_kill_time: Option<i64>,
    pub live_snapshot: Option<LiveSnapshot>,
    pub tx_broadcast: broadcast::Sender<String>,
}

impl AppState {
    pub fn new(auth_token: String, web_dir: PathBuf) -> (Arc<RwLock<Self>>, broadcast::Sender<String>) {
        let (tx, _rx) = broadcast::channel(100);
        let state = Self {
            auth_token,
            web_dir,
            last_gsi: None,
            roshan_kill_time: None,
            live_snapshot: None,
            tx_broadcast: tx.clone(),
        };
        (Arc::new(RwLock::new(state)), tx)
    }

    pub fn update_gsi(&mut self, payload: GsiPayload) {
        let clock_time = payload
            .map
            .as_ref()
            .and_then(|m| m.clock_time)
            .unwrap_or(0);

        let timers = MacroTimers::compute(clock_time, self.roshan_kill_time);

        // Извлекаем текущие предметы игрока
        let owned_items: Vec<String> = payload
            .items
            .as_ref()
            .map(|items| {
                items
                    .values()
                    .filter_map(|it| it.name.clone())
                    .filter(|n| n != "empty")
                    .collect()
            })
            .unwrap_or_default();

        let current_gold = payload
            .player
            .as_ref()
            .and_then(|p| p.gold)
            .unwrap_or(0);

        let hero_clean = payload.clean_hero_name();
        let hero_level = payload
            .hero
            .as_ref()
            .and_then(|h| h.level)
            .unwrap_or(1);

        let kills = payload.player.as_ref().and_then(|p| p.kills).unwrap_or(0);
        let deaths = payload.player.as_ref().and_then(|p| p.deaths).unwrap_or(0);
        let assists = payload.player.as_ref().and_then(|p| p.assists).unwrap_or(0);
        let last_hits = payload.player.as_ref().and_then(|p| p.last_hits).unwrap_or(0);
        let denies = payload.player.as_ref().and_then(|p| p.denies).unwrap_or(0);
        let net_worth = payload
            .player
            .as_ref()
            .and_then(|p| p.net_worth)
            .unwrap_or(current_gold);

        let radiant_score = payload.map.as_ref().and_then(|m| m.radiant_score).unwrap_or(0);
        let dire_score = payload.map.as_ref().and_then(|m| m.dire_score).unwrap_or(0);

        // Расчёт советов по закупке
        let advice = crate::advice::counter_items::AdviceEngine::build_advice(
            &hero_clean,
            current_gold,
            &owned_items,
            &[], // враги из draft при наличии
        );

        let alerts = timers.active_alerts.clone();
        let roshan_status = timers.roshan_status.clone();
        let is_daytime = timers.is_daytime;

        let snapshot = LiveSnapshot {
            hero_name: hero_clean,
            hero_level,
            kills,
            deaths,
            assists,
            gold: current_gold,
            net_worth,
            last_hits,
            denies,
            clock_time,
            is_daytime,
            radiant_score,
            dire_score,
            roshan_status,
            timers,
            advice,
            alerts,
        };

        if let Ok(json_str) = serde_json::to_string(&snapshot) {
            let _ = self.tx_broadcast.send(json_str);
        }

        self.live_snapshot = Some(snapshot);
        self.last_gsi = Some(payload);
    }
}
