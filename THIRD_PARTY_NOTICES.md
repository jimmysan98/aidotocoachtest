# Third-party notices

AIDOTA использует сторонние компоненты. Ниже перечислены основные из них и их
лицензии; полные тексты лицензий распространяются вместе с соответствующими
пакетами.

## Голос и звук

| Компонент | Назначение | Лицензия |
|---|---|---|
| [Vosk](https://github.com/alphacep/vosk-api) | офлайн-распознавание речи | Apache License 2.0 |
| [Vosk Russian models](https://alphacephei.com/vosk/models) | русские акустические модели | Apache License 2.0 |
| [Edge TTS](https://github.com/rany2/edge-tts) | нейросетевые голоса Астра / Джарвис | GPL-3.0 (используется как отдельный процесс/библиотека) |
| [pyttsx3](https://github.com/nateshmbhat/pyttsx3) | офлайн-откат на SAPI | MIT |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | ввод/вывод звука | MIT |
| [soundfile](https://github.com/bastibe/python-soundfile) | чтение/запись WAV | BSD-3-Clause |
| [webrtcvad](https://github.com/wiseman/py-webrtcvad) (опционально) | детектор голосовой активности | MIT |

## ИИ и данные

| Компонент | Назначение | Лицензия |
|---|---|---|
| [OpenDota API](https://www.opendota.com/) | статистика героев и матчей | MIT (клиент), данные — OpenDota |
| [DeepSeek](https://www.deepseek.com/) / [OpenRouter](https://openrouter.ai/) / [Groq](https://groq.com/) / [OpenAI](https://openai.com/) / [Ollama](https://ollama.com/) / [Pollinations](https://pollinations.ai/) | провайдеры LLM | собственные условия сервисов |

## Интерфейс и утилиты

| Компонент | Назначение | Лицензия |
|---|---|---|
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | современный GUI | MIT |
| [Pillow](https://python-pillow.org/) | изображения и иконки | MIT-CMU |
| [pywebview](https://pywebview.flowrl.com/) | веб-дашборд | BSD-3-Clause |
| [pystray](https://github.com/moses-palmer/pystray) | иконка в трее | LGPL-3.0 |
| [OpenCV](https://opencv.org/) | распознавание героя под курсором | Apache License 2.0 |
| [mss](https://github.com/BoboTiG/python-mss) | захват экрана | MIT |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | нечёткий поиск героев и команд | MIT |
| [requests](https://requests.readthedocs.io/) | HTTP-клиент | Apache License 2.0 |
| [NumPy](https://numpy.org/) | численные операции | BSD-3-Clause |
| [pywin32](https://github.com/mhammond/pywin32) | Win32 API, горячие клавиши, COM | PSF |
| [PyInstaller](https://pyinstaller.org/) | сборка приложения (только при сборке) | GPL-2.0 с исключением для bootloader |

## Иконки героев Dota 2

Иконки героев (`hero_icons/`) загружаются из открытых данных Dota 2 / OpenDota и
принадлежат Valve Corporation. Dota 2 является торговой маркой Valve Corporation.
Проект AIDOTA не аффилирован с Valve.

Если вы являетесь правообладателем и хотите изменить условия использования
каких-либо материалов — создайте issue в репозитории проекта.
