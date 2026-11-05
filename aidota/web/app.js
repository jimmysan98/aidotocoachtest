/**
 * AIDOTA PRO — Web Frontend Controller (app.js)
 * Interacts with Python backend via window.pywebview.api
 */

// Application State
const state = {
  coach: "Астра",
  position: 1,
  assistantRunning: false,
  gsiConnected: false,
  heroName: null,
  heroLevel: 1,
  gameTime: 0,
  isListening: false,
  isSpeaking: false,
  roshanDeathTime: null,
  activeTab: "dashboard",
  config: null,
  // v1.2: живые данные GSI и кэши иконок через Python API
  heroIconKey: null,
  inventorySignature: null,
  neutralItemSignature: null,
  adviceSignature: null,
  adviceFeedSignature: null,
};

// Preset configurations
const PRESETS = {
  pollinations: {
    base_url: "https://text.pollinations.ai/openai",
    model: "openai",
    api_key: "",
    hint: "100% бесплатно! Ключ не требуется, работает сразу через Pollinations AI.",
  },
  google_gemini: {
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    model: "gemini-2.5-flash",
    api_key: "",
    hint: "Бесплатный ключ на https://aistudio.google.com/app/apikey (Google AI Studio).",
  },
  pollinations_gemini: {
    base_url: "https://text.pollinations.ai/openai",
    model: "gemini",
    api_key: "",
    hint: "100% бесплатно! Модель Gemini без регистрации и ключей через Pollinations AI.",
  },
  pollinations_gemini_thinking: {
    base_url: "https://text.pollinations.ai/openai",
    model: "gemini-thinking",
    api_key: "",
    hint: "100% бесплатно! Рассуждающая модель Gemini Thinking через Pollinations AI.",
  },
  openrouter_free: {
    base_url: "https://openrouter.ai/api/v1",
    model: "google/gemini-2.0-flash-exp:free",
    api_key: "",
    hint: "Бесплатные модели на OpenRouter (требуется бесплатный ключ с openrouter.ai/keys).",
  },
  deepseek: {
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    api_key: "",
    hint: "Ключ на https://platform.deepseek.com.",
  },
  groq: {
    base_url: "https://api.groq.com/openai/v1",
    model: "llama-3.3-70b-versatile",
    api_key: "",
    hint: "Ключ на https://console.groq.com/keys.",
  },
  ollama: {
    base_url: "http://127.0.0.1:11434",
    model: "gemma3",
    api_key: "",
    hint: "Локальный Ollama (запустите ollama serve в командной строке).",
  },
};

// Подписи позиций для бейджа в карточке героя (индекс = позиция 1..5).
const POSITION_LABELS = [
  "",
  "Керри (Pos 1)",
  "Мидер (Pos 2)",
  "Оффлейн (Pos 3)",
  "Soft Sup (Pos 4)",
  "Hard Sup (Pos 5)",
];

// DOM Elements
const elements = {
  navTabs: document.querySelectorAll(".nav-tab"),
  tabPanes: document.querySelectorAll(".tab-pane"),
  coachBtns: document.querySelectorAll(".coach-btn"),
  roleBtns: document.querySelectorAll(".role-btn"),
  gsiStatusPill: document.getElementById("gsi-status-pill"),
  gsiStatusText: document.getElementById("gsi-status-text"),
  coachStatusText: document.getElementById("coach-status-text"),
  btnToggleAssistant: document.getElementById("btn-toggle-assistant"),
  btnToggleAssistantText: document.getElementById("btn-toggle-assistant-text"),
  btnTestVoice: document.getElementById("btn-test-voice"),
  btnToggleOverlay: document.getElementById("btn-toggle-overlay"),
  btnInstallGsiQuick: document.getElementById("btn-install-gsi-quick"),
  btnMinimizeTray: document.getElementById("btn-minimize-tray"),
  chatMessagesContainer: document.getElementById("chat-messages-container"),
  chatInputForm: document.getElementById("chat-input-form"),
  chatTextInput: document.getElementById("chat-text-input"),
  adviceFeedList: document.getElementById("advice-feed-list"),
  btnClearFeed: document.getElementById("btn-clear-feed"),
  presetCards: document.querySelectorAll(".preset-card"),
  cfgBaseUrl: document.getElementById("cfg-base-url"),
  cfgModel: document.getElementById("cfg-model"),
  cfgApiKey: document.getElementById("cfg-api-key"),
  cfgKeyHint: document.getElementById("cfg-key-hint"),
  btnTogglePw: document.getElementById("btn-toggle-pw"),
  btnTestLlm: document.getElementById("btn-test-llm"),
  testLlmResult: document.getElementById("test-llm-result"),
  cfgTtsProvider: document.getElementById("cfg-tts-provider"),
  cfgEdgeVoice: document.getElementById("cfg-edge-voice"),
  cfgInputDevice: document.getElementById("cfg-input-device"),
  cfgOutputDevice: document.getElementById("cfg-output-device"),
  cfgPttKey: document.getElementById("cfg-ptt-key"),
  btnRecordHotkey: document.getElementById("btn-record-hotkey"),
  cfgAutonomyLevel: document.getElementById("cfg-autonomy-level"),
  cfgSkillLevel: document.getElementById("cfg-skill-level"),
  cfgSkillHint: document.getElementById("cfg-skill-hint"),
  btnSaveSettings: document.getElementById("btn-save-settings"),
  saveSettingsIndicator: document.getElementById("save-settings-indicator"),
  btnRerunDoctor: document.getElementById("btn-rerun-doctor"),
  doctorChecksGrid: document.getElementById("doctor-checks-grid"),
  btnDoctorInstallGsi: document.getElementById("btn-doctor-install-gsi"),
  btnDoctorAddSteam: document.getElementById("btn-doctor-add-steam"),
  btnRoshanKilled: document.getElementById("btn-roshan-killed"),
  audioWaveCanvas: document.getElementById("audio-wave-canvas"),
};

