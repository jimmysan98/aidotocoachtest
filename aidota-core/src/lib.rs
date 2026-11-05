pub mod advice;
pub mod gsi;
pub mod state;
pub mod timers;
pub mod web;

use std::ffi::{CStr, CString};
use std::os::raw::c_char;

/// Вычисляет все макро-тайминги (руны, Рошан, лотосы, Терзатель) на Rust
#[no_mangle]
pub extern "C" fn aidota_compute_timers_json(clock_time: i64, roshan_kill_time: i64) -> *mut c_char {
    let roshan_opt = if roshan_kill_time >= 0 {
        Some(roshan_kill_time)
    } else {
        None
    };

    let timers = timers::macro_timer::MacroTimers::compute(clock_time, roshan_opt);
    let json_str = serde_json::to_string(&timers).unwrap_or_else(|_| "{}".to_string());
    CString::new(json_str).unwrap_or_default().into_raw()
}

/// Рассчитывает советы по контр-предметам на Rust
#[no_mangle]
pub extern "C" fn aidota_build_advice_json(
    hero_name: *const c_char,
    gold: u32,
    items_json: *const c_char,
    enemies_json: *const c_char,
) -> *mut c_char {
    let hero = if hero_name.is_null() {
        ""
    } else {
        unsafe { CStr::from_ptr(hero_name).to_str().unwrap_or("") }
    };

    let owned_items: Vec<String> = if items_json.is_null() {
        Vec::new()
    } else {
        let raw = unsafe { CStr::from_ptr(items_json).to_str().unwrap_or("[]") };
        serde_json::from_str(raw).unwrap_or_default()
    };

    let enemies: Vec<String> = if enemies_json.is_null() {
        Vec::new()
    } else {
        let raw = unsafe { CStr::from_ptr(enemies_json).to_str().unwrap_or("[]") };
        serde_json::from_str(raw).unwrap_or_default()
    };

    let advice = advice::counter_items::AdviceEngine::build_advice(hero, gold, &owned_items, &enemies);
    let json_str = serde_json::to_string(&advice).unwrap_or_else(|_| "[]".to_string());
    CString::new(json_str).unwrap_or_default().into_raw()
}

/// Освобождает строку, выделенную Rust
#[no_mangle]
pub extern "C" fn aidota_free_string(s: *mut c_char) {
    if !s.is_null() {
        unsafe {
            let _ = CString::from_raw(s);
        }
    }
}
