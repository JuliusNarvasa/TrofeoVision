"""PIL-based dashboard renderer for the Thermalright Trofeo Vision 9.16".

Buffer is 462x1920 portrait. Each frame is drawn from a SensorSnapshot.

Design notes:
    * All measurements in pixels at native resolution — no scaling.
    * The visual style is monochrome (matching the reference image),
      with subtle gray accents and white cards on a light gray background.
    * Every draw helper is small and self-contained so the layout is
      easy to tweak.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from .sensors_types import SensorSnapshot


# ─────────────────────────────────────────────────────────────────────────────
# Color palette — kept monochrome on purpose, matches the reference image.
# ─────────────────────────────────────────────────────────────────────────────
class C:
    BG          = (240, 240, 244)   # page background (light gray)
    CARD        = (255, 255, 255)   # card surface
    CARD_EDGE   = (228, 228, 232)   # card border (very light)
    SHADOW      = (220, 220, 225)   # soft shadow tint
    DIVIDER     = (235, 235, 240)
    TITLE       = ( 24,  24,  28)   # section headers (near-black)
    LABEL       = (130, 130, 138)   # field labels (medium gray)
    VALUE       = ( 30,  30,  34)   # primary values (dark)
    VALUE_DIM   = ( 90,  90,  98)   # secondary values
    BAR_BG      = (235, 235, 240)
    BAR_FILL    = ( 60,  60,  68)
    TAG_BG      = ( 28,  28,  32)
    TAG_FG      = (255, 255, 255)
    ACCENT      = ( 30, 144, 255)   # only used for tiny highlights
    TIME_FG     = ( 28,  28,  32)
    # History-chart series colors — kept subtle to preserve the monochrome feel.
    # Usage keeps the dark BAR_FILL tone. Temp is warm (heat), power is cool (watts).
    USAGE_LINE  = ( 50,  50,  58)
    TEMP_LINE   = (220, 130,  50)
    POWER_LINE  = ( 60, 130, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Typography — bumped for readability on the 9.16" LCD (small viewing distance).
# ─────────────────────────────────────────────────────────────────────────────
class T:
    SECTION     = 28
    HERO_BIG    = 72     # big % number
    HERO_LABEL  = 18     # "CPU USAGE"
    GRID_LBL    = 14     # "CPU FAN", "CLOCK", etc
    GRID_VAL    = 18     # main value (small enough that " MHz" / " RPM" fits inline)
    GRID_UNIT   = 12     # inline unit suffix ("MHz", "RPM")
    HERO_SUB_LBL = 18    # "TEMP", "POWER"
    HERO_SUB_VAL = 24    # "54°", "48W"
    METRIC_VAL  = 42     # RAM/ROM/FPS big
    METRIC_LBL  = 17
    CARD_TITLE  = 18     # "NETWORK SPEED", "TIME", "WEATHER"
    NET_VAL     = 30
    NET_LBL     = 15     # "UP", "DOWN"
    TIME_VAL    = 38
    TIME_LBL    = 19     # day + date
    WX_BIG      = 44
    WX_LOC      = 15
    WX_DAY      = 17
    WX_HI       = 19
    FOOTER      = 14
    TAG         = 14     # "ON DEMAND" pill text
    HIST_TITLE  = 11     # history-chart header label
    HIST_DOT    = 11     # history-chart legend dots (font size for spacing)


# ─────────────────────────────────────────────────────────────────────────────
# Font loading — use Microsoft YaHei (same as TRCC) on Windows, with fallbacks.
# ─────────────────────────────────────────────────────────────────────────────
_FONT_CACHE: dict[tuple[str, int], Any] = {}


def _font(name: str, size: int):
    key = (name, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        f = ImageFont.truetype(name, size=size)
    except OSError:
        f = ImageFont.load_default(size=size)
    _FONT_CACHE[key] = f
    return f


# Windows ships these. We try them in order.
YAHEI = "msyh.ttc"
SEGOE  = "seguisb.ttf"


# ─────────────────────────────────────────────────────────────────────────────
# Drawing primitives
# ─────────────────────────────────────────────────────────────────────────────
def _round_rect(draw: ImageDraw.ImageDraw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _shadow_rect(draw: ImageDraw.ImageDraw, xy, radius, offset=(0, 6)):
    """Cheap drop shadow: draw offset gray rounded rect underneath."""
    x0, y0, x1, y1 = xy
    sx0, sy0 = x0 + offset[0], y0 + offset[1]
    sx1, sy1 = x1 + offset[0], y1 + offset[1]
    draw.rounded_rectangle((sx0, sy0, sx1, sy1), radius=radius, fill=C.SHADOW)


def _bar(draw: ImageDraw.ImageDraw, x, y, w, h, pct: float):
    """Draw a thin progress bar. pct in [0, 100]."""
    pct = max(0.0, min(100.0, pct))
    _round_rect(draw, (x, y, x + w, y + h), radius=h // 2, fill=C.BAR_BG)
    fill_w = max(2, int(w * (pct / 100.0)))
    if fill_w > 0:
        _round_rect(draw, (x, y, x + fill_w, y + h), radius=h // 2, fill=C.BAR_FILL)


def _text(draw, xy, text, font, fill=C.VALUE, anchor="la"):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _text_wh(draw, text, font):
    """Return (w, h) of text in given font."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