// ---------------------------------------------------------------------------
// Audio Wave Visualizer (HTML5 Canvas)
// ---------------------------------------------------------------------------
let wavePhase = 0;
function initWaveVisualizer() {
  const canvas = elements.audioWaveCanvas;
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  function draw() {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const isAstra = state.coach === "Астра";
    const primaryColor = isAstra ? "rgba(168, 85, 247, " : "rgba(245, 158, 11, ";
    const activeColor = state.isSpeaking ? "rgba(16, 185, 129, " : primaryColor;

    wavePhase += state.isSpeaking ? 0.08 : state.isListening ? 0.05 : 0.02;

    const amplitude = state.isSpeaking ? 22 : state.isListening ? 16 : 6;
    const centerY = canvas.height / 2;

    // Background Wave 1
    ctx.beginPath();
    ctx.strokeStyle = activeColor + "0.3)";
    ctx.lineWidth = 2;
    for (let x = 0; x < canvas.width; x += 4) {
      const y = centerY + Math.sin(x * 0.03 + wavePhase) * amplitude;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Foreground Glowing Wave 2
    ctx.beginPath();
    ctx.strokeStyle = activeColor + "0.85)";
    ctx.lineWidth = 3;
    ctx.shadowBlur = 10;
    ctx.shadowColor = activeColor + "0.6)";
    for (let x = 0; x < canvas.width; x += 4) {
      const y = centerY + Math.cos(x * 0.04 - wavePhase) * (amplitude * 0.8);
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;
  }
  draw();
}

// ---------------------------------------------------------------------------
// Python Web API Safe Invocation
// ---------------------------------------------------------------------------
async function callApi(fnName, ...args) {
  if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[fnName] === "function") {
    try {
      return await window.pywebview.api[fnName](...args);
    } catch (err) {
      console.error(`API Call failed: ${fnName}`, err);
      return null;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// v1.2: Живые данные GSI — иконки через Python API (data URI), нетворс,
// предметы инвентаря и блок «Что купить дальше». Всё через callApi, с кэшем
// по ключу и без падений при отсутствии window.pywebview.
// ---------------------------------------------------------------------------
const heroIconCache = new Map(); // hero key -> data URI ("" = не удалось)
const itemIconCache = new Map(); // item key / display -> data URI ("" = не удалось)
const pendingHeroIcons = new Set();
const pendingItemIcons = new Set();

function formatGold(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return "0";
  return Math.round(num).toLocaleString("ru-RU");
}

function truncateText(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  return clean.slice(0, Math.max(0, limit - 1)).trimEnd() + "…";
}

function switchToTab(tabName) {
  const tab = Array.from(elements.navTabs || []).find(
    (item) => item.getAttribute("data-tab") === tabName
  );
  if (tab) tab.click();
}

// --- Иконка героя (hero_icon_key -> get_hero_icon -> data URI) -------------
function setHeroImage(src) {
  const img = document.getElementById("dashboard-hero-img");
  const placeholder = document.getElementById("hero-placeholder");
  if (!img) return;

  const showPlaceholder = () => {
    img.style.display = "none";
    img.removeAttribute("src");
    if (placeholder) placeholder.style.display = "flex";
  };

  if (!src) {
    showPlaceholder();
    return;
  }

  img.onload = () => {
    if (!img.naturalWidth) {
      showPlaceholder();
      return;
    }
    img.style.display = "block";
    if (placeholder) placeholder.style.display = "none";
  };
  img.onerror = showPlaceholder;
  img.src = src;
}

function syncHeroIcon(gsi) {
  const key = String((gsi && (gsi.hero_icon_key || gsi.hero_npc)) || "").trim();
  if (!key) {
    if (state.heroIconKey !== null) {
      state.heroIconKey = null;
      setHeroImage("");
    }
    return;
  }
  // Не дёргаем API и не сбрасываем картинку, пока герой тот же.
  if (state.heroIconKey === key) return;
  state.heroIconKey = key;

  const cached = heroIconCache.get(key);
  if (cached !== undefined) {
    setHeroImage(cached);
    return;
  }

  setHeroImage("");
  if (pendingHeroIcons.has(key)) return;
  pendingHeroIcons.add(key);
  callApi("get_hero_icon", key).then((uri) => {
    pendingHeroIcons.delete(key);
    const clean = typeof uri === "string" ? uri.trim() : "";
    heroIconCache.set(key, clean);
    if (state.heroIconKey === key) setHeroImage(clean);
  });
}

// --- Нетворс (networth / networth_estimated) -------------------------------
function renderNetworth(gsi) {
  const nwElem = document.getElementById("stat-networth");
  if (!nwElem) return;
  const value = Math.max(0, Number((gsi && gsi.networth) || 0) || 0);
  const estimated = Boolean(gsi && gsi.networth_estimated === true);

  nwElem.textContent = `${estimated ? "≈" : ""}${formatGold(value)} 💰`;
  nwElem.classList.toggle("is-estimated", estimated);
  if (estimated) {
    nwElem.setAttribute("title", "Оценка: GSI не прислал net_worth");
  } else {
    nwElem.removeAttribute("title");
  }
}

// --- Инвентарь: 6 слотов + нейтралка, иконки через get_item_icon ------------
function findItemSlots(key) {
  const slots = [];
  document.querySelectorAll(".item-slot[data-item-key]").forEach((slot) => {
    if (slot.getAttribute("data-item-key") === key) slots.push(slot);
  });
  return slots;
}

function applyItemIconToSlots(key, uri) {
  if (!uri) return;
  findItemSlots(key).forEach((slot) => {
    const img = slot.querySelector("img.item-icon");
    if (!img) return;
    img.onload = () => {
      if (img.naturalWidth > 0) slot.classList.add("has-icon");
    };
    img.onerror = () => {
      slot.classList.remove("has-icon");
      img.removeAttribute("src");
    };
    img.src = uri;
  });
}

function ensureItemIcon(key) {
  if (!key) return;
  if (itemIconCache.has(key)) {
    applyItemIconToSlots(key, itemIconCache.get(key));
    return;
  }
  if (pendingItemIcons.has(key)) return;
  pendingItemIcons.add(key);
  callApi("get_item_icon", key).then((uri) => {
    pendingItemIcons.delete(key);
    const clean = typeof uri === "string" ? uri.trim() : "";
    itemIconCache.set(key, clean);
    applyItemIconToSlots(key, clean);
  });
}

function ensureItemIcons() {
  document.querySelectorAll(".item-slot[data-item-key]").forEach((slot) => {
    ensureItemIcon(slot.getAttribute("data-item-key") || "");
  });
}

function renderInventory(gsi) {
  const invBar = document.getElementById("inventory-bar");
  if (!invBar) return;

  const items = [];
  if (gsi && Array.isArray(gsi.inventory_items)) {
    gsi.inventory_items.slice(0, 6).forEach((raw) => {
      if (!raw || typeof raw !== "object") {
        items.push(null);
        return;
      }
      const display = String(raw.display || raw.slug || raw.key || "").trim();
      if (!display) {
        items.push(null);
        return;
      }
      items.push({
        key: String(raw.key || "").trim(),
        display,
        cost: Math.max(0, Number(raw.cost) || 0),
      });
    });
  }
  // Если новых данных нет/они пустые — старое поведение по текстовым именам.
  if (!items.some(Boolean) && gsi && Array.isArray(gsi.inventory)) {
    items.length = 0;
    gsi.inventory.slice(0, 6).forEach((name) => {
      const display = String(name || "").trim();
      items.push(display ? { key: "", display, cost: 0 } : null);
    });
  }
  while (items.length < 6) items.push(null);

  const signature = items
    .map((item) => (item ? `${item.key}|${item.display}|${item.cost}` : "-"))
    .join(";");
  if (signature === state.inventorySignature) return;
  state.inventorySignature = signature;

  invBar.innerHTML = items
    .map((item, index) => {
      if (!item) {
        return `<div class="item-slot" title="Слот ${index + 1}"><span class="item-empty">-</span></div>`;
      }
      const title =
        item.cost > 0 ? `${item.display} · ${formatGold(item.cost)} золота` : item.display;
      return (
        `<div class="item-slot" data-slot="${index}"` +
        (item.key ? ` data-item-key="${escapeHtml(item.key)}"` : "") +
        ` title="${escapeHtml(title)}">` +
        `<img class="item-icon" alt="" title="${escapeHtml(title)}">` +
        `<span class="item-name">${escapeHtml(item.display)}</span>` +
        `</div>`
      );
    })
    .join("");

  ensureItemIcons();
}

function renderNeutralItem(gsi) {
  const slot = document.getElementById("neutral-item-slot");
  if (!slot) return;

  const name = String((gsi && gsi.neutral_item) || "").trim();
  if (name === state.neutralItemSignature) return;
  state.neutralItemSignature = name;

  if (!name) {
    slot.style.display = "none";
    slot.removeAttribute("data-item-key");
    slot.innerHTML = '<span class="item-empty">-</span>';
    return;
  }

  const title = `${name} · нейтральный предмет`;
  slot.style.display = "flex";
  slot.setAttribute("data-item-key", name);
  slot.setAttribute("title", title);
  slot.innerHTML =
    `<img class="item-icon" alt="" title="${escapeHtml(title)}">` +
    `<span class="item-name">${escapeHtml(name)}</span>`;
  ensureItemIcon(name);
}

// --- «Что купить дальше»: gsi.item_advice ----------------------------------
const ADVICE_PRIORITY_LABELS = { HIGH: "Срочно", MEDIUM: "Важно", LOW: "Планово" };
const ADVICE_PRIORITY_CLASSES = {
  HIGH: "priority-high",
  MEDIUM: "priority-medium",
  LOW: "priority-low",
};

function normalizeItemAdvice(gsi) {
  if (!gsi || !Array.isArray(gsi.item_advice)) return [];
  return gsi.item_advice
    .filter((raw) => raw && typeof raw === "object" && String(raw.item || "").trim())
    .slice(0, 3)
    .map((raw) => ({
      item: String(raw.item).trim(),
      cost: Math.max(0, Number(raw.cost) || 0),
      goldNeeded: Math.max(0, Number(raw.gold_needed) || 0),
      affordable: raw.affordable === true,
      priority: String(raw.priority || "").toUpperCase(),
      reason: String(raw.reason || "").trim(),
      warning: String(raw.warning || "").trim(),
    }));
}

function adviceSignature(advice) {
  return advice.map((item) => `${item.item}|${item.priority}`).join(";;");
}

function adviceGoldText(adviceItem) {
  if (adviceItem.affordable) return "можно купить сейчас";
  return adviceItem.goldNeeded > 0
    ? `не хватает ${formatGold(adviceItem.goldNeeded)} золота`
    : "не хватает золота";
}

function applyAdviceIcon(wrapper, img, uri) {
  if (!wrapper || !img || !uri) return;
  img.onload = () => {
    if (img.naturalWidth > 0) wrapper.classList.add("has-icon");
  };
  img.onerror = () => {
    wrapper.classList.remove("has-icon");
    img.removeAttribute("src");
  };
  img.src = uri;
}

function ensureAdviceIcons() {
  document.querySelectorAll("#item-advice-list .advice-item").forEach((card) => {
    const name = card.getAttribute("data-item") || "";
    const wrapper = card.querySelector(".advice-icon-wrap");
    const img = card.querySelector("img.advice-icon");
    if (!name || !wrapper || !img) return;
    if (itemIconCache.has(name)) {
      applyAdviceIcon(wrapper, img, itemIconCache.get(name));
      return;
    }
    if (pendingItemIcons.has(name)) return;
    pendingItemIcons.add(name);
    callApi("get_item_icon", name).then((uri) => {
      pendingItemIcons.delete(name);
      const clean = typeof uri === "string" ? uri.trim() : "";
      itemIconCache.set(name, clean);
      document.querySelectorAll("#item-advice-list .advice-item").forEach((liveCard) => {
        if ((liveCard.getAttribute("data-item") || "") !== name) return;
        applyAdviceIcon(
          liveCard.querySelector(".advice-icon-wrap"),
          liveCard.querySelector("img.advice-icon"),
          clean
        );
      });
    });
  });
}

function adviceCardHtml(item, warningText = "") {
  const priorityLabel = ADVICE_PRIORITY_LABELS[item.priority] || "";
  const priorityClass = ADVICE_PRIORITY_CLASSES[item.priority] || "priority-low";
  const initials =
    item.item.replace(/[^A-Za-zА-Яа-яЁё0-9]/g, "").slice(0, 2).toUpperCase() || "?";
  const price =
    item.cost > 0 ? `<span class="advice-price">${formatGold(item.cost)} 💰</span>` : "";
  const reason = item.reason || "Тренер рекомендует этот предмет по текущей ситуации.";
  const warning = warningText
    ? `<div class="advice-warning" title="${escapeHtml(warningText)}">⚠ ${escapeHtml(warningText)}</div>`
    : "";

  return `
    <div class="advice-item" data-advice-item data-item="${escapeHtml(item.item)}" role="button" tabindex="0"
         title="Спросить ИИ: Зачем покупать ${escapeHtml(item.item)}?"
         aria-label="Совет по предмету ${escapeHtml(item.item)}: ${escapeHtml(reason)}">
      <div class="advice-icon-wrap">
        <span class="advice-icon-fallback">${escapeHtml(initials)}</span>
        <img class="advice-icon" alt="">
      </div>
      <div class="advice-main">
        <div class="advice-top">
          <span class="advice-name" title="${escapeHtml(item.item)}">${escapeHtml(item.item)}</span>
          ${price}
        </div>
        <div class="advice-reason" title="${escapeHtml(reason)}">${escapeHtml(reason)}</div>
        ${warning}
        <div class="advice-meta">
          ${
            priorityLabel
              ? `<span class="advice-pill ${priorityClass}" title="Приоритет: ${escapeHtml(item.priority)}">${priorityLabel}</span>`
              : ""
          }
          <span class="advice-gold ${item.affordable ? "is-affordable" : "is-short"}">${escapeHtml(
            adviceGoldText(item)
          )}</span>
        </div>
      </div>
      <span class="advice-open-hint" aria-hidden="true">💬</span>
    </div>`;
}

function updateItemAdviceLive(advice) {
  const list = document.getElementById("item-advice-list");
  if (!list || !advice.length) return;
  const byItem = new Map(advice.map((item) => [item.item, item]));
  list.querySelectorAll(".advice-item").forEach((card) => {
    const item = byItem.get(card.getAttribute("data-item") || "");
    if (!item) return;
    const goldElem = card.querySelector(".advice-gold");
    if (goldElem) {
      goldElem.textContent = adviceGoldText(item);
      goldElem.classList.toggle("is-affordable", item.affordable);
      goldElem.classList.toggle("is-short", !item.affordable);
    }
    const priceElem = card.querySelector(".advice-price");
    if (priceElem && item.cost > 0) priceElem.textContent = `${formatGold(item.cost)} 💰`;
  });
}

function renderItemAdvice(gsi) {
  const list = document.getElementById("item-advice-list");
  if (!list) return;
  const advice = normalizeItemAdvice(gsi);
  const signature = adviceSignature(advice);

  if (signature !== state.adviceSignature) {
    state.adviceSignature = signature;

    if (!advice.length) {
      list.innerHTML =
        '<div class="advice-empty" id="item-advice-empty">Свежих советов по закупу пока нет — начните матч, и тренер подскажет следующий предмет.</div>';
      state.adviceFeedSignature = "";
    } else {
      // Одинаковый warning (например, «Linken's Sphere НЕ блокирует Omnislash…»
      // от одного и того же врага) показываем один раз — в первой карточке.
      // Так он остаётся видимым, но не дублируется и не раздувает layout.
      const shownWarnings = new Set();
      list.innerHTML = advice
        .map((item) => {
          const warningText =
            item.warning && !shownWarnings.has(item.warning) ? item.warning : "";
          if (warningText) shownWarnings.add(warningText);
          return adviceCardHtml(item, warningText);
        })
        .join("");
      list.querySelectorAll(".advice-item").forEach((card) => {
        const itemName = card.getAttribute("data-item") || "";
        const openAdvice = () => askCoachAboutItem(itemName);
        card.addEventListener("click", openAdvice);
        card.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openAdvice();
          }
        });
      });
      ensureAdviceIcons();

      // Одна запись в ленту на смену набора советов (без спама от золота).
      const feedSignature = advice.map((item) => item.item).join("|");
      if (feedSignature !== state.adviceFeedSignature) {
        state.adviceFeedSignature = feedSignature;
        const first = advice[0];
        addFeedItem(
          "Что купить дальше",
          `${first.item}${first.reason ? " — " + truncateText(first.reason, 160) : ""}`
        );
      }
    }
  }

  updateItemAdviceLive(advice);
}

function askCoachAboutItem(itemName) {
  const item = String(itemName || "").trim();
  if (!item) return;
  switchToTab("chat");
  if (typeof handleUserMessage === "function") {
    handleUserMessage(`Зачем покупать ${item}?`);
  }
}

// ---------------------------------------------------------------------------
// Navigation & Tab Switching
// ---------------------------------------------------------------------------
elements.navTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.getAttribute("data-tab");
    state.activeTab = target;

    elements.navTabs.forEach((t) => t.classList.remove("active"));
    elements.tabPanes.forEach((p) => p.classList.remove("active"));

    tab.classList.add("active");
    const pane = document.getElementById(`tab-${target}`);
    if (pane) pane.classList.add("active");

    if (target === "doctor") {
      refreshDoctorChecks();
    }
  });
});

