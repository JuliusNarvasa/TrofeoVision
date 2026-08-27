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

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .sensors_types import SensorSnapshot
from .weather import (
    WeatherImages,
    greeting_for_bucket,
    icon_to_state,
    time_bucket,
)


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
    # Greeting-tile text (drawn on top of a photo background).
    GREET_FG    = (255, 255, 255)   # primary text (white)
    GREET_DIM   = (220, 220, 228)   # secondary text (dimmer white)


# ─────────────────────────────────────────────────────────────────────────────
# Typography — bumped for readability on the 9.16" LCD (small viewing distance).
# ─────────────────────────────────────────────────────────────────────────────
class T:
    SECTION     = 28
    HERO_BIG    = 72     # big % number
    HERO_LABEL  = 18     # "CPU USAGE"
    GRID_LBL    = 14     # "CPU FAN", "CLOCK", etc
    GRID_VAL    = 22     # main value (bigger so the measurement reads at distance)
    GRID_UNIT   = 14     # inline unit suffix ("MHz", "RPM") — still smaller + dimmer than value
    HERO_SUB_LBL = 18    # "TEMP", "POWER"
    HERO_SUB_VAL = 24    # "54°", "48W"
    METRIC_VAL  = 42     # RAM/ROM big
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
    # Greeting-tile typography (sits over a photo background).
    GREETING     = 30    # "Good evening, Bambaw"
    GREET_LOC    = 18    # location + condition secondary lines
    GREET_TEMP   = 84    # big temperature
    GREET_COND   = 26    # condition text (right side)
    GREET_HILO   = 18    # hi/lo + feels-like line


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
    """Draw a section header (small caps + horizontal rule). Returns new y.

    Returns ``y + 46`` so the gap between the header text bottom and the
    next card top matches the 16-px gap above the header (where the
    previous card ends + 16 px). The text itself is ~28 px tall, so this
    leaves 18 px of padding between the text and the card.
    """
    font = _font(YAHEI, T.SECTION)
    _text(draw, (24, y), title.upper(), font=font, fill=C.TITLE)
    w, _ = _text_wh(draw, title.upper(), font)
    rule_y = y + 16
    draw.line((24 + w + 12, rule_y, 462 - 24, rule_y), fill=C.DIVIDER, width=1)
    return y + 46