# ─────────────────────────────────────────────────────────────────────────────
# Card builders
# ─────────────────────────────────────────────────────────────────────────────
def draw_section_header(draw: ImageDraw.ImageDraw, y: int, title: str) -> int:
    """Draw a section header (small caps + horizontal rule). Returns new y."""
    font = _font(YAHEI, T.SECTION)
    _text(draw, (24, y), title.upper(), font=font, fill=C.TITLE)
    w, _ = _text_wh(draw, title.upper(), font)
    rule_y = y + 16
    draw.line((24 + w + 12, rule_y, 462 - 24, rule_y), fill=C.DIVIDER, width=1)
    return y + 38


def draw_usage_hero(draw, x, y, w, h, big_value: str, label: str, temp_str: str,
                     power_str: str, tag: str = "ON DEMAND",
                     history: Optional[list] = None,
                     hist_label: str = "60s HISTORY"):
    """Big-number hero card with usage, temp, power, and a history chart.

    Layout:
        [TAG]                          <-- top-left pill
        20%   CPU USAGE                <-- huge number + label side-by-side
        TEMP     54°
        POWER    48W
        [HISTORY CHART]                <-- mixed usage/temp/power sparklines

    ``history`` is a list of (usage%, temp°C, powerW) tuples, oldest first.
    When omitted, the chart area is left blank (still gets its card frame).
    """
    card_xy = (x, y, x + w, y + h)
    _shadow_rect(draw, card_xy, radius=14)
    _round_rect(draw, card_xy, radius=14, fill=C.CARD, outline=C.CARD_EDGE)

    # Tag pill (top-left) — text vertically centered using anchor="lm" (left-middle).
    tag_font = _font(SEGOE, T.TAG)
    tag_text = tag
    tw, th = _text_wh(draw, tag_text, tag_font)
    pad_x, pad_y = 12, 8
    pill_xy = (x + 20, y + 20, x + 20 + tw + pad_x * 2, y + 20 + th + pad_y * 2)
    _round_rect(draw, pill_xy, radius=6, fill=C.TAG_BG)
    # Center text horizontally and vertically inside the pill using anchor="lm".
    pill_cy = (pill_xy[1] + pill_xy[3]) // 2
    _text(draw, (x + 20 + pad_x, pill_cy), tag_text,
          font=tag_font, fill=C.TAG_FG, anchor="lm")

    # Big number on its own row, label below it. The font ascender + descender
    # means textbbox height is much smaller than the actual rendered glyph extent,
    # so we add a generous gap below the big number for the label.
    big_font = _font(SEGOE, T.HERO_BIG)
    lbl_font = _font(SEGOE, T.HERO_LABEL)
    _text(draw, (x + 20, y + 80), big_value, font=big_font, fill=C.VALUE)
    # Label well below the big number (font_size is more reliable than textbbox)
    _text(draw, (x + 20, y + 80 + T.HERO_BIG + 8),
          label.upper(), font=lbl_font, fill=C.LABEL)

    # TEMP and POWER rows
    small = _font(SEGOE, T.HERO_SUB_LBL)
    val = _font(SEGOE, T.HERO_SUB_VAL)
    rows_y_start = y + 80 + T.HERO_BIG + 8 + 36
    _text(draw, (x + 20, rows_y_start), "TEMP", font=small, fill=C.LABEL)
    _text(draw, (x + 20 + 110, rows_y_start), temp_str, font=val, fill=C.VALUE)
    _text(draw, (x + 20, rows_y_start + 42), "POWER", font=small, fill=C.LABEL)
    _text(draw, (x + 20 + 110, rows_y_start + 42), power_str, font=val, fill=C.VALUE)

    # History chart fills the bottom of the card. Replaces the old thin
    # progress bar — the chart conveys trends, which is more informative
    # than a single fill level that duplicates the big usage number above.
    chart_x = x + 20
    chart_y = y + 300
    chart_w = w - 40
    chart_h = 170
    samples = history if history is not None else []
    draw_mixed_history_chart(draw, chart_x, chart_y, chart_w, chart_h,
                             samples, label=hist_label)