// ---------------------------------------------------------------------------
// Coach Switcher
// ---------------------------------------------------------------------------
elements.coachBtns.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const coachName = btn.getAttribute("data-coach");
    state.coach = coachName;

    elements.coachBtns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");

    const visualizerStatus = document.getElementById("visualizer-status");
    if (visualizerStatus) {
      visualizerStatus.textContent = `Активен тренер: ${coachName}`;
    }

    // Auto-update TTS voice matching persona
    const voiceSelect = elements.cfgEdgeVoice;
    if (voiceSelect) {
      if (coachName === "Джарвис") {
        voiceSelect.value = "ru-RU-DmitryNeural";
      } else {
        voiceSelect.value = "ru-RU-SvetlanaNeural";
      }
    }

    await callApi("set_coach", coachName);
    addFeedItem(`Выбран тренер ${coachName}`, `Стиль общения и системный промпт переключены на ${coachName}.`);
  });
});

// ---------------------------------------------------------------------------
// Role Selector (1..5)
// ---------------------------------------------------------------------------
elements.roleBtns.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const pos = parseInt(btn.getAttribute("data-pos"), 10);
    state.position = pos;

    elements.roleBtns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");

    updateHeroPositionBadge(pos);

    await callApi("set_position", pos);
    addFeedItem(`Выбрана роль: Позиция ${pos}`, `Тайминги, советы по лайнингу и предметам настроены под позицию ${pos}.`);
  });
});

// ---------------------------------------------------------------------------
// Main Assistant Control
// ---------------------------------------------------------------------------
elements.btnToggleAssistant.addEventListener("click", async () => {
  const isRunning = await callApi("toggle_assistant");
  state.assistantRunning = Boolean(isRunning);
  updateAssistantUI(state.assistantRunning);
});

