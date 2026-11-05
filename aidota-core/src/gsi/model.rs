use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Auth {
    pub token: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Provider {
    pub name: Option<String>,
    pub appid: Option<u32>,
    pub version: Option<u32>,
    pub timestamp: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct MapState {
    pub name: Option<String>,
    pub matchid: Option<String>,
    pub game_time: Option<i64>,
    pub clock_time: Option<i64>,
    pub daytime: Option<bool>,
    pub nightstalker_night: Option<bool>,
    pub game_state: Option<String>,
    pub radiant_score: Option<u32>,
    pub dire_score: Option<u32>,
    pub win_team: Option<String>,
    pub customgamename: Option<String>,
    pub ward_purchase_cooldown: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PlayerState {
    pub steamid: Option<String>,
    pub name: Option<String>,
    pub activity: Option<String>,
    pub kills: Option<u32>,
    pub deaths: Option<u32>,
    pub assists: Option<u32>,
    pub last_hits: Option<u32>,
    pub denies: Option<u32>,
    pub kill_streak: Option<u32>,
    pub commands_issued: Option<u32>,
    pub kill_list: Option<HashMap<String, u32>>,
    pub team_name: Option<String>,
    pub gold: Option<u32>,
    pub gold_reliable: Option<u32>,
    pub gold_unreliable: Option<u32>,
    pub gold_per_min: Option<u32>,
    pub xp_per_min: Option<u32>,
    pub net_worth: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct HeroState {
    pub xpos: Option<f32>,
    pub ypos: Option<f32>,
    pub id: Option<i32>,
    pub name: Option<String>,
    pub level: Option<u32>,
    pub alive: Option<bool>,
    pub respawn_seconds: Option<u32>,
    pub buyback_cost: Option<u32>,
    pub buyback_cooldown: Option<u32>,
    pub health: Option<u32>,
    pub max_health: Option<u32>,
    pub health_percent: Option<f32>,
    pub mana: Option<u32>,
    pub max_mana: Option<u32>,
    pub mana_percent: Option<f32>,
    pub silenced: Option<bool>,
    pub stunned: Option<bool>,
    pub disarmed: Option<bool>,
    pub magicimmune: Option<bool>,
    pub hexed: Option<bool>,
    pub muted: Option<bool>,
    pub break_active: Option<bool>,
    pub has_debuff: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ItemSlot {
    pub name: Option<String>,
    pub purchaser: Option<i32>,
    pub can_cast: Option<bool>,
    pub cooldown: Option<f32>,
    pub passive: Option<bool>,
    pub charges: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AbilitySlot {
    pub name: Option<String>,
    pub level: Option<u32>,
    pub can_cast: Option<bool>,
    pub passive: Option<bool>,
    pub ability_active: Option<bool>,
    pub cooldown: Option<f32>,
    pub ultimate: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GsiPayload {
    pub auth: Option<Auth>,
    pub provider: Option<Provider>,
    pub map: Option<MapState>,
    pub player: Option<PlayerState>,
    pub hero: Option<HeroState>,
    pub abilities: Option<HashMap<String, AbilitySlot>>,
    pub items: Option<HashMap<String, ItemSlot>>,
    pub draft: Option<serde_json::Value>,
}

impl GsiPayload {
    /// Проверяет, валиден ли токен авторизации
    pub fn is_auth_valid(&self, expected_token: &str) -> bool {
        if expected_token.is_empty() {
            return true;
        }
        self.auth
            .as_ref()
            .and_then(|a| a.token.as_deref())
            .map(|t| t == expected_token)
            .unwrap_or(false)
    }

    /// Извлекает короткое имя героя (например, "queenofpain" из "npc_dota_hero_queenofpain")
    pub fn clean_hero_name(&self) -> String {
        self.hero
            .as_ref()
            .and_then(|h| h.name.as_deref())
            .map(|raw| {
                raw.strip_prefix("npc_dota_hero_")
                    .unwrap_or(raw)
                    .to_lowercase()
            })
            .unwrap_or_else(|| "none".to_string())
    }
}
