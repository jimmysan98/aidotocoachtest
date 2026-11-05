use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct MacroTimers {
    pub clock_time: i64,
    pub is_daytime: bool,
    pub day_night_remaining_sec: i64,
    pub wisdom_runes_sec: i64,
    pub wisdom_runes_next_min: i64,
    pub water_runes_sec: Option<i64>,
    pub power_runes_sec: i64,
    pub bounty_runes_sec: i64,
    pub lotus_pool_sec: i64,
    pub tormentor_sec: Option<i64>,
    pub stack_pull_sec: i64,
    pub roshan_status: String,
    pub active_alerts: Vec<String>,
}

impl MacroTimers {
    pub fn compute(clock_time: i64, roshan_killed_at: Option<i64>) -> Self {
        let mut alerts = Vec::new();

        // 1. День / Ночь (каждые 300 секунд = 5 минут)
        let normalized_time = if clock_time < 0 { 0 } else { clock_time };
        let cycle_index = normalized_time / 300;
        let is_daytime = cycle_index % 2 == 0;
        let day_night_remaining_sec = 300 - (normalized_time % 300);

        if day_night_remaining_sec <= 20 {
            let next_phase = if is_daytime { "ночь" } else { "день" };
            alerts.push(format!("Через {} сек наступит {}", day_night_remaining_sec, next_phase));
        }

        // 2. Руны мудрости (7:00 = 420s, 14:00 = 840s, 21:00 = 1260s...)
        let wisdom_interval = 420;
        let wisdom_runes_sec = wisdom_interval - (normalized_time % wisdom_interval);
        let wisdom_runes_next_min = ((normalized_time / wisdom_interval) + 1) * 7;

        if wisdom_runes_sec <= 30 && clock_time > 0 {
            alerts.push(format!("Руны мудрости через {} сек (минута {})", wisdom_runes_sec, wisdom_runes_next_min));
        }

        // 3. Водные руны (только 2:00 = 120s и 4:00 = 240s на миду)
        let water_runes_sec = if clock_time < 120 {
            Some(120 - clock_time)
        } else if clock_time < 240 {
            Some(240 - clock_time)
        } else {
            None
        };

        if let Some(sec) = water_runes_sec {
            if sec <= 20 {
                alerts.push(format!("Водные руны на миду через {} сек", sec));
            }
        }

        // 4. Активные руны (с 6:00 = 360s, каждые 120s)
        let power_runes_sec = if clock_time < 360 {
            360 - clock_time
        } else {
            120 - ((clock_time - 360) % 120)
        };

        if power_runes_sec <= 25 && clock_time >= 300 {
            alerts.push(format!("Активные руны на реке через {} сек", power_runes_sec));
        }

        // 5. Руны богатства (каждые 3 минуты = 180s)
        let bounty_runes_sec = 180 - (normalized_time % 180);

        // 6. Пруд лотосов (каждые 3 минуты = 180s, начиная с 3:00)
        let lotus_pool_sec = if clock_time < 180 {
            180 - clock_time
        } else {
            180 - (clock_time % 180)
        };

        // 7. Терзатель (20:00 = 1200s)
        let tormentor_sec = if clock_time < 1200 {
            Some(1200 - clock_time)
        } else {
            None
        };

        if let Some(sec) = tormentor_sec {
            if (1170..=1200).contains(&clock_time) {
                alerts.push(format!("Появление Терзателя через {} сек", sec));
            }
        }

        // 8. Тайминг отводов и стаков (каждую минуту на 53-55 сек)
        let sec_in_min = normalized_time % 60;
        let stack_pull_sec = if sec_in_min <= 53 {
            53 - sec_in_min
        } else {
            60 - sec_in_min + 53
        };

        // 9. Рошан
        let roshan_status = match roshan_killed_at {
            Some(kill_time) => {
                let elapsed = clock_time - kill_time;
                let min_respawn = 480; // 8 минут
                let max_respawn = 660; // 11 минут
                if elapsed < min_respawn {
                    let left = min_respawn - elapsed;
                    format!("Рошан мёртв (окно через {:02}:{:02})", left / 60, left % 60)
                } else if elapsed <= max_respawn {
                    let left = max_respawn - elapsed;
                    format!("ОКНО РОШАНА (до закрытия {:02}:{:02})", left / 60, left % 60)
                } else {
                    "Рошан жив".to_string()
                }
            }
            None => "Рошан жив (тайминг не зафиксирован)".to_string(),
        };

        Self {
            clock_time,
            is_daytime,
            day_night_remaining_sec,
            wisdom_runes_sec,
            wisdom_runes_next_min,
            water_runes_sec,
            power_runes_sec,
            bounty_runes_sec,
            lotus_pool_sec,
            tormentor_sec,
            stack_pull_sec,
            roshan_status,
            active_alerts: alerts,
        }
    }
}