function updateAssistantUI(running) {
  const btn = elements.btnToggleAssistant;
  const text = elements.btnToggleAssistantText;
  const coachDot = document.querySelector(".coach-dot-status");
  const coachStatus = elements.coachStatusText;
  const engineBadge = document.getElementById("coach-engine-badge");

  if (running) {
    btn.classList.remove("btn-primary");
    btn.classList.add("btn-danger");
    text.textContent = "⏹ Остановить тренера";
    if (coachDot) coachDot.classList.add("online");
    if (coachStatus) coachStatus.textContent = "Активен (Слушает)";
    if (engineBadge) {
      engineBadge.textContent = "В работе";
      engineBadge.style.color = "var(--color-radiant)";
    }
    addFeedItem("Ассистент запущен", "Голосовое распознавание и мониторинг матча активны.");
  } else {
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-primary");
    text.textContent = "🚀 Запустить ассистента";
    if (coachDot) coachDot.classList.remove("online");
    if (coachStatus) coachStatus.textContent = "Остановлен";
    if (engineBadge) {
      engineBadge.textContent = "Готов к запуску";
      engineBadge.style.color = "var(--text-dim)";
    }
  }
}

// ---------------------------------------------------------------------------
// Voice & Overlay Quick Actions
// ---------------------------------------------------------------------------
elements.btnTestVoice.addEventListener("click", async () => {
  state.isSpeaking = true;
  const phrase = state.coach === "Джарвис"
    ? "Джарвис на связи. Все тактические системы в боевой готовности."
    : "Привет! Я Астра, твой персональный ИИ-тренер. Готова к победе!";
  await callApi("test_voice", phrase);
  setTimeout(() => { state.isSpeaking = false; }, 3500);
});

elements.btnToggleOverlay.addEventListener("click", async () => {
  await callApi("toggle_overlay");
});

elements.btnInstallGsiQuick.addEventListener("click", async () => {
  const res = await callApi("install_gsi");
  if (res && res.success) {
    alert("Конфиг GSI успешно установлен в Dota 2!\nПерезапустите Dota 2 для применения.");
  } else {
    alert(res?.message || "Не удалось автоматически найти каталог Dota 2. Укажите путь вручную.");
  }
});

elements.btnMinimizeTray.addEventListener("click", async () => {
  await callApi("minimize_to_tray");
});

// ---------------------------------------------------------------------------
// Interactive AI Chat
// ---------------------------------------------------------------------------
elements.chatInputForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = elements.chatTextInput.value.trim();
  if (!text) return;
  elements.chatTextInput.value = "";
  await handleUserMessage(text);
});

document.querySelectorAll(".pill-btn").forEach((pill) => {
  pill.addEventListener("click", async () => {
    const query = pill.getAttribute("data-query");
    if (query) {
      await handleUserMessage(query);
    }
  });
});

async function handleUserMessage(text) {
  appendChatMessage("user", "Вы", text);

  // Animated typing indicator
  const typingId = appendChatMessage("coach", state.coach, "");
  const typingElem = document.getElementById(typingId);
  if (typingElem) {
    typingElem.classList.add("is-typing");
    const bubble = typingElem.querySelector(".msg-bubble");
    const textElem = typingElem.querySelector(".msg-text");
    if (bubble) bubble.classList.add("typing-bubble");
    if (textElem) {
      textElem.innerHTML = '<span class="typing-dots"><i></i><i></i><i></i></span>';
    }
    const actions = typingElem.querySelector(".msg-actions");
    if (actions) actions.style.display = "none";
  }

  state.isSpeaking = true;
  const reply = await callApi("send_chat", text);
  state.isSpeaking = false;

  if (typingElem) {
    typingElem.classList.remove("is-typing");
    const bubble = typingElem.querySelector(".msg-bubble");
    const textElem = typingElem.querySelector(".msg-text");
    if (bubble) bubble.classList.remove("typing-bubble");
    if (textElem) textElem.textContent = reply || "Не удалось получить ответ от ИИ.";
    const actions = typingElem.querySelector(".msg-actions");
    if (actions) actions.style.display = "";
  }

  // Scroll to bottom
  elements.chatMessagesContainer.scrollTop = elements.chatMessagesContainer.scrollHeight;
}

function appendChatMessage(role, author, text) {
  const container = elements.chatMessagesContainer;
  const msgId = "msg-" + Date.now() + "-" + Math.floor(Math.random() * 1000);

  const isUser = role === "user";
  const avatarClass = isUser ? "user-avatar" : state.coach === "Джарвис" ? "jarvis-avatar" : "astra-avatar";
  const avatarLetter = isUser ? "Я" : "✦";

  const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const msgDiv = document.createElement("div");
  msgDiv.className = `chat-msg ${isUser ? "user-msg" : "coach-msg"}`;
  msgDiv.id = msgId;

  msgDiv.innerHTML = `
    <div class="msg-avatar ${avatarClass}">${avatarLetter}</div>
    <div class="msg-bubble">
      <div class="msg-header">
        <span class="msg-author">${author}</span>
        <span class="msg-time">${timeStr}</span>
      </div>
      <div class="msg-text">${escapeHtml(text)}</div>
      ${!isUser ? `
      <div class="msg-actions">
        <button class="msg-action-btn btn-speak-msg" onclick="speakMessage('${escapeJsString(text)}')">🔊 Озвучить</button>
        <button class="msg-action-btn btn-copy-msg" onclick="copyMessage('${escapeJsString(text)}')">📋 Копировать</button>
      </div>` : ""}
    </div>
  `;

  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;
  return msgId;
}

window.speakMessage = async function(text) {
  state.isSpeaking = true;
  await callApi("test_voice", text);
  setTimeout(() => { state.isSpeaking = false; }, 4000);
};

window.copyMessage = function(text) {
  navigator.clipboard.writeText(text);
  alert("Текст скопирован в буфер обмена!");
};

// ---------------------------------------------------------------------------
// Advice Feed
// ---------------------------------------------------------------------------
function addFeedItem(title, body) {
  const container = elements.adviceFeedList;
  if (!container) return;

  const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const item = document.createElement("div");
  item.className = "feed-item";
  item.innerHTML = `
    <div class="feed-icon">✦</div>
    <div class="feed-content">
      <div class="feed-title">${escapeHtml(title)}</div>
      <div class="feed-body">${escapeHtml(body)}</div>
      <div class="feed-time">${timeStr}</div>
    </div>
  `;
  container.prepend(item);

  // Limit feed to 20 items
  while (container.children.length > 20) {
    container.removeChild(container.lastChild);
  }
}

elements.btnClearFeed.addEventListener("click", () => {
  elements.adviceFeedList.innerHTML = "";
});

// ---------------------------------------------------------------------------
// Timers & Roshan
// ---------------------------------------------------------------------------
elements.btnRoshanKilled.addEventListener("click", () => {
  state.roshanDeathTime = state.gameTime;
  addFeedItem("Рошан уничтожен!", "Аегис истекает через 5:00. Окно респавна: от 8:00 до 11:00.");
  alert("Смерть Рошана зафиксирована! Таймер Аегиса и респавна запущен.");
});

function updateMacroTimersDisplay(gameTime) {
  state.gameTime = gameTime;

  // Wisdom Runes (every 7 mins: 420s)
  const nextWisdom = (Math.floor(gameTime / 420) + 1) * 420;
  const remWisdom = Math.max(0, nextWisdom - gameTime);
  const wElem = document.getElementById("timer-val-wisdom");
  if (wElem) wElem.textContent = formatTime(remWisdom);
  const wBar = document.getElementById("bar-wisdom");
  if (wBar) wBar.style.width = `${Math.min(100, (remWisdom / 420) * 100)}%`;

  // Active Runes (every 2 mins after 6:00)
  const nextActive = (Math.floor(gameTime / 120) + 1) * 120;
  const remActive = Math.max(0, nextActive - gameTime);
  const aElem = document.getElementById("timer-val-active-rune");
  if (aElem) aElem.textContent = formatTime(remActive);
  const aBar = document.getElementById("bar-active-rune");
  if (aBar) aBar.style.width = `${Math.min(100, (remActive / 120) * 100)}%`;

  // Tormentor (20:00 = 1200s)
  const torElem = document.getElementById("timer-val-tormentor");
  if (torElem) {
    const remTor = Math.max(0, 1200 - gameTime);
    torElem.textContent = remTor > 0 ? formatTime(remTor) : "Доступен!";
  }

  // Stacks (:53 of current minute)
  const secInMin = gameTime % 60;
  const remStack = secInMin <= 53 ? 53 - secInMin : 60 - secInMin + 53;
  const sElem = document.getElementById("timer-val-stack");
  if (sElem) sElem.textContent = `00:${String(remStack).padStart(2, "0")}`;

  // Game clock badge
  const clockBadge = document.getElementById("game-clock-badge");
  if (clockBadge) clockBadge.textContent = formatTime(gameTime);
}

