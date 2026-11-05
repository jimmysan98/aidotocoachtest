#!/usr/bin/env python
"""Генерация фирменных ассетов AIDOTA (логотип, иконки, баннер) из кода.

Воспроизводимая генерация без внешних ассетов и без сети::

    .\\.venv\\Scripts\\python.exe tools/generate_logo.py

Скрипт создаёт:

* ``assets/logo.svg``          — векторный логотип (гексагон-щит + «A» + искра);
* ``assets/logo.png``          — 512×512 (прозрачный фон);
* ``assets/logo-256.png``      — 256×256;
* ``assets/icon.png``          — 256×256 (иконка приложения/трея);
* ``assets/icon.ico``          — мультиразмерная иконка Windows 16/24/32/48/64/128/256;
* ``assets/readme-banner.png`` — 1280×320, тёмный фон, логотип + название + подпись.

Палитра: фиолетовый Astra ``#a855f7`` → циан ``#38bdf8``.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

# Базовый суперсэмплинг: рисуем крупно, затем аккуратно уменьшаем (LANCZOS).
MASTER = 1024
SS = 2  # множитель суперсэмплинга для мастер-изображения (2048×2048)
S = MASTER * SS

# --- фирменная палитра ---------------------------------------------------- #
ASTRA = (168, 85, 247, 255)        # #a855f7
ASTRA_LIGHT = (216, 180, 254, 255)  # #d8b4fe
VIOLET = (124, 58, 237, 255)       # #7c3aed
CYAN = (56, 189, 248, 255)         # #38bdf8
CYAN_LIGHT = (186, 230, 253, 255)  # #bae6fd
BG_TOP = (21, 17, 41, 255)         # #151129
BG_BOTTOM = (8, 13, 26, 255)       # #080d1a
INK = (6, 9, 18, 255)              # почти чёрный для баннера

FONT_DIR = Path(r"C:\Windows\Fonts")


# --------------------------------------------------------------------------- #
# Низкоуровневые помощники
# --------------------------------------------------------------------------- #
def _hex(color: tuple[int, int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color[:3])


def _lerp(c1, c2, t: float) -> tuple[int, int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))  # type: ignore[return-value]


def linear_gradient(
    size: tuple[int, int],
    stops: list[tuple[float, tuple[int, int, int, int]]],
    angle_deg: float = 135.0,
) -> Image.Image:
    """Линейный градиент по списку (позиция 0..1, цвет)."""
    w, h = size
    y, x = np.mgrid[0:h, 0:w]
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)
    t = x * dx + y * dy
    lo, hi = float(t.min()), float(t.max())
    t = (t - lo) / max(1e-6, hi - lo)

    lut = np.zeros((256, 4), dtype=np.uint8)
    for i in range(256):
        v = i / 255.0
        color = stops[-1][1]
        for j in range(len(stops) - 1):
            p0, c0 = stops[j]
            p1, c1 = stops[j + 1]
            if p0 <= v <= p1:
                local = 0.0 if p1 <= p0 else (v - p0) / (p1 - p0)
                color = _lerp(c0, c1, local)
                break
        lut[i] = color
    idx = (t * 255.0).astype(np.uint8)
    return Image.fromarray(lut[idx], "RGBA")


def radial_glow(
    size: tuple[int, int],
    center: tuple[float, float],
    radius: float,
    color: tuple[int, int, int, int],
) -> Image.Image:
    """Мягкое радиальное свечение (RGBA) в относительных координатах центра."""
    w, h = size
    cx, cy = center[0] * w, center[1] * h
    y, x = np.mgrid[0:h, 0:w]
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max(1e-6, radius)
    alpha = np.clip(1.0 - dist, 0.0, 1.0) ** 2
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0] = color[0]
    arr[..., 1] = color[1]
    arr[..., 2] = color[2]
    arr[..., 3] = (alpha * color[3]).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def rounded_polygon_mask(
    size: int,
    points: list[tuple[float, float]],
    radius: float,
) -> Image.Image:
    """Маска выпуклого многоугольника со скруглёнными углами.

    Скругление делается размытием + порогом (классический трюк Pillow):
    на суперсэмплинге это даёт гладкие радиусы без внешних библиотек.
    """
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.polygon(points, fill=255)
    if radius > 0:
        blurred = mask.filter(ImageFilter.GaussianBlur(radius * 0.62))
        mask = blurred.point(lambda v: 255 if v >= 128 else 0)
    # Сглаживаем бинарный порог для аккуратных краёв после downscale.
    return mask.filter(ImageFilter.GaussianBlur(radius * 0.10 + 0.6))


def star_points(
    cx: float, cy: float, outer: float, inner: float, spikes: int = 4
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    step = math.pi / spikes
    for i in range(spikes * 2):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * step
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    return pts


# --------------------------------------------------------------------------- #
# Логотип
# --------------------------------------------------------------------------- #
# Нормализованный контур щита-гексагона (центр 0,0; диапазон примерно ±0.5).
SHIELD_OUTER = [
    (-0.470, -0.335),
    (-0.245, -0.462),
    (0.245, -0.462),
    (0.470, -0.335),
    (0.470, 0.115),
    (0.000, 0.478),
    (-0.470, 0.115),
]

# Стилизованная «A»: жирные ноги + перекладина, скошенный апекс.
A_OUTLINE = [
    (50.0, 2.0),
    (83.0, 96.0),
    (63.0, 96.0),
    (56.5, 67.0),
    (43.5, 67.0),
    (37.0, 96.0),
    (17.0, 96.0),
]


def _scale_points(
    points: list[tuple[float, float]], size: int, scale: float = 1.0
) -> list[tuple[float, float]]:
    return [((x * scale + 0.5) * size, (y * scale + 0.5) * size) for x, y in points]


def _a_norm_points() -> list[tuple[float, float]]:
    """Контур «A» в нормализованных координатах логотипа."""
    out: list[tuple[float, float]] = []
    for x, y in A_OUTLINE:
        nx = (x - 50.0) / 100.0 * 0.635
        ny = (y - 2.0) / 94.0 * 0.685 - 0.355
        out.append((nx, ny))
    return out


def _a_points(size: int) -> list[tuple[float, float]]:
    """«A» в пикселях холста (масштаб и сдвиг подобраны под контур щита)."""
    return [((nx + 0.5) * size, (ny + 0.5) * size) for nx, ny in _a_norm_points()]


def render_logo_master(compact: bool = False) -> Image.Image:
    """Рисует логотип в суперсэмплинге и возвращает мастер 1024×1024 RGBA.

    ``compact=True`` — упрощённая версия для мелких размеров иконки
    (16–48 px): толще рамка, крупнее «A», без внешнего свечения и искры-тени,
    чтобы силуэт оставался чётким, а не «мыльным».
    """
    size = S
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 1) Внешнее свечение вокруг щита.
    outer_pts = _scale_points(SHIELD_OUTER, size)
    shield_mask = rounded_polygon_mask(size, outer_pts, radius=0.034 * size)
    if not compact:
        halo = shield_mask.filter(ImageFilter.GaussianBlur(0.030 * size))
        halo_img = Image.new("RGBA", (size, size), ASTRA)
        halo_img.putalpha(halo.point(lambda v: int(v * 0.34)))
        image = Image.alpha_composite(image, halo_img)

    # 2) Градиентная рамка (фиолетовый Astra → циан).
    border_frac = 0.042 if compact else 0.026
    border = max(2, int(border_frac * size))
    shrink = 1.0 - 2.0 * border / (size * 0.94)
    inner_pts = _scale_points(SHIELD_OUTER, size, scale=shrink)
    inner_mask = rounded_polygon_mask(size, inner_pts, radius=0.030 * size)
    ring_mask = ImageChops.subtract(shield_mask, inner_mask)

    border_grad = linear_gradient(
        (size, size),
        [(0.0, ASTRA_LIGHT), (0.28, ASTRA), (0.62, VIOLET), (1.0, CYAN)],
        angle_deg=125,
    )
    image.paste(border_grad, (0, 0), ring_mask)

    # 3) Тёмная «стеклянная» заливка щита с фиолетовым свечением сверху.
    fill = linear_gradient((size, size), [(0.0, BG_TOP), (1.0, BG_BOTTOM)], angle_deg=100)
    if not compact:
        glow = radial_glow((size, size), (0.30, 0.14), 0.46 * size, (168, 85, 247, 90))
        fill = Image.alpha_composite(fill, glow)
    inner_fill = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner_fill.paste(fill, (0, 0), inner_mask)
    image = Image.alpha_composite(image, inner_fill)

    # 4) Буква «A» с градиентом и лёгким свечением.
    a_pts = _a_points(size)
    a_mask = rounded_polygon_mask(size, a_pts, radius=0.012 * size)
    if not compact:
        a_glow = a_mask.filter(ImageFilter.GaussianBlur(0.022 * size))
        a_glow_img = Image.new("RGBA", (size, size), (168, 85, 247, 0))
        a_glow_img.putalpha(a_glow.point(lambda v: int(v * 0.55)))
        image = Image.alpha_composite(image, a_glow_img)

    a_grad = linear_gradient(
        (size, size),
        [
            (0.00, (244, 232, 255, 255)),
            (0.34, ASTRA_LIGHT),
            (0.62, ASTRA),
            (0.84, CYAN),
            (1.00, CYAN_LIGHT),
        ],
        angle_deg=115,
    )
    a_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    a_layer.paste(a_grad, (0, 0), a_mask)
    image = Image.alpha_composite(image, a_layer)

    # 5) Искра ИИ (+ руна-точка в полноразмерной версии).
    spark_cx, spark_cy = size * 0.815, size * 0.205
    spark_mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(spark_mask)
    draw.polygon(
        star_points(spark_cx, spark_cy, size * 0.070, size * 0.020, spikes=4), fill=255
    )
    spark_mask = spark_mask.filter(ImageFilter.GaussianBlur(size * 0.0015))
    if not compact:
        spark_glow = spark_mask.filter(ImageFilter.GaussianBlur(size * 0.012))
        glow_img = Image.new(
            "RGBA", (size, size), (CYAN_LIGHT[0], CYAN_LIGHT[1], CYAN_LIGHT[2], 0)
        )
        glow_img.putalpha(spark_glow.point(lambda v: int(v * 0.75)))
        image = Image.alpha_composite(image, glow_img)

    spark_grad = linear_gradient(
        (size, size),
        [(0.0, (255, 255, 255, 255)), (1.0, CYAN_LIGHT)],
        angle_deg=135,
    )
    spark_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    spark_layer.paste(spark_grad, (0, 0), spark_mask)
    image = Image.alpha_composite(image, spark_layer)

    if not compact:
        # Маленькая руна-точка под искрой.
        dot_mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(dot_mask)
        r = size * 0.011
        d.ellipse(
            [spark_cx - size * 0.043 - r, spark_cy + size * 0.052 - r,
             spark_cx - size * 0.043 + r, spark_cy + size * 0.052 + r],
            fill=255,
        )
        dot_layer = Image.new("RGBA", (size, size), CYAN)
        dot_layer.putalpha(dot_mask)
        image = Image.alpha_composite(image, dot_layer)

    # 6) Финальный downscale до 1024 — сглаживает все края.
    return image.resize((MASTER, MASTER), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- #
# SVG (векторная версия того же знака)
# --------------------------------------------------------------------------- #
def build_svg() -> str:
    # Координаты в viewBox 512×512.
    shield = " ".join(f"{x * 512 + 256:.1f},{y * 512 + 256:.1f}" for x, y in SHIELD_OUTER)

    a_pts = []
    for nx, ny in _a_norm_points():
        a_pts.append(f"{nx * 512 + 256:.1f},{ny * 512 + 256:.1f}")
    a_path = " ".join(a_pts)
    spark_cx = 0.815 * 512
    spark_cy = 0.205 * 512
    star = star_points(spark_cx, spark_cy, 512 * 0.070, 512 * 0.020, spikes=4)
    star_path = " ".join(f"{x:.1f},{y:.1f}" for x, y in star)
    dot_cx, dot_cy = spark_cx - 512 * 0.043, spark_cy + 512 * 0.052

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512" role="img" aria-label="AIDOTA logo">
  <defs>
    <linearGradient id="aidota-border" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{_hex(ASTRA_LIGHT)}"/>
      <stop offset="0.34" stop-color="{_hex(ASTRA)}"/>
      <stop offset="0.68" stop-color="{_hex(VIOLET)}"/>
      <stop offset="1" stop-color="{_hex(CYAN)}"/>
    </linearGradient>
    <linearGradient id="aidota-letter" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f0d8ff"/>
      <stop offset="0.42" stop-color="{_hex(ASTRA_LIGHT)}"/>
      <stop offset="0.78" stop-color="{_hex(ASTRA)}"/>
      <stop offset="1" stop-color="{_hex(CYAN)}"/>
    </linearGradient>
    <linearGradient id="aidota-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#151129"/>
      <stop offset="1" stop-color="#080d1a"/>
    </linearGradient>
    <linearGradient id="aidota-spark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="1" stop-color="{_hex(CYAN_LIGHT)}"/>
    </linearGradient>
    <radialGradient id="aidota-glow" cx="0.32" cy="0.18" r="0.65">
      <stop offset="0" stop-color="{_hex(ASTRA)}" stop-opacity="0.35"/>
      <stop offset="1" stop-color="{_hex(ASTRA)}" stop-opacity="0"/>
    </radialGradient>
    <filter id="aidota-soft-glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="9" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>
  <g filter="url(#aidota-soft-glow)">
    <polygon points="{shield}" fill="url(#aidota-fill)" stroke="url(#aidota-border)" stroke-width="14" stroke-linejoin="round"/>
    <polygon points="{shield}" fill="url(#aidota-glow)" opacity="0.9"/>
    <polygon points="{a_path}" fill="url(#aidota-letter)" stroke="url(#aidota-letter)" stroke-width="5" stroke-linejoin="round"/>
    <polygon points="{star_path}" fill="url(#aidota-spark)"/>
    <circle cx="{dot_cx:.1f}" cy="{dot_cy:.1f}" r="5.6" fill="{_hex(CYAN)}"/>
  </g>
</svg>
'''


# --------------------------------------------------------------------------- #
# Текст для баннера.
#
# В этом окружении Pillow собран без FreeType (``_imagingft`` отсутствует),
# поэтому основной путь — рендер текста через Windows GDI: он даёт сглаженные
# кириллические надписи шрифтом Segoe UI без сетевых зависимостей. Если
# FreeType доступен (обычный Pillow), используется он — так скрипт остаётся
# воспроизводимым и на других сборках.
# --------------------------------------------------------------------------- #
def _pillow_text_mask(text: str, size: int, bold: bool) -> Image.Image | None:
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(
            str(FONT_DIR / ("segoeuib.ttf" if bold else "segoeui.ttf")), size
        )
    except Exception:  # noqa: BLE001 — нет FreeType / шрифта
        return None
    probe = Image.new("L", (8, 8))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font)
    pad = 6
    mask = Image.new("L", (bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2), 0)
    ImageDraw.Draw(mask).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)
    return mask


def _gdi_text_mask(
    text: str,
    size: int,
    bold: bool = True,
    font_name: str = "Segoe UI",
) -> Image.Image:
    """Рендер текста в L-маску через Windows GDI (кириллица поддерживается)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    DT_LEFT, DT_TOP, DT_SINGLELINE = 0x00000000, 0x00000000, 0x00000020
    DT_NOPREFIX, DT_CALCRECT = 0x00000800, 0x00000400
    TRANSPARENT, BI_RGB, DIB_RGB_COLORS = 1, 0, 0
    DEFAULT_CHARSET, ANTIALIASED_QUALITY = 1, 4
    FW_BOLD, FW_NORMAL = 700, 400
    base_flags = DT_LEFT | DT_TOP | DT_SINGLELINE | DT_NOPREFIX

    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    font = None
    hbm = None
    old_bmp = None
    try:
        font = gdi32.CreateFontW(
            -int(size), 0, 0, 0, FW_BOLD if bold else FW_NORMAL,
            0, 0, 0, DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, font_name,
        )
        gdi32.SelectObject(hdc, font)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, 0x00FFFFFF)

        # 1) Измеряем текст.
        rect = RECT(0, 0, 0, 0)
        user32.DrawTextW(
            hdc, ctypes.c_wchar_p(text), -1, ctypes.byref(rect),
            base_flags | DT_CALCRECT,
        )
        tw = max(1, rect.right - rect.left)
        th = max(1, rect.bottom - rect.top)
        pad = 4
        w, h = tw + pad * 2, th + pad * 2

        # 2) Рисуем текст белым на чёрном DIB.
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        old_bmp = gdi32.SelectObject(hdc, hbm)
        ctypes.memset(bits, 0, w * h * 4)
        draw_rect = RECT(pad, pad, w - pad, h - pad)
        user32.DrawTextW(
            hdc, ctypes.c_wchar_p(text), -1, ctypes.byref(draw_rect), base_flags
        )
        raw = ctypes.string_at(bits, w * h * 4)
    finally:
        if old_bmp is not None:
            gdi32.SelectObject(hdc, old_bmp)
        if hbm:
            gdi32.DeleteObject(hbm)
        if font:
            gdi32.DeleteObject(font)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(0, hdc_screen)

    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    luma = arr[:, :, :3].max(axis=2)
    mask = Image.fromarray(luma, "L")
    bbox = mask.getbbox()
    if bbox:
        mask = mask.crop(bbox)
    return mask


