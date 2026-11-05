"""Графический интерфейс настроек AIDOTA: выбор API-ключа ИИ и аудиоустройств."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .audio import list_audio_devices, resolve_device
from .config import SKILL_LEVELS, SKILL_LEVEL_LABELS, Config, normalize_skill_level

log = logging.getLogger("aidota.settings_gui")

#: Пояснения к каждому уровню игры в диалоге настроек.
SKILL_LEVEL_HINTS: dict[str, str] = {
    "beginner": "Новичок — тренер объясняет термины и даёт простые шаги.",
    "amateur": "Любитель — советы без лишней теории, с подсказками по механике.",
    "advanced": "Опытный — тактический разбор без объяснения базовых понятий.",
    "expert": "Профи — мета, тайминги, контрпики и тонкости на уровне высоких MMR.",
}

_BG = "#11161c"
_CARD_BG = "#18202a"
_INPUT_BG = "#0d1117"
_BORDER = "#30363d"
_FG = "#e6edf3"
_MUTED = "#8b98a5"
_ACCENT = "#59d06b"
_BLUE = "#58a6ff"
_DANGER = "#f85149"
_BTN_BG = "#21262d"
_BTN_HOVER = "#30363d"
_SAVE_BG = "#238636"
_SAVE_HOVER = "#2ea043"

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "Бесплатный ИИ (Без ключа)": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "model": "openai",
        "hint": "100% бесплатно, работает сразу без регистрации и без ключа",
    },
    "Gemini Free (Без ключа)": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "model": "gemini",
        "hint": "100% бесплатно, модель Gemini без ключа через Pollinations",
    },
    "Gemini Thinking Free (Рассуждения)": {
        "provider": "openai_compatible",
        "base_url": "https://text.pollinations.ai/openai",
        "model": "gemini-thinking",
        "hint": "100% бесплатно, рассуждающая модель Gemini Thinking через Pollinations",
    },
    "Google Gemini (Официальный)": {
        "provider": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "hint": "Бесплатный ключ на https://aistudio.google.com/app/apikey (Google AI Studio)",
    },
    "OpenRouter Free (Бесплатные)": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.0-flash-exp:free",
        "hint": "Бесплатные модели на OpenRouter (нужен бесплатный ключ c openrouter.ai/keys)",
    },
    "DeepSeek": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "Ключ на https://platform.deepseek.com",
    },
    "OpenRouter": {
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "hint": "Ключ на https://openrouter.ai/keys",
    },
    "Groq (быстрый/бесплатный)": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "hint": "Ключ на https://console.groq.com/keys",
    },
    "OpenAI (ChatGPT)": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "Ключ на https://platform.openai.com/api-keys",
    },
    "Свой OpenAI-совместимый API": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "hint": "Любой совместимый API (LM Studio, vLLM, локальный сервер)",
    },
    "Ollama (локальная)": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "gemma3",
        "hint": "Локальный Ollama (ollama serve)",
    },
}


def test_llm_connection(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Проверяет подключение к указанному API."""
    import requests

    prov = (provider or "openai_compatible").strip().lower()
    base = (base_url or "").rstrip("/")
    if not base:
        return False, "Не указан URL сервера (base_url)"

    if prov == "openai_compatible":
        key = (api_key or "").strip()
        if not key and "pollinations" not in base.lower():
            return False, "Введите API-ключ для проверки"
        headers = {
            "Content-Type": "application/json",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": (model or "deepseek-chat").strip(),
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return True, f"Подключение успешно! Модель «{model}» отвечает."
            if resp.status_code in (401, 403):
                return False, f"Ошибка авторизации (HTTP {resp.status_code}): неверный API-ключ."
            if resp.status_code == 404:
                return False, f"HTTP 404: модель «{model}» не найдена или неверный URL ({base})."
            return False, f"Ошибка HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Не удалось связаться с сервером: {exc}"

    if prov == "ollama":
        try:
            resp = requests.get(f"{base}/api/tags", timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name") for m in data.get("models", []) if m.get("name")]
                return True, f"Ollama подключена! Доступно моделей: {len(models)}."
            return False, f"Ollama вернула код {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Не удалось подключиться к Ollama: {exc}"

    return False, f"Неизвестный провайдер: {provider}"


def play_test_chime(device: int | str | None = None) -> tuple[bool, str]:
    """Воспроизводит приятный проверочный звук на выбранное устройство."""
    try:
        import numpy as np
        import sounddevice as sd

        dev_id, dev_name = resolve_device(device, kind="output")
        sr = 22050
        t1 = np.linspace(0, 0.12, int(sr * 0.12), False)
        t2 = np.linspace(0, 0.22, int(sr * 0.22), False)
        chime1 = np.sin(2 * np.pi * 523 * t1) * 0.15
        chime2 = np.sin(2 * np.pi * 659 * t2) * 0.15
        sound = np.concatenate([chime1, chime2])
        sound = (sound * np.linspace(1, 0, len(sound))).astype(np.float32)

        sd.play(sound, sr, device=dev_id)
        sd.wait()
        return True, f"Звук воспроизведён на «{dev_name}»"
    except Exception as exc:  # noqa: BLE001
        return False, f"Ошибка вывода звука: {exc}"


def enable_universal_clipboard(root: Any) -> None:
    """Включает универсальную обработку Ctrl+C, Ctrl+V, Ctrl+X, Ctrl+A при любой раскладке клавиатуры (включая русскую)."""
    def on_ctrl_key(event: Any) -> str | None:
        w = event.widget
        if not hasattr(w, "insert"):
            return None

        # 86 = V / М, 45 = Insert
        if event.keycode in (86, 45):
            try:
                text = root.clipboard_get()
                if text:
                    try:
                        w.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    w.insert("insert", text)
                return "break"
            except Exception:
                pass
        # 67 = C / С
        elif event.keycode == 67:
            try:
                text = w.selection_get()
                if text:
                    root.clipboard_clear()
                    root.clipboard_append(text)
                return "break"
            except Exception:
                pass
        # 88 = X / Ч
        elif event.keycode == 88:
            try:
                text = w.selection_get()
                if text:
                    root.clipboard_clear()
                    root.clipboard_append(text)
                    w.delete("sel.first", "sel.last")
                return "break"
            except Exception:
                pass
        # 65 = A / Ф
        elif event.keycode == 65:
            try:
                if hasattr(w, "tag_add"):
                    w.tag_add("sel", "1.0", "end")
                elif hasattr(w, "select_range"):
                    w.select_range(0, "end")
                    w.icursor("end")
                return "break"
            except Exception:
                pass
        return None

    try:
        root.bind_all("<Control-KeyPress>", on_ctrl_key, add="+")
    except Exception:
        pass


def attach_context_menu(entry: Any) -> None:
    """Добавляет контекстное меню правой кнопки мыши (Вставить, Копировать, Вырезать, Выделить всё)."""
    target = getattr(entry, "_entry", entry)
    import tkinter as tk

    try:
        menu = tk.Menu(
            target,
            tearoff=0,
            bg="#18202a",
            fg="#f0f6fc",
            activebackground="#253547",
            activeforeground="#ffffff",
            font=("Segoe UI", 9),
            relief="flat",
            borderwidth=1,
        )

        def do_paste():
            try:
                text = target.clipboard_get()
                if text:
                    try:
                        target.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    target.insert("insert", text)
            except Exception:
                pass

        def do_copy():
            try:
                text = target.selection_get()
                if text:
                    target.clipboard_clear()
                    target.clipboard_append(text)
            except Exception:
                pass

        def do_cut():
            try:
                text = target.selection_get()
                if text:
                    target.clipboard_clear()
                    target.clipboard_append(text)
                    target.delete("sel.first", "sel.last")
            except Exception:
                pass

        def do_select_all():
            try:
                if hasattr(target, "select_range"):
                    target.select_range(0, "end")
                    target.icursor("end")
            except Exception:
                pass

        def do_clear():
            try:
                target.delete(0, "end")
            except Exception:
                pass

        menu.add_command(label="📋 Вставить", command=do_paste)
        menu.add_command(label="📄 Копировать", command=do_copy)
        menu.add_command(label="✂ Вырезать", command=do_cut)
        menu.add_separator()
        menu.add_command(label="🔘 Выделить всё", command=do_select_all)
        menu.add_command(label="❌ Очистить", command=do_clear)

        def show_menu(event):
            target.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        target.bind("<Button-3>", show_menu, add="+")
        if hasattr(entry, "bind"):
            entry.bind("<Button-3>", show_menu, add="+")
    except Exception:
        pass


class SettingsDialog:
    """Окно настроек: выбор API-ключа ИИ и устройств ввода/вывода звука."""

    def __init__(
        self,
        parent: Any,
        config: Config,
        on_save: Callable[[dict[str, Any]], None] | None = None,
        on_test_api: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
        on_test_audio: Callable[[int | str | None], tuple[bool, str]] | None = None,
        on_test_match: Callable[[int | None], tuple[bool, str]] | None = None,
    ):
        self.config = config
        self.on_save = on_save
        self.on_test_api = on_test_api or (lambda d: test_llm_connection(
            d.get("provider", ""),
            d.get("base_url", ""),
            d.get("api_key", ""),
            d.get("model", ""),
        ))
        self.on_test_audio = on_test_audio or play_test_chime
        self.on_test_match = on_test_match

        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self._ttk = ttk

        if parent is not None and hasattr(parent, "winfo_exists") and parent.winfo_exists():
            top = tk.Toplevel(parent)
            top.transient(parent)
        else:
            top = tk.Tk()
        self.window = top
        top.title("Настройки AIDOTA")
        top.geometry("580x860")
        top.minsize(530, 720)
        top.configure(bg=_BG)
        try:
            top.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass

        self._captured_mods: set[str] = set()
        self._saved_keys_by_provider: dict[str, str] = {}
        # Запоминаем текущий ключ
        curr_provider = self.config.llm.provider
        if self.config.llm.api_key:
            self._saved_keys_by_provider[curr_provider] = self.config.llm.api_key
        for preset_name, preset_data in (self.config.llm.presets or {}).items():
            if isinstance(preset_data, dict) and preset_data.get("api_key"):
                self._saved_keys_by_provider[preset_name] = preset_data["api_key"]

        self._show_key = False
        self._input_devices: list[dict] = []
        self._output_devices: list[dict] = []
        # Подписи уровней игры в порядке SKILL_LEVELS (для Combobox).
        self.skill_labels: list[str] = [SKILL_LEVEL_LABELS[code] for code in SKILL_LEVELS]

        self._setup_style()
        self._build_ui()
        self._load_values()
        enable_universal_clipboard(self.window)

    def _setup_style(self) -> None:
        style = self._ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure(
            "TCombobox",
            fieldbackground=_INPUT_BG,
            background=_CARD_BG,
            foreground=_FG,
            darkcolor=_BORDER,
            lightcolor=_BORDER,
            arrowcolor=_FG,
            insertcolor=_FG,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", _INPUT_BG), ("focus", _INPUT_BG)],
            foreground=[("readonly", _FG), ("focus", _FG)],
            background=[("readonly", _CARD_BG), ("focus", _CARD_BG)],
        )

    def _build_ui(self) -> None:
        tk = self._tk
        ttk = self._ttk

        container = tk.Frame(self.window, bg=_BG)
        container.pack(fill="both", expand=True, padx=16, pady=12)

        # Заголовок
        header = tk.Label(
            container,
            text="⚙ Настройки AIDOTA",
            font=("Segoe UI", 13, "bold"),
            bg=_BG,
            fg=_ACCENT,
            anchor="w",
        )
        header.pack(fill="x", pady=(0, 2))

        sub = tk.Label(
            container,
            text="Настройка подключения к ИИ через API и устройств звука",
            font=("Segoe UI", 9),
            bg=_BG,
            fg=_MUTED,
            anchor="w",
        )
        sub.pack(fill="x", pady=(0, 10))

        # ============================================================== #
        # Карточка 1: Искусственный Интеллект (LLM API)
        # ============================================================== #
        ai_card = tk.LabelFrame(
            container,
            text=" 🧠 Искусственный Интеллект (API) ",
            font=("Segoe UI", 10, "bold"),
            bg=_CARD_BG,
            fg=_BLUE,
            relief="solid",
            bd=1,
            padx=12,
            pady=10,
        )
        ai_card.pack(fill="x", pady=(0, 10))

        # Выбор провайдера
        tk.Label(ai_card, text="Провайдер ИИ:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.provider_var = tk.StringVar()
        self.provider_combo = ttk.Combobox(
            ai_card,
            textvariable=self.provider_var,
            values=list(PROVIDER_PRESETS.keys()),
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.provider_combo.grid(row=0, column=1, sticky="ew", pady=(0, 4), columnspan=2)
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)

        # Подсказка по получению ключа
        self.provider_hint_lbl = tk.Label(
            ai_card,
            text="",
            font=("Segoe UI", 8, "italic"),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
        )
        self.provider_hint_lbl.grid(row=1, column=1, sticky="w", pady=(0, 6), columnspan=2)

        # API Ключ
        tk.Label(ai_card, text="API-ключ:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=2, column=0, sticky="w", pady=4
        )
        key_box = tk.Frame(ai_card, bg=_CARD_BG)
        key_box.grid(row=2, column=1, sticky="ew", pady=4, columnspan=2)

        self.api_key_entry = tk.Entry(
            key_box,
            font=("Consolas", 10),
            bg=_INPUT_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="solid",
            bd=1,
            show="*",
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        attach_context_menu(self.api_key_entry)

        self.toggle_key_btn = tk.Button(
            key_box,
            text="👁",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=6,
            command=self._toggle_show_key,
        )
        self.toggle_key_btn.pack(side="left", padx=(4, 2))

        paste_btn = tk.Button(
            key_box,
            text="📋 Вставить",
            font=("Segoe UI", 8),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=6,
            command=self._paste_api_key,
        )
        paste_btn.pack(side="left")

        # Модель
        tk.Label(ai_card, text="Модель:", font=("Segoe UI", 9), bg=_CARD_BG, fg=_FG).grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.model_entry = tk.Entry(
            ai_card,
            font=("Segoe UI", 9),
            bg=_INPUT_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="solid",
            bd=1,
        )
        self.model_entry.grid(row=3, column=1, sticky="ew", pady=4, columnspan=2)

        # URL сервера
        tk.Label(ai_card, text="URL (base_url):", font=("Segoe UI", 9), bg=_CARD_BG, fg=_FG).grid(
            row=4, column=0, sticky="w", pady=4
        )
        self.base_url_entry = tk.Entry(
            ai_card,
            font=("Segoe UI", 9),
            bg=_INPUT_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="solid",
            bd=1,
        )
        self.base_url_entry.grid(row=4, column=1, sticky="ew", pady=4, columnspan=2)

        # Кнопка проверки ИИ и статус
        test_ai_box = tk.Frame(ai_card, bg=_CARD_BG)
        test_ai_box.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 2))

        self.test_ai_btn = tk.Button(
            test_ai_box,
            text="⚡ Проверить подключение к ИИ",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_BLUE,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=8,
            pady=3,
            command=self._start_test_api,
        )
        self.test_ai_btn.pack(side="left")

        self.ai_status_lbl = tk.Label(
            test_ai_box,
            text="",
            font=("Segoe UI", 8),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
        )
        self.ai_status_lbl.pack(side="left", padx=8, fill="x", expand=True)

        ai_card.columnconfigure(1, weight=1)

        # ============================================================== #
        # Карточка 2: Ввод и вывод звука
        # ============================================================== #
        audio_card = tk.LabelFrame(
            container,
            text=" 🔊 Устройства звука (Микрофон и Вывод) ",
            font=("Segoe UI", 10, "bold"),
            bg=_CARD_BG,
            fg=_ACCENT,
            relief="solid",
            bd=1,
            padx=12,
            pady=10,
        )
        audio_card.pack(fill="x", pady=(0, 10))

        # Микрофон (Ввод)
        tk.Label(audio_card, text="Микрофон (ввод):", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.input_combo = ttk.Combobox(audio_card, state="readonly", font=("Segoe UI", 9))
        self.input_combo.grid(row=0, column=1, sticky="ew", pady=4, columnspan=2)

        # Динамики (Вывод)
        tk.Label(audio_card, text="Динамики (вывод):", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.output_combo = ttk.Combobox(audio_card, state="readonly", font=("Segoe UI", 9))
        self.output_combo.grid(row=1, column=1, sticky="ew", pady=4)

        self.test_audio_btn = tk.Button(
            audio_card,
            text="🔊 Проверить звук",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=6,
            command=self._start_test_audio,
        )
        self.test_audio_btn.grid(row=1, column=2, sticky="e", padx=(4, 0), pady=4)

        self.audio_status_lbl = tk.Label(
            audio_card,
            text="",
            font=("Segoe UI", 8),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
        )
        self.audio_status_lbl.grid(row=2, column=1, sticky="w", columnspan=2, pady=(0, 4))

        # Голос ассистента (Озвучка)
        tk.Label(audio_card, text="Голос тренера:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.voice_combo = ttk.Combobox(
            audio_card,
            state="readonly",
            font=("Segoe UI", 9),
            values=[
                "✦ Астра (Нейросеть Светлана — женский)",
                "✦ Джарвис (Нейросеть Дмитрий — мужской)",
                "Системный Windows SAPI (офлайн)",
            ],
        )
        self.voice_combo.grid(row=3, column=1, sticky="ew", pady=4, columnspan=2)

        # Режим Push-to-Talk
        tk.Label(audio_card, text="Режим ввода:", font=("Segoe UI", 9), bg=_CARD_BG, fg=_FG).grid(
            row=4, column=0, sticky="w", pady=4
        )
        self.ptt_mode_var = tk.StringVar(value="ptt")
        self.ptt_combo = ttk.Combobox(
            audio_card,
            textvariable=self.ptt_mode_var,
            values=["🎙 Push-to-Talk (по клавише)", "🔊 Свободный микрофон (постоянно)"],
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.ptt_combo.grid(row=4, column=1, sticky="ew", pady=4, columnspan=2)

        # Клавиша скрытия/показа окна (Оверлей)
        tk.Label(audio_card, text="Скрыть/показать окно:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=5, column=0, sticky="w", pady=4
        )
        toggle_box = tk.Frame(audio_card, bg=_CARD_BG)
        toggle_box.grid(row=5, column=1, sticky="ew", pady=4, columnspan=2)

        self.toggle_key_entry = tk.Entry(
            toggle_box,
            font=("Segoe UI", 9),
            bg=_INPUT_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="solid",
            bd=1,
            width=12,
        )
        self.toggle_key_entry.pack(side="left", fill="x", expand=True)

        self.record_hotkey_btn = tk.Button(
            toggle_box,
            text="⌨ Задать",
            font=("Segoe UI", 8),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=6,
            command=self._start_record_hotkey,
        )
        self.record_hotkey_btn.pack(side="left", padx=(4, 4))

        self.toggle_preset_combo = ttk.Combobox(
            toggle_box,
            values=[
                "Пресеты...",
                "Alt+Z (по умолч.)",
                "Alt+X",
                "F8",
                "F9",
                "F10",
                "F11",
                "Ctrl+Shift+Z",
                "~ (Тильда)",
            ],
            state="readonly",
            font=("Segoe UI", 8),
            width=16,
        )
        self.toggle_preset_combo.pack(side="left")
        self.toggle_preset_combo.current(0)
        self.toggle_preset_combo.bind("<<ComboboxSelected>>", self._on_toggle_preset_selected)

        self.toggle_hint_lbl = tk.Label(
            audio_card,
            text="Горячая клавиша скрытия/показа окна поверх игры (по умолч. Alt+Z).",
            font=("Segoe UI", 8, "italic"),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
        )
        self.toggle_hint_lbl.grid(row=6, column=1, sticky="w", pady=(0, 4), columnspan=2)

        audio_card.columnconfigure(1, weight=1)



        # ============================================================== #
        # Карточка 3: Профиль игрока и Анализ сыгранных матчей
        # ============================================================== #
        profile_card = tk.LabelFrame(
            container,
            text=" 🎮 Профиль игрока и Анализ матчей ",
            font=("Segoe UI", 10, "bold"),
            bg=_CARD_BG,
            fg=_BLUE,
            relief="solid",
            bd=1,
            padx=12,
            pady=10,
        )
        profile_card.pack(fill="x", pady=(0, 10))

        # Выбор позиции (роли): 1-5
        tk.Label(profile_card, text="Основная позиция:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.position_combo = ttk.Combobox(
            profile_card,
            state="readonly",
            font=("Segoe UI", 9),
            values=[
                "1 — Керри (Carry / Позиция 1)",
                "2 — Мид (Mid / Позиция 2)",
                "3 — Оффлейн (Offlane / Позиция 3)",
                "4 — Поддержка (Soft Support / Позиция 4)",
                "5 — Полная поддержка (Hard Support / Позиция 5)",
            ],
        )
        self.position_combo.grid(row=0, column=1, sticky="ew", pady=4, columnspan=2)

        # Автономность тренера
        tk.Label(profile_card, text="Автономность тренера:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.autonomy_combo = ttk.Combobox(
            profile_card,
            state="readonly",
            font=("Segoe UI", 9),
            values=[
                "Высокая (советы, паверспайки, макро)",
                "Средняя (тайминги и кулдауны)",
                "Только по запросу (без непрошеных советов)",
            ],
        )
        self.autonomy_combo.grid(row=1, column=1, sticky="ew", pady=4, columnspan=2)

        # Уровень игры (насколько подробно тренер всё объясняет)
        tk.Label(profile_card, text="Уровень игры:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.skill_combo = ttk.Combobox(
            profile_card,
            state="readonly",
            font=("Segoe UI", 9),
            values=self.skill_labels,
        )
        self.skill_combo.grid(row=2, column=1, sticky="ew", pady=4, columnspan=2)
        self.skill_combo.bind("<<ComboboxSelected>>", self._on_skill_level_changed)

        self.skill_hint_lbl = tk.Label(
            profile_card,
            text=SKILL_LEVEL_HINTS["beginner"],
            font=("Segoe UI", 8, "italic"),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
            justify="left",
        )
        self.skill_hint_lbl.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

        # Dota 2 Account ID
        tk.Label(profile_card, text="Dota 2 Account ID:", font=("Segoe UI", 9, "bold"), bg=_CARD_BG, fg=_FG).grid(
            row=4, column=0, sticky="w", pady=4
        )
        self.account_id_entry = tk.Entry(
            profile_card,
            font=("Segoe UI", 9),
            bg=_INPUT_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="solid",
            bd=1,
        )
        self.account_id_entry.grid(row=4, column=1, sticky="ew", pady=4, columnspan=2)

        tk.Label(
            profile_card,
            text="Steam32 ID из профиля Dota 2 (или автоопределение через GSI).\n"
            "В игре включите: Настройки -> Сообщество -> «Общедоступная история матчей».",
            font=("Segoe UI", 8, "italic"),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 6))

        match_box = tk.Frame(profile_card, bg=_CARD_BG)
        match_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(2, 2))

        self.test_match_btn = tk.Button(
            match_box,
            text="📊 Разобрать последний матч",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_BLUE,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=8,
            pady=3,
            command=self._start_test_match,
        )
        self.test_match_btn.pack(side="left")

        self.match_status_lbl = tk.Label(
            match_box,
            text="",
            font=("Segoe UI", 8),
            bg=_CARD_BG,
            fg=_MUTED,
            anchor="w",
        )
        self.match_status_lbl.pack(side="left", padx=8, fill="x", expand=True)

        profile_card.columnconfigure(1, weight=1)

        # ============================================================== #
        # Нижняя панель с кнопками сохранения
        # ============================================================== #
        btn_bar = tk.Frame(container, bg=_BG)
        btn_bar.pack(fill="x", side="bottom", pady=(6, 0))

        self.save_status_lbl = tk.Label(
            btn_bar,
            text="",
            font=("Segoe UI", 9, "bold"),
            bg=_BG,
            fg=_ACCENT,
            anchor="w",
        )
        self.save_status_lbl.pack(side="left", fill="x", expand=True)

        reset_btn = tk.Button(
            btn_bar,
            text="🔄 Сбросить настройки по умолчанию",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=12,
            pady=5,
            command=self._reset_defaults,
        )
        reset_btn.pack(side="left")

        cancel_btn = tk.Button(
            btn_bar,
            text="Отмена",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_FG,
            activebackground=_BTN_HOVER,
            activeforeground=_FG,
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=12,
            pady=5,
            command=self.window.destroy,
        )
        cancel_btn.pack(side="right", padx=(8, 0))

        save_btn = tk.Button(
            btn_bar,
            text="💾 Сохранить и применить",
            font=("Segoe UI", 9, "bold"),
            bg=_SAVE_BG,
            fg="#ffffff",
            activebackground=_SAVE_HOVER,
            activeforeground="#ffffff",
            relief="flat",
            bd=1,
            cursor="hand2",
            padx=14,
            pady=5,
            command=self._save_settings,
        )
        save_btn.pack(side="right")

    # ------------------------------------------------------------------ #
    def _load_values(self) -> None:
        # 1. Загрузка устройств звука
        inputs, outputs = list_audio_devices()
        self._input_devices = inputs
        self._output_devices = outputs

        in_values = ["По умолчанию в системе"] + [d["label"] for d in inputs]
        out_values = ["По умолчанию в системе"] + [d["label"] for d in outputs]

        self.input_combo["values"] = in_values
        self.output_combo["values"] = out_values

        # Выбираем текущий input
        cur_in = self.config.audio.input_device
        in_idx = 0
        if cur_in is not None:
            for idx, d in enumerate(inputs, start=1):
                if d["index"] == cur_in or str(cur_in).lower() in d["name"].lower():
                    in_idx = idx
                    break
        self.input_combo.current(in_idx)

        # Выбираем текущий output
        cur_out = getattr(self.config.tts, "output_device", None)
        out_idx = 0
        if cur_out is not None:
            for idx, d in enumerate(outputs, start=1):
                if d["index"] == cur_out or str(cur_out).lower() in d["name"].lower():
                    out_idx = idx
                    break
        self.output_combo.current(out_idx)

        # Выбор голоса тренера (Astra)
        tts_prov = getattr(self.config.tts, "provider", "edge")
        edge_v = getattr(self.config.tts, "edge_voice", "ru-RU-SvetlanaNeural")
        if tts_prov == "sapi":
            self.voice_combo.current(2)
        elif "dmitry" in str(edge_v).lower():
            self.voice_combo.current(1)
        else:
            self.voice_combo.current(0)

        # PTT
        if self.config.audio.push_to_talk:
            self.ptt_combo.current(0)
        else:
            self.ptt_combo.current(1)

        # Хоткей скрытия/показа
        cur_toggle = getattr(self.config.overlay, "toggle_key", "alt+z") or "alt+z"
        if str(cur_toggle).strip() == "5":
            cur_toggle = "alt+z"
        from .hotkeys import format_key_name
        self.toggle_key_entry.delete(0, "end")
        self.toggle_key_entry.insert(0, format_key_name(cur_toggle))



        # 2. Загрузка провайдера ИИ
        cur_provider = (self.config.llm.provider or "openai_compatible").lower()
        cur_url = (self.config.llm.base_url or "").rstrip("/")

        matched_name = "DeepSeek"
        for name, preset in PROVIDER_PRESETS.items():
            if preset["provider"] == cur_provider and preset["base_url"].rstrip("/") == cur_url:
                matched_name = name
                break
        else:
            if cur_provider == "ollama":
                matched_name = "Ollama (локальная)"
            else:
                matched_name = "Свой OpenAI-совместимый API"

        self.provider_combo.set(matched_name)
        self.provider_hint_lbl.configure(text=PROVIDER_PRESETS[matched_name]["hint"])

        self.api_key_entry.delete(0, "end")
        self.api_key_entry.insert(0, self.config.llm.api_key or "")

        self.model_entry.delete(0, "end")
        self.model_entry.insert(0, self.config.llm.model or "")

        self.base_url_entry.delete(0, "end")
        self.base_url_entry.insert(0, self.config.llm.base_url or "")

        # 3. Профиль игрока, позиция (1..5) и автономность
        cur_pos = getattr(self.config.player_profile, "default_position", 1) or 1
        if isinstance(cur_pos, int) and 1 <= cur_pos <= 5:
            self.position_combo.current(cur_pos - 1)
        else:
            self.position_combo.current(0)

        cur_autonomy = str(getattr(self.config.player_profile, "autonomy_level", "medium")).lower()
        if cur_autonomy in ("high", "высокая"):
            self.autonomy_combo.current(0)
        elif cur_autonomy in ("off", "none", "request_only", "только по запросу", "выкл"):
            self.autonomy_combo.current(2)
        else:
            self.autonomy_combo.current(1)

        cur_acc = self.config.player_profile.account_id
        self.account_id_entry.delete(0, "end")
        if cur_acc:
            self.account_id_entry.insert(0, str(cur_acc))

        # 4. Уровень игры: beginner / amateur / advanced / expert
        cur_skill = normalize_skill_level(
            getattr(self.config.player_profile, "skill_level", "beginner")
        )
        try:
            self.skill_combo.current(SKILL_LEVELS.index(cur_skill))
        except ValueError:
            self.skill_combo.current(0)
        self.skill_hint_lbl.configure(text=SKILL_LEVEL_HINTS[cur_skill])

    def _on_skill_level_changed(self, event: Any = None) -> None:  # noqa: ANN001
        """Обновляет подсказку под выпадающим списком уровня игры."""
        idx = self.skill_combo.current()
        code = SKILL_LEVELS[idx] if 0 <= idx < len(SKILL_LEVELS) else "beginner"
        self.skill_hint_lbl.configure(text=SKILL_LEVEL_HINTS[code])

    def _start_record_hotkey(self) -> None:
        self.record_hotkey_btn.configure(
            text="[ Нажмите клавиши... (Esc — отмена) ]",
            fg=_ACCENT,
            relief="sunken",
        )
        self._captured_mods = set()
        self.window.bind_all("<KeyPress>", self._on_hotkey_press)
        self.window.bind_all("<KeyRelease>", self._on_hotkey_release)
        self.record_hotkey_btn.focus_force()

    def _on_hotkey_press(self, event: Any) -> str | None:
        vk = int(getattr(event, "keycode", 0) or 0)
        keysym = str(getattr(event, "keysym", "")).lower()

        if vk == 27 or keysym == "escape":
            self._finish_hotkey_record(None)
            return "break"

        # Если это клавиша-модификатор
        if vk in (18, 164, 165) or keysym in ("alt_l", "alt_r") or "alt" in keysym:
            self._captured_mods.add("Alt")
            self.record_hotkey_btn.configure(text="[ Alt + ... ]")
            return "break"
        if vk in (17, 162, 163) or keysym in ("control_l", "control_r") or "control" in keysym:
            self._captured_mods.add("Ctrl")
            self.record_hotkey_btn.configure(text="[ Ctrl + ... ]")
            return "break"
        if vk in (16, 160, 161) or keysym in ("shift_l", "shift_r") or "shift" in keysym:
            self._captured_mods.add("Shift")
            self.record_hotkey_btn.configure(text="[ Shift + ... ]")
            return "break"

        import ctypes
        u32 = ctypes.windll.user32
        alt_down = (
            bool(u32.GetAsyncKeyState(0x12) & 0x8000)
            or ("Alt" in self._captured_mods)
            or bool(event.state & 0x20000)
            or bool(event.state & 0x0008)
        )
        ctrl_down = (
            bool(u32.GetAsyncKeyState(0x11) & 0x8000)
            or ("Ctrl" in self._captured_mods)
            or bool(event.state & 0x0004)
        )
        shift_down = (
            bool(u32.GetAsyncKeyState(0x10) & 0x8000)
            or ("Shift" in self._captured_mods)
            or bool(event.state & 0x0001)
        )

        mods = []
        if ctrl_down:
            mods.append("Ctrl")
        if alt_down:
            mods.append("Alt")
        if shift_down:
            mods.append("Shift")

        from .hotkeys import format_key_name, _CYRILLIC_KEYSYMS, _RU_TO_EN
        if vk:
            key_name = format_key_name(vk)
        else:
            raw_sym = str(getattr(event, "keysym", "")).lower()
            if raw_sym in _CYRILLIC_KEYSYMS:
                raw_sym = _CYRILLIC_KEYSYMS[raw_sym]
            if raw_sym in _RU_TO_EN:
                raw_sym = _RU_TO_EN[raw_sym]
            key_name = raw_sym.upper()
        combo = "+".join(mods + [key_name]) if mods else key_name
        self._finish_hotkey_record(combo)
        return "break"

    def _on_hotkey_release(self, event: Any) -> None:
        vk = int(getattr(event, "keycode", 0) or 0)
        keysym = str(getattr(event, "keysym", "")).lower()
        if (vk in (18, 164, 165) or "alt" in keysym) and "Alt" in self._captured_mods:
            self._captured_mods.discard("Alt")
        if (vk in (17, 162, 163) or "control" in keysym) and "Ctrl" in self._captured_mods:
            self._captured_mods.discard("Ctrl")
        if (vk in (16, 160, 161) or "shift" in keysym) and "Shift" in self._captured_mods:
            self._captured_mods.discard("Shift")

    def _finish_hotkey_record(self, combo: str | None) -> None:
        try:
            self.window.unbind_all("<KeyPress>")
            self.window.unbind_all("<KeyRelease>")
        except Exception:  # noqa: BLE001
            pass
        self.record_hotkey_btn.configure(
            text="⌨ Задать",
            fg=_FG,
            relief="flat",
        )
        if combo:
            self.toggle_key_entry.delete(0, "end")
            self.toggle_key_entry.insert(0, combo)

    def _on_toggle_preset_selected(self, event: Any = None) -> None:
        val = self.toggle_preset_combo.get()
        if not val or val.startswith("Пресеты"):
            return
        combo = val.split(" ")[0].strip()
        self.toggle_key_entry.delete(0, "end")
        self.toggle_key_entry.insert(0, combo)
        self.toggle_preset_combo.current(0)

    def _on_provider_changed(self, event=None) -> None:  # noqa: ANN001
        name = self.provider_combo.get()
        if name not in PROVIDER_PRESETS:
            return
        preset = PROVIDER_PRESETS[name]
        self.provider_hint_lbl.configure(text=preset["hint"])

        # Сохраняем текущий введённый ключ в кэше провайдеров
        curr_key = self.api_key_entry.get().strip()

        # Обновляем base_url и model
        self.base_url_entry.delete(0, "end")
        self.base_url_entry.insert(0, preset["base_url"])

        if preset["model"]:
            self.model_entry.delete(0, "end")
            self.model_entry.insert(0, preset["model"])

        # Подставляем ключ из сохранённых пресетов, если он есть
        key_name = name.lower()
        if "deepseek" in key_name:
            preset_key = self._saved_keys_by_provider.get("deepseek")
        elif "openrouter" in key_name:
            preset_key = self._saved_keys_by_provider.get("openrouter")
        elif "groq" in key_name:
            preset_key = self._saved_keys_by_provider.get("groq")
        elif "openai" in key_name:
            preset_key = self._saved_keys_by_provider.get("openai")
        else:
            preset_key = None

        if preset_key:
            self.api_key_entry.delete(0, "end")
            self.api_key_entry.insert(0, preset_key)
        elif not curr_key and name == "Ollama (локальная)":
            self.api_key_entry.delete(0, "end")

    def _toggle_show_key(self) -> None:
        self._show_key = not self._show_key
        if self._show_key:
            self.api_key_entry.configure(show="")
            self.toggle_key_btn.configure(text="🙈")
        else:
            self.api_key_entry.configure(show="*")
            self.toggle_key_btn.configure(text="👁")

    def _paste_api_key(self) -> None:
        try:
            clip = self.window.clipboard_get().strip()
            if clip:
                self.api_key_entry.delete(0, "end")
                self.api_key_entry.insert(0, clip)
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка вставки из буфера: %s", exc)

    def _start_test_api(self) -> None:
        provider_name = self.provider_combo.get()
        provider_cfg = PROVIDER_PRESETS.get(provider_name, {})
        provider_type = provider_cfg.get("provider", "openai_compatible")

        settings = {
            "provider": provider_type,
            "base_url": self.base_url_entry.get().strip(),
            "api_key": self.api_key_entry.get().strip(),
            "model": self.model_entry.get().strip(),
        }

        self.ai_status_lbl.configure(text="⏳ Проверяю подключение...", fg=_BLUE)
        self.test_ai_btn.configure(state="disabled")

        def worker() -> None:
            ok, msg = self.on_test_api(settings)
            def update() -> None:
                if not self.window.winfo_exists():
                    return
                self.test_ai_btn.configure(state="normal")
                if ok:
                    self.ai_status_lbl.configure(text=f"✓ {msg}", fg=_ACCENT)
                else:
                    self.ai_status_lbl.configure(text=f"✗ {msg}", fg=_DANGER)
            self.window.after(0, update)

        threading.Thread(target=worker, name="aidota-test-api", daemon=True).start()

    def _start_test_audio(self) -> None:
        out_idx = self.output_combo.current()
        selected_dev = None
        if out_idx > 0 and out_idx - 1 < len(self._output_devices):
            selected_dev = self._output_devices[out_idx - 1]["index"]

        self.audio_status_lbl.configure(text="🔊 Воспроизведение проверочного звука...", fg=_BLUE)
        self.test_audio_btn.configure(state="disabled")

        def worker() -> None:
            ok, msg = self.on_test_audio(selected_dev)
            def update() -> None:
                if not self.window.winfo_exists():
                    return
                self.test_audio_btn.configure(state="normal")
                if ok:
                    self.audio_status_lbl.configure(text=f"✓ {msg}", fg=_ACCENT)
                else:
                    self.audio_status_lbl.configure(text=f"✗ {msg}", fg=_DANGER)
            self.window.after(0, update)

        threading.Thread(target=worker, name="aidota-test-audio", daemon=True).start()

    def _quick_match_check(self, account_id: int | None) -> tuple[bool, str]:
        try:
            from .coach import Coach
            from .dota_api import DotaApi
            from .heroes import HeroCatalog

            api = DotaApi(self.config.data_path)
            api.load_snapshots()
            catalog = HeroCatalog(api.heroes)
            coach = Coach(api, catalog, None, self.config.llm, self.config.player_profile)
            res = coach.analyze_match(account_id=account_id)
            return True, res
        except Exception as exc:  # noqa: BLE001
            return False, f"Ошибка: {exc}"

    def _start_test_match(self) -> None:
        raw_acc = self.account_id_entry.get().strip()
        acc_id = int(raw_acc) if (raw_acc.isdigit() and int(raw_acc) > 0) else None

        self.match_status_lbl.configure(text="⏳ Загрузка и анализ матча...", fg=_BLUE)
        self.test_match_btn.configure(state="disabled")

        def worker() -> None:
            if self.on_test_match is not None:
                ok, msg = self.on_test_match(acc_id)
            else:
                ok, msg = self._quick_match_check(acc_id)

            def update() -> None:
                if not self.window.winfo_exists():
                    return
                self.test_match_btn.configure(state="normal")
                if ok:
                    self.match_status_lbl.configure(text="✓ Анализ выполнен", fg=_ACCENT)
                    self._show_match_dialog(msg)
                else:
                    self.match_status_lbl.configure(text=f"✗ {msg[:55]}", fg=_DANGER)

            self.window.after(0, update)

        threading.Thread(target=worker, name="aidota-test-match", daemon=True).start()

    def _show_match_dialog(self, text: str) -> None:
        tk = self._tk
        top = tk.Toplevel(self.window)
        top.title("📊 Разбор матча — AIDOTA")
        top.geometry("640x520")
        top.minsize(450, 350)
        top.configure(bg=_BG)
        try:
            top.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass

        box = tk.Frame(top, bg=_BG)
        box.pack(fill="both", expand=True, padx=12, pady=12)

        txt = tk.Text(
            box,
            wrap="word",
            bg=_INPUT_BG,
            fg=_FG,
            font=("Segoe UI", 10),
            padx=10,
            pady=10,
            relief="solid",
            bd=1,
        )
        scr = tk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        scr.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.insert("1.0", text)
        txt.configure(state="disabled")

        close_btn = tk.Button(
            top,
            text="Закрыть",
            font=("Segoe UI", 9),
            bg=_BTN_BG,
            fg=_FG,
            relief="flat",
            bd=1,
            padx=16,
            pady=4,
            command=top.destroy,
        )
        close_btn.pack(pady=(0, 10))

    def _save_settings(self) -> None:
        provider_name = self.provider_combo.get()
        provider_cfg = PROVIDER_PRESETS.get(provider_name, {})
        provider_type = provider_cfg.get("provider", "openai_compatible")

        in_idx = self.input_combo.current()
        input_dev = None
        if in_idx > 0 and in_idx - 1 < len(self._input_devices):
            input_dev = self._input_devices[in_idx - 1]["index"]

        out_idx = self.output_combo.current()
        output_dev = None
        if out_idx > 0 and out_idx - 1 < len(self._output_devices):
            output_dev = self._output_devices[out_idx - 1]["index"]

        ptt_enabled = (self.ptt_combo.current() == 0)

        raw_acc = self.account_id_entry.get().strip()
        account_id = int(raw_acc) if (raw_acc.isdigit() and int(raw_acc) > 0) else None

        v_idx = self.voice_combo.current()
        if v_idx == 1:
            tts_provider = "edge"
            edge_voice = "ru-RU-DmitryNeural"
            coach_name = "Джарвис"
        elif v_idx == 2:
            tts_provider = "sapi"
            edge_voice = "ru-RU-SvetlanaNeural"
            coach_name = "Астра"
        else:
            tts_provider = "edge"
            edge_voice = "ru-RU-SvetlanaNeural"
            coach_name = "Астра"

        pos_idx = self.position_combo.current()
        default_pos = (pos_idx + 1) if pos_idx >= 0 else 1

        aut_idx = self.autonomy_combo.current()
        aut_map = {0: "high", 1: "medium", 2: "off"}
        autonomy_level = aut_map.get(aut_idx, "medium")

        skill_idx = self.skill_combo.current()
        skill_level = (
            SKILL_LEVELS[skill_idx]
            if 0 <= skill_idx < len(SKILL_LEVELS)
            else normalize_skill_level(getattr(self.config.player_profile, "skill_level", "beginner"))
        )

        raw_toggle = self.toggle_key_entry.get().strip().replace(" ", "") or "alt+z"
        if raw_toggle == "5":
            raw_toggle = "alt+z"

        data = {
            "provider": provider_type,
            "provider_name": provider_name,
            "base_url": self.base_url_entry.get().strip(),
            "api_key": self.api_key_entry.get().strip(),
            "model": self.model_entry.get().strip(),
            "input_device": input_dev,
            "output_device": output_dev,
            "push_to_talk": ptt_enabled,
            "account_id": account_id,
            "default_position": default_pos,
            "autonomy_level": autonomy_level,
            "skill_level": skill_level,
            "tts_provider": tts_provider,
            "edge_voice": edge_voice,
            "coach_name": coach_name,
            "toggle_key": raw_toggle,
        }

        if self.on_save is not None:
            try:
                self.on_save(data)
                self.save_status_lbl.configure(text="✓ Настройки сохранены!", fg=_ACCENT)
                self.window.after(600, self.window.destroy)
            except Exception as exc:  # noqa: BLE001
                self.save_status_lbl.configure(text=f"✗ Ошибка: {exc}", fg=_DANGER)
        else:
            self.window.destroy()

    def _reset_defaults(self) -> None:
        """Сбрасывает конфигурацию к эталонному пресету разработчика."""
        try:
            from tkinter import messagebox
            from .config import DEFAULT_CONFIG_PATH, reset_to_defaults

            cfg_path = getattr(self.config, "config_path", None) or DEFAULT_CONFIG_PATH
            new_cfg = reset_to_defaults(cfg_path)
            self.config = new_cfg
            self._load_values()
            messagebox.showinfo(
                "Сброс настроек",
                "Настройки успешно сброшены к эталонному пресету по умолчанию!",
                parent=self.window,
            )
        except Exception as exc:  # noqa: BLE001
            from tkinter import messagebox

            messagebox.showerror(
                "Ошибка сброса",
                f"Не удалось сбросить настройки: {exc}",
                parent=self.window,
            )
