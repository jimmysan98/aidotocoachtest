"""Проверка микрофона и модели Vosk без запуска всего ассистента.

Запуск:
    .venv\\Scripts\\python.exe tools\\mic_test.py
    .venv\\Scripts\\python.exe tools\\mic_test.py --device 8
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aidota.audio import VoiceInput  # noqa: E402
from aidota.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка микрофона и распознавания Vosk")
    parser.add_argument("--device", type=int, default=None, help="номер устройства ввода")
    parser.add_argument("--model", default=None, help="путь к модели Vosk")
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config.audio.model_path = args.model
    if args.device is not None:
        config.audio.input_device = args.device

    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        print(f"sounddevice недоступен: {exc}")
        return 1

    print("Доступные устройства ввода:")
    print(sd.query_devices())
    print("=" * 60)

    voice = VoiceInput(config.audio, PROJECT_ROOT)
    ok, detail = voice.check_model()
    if not ok:
        print(detail)
        return 1
    print("Загружаю модель Vosk (может занять 1–2 минуты)…")
    try:
        voice.load_model(progress=print)
        voice.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")
        return 1

    print("🎤 Говорите. Ctrl+C — выход.\n")

    try:
        while True:
            try:
                data = voice._queue.get(timeout=0.5)  # noqa: SLF001 — диагностический скрипт
            except queue.Empty:
                continue
            import numpy as np

            samples = np.frombuffer(data, dtype=np.int16)
            volume = int(abs(samples).mean()) if samples.size else 0
            if volume > 5:
                bar = "█" * min(volume // 30, 40)
                print(f"ГРОМКОСТЬ: {bar} ({volume})")
            try:
                if voice._recognizer.AcceptWaveform(data):  # noqa: SLF001
                    text = json.loads(voice._recognizer.Result()).get("text", "")  # noqa: SLF001
                    if text:
                        print(f"✅ РАСПОЗНАНО: {text}")
                else:
                    partial = json.loads(
                        voice._recognizer.PartialResult()  # noqa: SLF001
                    ).get("partial", "")
                    if partial:
                        print(f"... {partial}", end="\r")
            except Exception:  # noqa: BLE001
                continue
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        voice.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