def text_mask(text: str, size: int, bold: bool = True) -> Image.Image:
    """L-маска надписи: FreeType (если есть) или GDI."""
    mask = _pillow_text_mask(text, size, bold)
    if mask is not None:
        return mask
    try:
        return _gdi_text_mask(text, size, bold)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Не удалось отрисовать текст: Pillow без FreeType и GDI недоступен. "
            "Установите полноценный Pillow или запустите скрипт на Windows."
        ) from exc


def render_banner(logo: Image.Image) -> Image.Image:
    w, h = 1280, 320
    banner = Image.new("RGBA", (w, h), INK)

    # Фон: радиальные свечения Astra / cyan / violet.
    banner = Image.alpha_composite(
        banner, radial_glow((w, h), (0.10, 0.15), 520, (168, 85, 247, 120))
    )
    banner = Image.alpha_composite(
        banner, radial_glow((w, h), (0.92, 0.05), 420, (56, 189, 248, 110))
    )
    banner = Image.alpha_composite(
        banner, radial_glow((w, h), (0.62, 1.05), 480, (124, 58, 237, 80))
    )

    # Тонкая HUD-рамка: градиентная линия сверху и едва заметная снизу.
    grid = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    top = linear_gradient((w, 3), [(0.0, ASTRA), (0.65, VIOLET), (1.0, CYAN)], angle_deg=0)
    grid.paste(top, (0, 0), top)
    ImageDraw.Draw(grid).line([(0, h - 1), (w, h - 1)], fill=(56, 189, 248, 60), width=1)
    banner = Image.alpha_composite(banner, grid)

    # Логотип слева с мягким свечением.
    logo_size = 208
    logo_x, logo_y = 76, (h - logo_size) // 2
    logo_img = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
    logo_glow = logo_img.filter(ImageFilter.GaussianBlur(18))
    logo_glow.putalpha(logo_glow.getchannel("A").point(lambda v: int(v * 0.55)))
    banner.alpha_composite(logo_glow, (logo_x, logo_y))
    banner.alpha_composite(logo_img, (logo_x, logo_y))

    # Название: белый → фиолетовый → циан, с градиентной заливкой.
    title_mask = text_mask("AIDOTA", 132, bold=True)
    sub_mask = text_mask("голосовой ИИ-тренер по Dota 2", 38, bold=False)
    text_x = logo_x + logo_size + 52

    title_h = title_mask.height
    block_h = 6 + 14 + title_h + 14 + sub_mask.height
    top_y = (h - block_h) // 2

    accent = linear_gradient((96, 6), [(0.0, ASTRA), (1.0, CYAN)], angle_deg=0)
    accent.putalpha(230)
    banner.alpha_composite(accent, (text_x, top_y))

    title_y = top_y + 6 + 14
    grad = linear_gradient(
        (title_mask.width, title_h),
        [(0.0, (255, 255, 255, 255)), (0.28, ASTRA_LIGHT), (0.62, ASTRA), (1.0, CYAN)],
        angle_deg=4,
    )
    banner.paste(grad, (text_x, title_y), title_mask)

    sub_y = title_y + title_h + 14
    sub_layer = Image.new("RGBA", sub_mask.size, (148, 163, 184, 255))
    sub_layer.putalpha(sub_mask.point(lambda v: int(v * 0.92)))
    banner.alpha_composite(sub_layer, (text_x, sub_y))

    # Деликатные шестиугольные контуры справа.
    deco = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ddraw = ImageDraw.Draw(deco)
    for radius, alpha in ((150, 26), (108, 18)):
        cx, cy = 1130, 160
        pts = [
            (
                cx + math.cos(math.radians(60 * i - 30)) * radius,
                cy + math.sin(math.radians(60 * i - 30)) * radius,
            )
            for i in range(6)
        ]
        ddraw.polygon(pts, outline=(56, 189, 248, alpha))
    banner = Image.alpha_composite(banner, deco)

    return banner.convert("RGB")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)

    print("[logo] рендер мастер-изображения…")
    logo_master = render_logo_master()
    print("[logo] компактная версия для мелких иконок…")
    compact_master = render_logo_master(compact=True)

    print("[logo] SVG…")
    (ASSETS / "logo.svg").write_text(build_svg(), encoding="utf-8")

    print("[logo] logo.png 512…")
    logo512 = logo_master.resize((512, 512), Image.Resampling.LANCZOS)
    logo512.save(ASSETS / "logo.png")

    print("[logo] logo-256.png…")
    logo256 = logo_master.resize((256, 256), Image.Resampling.LANCZOS)
    logo256.save(ASSETS / "logo-256.png")

    print("[logo] icon.png 256…")
    icon256 = logo_master.resize((256, 256), Image.Resampling.LANCZOS)
    icon256.save(ASSETS / "icon.png")

    print("[logo] icon.ico (16/24/32/48/64/128/256)…")
    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    frames: list[Image.Image] = []
    for size in ico_sizes:
        source = compact_master if size <= 48 else logo_master
        frames.append(source.resize((size, size), Image.Resampling.LANCZOS))
    base = frames[-1]
    base.save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=frames[:-1],
        bitmap_format="bmp",
    )

    print("[logo] readme-banner.png 1280×320…")
    raster_logo = logo_master.resize((512, 512), Image.Resampling.LANCZOS)
    banner = render_banner(raster_logo)
    banner.save(ASSETS / "readme-banner.png")

    # Отчёт-проверка.
    print("\nГотово:")
    for name in ("logo.svg", "logo.png", "logo-256.png", "icon.png", "icon.ico", "readme-banner.png"):
        path = ASSETS / name
        print(f"  {name:20s} {path.stat().st_size:>8d} байт")
    with Image.open(ASSETS / "icon.ico") as ico:
        print(f"  icon.ico sizes: {sorted(ico.info.get('sizes', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
