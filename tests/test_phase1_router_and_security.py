"""Модульные и интеграционные тесты Фазы 1: DeepSeek-роутер, KV-кэш, секреты и TTS barge-in."""

from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aidota.coach import Coach, OllamaError
from aidota.config import Config, LlmConfig, PlayerProfile, TtsConfig, load_config, save_config
from aidota.doctor import runtime_diagnostics
from aidota.dota_api import DotaApi
from aidota.heroes import HeroCatalog
from aidota.llm_router import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_REASONER_MODEL,
    REASONER_MIN_API_MAX_TOKENS,
    TIER1_PICK_DEADLINE_SEC,
    TIER1_VOICE_DEADLINE_SEC,
    EndpointBreaker,
    LlmRouter,
    PreemptionController,
    classify_http_failure,
    compute_operating_mode,
    detect_provider_by_key,
    extract_openai_message,
)
from aidota.logging_setup import reset_logging, setup_logging
from aidota.prompt_assembly import (
    PromptAssembly,
    build_dynamic_suffix,
    build_match_stable_prefix,
    strip_reasoning_markup,
    trim_history_epoch,
)
from aidota.security import (
    clear_registered_secrets,
    mask_api_key_for_display,
    redact_secrets,
    register_secret,
)
from aidota.telemetry import TelemetrySink, TurnMetric, new_turn_id
from aidota.tts import HumanTTSManager, SpeechPriority
from aidota.web_app import AidotaWebApi, WebGuiController
from tests.helpers import FAKE_HEROES, FakeResponse, gsi_payload, make_temp_dir, remove_temp_dir


class _RecordingSession:
    """Лёгкая подмена requests.Session для проверки точного payload и timeout."""

    def __init__(self, post_handler) -> None:
        self._post_handler = post_handler
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, timeout=None, headers=None, **kwargs):
        return FakeResponse({})

    def post(self, url: str, json=None, timeout=None, headers=None, **kwargs):
        self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        return self._post_handler(url, json or {}, timeout, headers or {})


class _StaticGsi:
    def __init__(self, payload: dict | None) -> None:
        self.payload = payload

    def snapshot(self) -> dict | None:
        return self.payload

    def age(self) -> float:
        return 0.5


class PromptAssemblyTests(unittest.TestCase):
    def test_kv_prefix_is_byte_identical_across_gsi_ticks_and_order(self) -> None:
        prefix_a = build_match_stable_prefix(
            position=1,
            mmr_bracket="Divine",
            hero_pool={"2": ["Invoker"], "1": ["Anti-Mage", "Slark"]},
            signature_heroes=["Anti-Mage"],
            my_picks=["Oracle", "Anti-Mage"],
            enemy_picks=["Pudge", "Axe"],
        )
        prefix_b = build_match_stable_prefix(
            position=1,
            mmr_bracket="Divine",
            hero_pool={"1": ["Anti-Mage", "Slark"], "2": ["Invoker"]},
            signature_heroes=["Anti-Mage"],
            my_picks=["Anti-Mage", "Oracle"],
            enemy_picks=["Axe", "Pudge"],
        )
        self.assertEqual(prefix_a, prefix_b)

        tick1 = PromptAssembly(
            static_prefix="Ты тренер Астра.",
            match_prefix=prefix_a,
            dynamic_suffix=build_dynamic_suffix(gsi_summary="10:00 HP 800/1200 Gold 450"),
        )
        tick2 = PromptAssembly(
            static_prefix="Ты тренер Астра.",
            match_prefix=prefix_b,
            dynamic_suffix=build_dynamic_suffix(gsi_summary="11:30 HP 400/1200 Gold 1900"),
        )
        self.assertEqual(tick1.system_prefix, tick2.system_prefix)
        self.assertNotIn("HP 800", tick1.system_prefix)

        msgs1 = tick1.build_messages([("Привет", "Держи темп")], "Что купить?")
        msgs2 = tick2.build_messages([("Привет", "Держи темп")], "Что купить?")
        self.assertEqual(msgs1[0]["content"], msgs2[0]["content"])
        self.assertIn("HP 800/1200", msgs1[-1]["content"])
        self.assertIn("HP 400/1200", msgs2[-1]["content"])
        self.assertTrue(msgs2[-1]["content"].endswith("Что купить?"))

    def test_epoch_history_truncation_and_think_markup_stripping(self) -> None:
        self.assertEqual(
            strip_reasoning_markup("<think>секретные мысли</think> Купи BKB."),
            "Купи BKB.",
        )
        turns = [(f"Вопрос {i} " + ("x" * 180), f"<think>мысль</think>Ответ {i}") for i in range(10)]
        trimmed = trim_history_epoch(turns, char_budget=1000, epoch_size=4)
        self.assertLessEqual(len(trimmed), 6)
        self.assertEqual((10 - len(trimmed)) % 4, 0)
        for _q, a in trimmed:
            self.assertNotIn("<think>", a)


