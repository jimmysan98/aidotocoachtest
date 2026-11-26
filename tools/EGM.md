# tools

## Overview
Standalone developer and CI utility scripts for the AIDOTA repository. The package provides three independent CLI entry points: deterministic brand asset generation (`generate_logo.py`), interactive microphone and Vosk speech-recognition diagnostics (`mic_test.py`), and a CI-safe `unittest` runner that force-exits to prevent lingering background threads from hanging test runs (`run_tests.py`). This directory has no child code packages.

## Interface
- `[generate_logo.py:ROOT]`, `[generate_logo.py:ASSETS]`, `[generate_logo.py:MASTER]`, `[generate_logo.py:SS]`, `[generate_logo.py:S]`, `[generate_logo.py:FONT_DIR]` — Output path (`ROOT / "assets"`), supersampling dimensions (`MASTER = 1024`, `SS = 2`, `S = 2048`), and Windows font directory (`C:\Windows\Fonts`).
- `[generate_logo.py:ASTRA]`, `[generate_logo.py:ASTRA_LIGHT]`, `[generate_logo.py:VIOLET]`, `[generate_logo.py:CYAN]`, `[generate_logo.py:CYAN_LIGHT]`, `[generate_logo.py:BG_TOP]`, `[generate_logo.py:BG_BOTTOM]`, `[generate_logo.py:INK]`, `[generate_logo.py:SHIELD_OUTER]`, `[generate_logo.py:A_OUTLINE]` — Brand RGBA color palette and normalized shield/`A` vector outlines.
- `[generate_logo.py:linear_gradient]` — `linear_gradient(size, stops, angle_deg=135.0) -> Image.Image` builds a 256-step RGBA linear gradient image.
- `[generate_logo.py:radial_glow]` — `radial_glow(size, center, radius, color) -> Image.Image` builds a quadratic-falloff RGBA radial glow overlay.
- `[generate_logo.py:rounded_polygon_mask]` — `rounded_polygon_mask(size, points, radius) -> Image.Image` builds an antialiased `"L"` polygon mask via blur-and-threshold.
- `[generate_logo.py:star_points]` — `star_points(cx, cy, outer, inner, spikes=4) -> list[tuple[float, float]]` computes star polygon vertices.
- `[generate_logo.py:render_logo_master]` — `render_logo_master(compact=False) -> Image.Image` renders the supersampled shield/`A`/spark master emblem downscaled to `1024x1024` RGBA (`compact=True` thickens the border and omits outer halo, radial fill glow, letter glow, spark glow, and rune dot for small icon legibility).
- `[generate_logo.py:build_svg]` — `build_svg() -> str` returns the `512x512` vector SVG logo document.
- `[generate_logo.py:text_mask]` — `text_mask(text, size, bold=True) -> Image.Image` renders a cropped `"L"` text mask using Pillow FreeType (`segoeuib.ttf`/`segoeui.ttf`) or Windows GDI (`Segoe UI`).
- `[generate_logo.py:render_banner]` — `render_banner(logo) -> Image.Image` composites the `1280x320` RGB README banner from the raster logo and rendered title/subtitle masks.
- `[generate_logo.py:main]` — `main() -> int` CLI entry point (`.venv\Scripts\python.exe tools/generate_logo.py`) generating all brand assets and returning `0`.
- `[mic_test.py:PROJECT_ROOT]` — Repository root (`Path(__file__).resolve().parent.parent`) prepended to `sys.path` before importing `aidota.audio.VoiceInput` and `aidota.config.load_config`.
- `[mic_test.py:main]`, `[mic_test.py:--device]`, `[mic_test.py:--model]` — `main() -> int` interactive CLI entry point (`.venv\Scripts\python.exe tools/mic_test.py [--device INT] [--model PATH]`) returning `0` on `KeyboardInterrupt` or `1` if `sounddevice` is unavailable, `voice.check_model()` fails, or model load/stream start raises.
- `[run_tests.py:main]` — `main() -> int` CI test-runner entry point (`.venv\Scripts\python.exe tools/run_tests.py`) executing `unittest.TestLoader().discover` over `tests/` with `verbosity=2` and exiting `0` on pass or `1` on failure.

