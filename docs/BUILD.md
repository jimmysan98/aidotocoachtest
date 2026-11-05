# Сборка AIDOTA и релиз

Документ для разработчиков: как собрать `AIDOTA.exe`, установщик `AIDOTA_Setup.exe`
и подготовить релиз для GitHub.

## Требования

- Windows 10/11, Python 3.10–3.15 (проверено на 3.15).
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) — только для установщика.
  После установки `ISCC.exe` обычно лежит в
  `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` или
  `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` — `build_exe.py` найдёт его сам.
- Модель Vosk `vosk-model-small-ru-0.22` (~87 МБ) в корне проекта для автономного
  установщика (скачать: <https://alphacephei.com/vosk/models>). Большая модель
  `vosk-model-ru-0.22` (~2.5 ГБ) НЕ упаковывается — приложение умеет докачивать её
  из интерфейса.
1. запускает PyInstaller по спецификации `AIDOTA.spec` → `dist\AIDOTA\AIDOTA.exe`;
## Подготовка окружения

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Тесты

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Ориентир: **481 тест, все зелёные**. После строки `OK` процесс иногда висит из-за
фоновых потоков тестов — это не падение; итог смотри по строке `OK`/`FAILED`.

## Сборка приложения и установщика

```bat
.venv\Scripts\python.exe build_exe.py
```

Скрипт установщика — `installer.iss`. Скрипт:
1. запускает PyInstaller (`AIDOTA.spec`) → `dist\AIDOTA\AIDOTA.exe`;
2. копирует в `dist\AIDOTA` данные (`data/`, `hero_icons/`, `assets/`, `aidota/web/`),
   компактную модель Vosk, `default_preset.json`, `config.example.json`, `LICENSE`,
   `CHANGELOG.md`, `README.md`;
3. если найден Inno Setup — собирает `AIDOTA_Setup.exe` в корне проекта.

Версия приложения берётся из `aidota/__init__.py` (`__version__`) и передаётся в
Inno Setup, поэтому менять номер нужно только в одном месте.

## Проверка собранного приложения

```bat
dist\AIDOTA\AIDOTA.exe --version
dist\AIDOTA\AIDOTA.exe --doctor
```

Тихая установка установщика во временную папку (для проверки):

```bat
AIDOTA_Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /DIR="%TEMP%\AIDOTA-check" /LOG="%TEMP%\aidota-install.log"
"%TEMP%\AIDOTA-check\AIDOTA.exe" --version
"%TEMP%\AIDOTA-check\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
```

## Версия и changelog

1. Поднять `__version__` в `aidota/__init__.py` (semver).
2. Добавить запись в `CHANGELOG.md`.
3. Пересобрать приложение и установщик (см. выше).

## Публикация на GitHub

Репозиторий уже содержит `LICENSE`, `CHANGELOG.md`, `.github/workflows/ci.yml`,
`.gitignore` и `config.example.json`. Пользовательский `config.json`, логи, кэш,
модели Vosk и сборочные артефакты (`dist/`, `AIDOTA_Setup.exe`) в git не попадают.

Рекомендуемый порядок:

```bat
git add -A
git commit -m "AIDOTA 1.1.0: умный тренер, режим для новичков, установщик"
git tag v1.1.0
git push origin main --tags
```

Затем на GitHub в разделе **Releases** создайте релиз `v1.1.0`, прикрепите к нему
`AIDOTA_Setup.exe` (готовый установщик) и скопируйте туда раздел из `CHANGELOG.md`.
Не коммитьте сам `.exe` — он должен быть именно ассетом релиза.

## Частые проблемы

- **`Inno Setup (ISCC.exe) не найден`** — установите Inno Setup 6 и перезапустите
  `build_exe.py`. Установку можно сделать командой
  `winget install --id JRSoftware.InnoSetup`.
- **PyInstaller падает на нативной библиотеке** — обновите `pyinstaller` и
  `pyinstaller-hooks-contrib`, удалите `build/` и `dist/`, повторите сборку.
- **Установщик большой (~110 МБ)** — это ожидаемо: внутри Python-рантайм,
  зависимости и компактная модель Vosk.
- **Нет звука/микрофона на другом ПК** — устройства сбрасываются в `null` в
  `config.example.json`; пользователь выбирает их в Настройках при первом запуске.