def draw_side_grid(draw, x, y, w, h, items):
    """2x2 grid for the right side of a hero card.

    items can be (label, value) or (label, value, unit). When a unit is
    provided, it's rendered inline after the value in a smaller font — so
    "1342" + " MHz" both fit comfortably inside a narrow cell.
    """
    _round_rect(draw, (x, y, x + w, y + h), radius=14, fill=C.CARD, outline=C.CARD_EDGE)
    pad_x = 16
    pad_y = 18
    col_w = (w - pad_x * 2 - pad_y) // 2
    row_h = (h - pad_y * 3) // 2
    lbl_font = _font(SEGOE, T.GRID_LBL)
    val_font = _font(SEGOE, T.GRID_VAL)
    unit_font = _font(SEGOE, T.GRID_UNIT)
    for idx, item in enumerate(items[:4]):
        # Accept (label, value) or (label, value, unit).
        if len(item) == 2:
            label, value = item
            unit = None
        else:
            label, value, unit = item
        col = idx % 2
        row = idx // 2
        cx = x + pad_y + col * (col_w + pad_y)
        cy = y + pad_x + row * (row_h + pad_x)
        _, lh = _text_wh(draw, label.upper(), lbl_font)
        _text(draw, (cx, cy), label.upper(), font=lbl_font, fill=C.LABEL)
        val_y = cy + lh + 14
        _text(draw, (cx, val_y), value, font=val_font, fill=C.VALUE)
        if unit:
            vw, _ = _text_wh(draw, value, val_font)
            # Vertically center the smaller unit against the larger value glyph.
            unit_y = val_y + (T.GRID_VAL - T.GRID_UNIT) // 2
            _text(draw, (cx + vw + 4, unit_y),
                  unit, font=unit_font, fill=C.LABEL)