## Invariants
- Self-contained offline asset generation (`[generate_logo.py:render_logo_master]`, `[generate_logo.py:build_svg]`, `[generate_logo.py:render_banner]`): all geometry and colors are defined in code with zero network access or external image templates.
- Text rasterization fallback and GDI handle cleanup (`[generate_logo.py:text_mask]`, `[generate_logo.py:_pillow_text_mask]`, `[generate_logo.py:_gdi_text_mask]`): `text_mask` tries Pillow FreeType (`FONT_DIR`) first and falls back to Windows GDI (`user32`/`gdi32`), raising `RuntimeError` if neither succeeds; `_gdi_text_mask` releases `old_bmp`, `hbm`, `font`, `hdc`, and `hdc_screen` in a `finally` block.
- Multi-size ICO fidelity split (`[generate_logo.py:main]`): `assets/icon.ico` embeds `(16, 24, 32, 48, 64, 128, 256)` using `bitmap_format="bmp"`, sourcing `render_logo_master(compact=True)` for sizes `<= 48` and `render_logo_master(compact=False)` for `64..256`.
- Color mode boundary (`[generate_logo.py:render_logo_master]`, `[generate_logo.py:render_banner]`): `render_logo_master` returns `"RGBA"`, whereas `render_banner` returns `"RGB"`.
- Path resolution before package imports (`[mic_test.py:PROJECT_ROOT]`, `[run_tests.py:main]`): both CLI tools insert the resolved repository root (`parent.parent`) at `sys.path[0]` before importing `aidota` modules or running `unittest` discovery.
- Private `VoiceInput` coupling and teardown (`[mic_test.py:main]`): `mic_test.py` requires `sounddevice` and `VoiceInput.check_model()` before loading/starting the stream, reads raw `int16` audio directly from `voice._queue` (`timeout=0.5`) and `voice._recognizer` (`AcceptWaveform`, `Result`, `PartialResult`), ignores per-buffer recognition/JSON errors, and always invokes `voice.close()` in `finally`.
- Forced immediate test-runner exit (`[run_tests.py:main]`): `run_tests.py` flushes `sys.stdout` and `sys.stderr` and terminates via `os._exit(0 | 1)` rather than returning normally or calling `sys.exit`, preventing lingering non-daemon threads (GSI server, audio, hooks) from hanging CI while bypassing `atexit` handlers.

## Side Effects
- Disk writes (`[generate_logo.py:main]`): creates `ROOT / "assets"` if missing and writes/overwrites `assets/logo.svg`, `assets/logo.png`, `assets/logo-256.png`, `assets/icon.png`, `assets/icon.ico`, and `assets/readme-banner.png`, then re-opens `assets/icon.ico` to log embedded sizes to stdout.
- Font/Win32 GDI access (`[generate_logo.py:_pillow_text_mask]`, `[generate_logo.py:_gdi_text_mask]`): reads `C:\Windows\Fonts\segoeuib.ttf` or `segoeui.ttf` when Pillow FreeType is available, or loads `user32.dll`/`gdi32.dll` via `ctypes.WinDLL` to create device contexts, fonts, and DIB bitmaps for text rasterization.
- Process and hardware I/O (`[mic_test.py:main]`): mutates `sys.path`, loads config via `load_config()`, enumerates audio devices via `sounddevice.query_devices()`, loads the Vosk model from disk, opens a live microphone capture stream, and blocks printing volume bars and transcripts to stdout until `KeyboardInterrupt`.
- Process state mutation and test execution (`[run_tests.py:main]`): calls `os.chdir(root)`, mutates `sys.path`, executes the full `tests/` `unittest` suite, writes runner output to stdout/stderr, and terminates the process with `os._exit`.

## Verification
- Run: `.venv\Scripts\python.exe tools/run_tests.py`