class RouterAndBreakerTests(unittest.TestCase):
    def test_detect_provider_by_key_and_operating_mode(self) -> None:
        self.assertEqual(
            detect_provider_by_key("sk-deepseek-key-123456"),
            ("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL),
        )
        self.assertEqual(
            detect_provider_by_key("sk-or-v1-12345678")[1],
            "https://openrouter.ai/api/v1",
        )
        self.assertIsNotNone(detect_provider_by_key("AIzaSyTestKey12345"))
        self.assertIsNotNone(detect_provider_by_key("gsk_TestKey12345"))
        self.assertIsNone(detect_provider_by_key(""))

        self.assertEqual(compute_operating_mode(LlmConfig(provider="ollama")), "ollama")
        self.assertEqual(
            compute_operating_mode(
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="sk-test-123456",
                )
            ),
            "deepseek",
        )
        self.assertEqual(
            compute_operating_mode(
                LlmConfig(
                    provider="openai_compatible",
                    base_url=DEEPSEEK_BASE_URL,
                    api_key="",
                )
            ),
            "deterministic",
        )
        self.assertEqual(
            compute_operating_mode(
                LlmConfig(
                    provider="openai_compatible",
                    base_url="https://text.pollinations.ai/openai",
                    api_key="",
                )
            ),
            "pollinations",
        )

    def test_voice_hot_path_bans_reasoner_and_tools(self) -> None:
        router = LlmRouter()
        llm = LlmConfig(
            provider="openai_compatible",
            base_url=DEEPSEEK_BASE_URL,
            api_key="sk-live-secret-99999",
            model=DEEPSEEK_REASONER_MODEL,
        )
        decision = router.plan_route(llm, kind="chat", voice=True)
        self.assertEqual(decision.tier, "tier1")
        self.assertEqual(decision.model, DEEPSEEK_CHAT_MODEL)
        self.assertFalse(decision.allow_tools)
        self.assertFalse(decision.allow_pollinations_failover)
        self.assertLessEqual(decision.deadline_seconds, TIER1_VOICE_DEADLINE_SEC)

    def test_pick_uses_tier1_while_strategic_paths_use_uncapped_tier2(self) -> None:
        router = LlmRouter()
        llm = LlmConfig(
            provider="openai_compatible",
            base_url=DEEPSEEK_BASE_URL,
            api_key="sk-live-secret-99999",
            model=DEEPSEEK_CHAT_MODEL,
            max_tokens=300,
        )
        pick = router.plan_route(llm, kind="pick", voice=False)
        self.assertEqual(pick.tier, "tier1")
        self.assertEqual(pick.model, DEEPSEEK_CHAT_MODEL)
        self.assertLessEqual(pick.deadline_seconds, TIER1_PICK_DEADLINE_SEC)
        self.assertFalse(pick.allow_tools)

        draft = router.plan_route(llm, kind="draft", voice=False)
        self.assertEqual(draft.tier, "tier2")
        self.assertEqual(draft.model, DEEPSEEK_REASONER_MODEL)
        self.assertEqual(draft.fallback_model, DEEPSEEK_CHAT_MODEL)
        self.assertGreaterEqual(draft.api_max_tokens, REASONER_MIN_API_MAX_TOKENS)
        self.assertFalse(draft.send_temperature)
        self.assertFalse(draft.allow_tools)

        tavern_why = router.plan_route(
            llm,
            kind="why",
            voice=False,
            detail_mode="auto",
            hero_alive=False,
            respawn_seconds=25,
        )
        self.assertEqual(tavern_why.tier, "tier2")

    def test_extract_openai_message_separates_reasoning_and_cache_usage(self) -> None:
        text, tools, reasoning, usage = extract_openai_message(
            {
                "choices": [
                    {
                        "message": {
                            "content": "<think>внутренний ход</think>Бери Black King Bar.",
                            "reasoning_content": "Анализ контроля врагов.",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 40,
                    "prompt_cache_hit_tokens": 448,
                    "prompt_cache_miss_tokens": 52,
                    "completion_tokens_details": {"reasoning_tokens": 120},
                },
            }
        )
        self.assertEqual(text, "Бери Black King Bar.")
        self.assertEqual(tools, [])
        self.assertIn("Анализ контроля", reasoning)
        self.assertEqual(usage["cache_hit_tokens"], 448)
        self.assertEqual(usage["reasoning_tokens"], 120)

    def test_endpoint_breaker_transitions_and_http_classification(self) -> None:
        clock = [100.0]
        breaker = EndpointBreaker(failure_threshold=2, cooldown_seconds=10.0, monotonic_fn=lambda: clock[0])
        self.assertTrue(breaker.allow_request("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL))
        breaker.record_failure("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL)
        self.assertEqual(
            breaker.record_failure("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL),
            "open",
        )
        self.assertFalse(breaker.allow_request("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL))

        clock[0] += 11.0
        self.assertTrue(breaker.allow_request("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL))
        self.assertFalse(breaker.allow_request("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL))
        breaker.record_success("openai_compatible", DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL)
        self.assertEqual(breaker.snapshot(), {})

        self.assertEqual(classify_http_failure(401, "Unauthorized"), "auth")
        self.assertEqual(classify_http_failure(402, "Insufficient Balance"), "billing")
        self.assertEqual(classify_http_failure(429, "Rate limit"), "rate_limit")
        self.assertEqual(classify_http_failure(503, "Service unavailable"), "transient")

    def test_preemption_controller_invalidates_stale_turns(self) -> None:
        ctrl = PreemptionController()
        gen1, ev1 = ctrl.start_turn()
        self.assertTrue(ctrl.is_current(gen1))
        self.assertFalse(ev1.is_set())

        gen2, ev2 = ctrl.start_turn()
        self.assertGreater(gen2, gen1)
        self.assertTrue(ev1.is_set())
        self.assertFalse(ctrl.is_current(gen1))
        self.assertTrue(ctrl.is_current(gen2))

        ctrl.cancel_current()
        self.assertTrue(ev2.is_set())
        self.assertFalse(ctrl.is_current(gen2))


class CoachPhase1IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        data = self.tmp / "data"
        data.mkdir()
        (data / "heroes.json").write_text(json.dumps(FAKE_HEROES, ensure_ascii=False), encoding="utf-8")
        (data / "items.json").write_text(json.dumps({"blink": "Blink Dagger"}, ensure_ascii=False), encoding="utf-8")
        self.api = DotaApi(data, session=_RecordingSession(lambda *_a: FakeResponse({})))
        self.api.load_snapshots()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def _make_deepseek_coach(self, session, *, telemetry: TelemetrySink | None = None) -> Coach:
        llm = LlmConfig(
            provider="openai_compatible",
            base_url=DEEPSEEK_BASE_URL,
            api_key="sk-test-phase1-key-123456",
            model=DEEPSEEK_CHAT_MODEL,
        )
        return Coach(
            self.api,
            HeroCatalog(FAKE_HEROES),
            _StaticGsi(gsi_payload()),
            llm,
            PlayerProfile(default_position=1),
            session=session,
            memory_path=self.tmp / "memory.json",
            telemetry=telemetry,
        )

    def test_voice_turn_strips_tools_caps_deadline_and_logs_metrics_jsonl(self) -> None:
        def handler(_url: str, body: dict, _timeout, _headers):
            return FakeResponse(
                {
                    "choices": [{"message": {"content": "Возьми BKB и дави мид."}}],
                    "usage": {
                        "prompt_tokens": 320,
                        "completion_tokens": 12,
                        "prompt_cache_hit_tokens": 256,
                    },
                }
            )

        session = _RecordingSession(handler)
        sink = TelemetrySink(log_dir=self.tmp / "logs")
        coach = self._make_deepseek_coach(session, telemetry=sink)
        try:
            answer = coach.chat("что делать", system="Ты тренер.", kind="chat", voice=True)
        finally:
            coach.close()

        self.assertIn("BKB", answer)
        self.assertEqual(len(session.calls), 1)
        sent = session.calls[0]
        self.assertEqual(sent["json"]["model"], DEEPSEEK_CHAT_MODEL)
        self.assertNotIn("tools", sent["json"])
        self.assertLessEqual(float(sent["timeout"]), TIER1_VOICE_DEADLINE_SEC + 0.1)
        self.assertEqual(sent["json"]["messages"][0]["content"], "Ты тренер.")
        self.assertNotIn("GSI Live", sent["json"]["messages"][0]["content"])

        metrics_file = self.tmp / "logs" / "metrics.jsonl"
        self.assertTrue(metrics_file.is_file())
        lines = [json.loads(line) for line in metrics_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines[-1]["tier"], "tier1")
        self.assertEqual(lines[-1]["cache_hit_tokens"], 256)

    def test_tier2_reasoner_uncaps_max_tokens_strips_temp_and_falls_back_to_chat(self) -> None:
        def handler(_url: str, body: dict, _timeout, _headers):
            if body.get("model") == DEEPSEEK_REASONER_MODEL:
                raise RuntimeError("reasoner timeout")
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "Пикни Oracle под Anti-Mage.",
                                "reasoning_content": "Скрытые шаги рассуждений",
                            }
                        }
                    ]
                }
            )

        session = _RecordingSession(handler)
        coach = self._make_deepseek_coach(session)
        try:
            answer = coach.chat("кого пикнуть", system="Ты тренер.", kind="draft", voice=False)
        finally:
            coach.close()

        self.assertEqual(answer, "Пикни Oracle под Anti-Mage.")
        self.assertNotIn("Скрытые шаги", answer)
        self.assertEqual(len(session.calls), 2)
        first_body = session.calls[0]["json"]
        self.assertEqual(first_body["model"], DEEPSEEK_REASONER_MODEL)
        self.assertGreaterEqual(first_body["max_tokens"], REASONER_MIN_API_MAX_TOKENS)
        self.assertNotIn("temperature", first_body)
        self.assertNotIn("tools", first_body)
        self.assertEqual(session.calls[1]["json"]["model"], DEEPSEEK_CHAT_MODEL)

    def test_auth_401_never_fails_over_to_pollinations(self) -> None:
        def handler(_url: str, _body: dict, _timeout, _headers):
            return FakeResponse({"error": {"message": "Unauthorized API key"}}, status_code=401)

        session = _RecordingSession(handler)
        coach = self._make_deepseek_coach(session)
        try:
            with self.assertRaises(OllamaError):
                coach.chat("привет", system="Ты тренер.", kind="chat", voice=False)
        finally:
            coach.close()

        urls = [c["url"] for c in session.calls]
        self.assertTrue(urls)
        self.assertTrue(all("pollinations" not in u for u in urls))

    def test_check_skills_alerts_runs_without_dig_nameerror(self) -> None:
        payload = gsi_payload()
        payload["map"]["clock_time"] = 355  # 5:55 — близко к 6:00 руне силы
        coach = self._make_deepseek_coach(_RecordingSession(lambda *_a: FakeResponse({})))
        coach.gsi = _StaticGsi(payload)
        try:
            alert = coach.check_skills_alerts()
        finally:
            coach.close()
        self.assertTrue(alert is None or isinstance(alert, str))


class SecurityConfigAndTtsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        clear_registered_secrets()
        reset_logging()

    def tearDown(self) -> None:
        reset_logging()
        clear_registered_secrets()
        remove_temp_dir(self.tmp)

    def test_secret_redaction_in_strings_and_rotating_log(self) -> None:
        secret = "sk-very-secret-token-987654321"
        register_secret(secret)
        self.assertEqual(mask_api_key_for_display(secret), "sk-v...4321")
        self.assertNotIn(secret, redact_secrets(f"Bearer {secret} ?api_key={secret}"))

        log_dir = self.tmp / "logs"
        logger = setup_logging(log_dir, level="INFO")
        logger.error("Ошибка запроса token=%s url=https://x/?key=%s", secret, secret)
        reset_logging()

        log_text = (log_dir / "aidota.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log_text)
        self.assertIn("[REDACTED]", log_text)

    def test_save_and_load_config_obfuscates_api_key_on_disk(self) -> None:
        cfg_path = self.tmp / "config.json"
        cfg = Config(
            llm=LlmConfig(
                provider="openai_compatible",
                base_url=DEEPSEEK_BASE_URL,
                api_key="sk-roundtrip-secret-55555",
                model=DEEPSEEK_CHAT_MODEL,
            )
        )
        save_config(cfg, cfg_path)
        self.assertEqual(cfg.llm.api_key, "sk-roundtrip-secret-55555")

        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        disk_key = raw["llm"]["api_key"]
        self.assertTrue(disk_key.startswith("enc:"))
        self.assertNotIn("sk-roundtrip-secret-55555", json.dumps(raw))

        loaded = load_config(cfg_path)
        self.assertEqual(loaded.llm.api_key, "sk-roundtrip-secret-55555")

    def test_tts_barge_in_preempts_older_generation(self) -> None:
        tts = HumanTTSManager(TtsConfig(enabled=True, provider="edge_tts"))
        try:
            tts.play_audio_file = MagicMock(return_value=True)  # type: ignore[method-assign]
            tts.synthesize = MagicMock(return_value=None)  # type: ignore[method-assign]
            old_gen = tts.current_generation()
            tts.barge_in()
            new_gen = tts.current_generation()
            self.assertGreater(new_gen, old_gen)
            spoken = tts.speak("Устаревший совет", wait=False, priority=SpeechPriority.LLM_COACH, generation_id=old_gen)
            self.assertFalse(spoken)
        finally:
            tts.close()

    def test_telemetry_sink_redacts_turn_metric_errors(self) -> None:
        secret = "sk-leak-check-12345678"
        sink = TelemetrySink(log_dir=self.tmp / "logs")
        sink.record_turn(
            TurnMetric(
                ts=1.0,
                turn_id=new_turn_id(),
                tier="tier1",
                route="chat",
                provider="openai_compatible",
                model=DEEPSEEK_CHAT_MODEL,
                kind="chat",
                ttft_ms=120,
                total_ms=140,
                outcome="error",
                error=f"Authorization: Bearer {secret}",
            )
        )
        content = Path(sink.metrics_path).read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertIn("[REDACTED]", content)


class WebApiPhase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.config_path = self.tmp / "config.json"
        self.config = Config(
            llm=LlmConfig(
                provider="openai_compatible",
                base_url=DEEPSEEK_BASE_URL,
                api_key="sk-webui-secret-9999",
                model=DEEPSEEK_CHAT_MODEL,
            )
        )
        save_config(self.config, self.config_path)
        self.ctrl = WebGuiController(self.config, self.config_path, enable_tray=False)
        self.api = AidotaWebApi(self.ctrl)

    def tearDown(self) -> None:
        self.api.close()
        remove_temp_dir(self.tmp)

    def test_get_state_hides_rust_and_masks_api_key(self) -> None:
        state = self.api.get_state()
        self.assertFalse(state["rust_active"])
        self.assertEqual(state["operating_mode"], "deepseek")
        self.assertIn("runtime_diagnostics", state)
        settings = state["settings"]
        self.assertEqual(settings["llm_api_key"], "")
        self.assertTrue(settings["llm_api_key_set"])
        self.assertEqual(settings["llm_api_key_masked"], "sk-w...9999")

    def test_save_settings_ollama_doctor_and_toggle_mic(self) -> None:
        res = self.api.save_settings(
            {
                "llm_provider": "ollama",
                "llm_base_url": "http://localhost:11434",
                "llm_model": "gemma3:latest",
                "llm_api_key": "",
            }
        )
        self.assertTrue(res["success"])
        self.assertEqual(self.ctrl.config.llm.provider, "ollama")
        self.assertEqual(self.ctrl.config.llm.model, "gemma3:latest")

        checks = self.api.run_doctor(online=False)
        self.assertTrue(checks)
        self.assertIn("details", checks[0])
        self.assertIn("hint", checks[0])

        self.assertFalse(self.api.toggle_mic())
        mock_assistant = MagicMock()
        mock_assistant.toggle_mic.return_value = True
        self.ctrl.assistant = mock_assistant
        self.assertTrue(self.api.toggle_mic())
        mock_assistant.toggle_mic.assert_called_once()

        diag = runtime_diagnostics(self.ctrl.config)
        self.assertEqual(diag["operating_mode"], "ollama")