// ---------------------------------------------------------------------------
// Settings & Presets
// ---------------------------------------------------------------------------
elements.presetCards.forEach((card) => {
  card.addEventListener("click", () => {
    const presetKey = card.getAttribute("data-preset");
    const preset = PRESETS[presetKey];
    if (!preset) return;

    elements.presetCards.forEach((c) => c.classList.remove("active"));
    card.classList.add("active");

    elements.cfgBaseUrl.value = preset.base_url;
    elements.cfgModel.value = preset.model;
    if (preset.api_key !== undefined) {
      elements.cfgApiKey.value = preset.api_key;
    }
    elements.cfgKeyHint.textContent = preset.hint;
  });
});

elements.btnTogglePw.addEventListener("click", () => {
  const input = elements.cfgApiKey;
  if (input.type === "password") {
    input.type = "text";
    elements.btnTogglePw.textContent = "🔒";
  } else {
    input.type = "password";
    elements.btnTogglePw.textContent = "👁";
  }
});

elements.btnTestLlm.addEventListener("click", async () => {
  elements.testLlmResult.textContent = "Проверяю связь с моделью...";
  elements.testLlmResult.style.color = "var(--color-cyan)";

  const payload = {
    base_url: elements.cfgBaseUrl.value.trim(),
    model: elements.cfgModel.value.trim(),
    api_key: elements.cfgApiKey.value.trim(),
  };

  const res = await callApi("test_llm_settings", payload);
  if (res && res.success) {
    elements.testLlmResult.textContent = "✅ Подключение успешно! ИИ отвечает.";
    elements.testLlmResult.style.color = "var(--color-radiant)";
  } else {
    elements.testLlmResult.textContent = `❌ Ошибка: ${res?.message || "Не удалось связаться с сервером"}`;
    elements.testLlmResult.style.color = "var(--color-dire)";
  }
});

// Push-to-talk Key Recorder
let isRecordingKey = false;
elements.btnRecordHotkey.addEventListener("click", () => {
  isRecordingKey = true;
  elements.cfgPttKey.value = "...";
  elements.btnRecordHotkey.textContent = "Нажмите клавишу";
  elements.btnRecordHotkey.classList.add("btn-danger");
});

window.addEventListener("keydown", (e) => {
  if (isRecordingKey) {
    e.preventDefault();
    isRecordingKey = false;
    let keyName = e.key.toLowerCase();
    if (keyName === " ") keyName = "space";
    elements.cfgPttKey.value = keyName;
    elements.btnRecordHotkey.textContent = "Задать клавишу";
    elements.btnRecordHotkey.classList.remove("btn-danger");

    const pttDisplay = document.getElementById("ptt-key-display");
    if (pttDisplay) pttDisplay.textContent = keyName.toUpperCase();
  }
});

// Save Settings
elements.btnSaveSettings.addEventListener("click", async () => {
  const payload = {
    llm_base_url: elements.cfgBaseUrl.value.trim(),
    llm_model: elements.cfgModel.value.trim(),
    llm_api_key: elements.cfgApiKey.value.trim(),
    tts_provider: elements.cfgTtsProvider.value,
    edge_voice: elements.cfgEdgeVoice.value,
    input_device: elements.cfgInputDevice.value || null,
    output_device: elements.cfgOutputDevice.value || null,
    ptt_key: elements.cfgPttKey.value.trim(),
    autonomy_level: elements.cfgAutonomyLevel.value,
    ...(elements.cfgSkillLevel ? { skill_level: elements.cfgSkillLevel.value } : {}),
  };

  const res = await callApi("save_settings", payload);
  if (res && res.success) {
    elements.saveSettingsIndicator.textContent = "✅ Настройки успешно сохранены!";
    setTimeout(() => { elements.saveSettingsIndicator.textContent = ""; }, 3000);
  } else {
    alert("Ошибка при сохранении настроек: " + (res?.message || ""));
  }
});

// ---------------------------------------------------------------------------
// Doctor Diagnostics
// ---------------------------------------------------------------------------
async function refreshDoctorChecks() {
  const container = elements.doctorChecksGrid;
  if (!container) return;
  container.innerHTML = "<div class='check-item'><span class='check-name'>Выполняю самодиагностику...</span></div>";

  const checks = await callApi("run_doctor");
  if (!checks || !Array.isArray(checks)) {
    container.innerHTML = "<div class='check-item'><span class='check-name'>Не удалось загрузить отчёт диагностики.</span></div>";
    return;
  }

  container.innerHTML = "";
  checks.forEach((c) => {
    const isOk = c.status === "OK";
    const div = document.createElement("div");
    div.className = `check-item ${isOk ? "ok-item" : "warn-item"}`;
    div.innerHTML = `
      <span class="check-badge ${isOk ? "ok-badge" : "warn-badge"}">${c.status}</span>
      <div class="check-info">
        <span class="check-name">${escapeHtml(c.name)}</span>
        <span class="check-desc">${escapeHtml(c.details || "")}</span>
      </div>
    `;
    container.appendChild(div);
  });
}

elements.btnRerunDoctor.addEventListener("click", refreshDoctorChecks);

elements.btnDoctorInstallGsi.addEventListener("click", async () => {
  const res = await callApi("install_gsi");
  alert(res?.message || (res?.success ? "GSI установлен!" : "Ошибка установки GSI"));
});

elements.btnDoctorAddSteam.addEventListener("click", async () => {
  const res = await callApi("add_to_steam");
  alert(res?.message || "Готово!");
});