def draw_usage_hero(draw, x, y, w, h, big_value: str, label: str, temp_str: str,
                     power_str: str, tag: str = "ON DEMAND",
                     history: Optional[list] = None):
    """Big-number hero card with usage, temp, power, and a horizontal chart strip.

    Designed for a wide-but-short card (414×340 in the current layout).
    The full card width is used so the history chart is ~2× wider than the
    old side-by-side layout — gives the line plot real horizontal room to
    show trends without being cramped.

    Layout:
        [TAG]                              <-- top-left pill
        20%   CPU USAGE     TEMP 54°       <-- big number + label on left,
                              POWER 48W       stats stacked on right
        [──── history chart strip ────]    <-- full card width, below stats

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

    # ── Big number + label (left column) ────────────────────────────
    big_font = _font(SEGOE, T.HERO_BIG)
    lbl_font = _font(SEGOE, T.HERO_LABEL)
    _text(draw, (x + 20, y + 50), big_value, font=big_font, fill=C.VALUE)
    _text(draw, (x + 20, y + 50 + T.HERO_BIG + 8),
          label.upper(), font=lbl_font, fill=C.LABEL)

    # ── TEMP and POWER stacked on the right side ────────────────────
    small = _font(SEGOE, T.HERO_SUB_LBL)
    val = _font(SEGOE, T.HERO_SUB_VAL)
    # Right column starts a bit past card mid-line so the big number has
    # room to breathe. Value column offset to the right of the label so
    # values are visually grouped.
    right_label_x = x + w // 2 + 30
    right_val_x = right_label_x + 90
    _text(draw, (right_label_x, y + 50), "TEMP", font=small, fill=C.LABEL)
    _text(draw, (right_val_x, y + 50), temp_str, font=val, fill=C.VALUE)
    _text(draw, (right_label_x, y + 92), "POWER", font=small, fill=C.LABEL)
    _text(draw, (right_val_x, y + 92), power_str, font=val, fill=C.VALUE)

    # ── History chart strip — full card width ───────────────────────
    # Below the stats area; height is whatever the card has left after
    # the stats block + 20px bottom padding.
    chart_x = x + 20
    chart_y = y + 180
    chart_w = w - 40
    chart_h = h - 180 - 20
    samples = history if history is not None else []
    draw_mixed_history_chart(draw, chart_x, chart_y, chart_w, chart_h,
                             samples)


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


def draw_misc_strip(draw, x, y, w, h, items):
    """2×2 grid of sub-stats inside ONE big card.

    Sits below the hero card. All four cells share a single rounded
    rectangle, separated by a vertical divider (between columns) and a
    horizontal divider (between rows). In each cell the label sits at
    top-left and the value sits at bottom-right (right-aligned), so the
    four cells read as a balanced 2×2 dashboard.

    items can be (label, value) or (label, value, unit). When a unit is
    provided it's rendered inline after the value in a smaller font —
    same pattern as draw_side_grid.
    """
    # Single shared card for the whole 2×2.
    _round_rect(draw, (x, y, x + w, y + h), radius=14, fill=C.CARD, outline=C.CARD_EDGE)
    pad_outer = 16
    cols = 2
    rows = 2
    cell_w = (w - pad_outer * 2) // cols
    cell_h = (h - pad_outer * 2) // rows
    lbl_font = _font(SEGOE, T.GRID_LBL)
    val_font = _font(SEGOE, T.GRID_VAL)
    unit_font = _font(SEGOE, T.GRID_UNIT)

    # Cross dividers — vertical down the middle, horizontal across the middle.
    # Inset by ``pad_outer`` so the lines don't kiss the rounded corners.
    cx_mid = x + pad_outer + cell_w
    cy_mid = y + pad_outer + cell_h
    draw.line((cx_mid, y + pad_outer, cx_mid, y + h - pad_outer),
              fill=C.DIVIDER, width=1)
    draw.line((x + pad_outer, cy_mid, x + w - pad_outer, cy_mid),
              fill=C.DIVIDER, width=1)

    for idx, item in enumerate(items[:4]):
        if len(item) == 2:
            label, value = item
            unit = None
        else:
            label, value, unit = item
        col = idx % cols
        row = idx // cols
        # Cell origin (top-left of the cell, after pad_outer).
        cell_x = x + pad_outer + col * cell_w
        cell_y = y + pad_outer + row * cell_h
        cell_right = cell_x + cell_w
        cell_bottom = cell_y + cell_h
        # Inset for label/values so they don't sit flush against the
        # divider lines (the right-column label was touching the divider
        # before this padding was added).
        inner_pad_x = 12
        inner_pad_y = 8   # extra room above the label so the bottom-row
                           # header doesn't sit right on the horizontal divider
        # Label at top-left of the cell, offset by inner_pad so it has
        # breathing room from both the card edge (left col) and the
        # divider (right col).
        _text(draw, (cell_x + inner_pad_x, cell_y + inner_pad_y), label.upper(),
              font=lbl_font, fill=C.LABEL)
        # Value (with unit glued to its right) is centered in the cell —
        # both horizontally and vertically — so each quadrant reads as a
        # balanced labelled cell rather than left/right-anchored.
        vw, vh = _text_wh(draw, value, val_font)
        uw = _text_wh(draw, unit, unit_font)[0] if unit else 0
        gap = 4
        combined_w = vw + (gap + uw if unit else 0)
        pair_x = cell_x + (cell_w - combined_w) // 2
        val_y = cell_y + (cell_h - vh) // 2
        _text(draw, (pair_x, val_y), value, font=val_font, fill=C.VALUE)
        if unit:
            # Vertically center the smaller unit against the larger value glyph.
            uh = _text_wh(draw, unit, unit_font)[1]
            unit_y = val_y + (vh - uh) // 2
            _text(draw, (pair_x + vw + gap, unit_y), unit,
                  font=unit_font, fill=C.LABEL)


def draw_mixed_history_chart(draw, x, y, w, h, samples, label: str = ""):
    """Small mixed line chart: usage / temp / power, all normalized.

    ``samples`` is a list of ``(usage%, temp°C, powerW)`` tuples, oldest
    first. Each series is normalized against its own floor (so the line
    stays in the same vertical position even when the absolute value is
    low) — this gives stable visual range across loads.

    The card has an optional title row (pass ``label=""`` to skip) plus
    3 colored legend dots on the right, then a plot area with 2 gridlines
    and the three lines drawn on top.
    """
    _round_rect(draw, (x, y, x + w, y + h), radius=10,
                fill=C.CARD, outline=C.CARD_EDGE)

    # ── Header row (optional title + legend dots) ──────────────────
    title_font = _font(SEGOE, T.HIST_TITLE)
    if label:
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
    # The legend dots sit in the top ~22 px of the strip — pad_t must clear
    # them so a maxed-out series can't touch the legend text. 26 gives ~4 px
    # of breathing room even when font rendering runs slightly taller than
    # expected.
    pad_t = 26 if not label else 30
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
    """Small bottom-row card (RAM / ROM) with big number + label + bar."""
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


def _apply_gradient_overlay(image: Image.Image, x: int, y: int, w: int, h: int,
                             max_alpha: int = 90):
    """Paint a vertical top→bottom dark gradient over (x, y, w, h).

    Ensures the greeting-tile text stays legible across any photo
    background — even the bright noon images. ``max_alpha`` is the alpha
    value (0-255) at the bottom of the gradient; the top stays 0 so
    bright skies in the photos are preserved.
    """
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for row in range(h):
        # Quadratic falloff so the bottom is darker faster — text rows sit
        # near the bottom of the tile, so a stronger gradient there helps.
        ratio = row / max(1, h - 1)
        a = int(max_alpha * ratio * ratio)
        od.line([(0, row), (w, row)], fill=(0, 0, 0, a))
    image.paste(overlay, (x, y), overlay)


def _make_rounded_mask(w: int, h: int, radius: int) -> Image.Image:
    """Build a binary rounded-rectangle mask of size (w, h).

    Implemented manually with 4 pieslices + an interior rect because
    Pillow's ``ImageDraw.rounded_rectangle`` is marked experimental and
    effectively ignores the ``radius`` argument in some versions (only
    chamfers a couple of corner pixels). The pieslice + rect combo draws
    a true quarter-circle at every corner.
    """
    r = max(0, min(radius, w // 2, h // 2))
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    if r == 0:
        d.rectangle((0, 0, w - 1, h - 1), fill=255)
        return mask
    # Four corner pieslices fill the rounded quadrant at each corner.
    d.pieslice((0, 0, 2 * r - 1, 2 * r - 1), 180, 270, fill=255)
    d.pieslice((w - 2 * r, 0, w - 1, 2 * r - 1), 270, 360, fill=255)
    d.pieslice((0, h - 2 * r, 2 * r - 1, h - 1), 90, 180, fill=255)
    d.pieslice((w - 2 * r, h - 2 * r, w - 1, h - 1), 0, 90, fill=255)
    # Interior rectangle (excluding the four corner quadrants).
    d.rectangle((r, 0, w - r - 1, h - 1), fill=255)
    d.rectangle((0, r, w - 1, h - r - 1), fill=255)
    return mask


def _paste_rounded(image: Image.Image, source: Image.Image,
                   x: int, y: int, w: int, h: int, radius: int = 14):
    """Paste ``source`` into ``image`` at (x, y) clipped to a rounded rect.

    Used for the greeting tile so the photo background matches the rounded
    card corners of every other tile on the dashboard. Falls back to a
    plain paste if the source image is the wrong mode for alpha masking.
    """
    if source.size != (w, h):
        resample = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS)
        source = source.resize((w, h), resample)
    mask = _make_rounded_mask(w, h, radius)
    try:
        image.paste(source, (x, y), mask)
    except ValueError:
        # Source has no alpha channel (e.g. "P" or "1" mode) — convert
        # to RGBA first so the mask has something to clip against.
        image.paste(source.convert("RGBA"), (x, y), mask)


def draw_greeting_tile(image: Image.Image, draw: ImageDraw.ImageDraw,
                       x: int, y: int, w: int, h: int,
                       snapshot: SensorSnapshot,
                       greeting_name: str,
                       weather_images: Optional[WeatherImages]):
    """Top-of-dashboard greeting + weather tile.

    Layout (414 × 268, full card width minus side margins) — vertical
    stack so the big temperature stays the focal point without colliding
    with the greeting horizontally:

        y+ 22  Good <time>, <name>                              ← greeting (left)
        y+ 56  • <location>                                     ← location (left, dim)
        y+ 95
        y+110  26°                       Mostly Cloudy          ← big temp (left), condition (right of temp)
        y+194  ↑30° ↓24° • Feels like 30°                       ← hi/lo + feels-like (left)

    Background: weather-state image from ``weather_images`` (falls back to
    a solid dark-blue if the asset set is missing). Both the photo AND
    the readability gradient are clipped to a rounded rect so the tile
    corners match every other card on the dashboard.

    The greeting text + bucket come from the local-time hour; if weather
    is disabled (``snapshot.weather.available=False``), the temp + hi/lo
    placeholders still render but show em-dashes.
    """
    radius = 14
    # ── 1. Background image (rounded) ──────────────────────────────────
    now = datetime.fromtimestamp(snapshot.timestamp)
    bucket = time_bucket(now)
    if weather_images is not None and snapshot.weather.available:
        state = icon_to_state(snapshot.weather.current_icon)
        bg = weather_images.get(state, bucket)
    else:
        # Weather not configured: pick a neutral background (clear + current
        # bucket so the tile still looks intentional).
        from .weather import TIME_BUCKETS, _FALLBACK_STATE, _FALLBACK_BUCKET
        state = _FALLBACK_STATE
        bucket = bucket if bucket in TIME_BUCKETS else _FALLBACK_BUCKET
        bg = weather_images.get(state, bucket) if weather_images is not None else None

    if bg is not None:
        _paste_rounded(image, bg, x, y, w, h, radius=radius)

    # ── 2. Readability overlay (rounded) ───────────────────────────────
    # Build the gradient as an L-mode alpha mask (the alpha values for how
    # much black to mix in at each row), then paste solid black with that
    # mask — Pillow's ``paste`` only does the right thing when the source
    # is RGB and the mask is L; RGBA-on-L composites oddly.
    grad_alpha = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(grad_alpha)
    max_alpha = 110
    for row in range(h):
        ratio = row / max(1, h - 1)
        a = int(max_alpha * ratio * ratio)
        gd.line([(0, row), (w - 1, row)], fill=a)
    # Intersect the gradient alpha with the rounded mask so the gradient
    # doesn't bleed past the card corners.
    rounded = _make_rounded_mask(w, h, radius)
    combined = ImageChops.multiply(grad_alpha, rounded)
    image.paste((0, 0, 0), (x, y), combined)

    # ── 3. Text ─────────────────────────────────────────────────────────
    greet_font = _font(SEGOE, T.GREETING)
    loc_font   = _font(SEGOE, T.GREET_LOC)
    temp_font  = _font(SEGOE, T.GREET_TEMP)
    cond_font  = _font(SEGOE, T.GREET_COND)
    hilo_font  = _font(SEGOE, T.GREET_HILO)

    pad_x = 24

    # Greeting (top-left).
    greet_text = f"{greeting_for_bucket(bucket)}, {greeting_name}"
    _text(draw, (x + pad_x, y + 22), greet_text,
          font=greet_font, fill=C.GREET_FG)

    # Location + weather payload.
    wx = snapshot.weather
    if wx.available:
        location = wx.location or "—"
        temp_str = f"{int(round(wx.current_temp_c))}°"
        condition = _wx_condition_text(wx.current_icon)
        # Today's hi/lo from the first forecast entry (label == "Today").
        hi_lo = "—"
        for day in wx.forecast:
            if day.label == "Today":
                hi_lo = f"↑{int(round(day.high_c))}° ↓{int(round(day.low_c))}°"
                break
    else:
        location = "weather disabled"
        temp_str = "?°"
        condition = "—"
        hi_lo = "—"

    # Location (left, below greeting).
    _text(draw, (x + pad_x, y + 56),
          f"\u2022 {location}",  # bullet glyph as a stand-in for a pin icon
          font=loc_font, fill=C.GREET_DIM)

    # Big temperature (left, dominant). anchor="la" (default) so it grows
    # rightward but stays clear of the greeting column on the left.
    _text(draw, (x + pad_x, y + 110), temp_str,
          font=temp_font, fill=C.GREET_FG)

    # Condition (right of the temp, on the same baseline). Measure the
    # temp's width so we can offset the condition cleanly without overlap.
    temp_w, _ = _text_wh(draw, temp_str, temp_font)
    cond_x = x + pad_x + temp_w + 18
    _text(draw, (cond_x, y + 134), condition,
          font=cond_font, fill=C.GREET_FG)

    # Hi/lo + feels-like (bottom-left).
    feels_like_str = (
        f"{int(round(wx.current_feels_like_c))}°"
        if wx.current_feels_like_c > 0
        else "—"
    )
    hilo_text = f"{hi_lo}  \u2022  Feels like {feels_like_str}"
    _text(draw, (x + pad_x, y + h - 26), hilo_text,
          font=hilo_font, fill=C.GREET_DIM)


# OpenWeatherMap icon prefix → human-readable condition string.
# Covers the 9 prefixes exposed by OWM; anything else falls back to "".
_WX_CONDITION_LABELS = {
    "01": "Clear",
    "02": "Partly Cloudy",
    "03": "Cloudy",
    "04": "Overcast",
    "09": "Drizzle",
    "10": "Rain",
    "11": "Thunderstorms",
    "13": "Snow",
    "50": "Fog",
}


def _wx_condition_text(icon_code: str) -> str:
    """Return a short human label for an OpenWeatherMap icon code."""
    prefix = icon_code[:2] if icon_code else ""
    return _WX_CONDITION_LABELS.get(prefix, "—")


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
               history: Optional[Any] = None,
               greeting_name: str = "",
               weather_images: Optional[WeatherImages] = None) -> Image.Image:
        """Draw a full frame and return it as a PIL.Image (RGB).

        ``history`` is an optional HistoryBuffer; when provided, its
        CPU/GPU sample lists are drawn into the matching hero cards.

        ``greeting_name`` and ``weather_images`` drive the greeting tile at
        the top of the dashboard. When ``weather_images`` is None the tile
        still renders the greeting + dimmed fallback so the layout stays
        intact even on a fresh install.
        """
        img, draw = self._fresh()
        self.image, self.draw = img, draw
        self._draw_layout(snapshot, history, greeting_name, weather_images)
        return img

    def _draw_layout(self, snap: SensorSnapshot, history: Optional[Any] = None,
                     greeting_name: str = "",
                     weather_images: Optional[WeatherImages] = None):
        d = self.draw
        assert d is not None
        img = self.image
        assert img is not None

        # Pull sample lists out of the history buffer (or empty if None).
        cpu_hist = history.cpu_tuples() if history is not None else []
        gpu_hist = history.gpu_tuples() if history is not None else []

        # Two-row stack per section: hero card (with chart strip) on top,
        # misc tiles strip below. Hero gets the favored ~63% of the height
        # so the usage number and history chart both breathe.
        # Note: section_h below is visual reference only — actual y tracking
        # is done with explicit positions because the greeting tile shifts
        # the first CPU header by a non-uniform amount vs the rest.
        section_h = 540
        hero_h = 340
        gap = 16
        misc_h = section_h - hero_h - gap  # 184
        card_w = self.width - 24 * 2
        left_x = 24

        # ── Greeting tile (replaces the old bottom-of-screen weather card)
        # Height trimmed 280 → 268 so the layout still fits after the
        # header-bottom-padding increase below.
        greet_h = 268
        draw_greeting_tile(img, d, left_x, 30, card_w, greet_h,
                           snap, greeting_name or "friend", weather_images)
        y = 30 + greet_h + gap  # 314

        # ── CPU section ──────────────────────────────────────────────
        y = draw_section_header(d, y, "CPU STATUS MONITORING")  # y → 360
        draw_usage_hero(d, left_x, y, card_w, hero_h,
                        snap.cpu.usage_str, "CPU USAGE",
                        snap.cpu.temp_str, snap.cpu.power_str,
                        tag="ON DEMAND",
                        history=cpu_hist)
        draw_misc_strip(d, left_x, y + hero_h + gap, card_w, misc_h, [
            ("CPU FAN",   snap.cpu.fan_str,   "RPM"),
            ("CPU CLOCK", snap.cpu.clock_str, "MHz"),
            ("AIO PUMP",  snap.cpu.pump_str,  "RPM"),
            ("VOLTAGE",   snap.cpu.voltage_str),
        ])
        y = y + hero_h + gap + misc_h + gap  # 916 — explicit step to absorb
                                             # header-height changes

        # ── GPU section ──────────────────────────────────────────────
        y = draw_section_header(d, y, "GPU STATUS MONITORING")  # y → 962
        draw_usage_hero(d, left_x, y, card_w, hero_h,
                        snap.gpu.usage_str, "GPU USAGE",
                        snap.gpu.temp_str, snap.gpu.power_str,
                        tag="ON DEMAND",
                        history=gpu_hist)
        draw_misc_strip(d, left_x, y + hero_h + gap, card_w, misc_h, [
            ("VRAM",       snap.gpu.vram_used_str),
            ("GPU CLOCK",  snap.gpu.clock_str,    "MHz"),
            ("GPU FAN",    snap.gpu.fan_str,      "RPM"),
            ("VRAM CLOCK", snap.gpu.vram_clock_str, "MHz"),
        ])
        y = y + hero_h + gap + misc_h + gap  # 1518

        # ── Bottom row: 2 mini cards (RAM / ROM) + network + time ──────
        # FPS tile was removed; the freed slot is folded back into RAM + ROM
        # so each card is wider and the bar/labels breathe.
        bottom_h = 160
        mini_w = (self.width - 24 * 2 - 12) // 2
        draw_metric_card(d, left_x, y, mini_w, bottom_h,
                         snap.mem.ram_str, "RAM", snap.mem.ram_percent)
        draw_metric_card(d, left_x + mini_w + 12, y, mini_w, bottom_h,
                         snap.mem.disk_str, "ROM", snap.mem.disk_percent)
        y += bottom_h + gap  # 1694

        # Network + Time on a single compact row.
        wide_card_w = (self.width - 24 * 2 - 12) // 2
        net_h = 160
        draw_network_card(d, left_x, y, wide_card_w, net_h,
                          snap.net.up_str, snap.net.down_str)
        draw_time_card(d, left_x + wide_card_w + 12, y, wide_card_w, net_h,
                       datetime.fromtimestamp(snap.timestamp))
        y += net_h + gap  # 1870

        # ── Brand strip / footer (50-px band at bottom) ──────────────
        # Footer line moved from height-70 → height-50 so the divider no
        # longer clips the rounded bottom corners of the Net/Time cards.
        footer_y = self.height - 50  # 1870
        d.line((24, footer_y, self.width - 24, footer_y), fill=C.DIVIDER, width=1)
        brand_font = _font(YAHEI, T.FOOTER)
        _text(d, (24, footer_y + 14), "TROFEO VISION 9.16\"",
              font=brand_font, fill=C.LABEL)
        _text(d, (self.width - 24, footer_y + 14), "PYTHON  ·  PIL  ·  WINUSB",
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