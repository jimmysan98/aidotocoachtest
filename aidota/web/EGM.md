# aidota/web

## Overview
WebView single-page application frontend (`index.html` + `app.js`) providing 6 interactive coaching tabs (Dashboard, AI Chat, Timers & Radar, Draft & Knowledge Base, Settings, and System Doctor) backed by the Python `window.pywebview.api` RPC bridge. (CSS stylesheets `style.css` and `style-modern.css` are visual-only assets excluded from source symbol scanning.)

## Interface
- `[index.html]` — 6-tab SPA shell (`#tab-dashboard`, `#tab-chat`, `#tab-timers` with accurate neutral tier `7:00 / 15:00 / 25:00 / 35:00 / 60:00` hints, `#tab-meta` with subpanes `#subpane-draft-picker` and `#subpane-hero-builds`, `#tab-settings`, `#tab-doctor`), coach switcher (`#coach-astra-btn`, `#coach-jarvis-btn`), role selector (`.role-btn[data-pos="1..5"]`), live hero/HUD containers (`#hero-name-display`, `#inventory-bar`, `#neutral-item-slot`, `#item-advice-list`, `#advice-feed-list`), splash overlay (`#ui-splash`), and toast stack (`#toast-stack`); legacy `#rust-status-pill` badge removed.
- `[app.js:state]` — singleton frontend state (`coach`, `position`, `assistantRunning`, `gsiConnected`, `heroName`, `heroLevel`, `gameTime`, `isListening`, `isSpeaking`, `roshanDeathTime`, `activeTab`, `config`, `heroIconKey`, `inventorySignature`, `neutralItemSignature`, `adviceSignature`, `adviceFeedSignature`, `settingsLoaded`, `skillLevel`, `skillLevelLoaded`).
- `[app.js:PRESETS]` — 11 LLM preset definitions (`pollinations`, `pollinations_gemini`, `google_gemini`, `google_gemini_lite`, `deepseek`, `deepseek_r1`, `groq`, `groq_fast`, `pollinations_deepseek`, `openrouter`, `ollama`).
- `[app.js:POSITION_LABELS]`, `[app.js:ADVICE_PRIORITY_LABELS]`, `[app.js:ADVICE_PRIORITY_CLASSES]`, `[app.js:SKILL_LEVEL_HINTS]`, `[app.js:TRAINER_TIPS]` — Russian UI labels, priority CSS class mappings, and skill-level tip rotations.
- `[app.js:callApi]` — RPC wrapper around `window.pywebview.api` calling `get_state`, `record_roshan`, `set_coach`, `set_position`, `toggle_assistant`, `toggle_mic`, `test_voice`, `toggle_overlay`, `install_gsi`, `minimize_to_tray`, `send_chat`, `get_settings`, `save_settings`, `test_llm_settings`, `run_doctor`, `add_to_steam`, `get_hero_icon`, `get_item_icon`, `get_meta_heroes_list`, `get_live_draft_heroes`, `get_draft_recommendation`, and `get_hero_build_guide`.
- `[app.js]` UI & rendering helpers — `initWaveVisualizer`, `syncStateLoop`, `switchToTab`, `updateAssistantUI`, `syncHeroIcon`, `setHeroImage`, `renderNetworth`, `renderInventory`, `renderNeutralItem`, `normalizeItemAdvice`, `renderItemAdvice`, `updateItemAdviceLive`, `askCoachAboutItem`, `handleUserMessage`, `appendChatMessage` (uses delegated `.btn-speak-msg`/`.btn-copy-msg` rather than inline `onclick`), `addFeedItem`, `updateMacroTimersDisplay` (consumes Python SSOT `gsi.macro_timers` across `#timer-val-wisdom`, `#timer-val-active-rune`, `#timer-val-tormentor`, `#timer-val-lotus`, `#timer-val-stack`, `#timer-val-neutral`), `updateHeroPositionBadge`, `applySettingsToUi`, `selectPresetCard`, `highlightMatchingPreset`, `refreshDoctorChecks`, `loadMetaHeroes`, `renderEnemyChips`, `addEnemyHero`, `triggerDraftCalculation` (binds `.btn-speak-draft` to `window.speakMessage`), `showToast`, `formatGold`, `truncateText`, `formatTime`, `escapeHtml`, `escapeJsString`.
- `[app.js:window]` exported globals — `window.speakMessage`, `window.copyMessage`, `window.updateSkillLevelHint`, `window.aidotaToast`, and `window.alert` (rebound to `showToast`).
- Subdirectories: none (leaf directory).

## Invariants
- `[app.js:callApi]` checks `window.pywebview && window.pywebview.api && typeof window.pywebview.api[fnName] === "function"` and catches exceptions, returning `null` when the native bridge is absent or errors.
- `[app.js:syncStateLoop]` polls `get_state` every `1000ms` when visible, slows to `1500ms` without invoking `callApi` when `document.hidden` is true, and resets hero portrait, neutral item slot, and item advice UI whenever `gsi.connected` is falsy.
- `[app.js:renderInventory]`, `[app.js:renderNeutralItem]`, and `[app.js:renderItemAdvice]` guard DOM re-renders with `state.inventorySignature`, `state.neutralItemSignature`, and `state.adviceSignature` so identical snapshots do not rebuild nodes.
- `[app.js:normalizeItemAdvice]` / `[app.js:renderItemAdvice]` limit rendered recommendations to 3 items and show each unique `warning` string only once (on the first matching card).
- `[app.js:syncHeroIcon]` / `[app.js:ensureItemIcon]` / `[app.js:ensureAdviceIcons]` cache resolved data URIs in `heroIconCache` and `itemIconCache` and deduplicate concurrent requests via `pendingHeroIcons` and `pendingItemIcons`.
- `[app.js:addEnemyHero]` enforces a maximum of 5 unique `selectedEnemyHeroes`; `[app.js:addFeedItem]` caps `#advice-feed-list` at 20 items; `[app.js:showToast]` caps `#toast-stack` at 4 visible toasts.
- `[app.js:cfgApiKey]` auto-selects provider `base_url` and `model` on API key prefix detection (`AIzaSy`, `gsk_`, `sk-or-`, `sk-`).
- `[app.js:escapeHtml]` escapes `&`, `<`, `>`, `"`, and `'` before interpolating backend or user strings into HTML templates.

## Side Effects
- `[app.js:callApi]` triggers backend mutations and IPC calls (`set_coach`, `set_position`, `toggle_assistant`, `save_settings`, `install_gsi`, `add_to_steam`, `send_chat`, `test_voice`, `toggle_overlay`, `minimize_to_tray`).
- `[app.js]` writes active tab selection to `localStorage` (`aidota.tab`) and updates the URL hash via `history.replaceState`.
- `[app.js:window.copyMessage]` writes message text to the system clipboard via `navigator.clipboard.writeText`.
- `[app.js]` overrides `window.alert` with `showToast`, mutates DOM elements across all tabs, and runs background `requestAnimationFrame` (`initWaveVisualizer`) and `setInterval`/`setTimeout` timers (`syncStateLoop`, tip rotation).

## Verification
- Run: `.venv\Scripts\python.exe tools/run_tests.py`
- Or: `.venv\Scripts\python.exe -m unittest discover -s tests`