// ---------------------------------------------------------------------------
// Real-Time Polling & State Sync Loop (1 second)
// ---------------------------------------------------------------------------
async function syncStateLoop() {
  // Когда окно свёрнуто/скрыто — не дёргаем Python-мост (экономия CPU/батареи).
  if (document.hidden) {
    setTimeout(syncStateLoop, 1500);
    return;
  }
  const data = await callApi("get_state");
  if (data) {
    // 1. Coach & Role
    if (data.coach && data.coach !== state.coach) {
      state.coach = data.coach;
      elements.coachBtns.forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-coach") === state.coach);
      });
    }

    const serverPosition = parseInt(data.position, 10);
    if (serverPosition && serverPosition !== state.position) {
      state.position = serverPosition;
      elements.roleBtns.forEach((b) => {
        b.classList.toggle("active", parseInt(b.getAttribute("data-pos"), 10) === state.position);
      });
    }
    // Бейдж в карточке героя всегда соответствует сохранённой позиции из get_state.
    if (serverPosition) updateHeroPositionBadge(serverPosition);

    // Уровень игры: подставляем сохранённое значение в форму настроек один раз.
    if (data.skill_level && elements.cfgSkillLevel && !state.skillLevelLoaded) {
      elements.cfgSkillLevel.value = data.skill_level;
      state.skillLevelLoaded = true;
      if (typeof updateSkillLevelHint === "function") updateSkillLevelHint(data.skill_level);
    }
    if (data.skill_level) {
      state.skillLevel = data.skill_level;
      const labels = { beginner: "Новичок", amateur: "Любитель", advanced: "Опытный", expert: "Профи" };
      const label = labels[data.skill_level] || "Новичок";
      const skillText = document.getElementById("skill-level-text");
      if (skillText && skillText.textContent !== label) skillText.textContent = label;
      const tipBadge = document.getElementById("tip-level-badge");
      if (tipBadge && tipBadge.textContent !== label) tipBadge.textContent = label;
    }

    // Rust Core indicator
    if (data.rust_active !== undefined) {
      const rustPill = document.getElementById("rust-status-pill");
      if (rustPill) {
        rustPill.style.display = data.rust_active ? "inline-flex" : "none";
      }
    }

    // 2. Assistant Status
    if (data.assistant_running !== undefined && data.assistant_running !== state.assistantRunning) {
      state.assistantRunning = data.assistant_running;
      updateAssistantUI(state.assistantRunning);
    }

    // 3. GSI Data
    const gsi = data.gsi;
    if (gsi && gsi.connected) {
      const dot = document.querySelector(".gsi-dot");
      if (dot) dot.classList.add("online");
      elements.gsiStatusText.textContent = gsi.hero_name ? `В игре: ${gsi.hero_name}` : "GSI активен";

      // Hero Name & Level
      const nameElem = document.getElementById("hero-name-display");
      if (nameElem && gsi.hero_name) nameElem.textContent = gsi.hero_name;
      const lvlElem = document.getElementById("hero-level-badge");
      if (lvlElem && gsi.hero_level) lvlElem.textContent = `LVL ${gsi.hero_level}`;

      // Hero Image: через Python API (data URI), кэш по hero_icon_key
      syncHeroIcon(gsi);

      // KDA & Networth
      const kdaElem = document.getElementById("stat-kda");
      if (kdaElem) kdaElem.textContent = `${gsi.kills || 0} / ${gsi.deaths || 0} / ${gsi.assists || 0}`;
      // Нетворс: форматирование + пометка «≈» для оценки
      renderNetworth(gsi);
      const goldElem = document.getElementById("stat-gold");
      if (goldElem) goldElem.textContent = `${gsi.gold || 0}`;
      const csElem = document.getElementById("stat-cs");
      if (csElem) csElem.textContent = `${gsi.last_hits || 0} / ${gsi.denies || 0}`;
      const scoreElem = document.getElementById("stat-score");
      if (scoreElem) scoreElem.textContent = `${gsi.radiant_score || 0} : ${gsi.dire_score || 0}`;

      // Инвентарь: 6 слотов из gsi.inventory_items + иконки через API
      renderInventory(gsi);
      renderNeutralItem(gsi);

      // «Что купить дальше»: живые советы ИИ-тренера (gsi.item_advice)
      renderItemAdvice(gsi);

      // Кулдаун ультимейта
      const ultElem = document.getElementById("stat-ult");
      if (ultElem) {
        const cd = Number(gsi.ult_cooldown || 0);
        if (gsi.ult_ready === true && cd <= 0) {
          ultElem.textContent = "готов";
          ultElem.style.color = "var(--color-radiant)";
        } else if (cd > 0) {
          ultElem.textContent = `${cd} с`;
          ultElem.style.color = "var(--color-gold)";
        } else {
          ultElem.textContent = "—";
          ultElem.style.color = "";
        }
      }

      // Timers
      if (gsi.clock_time !== undefined) {
        updateMacroTimersDisplay(gsi.clock_time);
      }
    } else {
      const dot = document.querySelector(".gsi-dot");
      if (dot) dot.classList.remove("online");
      elements.gsiStatusText.textContent = "Ожидание Dota 2";

      // Вне матча: иконка героя и советы сбрасываются, UI не ломается.
      if (state.heroIconKey !== null) {
        state.heroIconKey = null;
        setHeroImage("");
      }
      if (state.neutralItemSignature) {
        state.neutralItemSignature = null;
        renderNeutralItem({});
      }
      if (state.adviceSignature !== "" && state.adviceSignature !== null) {
        state.adviceSignature = null;
        state.adviceFeedSignature = "";
        renderItemAdvice({});
      }
    }

    // Audio Devices populate if empty
    if (data.audio_devices && elements.cfgInputDevice.children.length <= 1) {
      data.audio_devices.inputs?.forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.id;
        opt.textContent = d.name;
        elements.cfgInputDevice.appendChild(opt);
      });
      data.audio_devices.outputs?.forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.id;
        opt.textContent = d.name;
        elements.cfgOutputDevice.appendChild(opt);
      });
    }
  }

  setTimeout(syncStateLoop, 1000);
}

// ---------------------------------------------------------------------------
// Helper Utilities
// ---------------------------------------------------------------------------
function updateHeroPositionBadge(position) {
  const posBadge = document.getElementById("hero-pos-badge");
  if (!posBadge) return;
  const pos = parseInt(position, 10) || 1;
  const label = POSITION_LABELS[pos] || `Позиция ${pos}`;
  if (posBadge.textContent !== label) posBadge.textContent = label;
}

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeJsString(str) {
  if (!str) return "";
  return String(str).replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\n/g, " ");
}

// ---------------------------------------------------------------------------
// Quick Model Chips
// ---------------------------------------------------------------------------
document.querySelectorAll(".model-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const model = chip.getAttribute("data-model");
    if (elements.cfgModel) elements.cfgModel.value = model;
    if (model.startsWith("gemini") && !elements.cfgBaseUrl.value.includes("generativelanguage") && !elements.cfgBaseUrl.value.includes("pollinations")) {
      elements.cfgBaseUrl.value = "https://generativelanguage.googleapis.com/v1beta/openai";
    }
  });
});

// ---------------------------------------------------------------------------
// Meta DB & Draft Assistant Logic
// ---------------------------------------------------------------------------
let allMetaHeroes = [];
let selectedEnemyHeroes = [];
let draftSelectedPosition = 1;

// Subnav switcher
document.querySelectorAll(".meta-subnav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".meta-subnav-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const subtab = btn.getAttribute("data-subtab");
    const picker = document.getElementById("subpane-draft-picker");
    const builds = document.getElementById("subpane-hero-builds");
    if (picker) picker.style.display = subtab === "draft-picker" ? "block" : "none";
    if (builds) builds.style.display = subtab === "hero-builds" ? "block" : "none";
  });
});

// Position pills for draft
document.querySelectorAll("#draft-pos-selector .pos-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    document.querySelectorAll("#draft-pos-selector .pos-pill").forEach((p) => p.classList.remove("active"));
    pill.classList.add("active");
    draftSelectedPosition = parseInt(pill.getAttribute("data-pos") || "1", 10);
  });
});

// Load hero list from backend
async function loadMetaHeroes() {
  const heroes = await callApi("get_meta_heroes_list");
  if (Array.isArray(heroes) && heroes.length > 0) {
    allMetaHeroes = heroes;
    const select = document.getElementById("build-hero-select");
    if (select && select.children.length === 0) {
      heroes.forEach((h) => {
        const opt = document.createElement("option");
        opt.value = h.id;
        opt.textContent = `${h.ru_name} (${h.name})`;
        select.appendChild(opt);
      });
    }
  }
}

// Enemy Chips & Autocomplete
const enemySearchInput = document.getElementById("enemy-hero-search");
const enemyAutocomplete = document.getElementById("enemy-autocomplete");
const enemyChipsContainer = document.getElementById("enemy-chips-container");

function renderEnemyChips() {
  if (!enemyChipsContainer) return;
  enemyChipsContainer.innerHTML = "";
  selectedEnemyHeroes.forEach((heroName, idx) => {
    const chip = document.createElement("div");
    chip.className = "hero-chip";
    chip.innerHTML = `<span>${escapeHtml(heroName)}</span><button type="button" class="hero-chip-remove" title="Удалить">✕</button>`;
    chip.querySelector(".hero-chip-remove").addEventListener("click", () => {
      selectedEnemyHeroes.splice(idx, 1);
      renderEnemyChips();
    });
    enemyChipsContainer.appendChild(chip);
  });
}

function addEnemyHero(heroName) {
  if (!heroName || selectedEnemyHeroes.includes(heroName)) return;
  if (selectedEnemyHeroes.length >= 5) {
    alert("Максимум 5 вражеских героев.");
    return;
  }
  selectedEnemyHeroes.push(heroName);
  renderEnemyChips();
  if (enemySearchInput) enemySearchInput.value = "";
  if (enemyAutocomplete) enemyAutocomplete.style.display = "none";
}

if (enemySearchInput && enemyAutocomplete) {
  enemySearchInput.addEventListener("input", () => {
    const query = enemySearchInput.value.trim().toLowerCase();
    if (!query) {
      enemyAutocomplete.style.display = "none";
      return;
    }
    const matches = allMetaHeroes.filter((h) =>
      h.ru_name.toLowerCase().includes(query) ||
      h.name.toLowerCase().includes(query) ||
      h.id.toLowerCase().includes(query)
    );
    if (matches.length === 0) {
      enemyAutocomplete.style.display = "none";
      return;
    }
    enemyAutocomplete.innerHTML = "";
    matches.slice(0, 8).forEach((h) => {
      const item = document.createElement("div");
      item.className = "autocomplete-item";
      item.innerHTML = `<span><strong>${escapeHtml(h.ru_name)}</strong> (${escapeHtml(h.name)})</span><span class="role-tag">Поз. ${h.roles.join(", ")}</span>`;
      item.addEventListener("click", () => {
        addEnemyHero(h.ru_name);
      });
      enemyAutocomplete.appendChild(item);
    });
    enemyAutocomplete.style.display = "block";
  });

  document.addEventListener("click", (e) => {
    if (!enemySearchInput.contains(e.target) && !enemyAutocomplete.contains(e.target)) {
      enemyAutocomplete.style.display = "none";
    }
  });
}