def draw_mixed_history_chart(draw, x, y, w, h, samples, label: str):
    """Small mixed line chart: usage / temp / power, all normalized.

    ``samples`` is a list of ``(usage%, temp°C, powerW)`` tuples, oldest
    first. Each series is normalized against its own floor (so the line
    stays in the same vertical position even when the absolute value is
    low) — this gives stable visual range across loads.

    The card has a header row with the title + 3 colored legend dots,
    then a plot area with 2 gridlines and the three lines drawn on top.
    """
    _round_rect(draw, (x, y, x + w, y + h), radius=10,
                fill=C.CARD, outline=C.CARD_EDGE)

    # ── Header row (title + legend dots) ────────────────────────────
    title_font = _font(SEGOE, T.HIST_TITLE)
    _text(draw, (x + 12, y + 8), label, font=title_font, fill=C.LABEL)

    # 3 colored dots, right-aligned in the header row, each with a tiny
    # abbreviated label so the user knows what each color means.
    dot_meta = [
        ("USE",  C.USAGE_LINE),
        ("TEMP", C.TEMP_LINE),
        ("PWR",  C.POWER_LINE),
    ]
    dot_label_font = _font(SEGOE, T.HIST_DOT)
    leg_right = x + w - 10
    # Build right-to-left so we can right-align cleanly.
    for name, color in reversed(dot_meta):
        lw, _ = _text_wh(draw, name, dot_label_font)
        leg_right -= lw
        _text(draw, (leg_right, y + 8), name, font=dot_label_font,
              fill=C.LABEL, anchor="la")
        leg_right -= 4
        leg_right -= 8  # dot width
        dot_cy = y + 8 + T.HIST_DOT // 2
        draw.ellipse((leg_right, dot_cy - 3, leg_right + 6, dot_cy + 3),
                     fill=color)
        leg_right -= 12  # gap before next legend group

    # ── Inner plot area ─────────────────────────────────────────────
    pad_l, pad_r = 12, 12
    pad_t = 28         # leave room for header
    pad_b = 14         # leave a little breathing room at the bottom
    px = x + pad_l
    py = y + pad_t
    pw = w - pad_l - pad_r
    ph = h - pad_t - pad_b

    # Faint gridlines (baseline + 2 upper divisions)
    draw.line((px, py + ph, px + pw, py + ph), fill=C.DIVIDER, width=1)
    for i in (1, 2):
        gy = py + int(ph * i / 3)
        draw.line((px, gy, px + pw, gy), fill=C.DIVIDER, width=1)

    if len(samples) < 2:
        return  # need at least two points to draw a line

    # ── Draw each series ────────────────────────────────────────────
    # Floor values give visual stability when the actual data is low —
    # e.g. CPU temp floor 60°C keeps the line from "stuck to the bottom"
    # when the chip is idle at 40°C.
    series_cfg = [
        (C.USAGE_LINE, 100.0, [s[0] for s in samples]),  # usage %
        (C.TEMP_LINE,   60.0, [s[1] for s in samples]),  # °C
        (C.POWER_LINE,  80.0, [s[2] for s in samples]),  # W
    ]
    n = len(samples)
    for color, floor, values in series_cfg:
        if not values:
            continue
        max_v = max(floor, max(values))
        pts = []
        for i, v in enumerate(values):
            xi = px + int(pw * i / max(1, n - 1))
            ratio = max(0.0, min(1.0, v / max_v))
            yi = py + ph - int(ph * ratio)
            pts.append((xi, yi))
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=2)


def draw_metric_card(draw, x, y, w, h, value: str, label: str, pct: float):
    """Small bottom-row card (RAM / ROM / FPS) with big number + label + bar."""
    _shadow_rect(draw, (x, y, x + w, y + h), radius=12)
    _round_rect(draw, (x, y, x + w, y + h), radius=12, fill=C.CARD, outline=C.CARD_EDGE)
    val_font = _font(SEGOE, T.METRIC_VAL)
    lbl_font = _font(SEGOE, T.METRIC_LBL)
    _text(draw, (x + 18, y + 18), value, font=val_font, fill=C.VALUE)
    _text(draw, (x + 18, y + 70), label.upper(), font=lbl_font, fill=C.LABEL)
    _bar(draw, x + 18, y + h - 20, w - 36, 6, pct)


def draw_network_card(draw, x, y, w, h, up: str, down: str):
    """NETWORK SPEED card with UP / DOWN rows."""
    _shadow_rect(draw, (x, y, x + w, y + h), radius=12)
    _round_rect(draw, (x, y, x + w, y + h), radius=12, fill=C.CARD, outline=C.CARD_EDGE)
    title_font = _font(SEGOE, T.CARD_TITLE)
    val_font = _font(SEGOE, T.NET_VAL)
    lbl_font = _font(SEGOE, T.NET_LBL)

    _text(draw, (x + 18, y + 16), "NETWORK SPEED", font=title_font, fill=C.TITLE)

    # UP row
    _text(draw, (x + 18, y + 64), "UP", font=lbl_font, fill=C.LABEL)
    _text(draw, (x + w - 18, y + 56), up, font=val_font, fill=C.VALUE, anchor="ra")
    # Down row
    _text(draw, (x + 18, y + 110), "DOWN", font=lbl_font, fill=C.LABEL)
    _text(draw, (x + w - 18, y + 102), down, font=val_font, fill=C.VALUE, anchor="ra")


