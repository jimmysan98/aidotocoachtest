"""Надёжный запуск тестов AIDOTA для CI.

`unittest discover` иногда зависает после вывода результата, если в тестах
остаются фоновые потоки (GSI-сервер, аудио, хуки). В CI это приводило бы к
таймауту job'а, хотя тесты уже прошли. Этот раннер печатает результат и
завершает процесс принудительно с правильным кодом возврата.

Запуск: python tools/run_tests.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))

    loader = unittest.TestLoader()
    suite = loader.discover(str(root / "tests"), top_level_dir=str(root))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    sys.stdout.flush()
    sys.stderr.flush()
    # Немедленный выход: не ждём фоновые потоки, если они остались.
    os._exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    raise SystemExit(main())