// Sync heroes from GSI
const btnSyncGsiDraft = document.getElementById("btn-sync-gsi-draft");
if (btnSyncGsiDraft) {
  btnSyncGsiDraft.addEventListener("click", async () => {
    const res = await callApi("get_live_draft_heroes");
    if (res && Array.isArray(res.enemies) && res.enemies.length > 0) {
      selectedEnemyHeroes = res.enemies.slice(0, 5);
      renderEnemyChips();
      triggerDraftCalculation();
    } else {
      alert("В текущем GSI снапшоте нет данных о пике врагов. Начните матч или фазу выбора в Dota 2.");
    }
  });
}

// Clear draft button
const btnClearDraft = document.getElementById("btn-clear-draft");
if (btnClearDraft) {
  btnClearDraft.addEventListener("click", () => {
    selectedEnemyHeroes = [];
    renderEnemyChips();
    const grid = document.getElementById("draft-cards-grid");
    if (grid) {
      grid.innerHTML = `
        <div class="empty-draft-placeholder">
          <span class="empty-icon">🛡</span>
          <p>Добавьте вражеских героев выше или нажмите «Взять героев из матча GSI», чтобы получить персональные контрпики с объяснением.</p>
        </div>`;
    }
  });
}

// Calculate draft recommendations
async function triggerDraftCalculation() {
  const grid = document.getElementById("draft-cards-grid");
  if (!grid) return;
  grid.innerHTML = "<div class='empty-draft-placeholder'><p>⚡ ИИ-тренер рассчитывает идеальные контрпики...</p></div>";

  const res = await callApi("get_draft_recommendation", {
    enemies: selectedEnemyHeroes,
    allies: [],
    role: draftSelectedPosition,
  });

  if (!res || !Array.isArray(res.picks) || res.picks.length === 0) {
    grid.innerHTML = "<div class='empty-draft-placeholder'><p>Не удалось подобрать героев. Укажите хотя бы одного врага.</p></div>";
    return;
  }

  grid.innerHTML = "";
  res.picks.forEach((pick) => {
    const card = document.createElement("div");
    card.className = "draft-hero-card";

    const scoreClass = pick.score >= 75 ? "score-high" : "score-med";
    const countersHtml = pick.strong_against.map((c) => `<div class="matchup-item"><span class="tag-counter">✔</span> <span>${escapeHtml(c)}</span></div>`).join("");
    const dangerHtml = (pick.weak_against || []).map((w) => `<div class="matchup-item"><span class="tag-danger">⚠</span> <span>${escapeHtml(w)}</span></div>`).join("");
    const coresHtml = (pick.core_items || []).map((it) => `<span class="core-chip">${escapeHtml(it)}</span>`).join("");

    card.innerHTML = `
      <div class="card-top-row">
        <div class="hero-meta-title">
          <h4>${escapeHtml(pick.ru_name)} (${escapeHtml(pick.name)})</h4>
          <span class="role-tag">${escapeHtml(pick.role_name)}</span>
        </div>
        <div class="matchup-score-badge ${scoreClass}">${pick.score}%</div>
      </div>

      <div class="matchup-details-list">
        ${countersHtml}
        ${dangerHtml}
      </div>

      <div>
        <span class="stat-label">Ключевой кор-билд:</span>
        <div class="core-preview-row">${coresHtml || "<span class='stat-label'>По ситуации</span>"}</div>
      </div>

      <div class="hero-advice-snippet">${escapeHtml(pick.advice)}</div>

      <div class="card-footer-actions">
        <button type="button" class="btn btn-secondary btn-sm" onclick="speakPhrase('На ${escapeJsString(pick.ru_name)}: ${escapeJsString(pick.advice)}')">
          <span>Озвучить 🔊</span>
        </button>
      </div>
    `;
    grid.appendChild(card);
  });
}

const btnCalculateDraft = document.getElementById("btn-calculate-draft");
if (btnCalculateDraft) {
  btnCalculateDraft.addEventListener("click", triggerDraftCalculation);
}

// Load Hero Build Guide
const btnLoadBuild = document.getElementById("btn-load-build");
if (btnLoadBuild) {
  btnLoadBuild.addEventListener("click", async () => {
    const heroSelect = document.getElementById("build-hero-select");
    const roleSelect = document.getElementById("build-role-select");
    const container = document.getElementById("build-details-container");
    if (!heroSelect || !roleSelect || !container) return;

    const heroName = heroSelect.value;
    const role = parseInt(roleSelect.value, 10);

    container.innerHTML = "<div class='empty-draft-placeholder'><p>Загрузка гайда по закупкам...</p></div>";

    const res = await callApi("get_hero_build_guide", {
      hero_name: heroName,
      role: role,
      enemies: selectedEnemyHeroes,
    });

    if (!res || !res.build) {
      container.innerHTML = "<div class='empty-draft-placeholder'><p>Не удалось получить билд.</p></div>";
      return;
    }

    const b = res.build;
    const startItemsHtml = (b.starting || []).map((it) => `<div class="item-badge-pill"><span>📦</span> <span>${escapeHtml(it)}</span></div>`).join("");
    const earlyItemsHtml = (b.early_game || []).map((it) => `<div class="item-badge-pill"><span>👢</span> <span>${escapeHtml(it)}</span></div>`).join("");
    const coreItemsHtml = (b.core || []).map((it) => `
      <div class="item-badge-pill core-item">
        <span>⚔</span>
        <strong>${escapeHtml(it.item)}</strong>
        <span class="timing-span">${escapeHtml(it.timing)}</span>
      </div>`).join("");

    const sitItemsHtml = (b.situational || []).map((s) => `
      <div class="sit-card">
        <div class="sit-title">
          <span>${escapeHtml(s.item)}</span>
          <span class="sit-trigger">${escapeHtml(s.trigger || "Контр-айтем")}</span>
        </div>
        <p class="sit-desc">${escapeHtml(s.why || s.explanation || "")}</p>
      </div>`).join("");

    container.innerHTML = `
      <div class="build-block-card">
        <div class="build-block-header">
          <h4>🛒 Стартовый закуп (0:00) & Лайнинг</h4>
        </div>
        <div class="items-chips-grid">${startItemsHtml}</div>
        <div style="margin-top: 10px;">
          <span class="stat-label">Ранняя игра:</span>
          <div class="items-chips-grid" style="margin-top: 6px;">${earlyItemsHtml}</div>
        </div>
      </div>

      <div class="build-block-card">
        <div class="build-block-header">
          <h4>⚔ Основные предметы (Core) с таймингами</h4>
        </div>
        <div class="items-chips-grid">${coreItemsHtml}</div>
      </div>

      <div class="build-block-card">
        <div class="build-block-header">
          <h4>🛡 Ситуативные предметы и контр-артефакты</h4>
        </div>
        <div class="situational-grid">${sitItemsHtml}</div>
      </div>

      <div class="build-block-card">
        <div class="build-block-header">
          <h4>🔮 Шард & Аганим</h4>
        </div>
        <p style="font-size: 12px; color: var(--text-main); margin-bottom: 6px;"><strong>Aghanim's Shard:</strong> ${escapeHtml(b.shard || "По таймингу")}</p>
        <p style="font-size: 12px; color: var(--text-dim);"><strong>Aghanim's Scepter:</strong> ${escapeHtml(b.scepter || "По ситуации")}</p>
      </div>

      <div class="build-block-card" style="border-left: 3px solid var(--color-astra);">
        <div class="build-block-header">
          <h4>💡 Тактический совет тренера по игре на линии</h4>
        </div>
        <p style="font-size: 13px; color: var(--text-main); line-height: 1.5;">${escapeHtml(b.lane_tips || "")}</p>
      </div>
    `;
  });
}

// Initialize on DOM ready
document.addEventListener("DOMContentLoaded", () => {
  initWaveVisualizer();
  syncStateLoop();
  loadMetaHeroes();
});

// ===========================================================================
// Modern UX polish layer: splash, toasts, ripple, skill-level hint, tab memory
// ===========================================================================
const SKILL_LEVEL_HINTS = {
  beginner: "Новичкам тренер объясняет термины (БКБ, байбек, вижн) и даёт один простой шаг.",
  amateur: "Баланс: объяснения сложных моментов + тактические советы под позицию.",
  advanced: "Опытным: тайминги, паверспайки, темп карты и меньше базовых объяснений.",
  expert: "Профи: глубокая аналитика, окна драфта, экономика байбека и контроль объектов.",
};

function updateSkillLevelHint(level) {
  const hint = document.getElementById("cfg-skill-hint");
  if (!hint) return;
  hint.textContent = SKILL_LEVEL_HINTS[level] || SKILL_LEVEL_HINTS.beginner;
}

window.updateSkillLevelHint = updateSkillLevelHint;