def draw_time_card(draw, x, y, w, h, now: datetime):
    """TIME card with day name, time, date, plus a small clock icon."""
    _shadow_rect(draw, (x, y, x + w, y + h), radius=12)
    _round_rect(draw, (x, y, x + w, y + h), radius=12, fill=C.CARD, outline=C.CARD_EDGE)
    title_font = _font(SEGOE, T.CARD_TITLE)
    time_font = _font(SEGOE, T.TIME_VAL)
    date_font = _font(SEGOE, T.TIME_LBL)

    _text(draw, (x + 18, y + 16), "TIME", font=title_font, fill=C.TITLE)
    # Draw a small clock icon (circle + hour/minute hands) at top-right.
    cx, cy, r = x + w - 36, y + 38, 18
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=C.TIME_FG, width=2)
    # minute hand (up)
    draw.line((cx, cy, cx, cy - r + 5), fill=C.TIME_FG, width=2)
    # hour hand (slight right)
    draw.line((cx, cy, cx + r - 8, cy + 3), fill=C.TIME_FG, width=2)
    # 12-hour with AM/PM, no leading zero on hour ("6:15 PM").
    time_str = now.strftime("%I:%M %p").lstrip("0")
    _text(draw, (x + 18, y + 54), time_str, font=time_font, fill=C.VALUE)
    day_str = now.strftime("%a").upper()
    date_str = now.strftime("%Y-%m-%d")
    _text(draw, (x + 18, y + 100), day_str, font=date_font, fill=C.LABEL)
    _text(draw, (x + 18, y + 124), date_str, font=date_font, fill=C.LABEL)


def draw_clock_icon(draw, cx, cy, r):
    """Small clock glyph: circle outline + minute hand up + hour hand right."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=C.VALUE, width=2)
    draw.line((cx, cy, cx, cy - r + 4), fill=C.VALUE, width=2)
    draw.line((cx, cy, cx + r - 7, cy + 2), fill=C.VALUE, width=2)


def draw_weather_icon(draw, cx, cy, r, code: str):
    """Tiny weather glyph based on OpenWeatherMap icon code prefix.

    01 = sun, 02 = partly cloudy, 03/04 = cloud, 09/10 = rain, 11 = thunder,
    13 = snow, 50 = mist.
    """
    prefix = code[:2] if code else "01"
    if prefix == "01":  # clear
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 196, 60))
    elif prefix in ("02",):  # partly cloudy: sun + cloud
        draw.ellipse((cx - r, cy - r, cx + r // 2, cy - r // 2),
                     fill=(255, 196, 60))
        draw.ellipse((cx - 2, cy - r // 2, cx + r, cy + r // 2), fill=(180, 195, 215))
    elif prefix in ("03", "04"):  # cloudy
        draw.ellipse((cx - r, cy - r // 2, cx + r // 2, cy + r // 2), fill=(180, 195, 215))
        draw.ellipse((cx - 2, cy - r // 2, cx + r, cy + r // 2), fill=(180, 195, 215))
    elif prefix in ("09", "10"):  # rain
        draw.ellipse((cx - r, cy - r // 2, cx + r // 2, cy + r // 4), fill=(180, 195, 215))
        draw.line((cx - 4, cy + r // 2, cx - 6, cy + r), fill=(80, 140, 200), width=2)
        draw.line((cx + 2, cy + r // 2, cx, cy + r), fill=(80, 140, 200), width=2)
        draw.line((cx + 8, cy + r // 2, cx + 6, cy + r), fill=(80, 140, 200), width=2)
    elif prefix == "11":  # thunder
        draw.ellipse((cx - r, cy - r // 2, cx + r // 2, cy + r // 4), fill=(180, 195, 215))
        draw.polygon([(cx - 2, cy), (cx + 4, cy), (cx, cy + 8),
                      (cx + 6, cy + 8), (cx, cy + r)], fill=(255, 196, 60))
    elif prefix == "13":  # snow
        draw.ellipse((cx - r, cy - r // 2, cx + r // 2, cy + r // 4), fill=(180, 195, 215))
        for dx in (-4, 0, 4):
            draw.line((cx + dx - 2, cy + r // 2 + 2, cx + dx + 2, cy + r // 2 + 6),
                      fill=(120, 170, 220), width=2)
    else:  # mist / unknown
        for dy in (-4, 0, 4):
            draw.line((cx - r + 2, cy + dy, cx + r - 2, cy + dy),
                      fill=(180, 195, 215), width=2)


def draw_weather_card(draw, x, y, w, h, snapshot: SensorSnapshot):
    """Bottom weather card with today's icon + 3-day forecast."""
    _shadow_rect(draw, (x, y, x + w, y + h), radius=12)
    _round_rect(draw, (x, y, x + w, y + h), radius=12, fill=C.CARD, outline=C.CARD_EDGE)

    title_font = _font(SEGOE, T.CARD_TITLE)
    loc_font   = _font(SEGOE, T.WX_LOC)
    big_font   = _font(SEGOE, T.WX_BIG)
    day_font   = _font(SEGOE, T.WX_DAY)
    hi_font    = _font(SEGOE, T.WX_HI)

    _text(draw, (x + 18, y + 16), "WEATHER", font=title_font, fill=C.TITLE)

    if not snapshot.weather.available:
        msg = snapshot.weather.error or "weather disabled"
        _text(draw, (x + 18, y + 60), msg, font=loc_font, fill=C.LABEL)
        _text(draw, (x + 18, y + 90), "Set weather.api_key in config.toml to enable.",
              font=loc_font, fill=C.LABEL)
        draw_weather_icon(draw, x + w - 50, y + 60, 22, "")
        return

    wx = snapshot.weather
    _text(draw, (x + 18, y + 60), wx.location or "Today", font=loc_font, fill=C.LABEL)
    _text(draw, (x + 18, y + 88), f"{int(round(wx.current_temp_c))}°",
          font=big_font, fill=C.VALUE)
    # Draw the weather icon at top-right of the weather block
    draw_weather_icon(draw, x + w - 52, y + 62, 26, wx.current_icon)

    # 3-day forecast (right) — small columns
    col_x = x + 200
    col_w = (w - 200 - 18) // max(1, len(wx.forecast))
    for i, day in enumerate(wx.forecast[:3]):
        cx = col_x + i * col_w
        _text(draw, (cx, y + 50), day.label, font=day_font, fill=C.LABEL)
        draw_weather_icon(draw, cx + 16, y + 84, 18, day.icon_code)
        _text(draw, (cx, y + 124), f"{int(round(day.high_c))}°",
              font=hi_font, fill=C.VALUE)
        _text(draw, (cx, y + 148), f"{int(round(day.low_c))}°",
              font=day_font, fill=C.LABEL)