function showToast(message, type = "info", timeout = 3400) {
  const stack = document.getElementById("toast-stack");
  const text = String(message || "").trim();
  if (!stack || !text) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = text;
  stack.appendChild(toast);
  while (stack.children.length > 4) stack.removeChild(stack.firstChild);
  setTimeout(() => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 320);
  }, timeout);
}
window.aidotaToast = showToast;

document.addEventListener("DOMContentLoaded", () => {
  // 1. Плавное скрытие splash-экрана
  const splash = document.getElementById("ui-splash");
  if (splash) {
    setTimeout(() => {
      splash.classList.add("hidden");
      setTimeout(() => splash.remove(), 700);
    }, 620);
  }

  // 2. Подсказка для уровня игры + мгновенная реакция на смену
  if (elements.cfgSkillLevel) {
    elements.cfgSkillLevel.addEventListener("change", () => updateSkillLevelHint(elements.cfgSkillLevel.value));
    updateSkillLevelHint(elements.cfgSkillLevel.value);
  }

  // 3. Ripple-анимация на кнопках
  document.addEventListener("pointerdown", (event) => {
    const target = event.target.closest(
      ".btn, .icon-btn, .nav-tab, .coach-btn, .role-btn, .preset-card, .model-chip, .pill-btn, .meta-subnav-btn"
    );
    if (!target) return;
    const rect = target.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const ink = document.createElement("span");
    ink.className = "ripple-ink";
    ink.style.width = ink.style.height = `${size}px`;
    ink.style.left = `${event.clientX - rect.left - size / 2}px`;
    ink.style.top = `${event.clientY - rect.top - size / 2}px`;
    if (getComputedStyle(target).position === "static") target.style.position = "relative";
    target.appendChild(ink);
    setTimeout(() => ink.remove(), 650);
  });

  // 4. Красивые toast-уведомления вместо блокирующих alert()
  window.alert = (message) => showToast(message, "info", 4200);

  const saveIndicator = document.getElementById("save-settings-indicator");
  if (saveIndicator) {
    new MutationObserver(() => {
      const text = saveIndicator.textContent.trim();
      if (text) showToast(text, "success");
    }).observe(saveIndicator, { childList: true, characterData: true, subtree: true });
  }

  // 5. Подсветка изменившихся чисел статистики (KDA, золото, нетворс и т.д.)
  const flashValue = (element) => {
    if (!element || element.classList.contains("value-flash")) return;
    element.classList.add("value-flash");
    setTimeout(() => element.classList.remove("value-flash"), 320);
  };
  document.querySelectorAll(".stat-value, .timer-countdown").forEach((element) => {
    let previous = element.textContent;
    new MutationObserver(() => {
      if (element.textContent !== previous) {
        previous = element.textContent;
        flashValue(element);
      }
    }).observe(element, { childList: true, characterData: true, subtree: true });
  });

  // 6. Запоминаем последнюю открытую вкладку + поддержка deep-link (#chat, #settings)
  const activateTab = (name) => {
    const button = Array.from(elements.navTabs).find((tab) => tab.getAttribute("data-tab") === name);
    if (button) button.click();
    return Boolean(button);
  };

  try {
    const hashTab = (location.hash || "").replace("#", "").trim();
    const savedTab = localStorage.getItem("aidota.tab");
    if (!(hashTab && activateTab(hashTab)) && savedTab) {
      activateTab(savedTab);
    }
  } catch (error) {
    /* localStorage/история могут быть недоступны — не критично */
  }
  elements.navTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.getAttribute("data-tab");
      try {
        localStorage.setItem("aidota.tab", name);
        history.replaceState(null, "", `#${name}`);
      } catch (error) {
        /* ignore */
      }
    });
  });

  // 7. Автовысота поля чата
  if (elements.chatTextInput) {
    elements.chatTextInput.addEventListener("input", () => {
      elements.chatTextInput.style.height = "auto";
      elements.chatTextInput.style.height = `${Math.min(120, elements.chatTextInput.scrollHeight)}px`;
    });
  }

  // 8. «Печатает…» в статусе ассистента
  const coachStatus = document.getElementById("coach-status-text");
  if (coachStatus) {
    new MutationObserver(() => {
      const text = coachStatus.textContent.trim();
      if (/активен|слушает/i.test(text)) {
        coachStatus.dataset.typing = "1";
      } else {
        delete coachStatus.dataset.typing;
      }
    }).observe(coachStatus, { childList: true, characterData: true, subtree: true });
  }

  // 9. Совет тренера: ротация подсказок под уровень игры + переход в чат
  const TRAINER_TIPS = {
    beginner: [
      "БКБ (Black King Bar) даёт иммунитет к магии и контролю на несколько секунд — включай его до того, как в тебя прилетит стан.",
      "Байбек — мгновенное возвращение в бой после смерти за золото. Держи запас золота к поздней игре, чтобы не проиграть драку.",
      "Ластхит — добивание крипа. Тренируй 60+ ластхитов за 10 минут: с этого начинается любой фарм.",
      "Вижн — варды, которые видят врагов. Поставил вард — выиграл информацию, а значит и драку.",
      "Не дерись без вижна: сначала посмотри, где враги, потом иди на объектив.",
      "После смерти не беги сразу в ту же драку — посмотри, что можно забрать на карте (руна, лагерь, вышка).",
    ],
    amateur: [
      "Паверспайк — момент, когда твой герой стал сильнее. Собери ключевой предмет и сразу ищи драку или объектив.",
      "Контролируй руны мудрости каждые 7 минут: опыт с руны ускоряет тайминги всей команды.",
      "Принцип «сначала вижн, потом драка»: вард перед объективом экономит больше золота, чем стоит.",
      "Если у врага керри собрал BKB — не трать на него контроль впустую, переключайся на других целей.",
      "Держи линию в балансе: не пушь под вражескую вышку без вардов и выхода на ганги.",
    ],
    advanced: [
      "Считай окна драфта: пока у врага нет BKB, окно контроля открыто — форсируй драки на первых таймингах.",
      "Следи за нетворсом врага: покупка Blink/BKB у их кора — сигнал менять позиционирование и сдвигать фарм.",
      "Темп карты важнее личных киллов: после драки забери ближайший объектив, потом возвращайся в фарм-зону.",
      "Держи экономику байбека к 25+ минуте и учитывай кулдаун выкупа врага перед дракой.",
      "Смоки на 4-6 минуте чаще конвертируются в объектив, если заранее почищен вижн врага.",
    ],
    expert: [
      "Разбирай драфт по окнам: сопоставь тайминги BKB/Pipe у врага с вашими пиками инициации.",
      "Строй план фарма вокруг треугольников и стаков: чисти лагеря по ротации, а не по привычке.",
      "Перед боем проверь: есть ли у врага сейвы (Lotus/Aeon/Glimmer), готов ли ваш контроль пробить BKB.",
      "Играй от экономики команды: одна выигранная драка под Рошана ценнее двух фрагов в пустом лесу.",
      "Фокусь на темпе: если у вас нет инициации — сдвигай карту вижном и вынуждай врага драться в неудобной зоне.",
    ],
  };

  const tipText = document.getElementById("tip-text");
  const tipBadge = document.getElementById("tip-level-badge");
  let tipIndex = 0;

  function currentSkillLevel() {
    return state.skillLevel || "beginner";
  }

  function currentTips() {
    return TRAINER_TIPS[currentSkillLevel()] || TRAINER_TIPS.beginner;
  }

  function renderTip() {
    if (!tipText) return;
    const tips = currentTips();
    tipIndex = tipIndex % tips.length;
    tipText.classList.add("tip-fading");
    setTimeout(() => {
      tipText.textContent = tips[tipIndex];
      tipText.classList.remove("tip-fading");
      tipIndex = (tipIndex + 1) % tips.length;
    }, 220);
  }

  if (tipText) {
    renderTip();
    setInterval(() => {
      if (!document.hidden) renderTip();
    }, 9000);
    setInterval(() => {
      const labels = { beginner: "Новичок", amateur: "Любитель", advanced: "Опытный", expert: "Профи" };
      if (tipBadge) tipBadge.textContent = labels[currentSkillLevel()] || "Новичок";
    }, 1500);
  }

  // Клик по совету-чипу: переключаемся на вкладку чата (сообщение отправит
  // основной обработчик .pill-btn, который уже навешан).
  document.querySelectorAll(".tip-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const chatTab = Array.from(elements.navTabs).find((tab) => tab.getAttribute("data-tab") === "chat");
      if (chatTab) chatTab.click();
    });
  });
});