# ─────────────────────────────────────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────────────────────────────────────
class Renderer:
    """Owns a fresh PIL.Image + ImageDraw and renders one frame per call."""

    WIDTH = 462
    HEIGHT = 1920

    def __init__(self, width: int = WIDTH, height: int = HEIGHT):
        self.width = width
        self.height = height
        self.image: Optional[Image.Image] = None
        self.draw: Optional[ImageDraw.ImageDraw] = None

    def _fresh(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGB", (self.width, self.height), C.BG)
        draw = ImageDraw.Draw(img)
        return img, draw

    def render(self, snapshot: SensorSnapshot,
               history: Optional[Any] = None) -> Image.Image:
        """Draw a full frame and return it as a PIL.Image (RGB).

        ``history`` is an optional HistoryBuffer; when provided, its
        CPU/GPU sample lists are drawn into the matching hero cards.
        """
        img, draw = self._fresh()
        self.image, self.draw = img, draw
        self._draw_layout(snapshot, history)
        return img

    def _draw_layout(self, snap: SensorSnapshot, history: Optional[Any] = None):
        d = self.draw
        assert d is not None

        # Pull sample lists out of the history buffer (or empty if None).
        cpu_hist = history.cpu_tuples() if history is not None else []
        gpu_hist = history.gpu_tuples() if history is not None else []
        # The label reflects the buffer size so the user sees when the
        # chart is still filling up (e.g. "12s HISTORY" -> "60s HISTORY").
        hist_label = f"{len(cpu_hist)}s HISTORY"

        y = 30
        # ── CPU section ──────────────────────────────────────────────
        y = draw_section_header(d, y, "CPU STATUS MONITORING")
        cpu_h = 540
        cpu_card_w = (self.width - 24 * 2 - 12) // 2
        left_x = 24
        right_x = left_x + cpu_card_w + 12
        draw_usage_hero(d, left_x, y, cpu_card_w, cpu_h,
                        snap.cpu.usage_str, "CPU USAGE",
                        snap.cpu.temp_str, snap.cpu.power_str,
                        tag="ON DEMAND",
                        history=cpu_hist, hist_label=hist_label)
        draw_side_grid(d, right_x, y, cpu_card_w, cpu_h, [
            ("CPU FAN",   snap.cpu.fan_str,   "RPM"),
            ("CPU CLOCK", snap.cpu.clock_str, "MHz"),
            ("AIO PUMP",  snap.cpu.pump_str,  "RPM"),
            ("VOLTAGE",   snap.cpu.voltage_str),
        ])
        y += cpu_h + 28

        # ── GPU section ──────────────────────────────────────────────
        y = draw_section_header(d, y, "GPU STATUS MONITORING")
        gpu_h = 540
        draw_usage_hero(d, left_x, y, cpu_card_w, gpu_h,
                        snap.gpu.usage_str, "GPU USAGE",
                        snap.gpu.temp_str, snap.gpu.power_str,
                        tag="ON DEMAND",
                        history=gpu_hist, hist_label=hist_label)
        draw_side_grid(d, right_x, y, cpu_card_w, gpu_h, [
            ("VRAM",      snap.gpu.vram_used_str),
            ("GPU CLOCK", snap.gpu.clock_str, "MHz"),
            ("GPU FAN",   snap.gpu.fan_str,   "RPM"),
            ("VOLTAGE",   snap.gpu.voltage_str),
        ])
        y += gpu_h + 28

        # ── Bottom row: 3 mini cards + network + time ─────────────────
        bottom_h = 180
        mini_w = (self.width - 24 * 2 - 12 * 2) // 3
        draw_metric_card(d, left_x, y, mini_w, bottom_h,
                         snap.mem.ram_str, "RAM", snap.mem.ram_percent)
        draw_metric_card(d, left_x + mini_w + 12, y, mini_w, bottom_h,
                         snap.mem.disk_str, "ROM", snap.mem.disk_percent)
        draw_metric_card(d, left_x + (mini_w + 12) * 2, y, mini_w, bottom_h,
                         snap.fps.fps_str, "FPS", min(100.0, snap.fps.fps))
        y += bottom_h + 22

        # Network + Time on a single compact row
        wide_card_w = (self.width - 24 * 2 - 12) // 2
        net_h = 180
        draw_network_card(d, left_x, y, wide_card_w, net_h,
                          snap.net.up_str, snap.net.down_str)
        draw_time_card(d, left_x + wide_card_w + 12, y, wide_card_w, net_h,
                       datetime.fromtimestamp(snap.timestamp))
        y += net_h + 22

        # ── Weather card (full width) ────────────────────────────────
        wx_h = 200
        draw_weather_card(d, 24, y, self.width - 48, wx_h, snap)
        y += wx_h + 16

        # ── Brand strip / footer (fills bottom) ──────────────────────
        footer_y = self.height - 70
        d.line((24, footer_y, self.width - 24, footer_y), fill=C.DIVIDER, width=1)
        brand_font = _font(YAHEI, T.FOOTER)
        _text(d, (24, footer_y + 18), "TROFEO VISION 9.16\"",
              font=brand_font, fill=C.LABEL)
        _text(d, (self.width - 24, footer_y + 18), "PYTHON  ·  PIL  ·  WINUSB",
              font=brand_font, fill=C.LABEL, anchor="ra")

    # ─── Output helpers ────────────────────────────────────────────
    def to_jpeg(self, quality: int = 85) -> bytes:
        assert self.image is not None, "Call render() first"
        buf = io.BytesIO()
        self.image.save(buf, format="JPEG", quality=quality, subsampling=0)
        return buf.getvalue()

    def save_png(self, path: Path) -> Path:
        assert self.image is not None, "Call render() first"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path)
        return path