# age_badge.py  — typographic age-rating + quality-tier colour (badge mode 1)
#
# Scoring (max 2 pts per category, 6 pts total):
#   Resolution:  4K=2,    1080P=1
#   Source:      REMUX=2, WEBDL=1
#   Visual:      DV=2,    HDR10+=2, HDR10=1
#
# Tiers → font colour:
#   0–1 pts  → Grey     (unknown / poor quality)
#   2–3 pts  → Bronze
#   4–5 pts  → Silver
#   6+ pts   → Gold

from __future__ import annotations
import math
import os
from typing import Sequence
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import numpy as np

try:
    import cairo as _cairo
    _HAS_CAIRO = True
except ImportError:
    _HAS_CAIRO = False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_CATEGORIES: dict[str, dict[str, int]] = {
    "resolution": {"4K": 2, "1080P": 1},
    "source":     {"REMUX": 2, "WEBDL": 1},
    "visual":     {"DV": 2, "HDR10+": 2, "HDR10": 1},
}

_CATEGORY_CAP = 2


def _score_points(tokens: Sequence[str]) -> int:
    token_set = set(tokens)
    total = 0
    for category_tokens in _CATEGORIES.values():
        pts = sum(v for k, v in category_tokens.items() if k in token_set)
        total += min(pts, _CATEGORY_CAP)
    return total


# ---------------------------------------------------------------------------
# Tier colours  (RGBA)
#
# Each tier carries three layers that build the premium look:
#   glow      — wide, very soft halo drawn underneath (large blur radius)
#   shadow    — tight drop shadow for depth
#   primary   — the face colour of the numeral
#   highlight — a near-white tint composited at low opacity for an inner-light
#               effect (simulated by blending a white copy at reduced alpha)
# ---------------------------------------------------------------------------

_TIERS = {
    "grey": {
        "glow":      (100, 100, 104,  28),   # barely any halo — looks flat/unlit
        "shadow":    (12,  12,  14, 190),
        "primary":   (130, 130, 136, 75),   # noticeably darker and more faded than silver
        "highlight": (160, 160, 166,  18),   # near-invisible — no metallic sheen
    },
    "bronze": {
        "glow":      (200, 110,  40,  70),
        "shadow":    (45,  18,   0, 210),
        "primary":   (200, 110,  45, 125),
        "highlight": (255, 200, 150, 45),
    },
    "silver": {
        "glow":      (195, 205, 228,  65),
        "shadow":    (30,  34,  50, 215),
        "primary":   (218, 224, 240, 125),
        "highlight": (255, 255, 255, 55),
    },
    "gold": {
        "glow":      (255, 215,  70,  80),
        "shadow":    (60,  45,   0, 220),
        "primary":   (255, 205,  60, 200),
        "highlight": (255, 250, 200, 55),
    },
}


def _tier(pts: int) -> dict:
    if pts >= 6:
        return _TIERS["gold"]
    elif pts >= 4:
        return _TIERS["silver"]
    elif pts >= 2:
        return _TIERS["bronze"]
    else:
        return _TIERS["grey"]


# ---------------------------------------------------------------------------
# Font cache
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(name: str, size: int):
    key = (name, size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(name, size)
        except IOError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cairo_pill_mask(w: int, h: int, radius: int) -> Image.Image:
    """Antialiased greyscale pill mask via cairo; falls back to PIL if cairo unavailable."""
    if _HAS_CAIRO:
        r = min(radius, w / 2, h / 2)
        surface = _cairo.ImageSurface(_cairo.FORMAT_A8, w, h)
        ctx = _cairo.Context(surface)
        ctx.set_antialias(_cairo.ANTIALIAS_BEST)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        ctx.new_sub_path()
        ctx.arc(w - r, r,     r, -math.pi / 2,  0.0)
        ctx.arc(w - r, h - r, r,  0.0,           math.pi / 2)
        ctx.arc(r,     h - r, r,  math.pi / 2,   math.pi)
        ctx.arc(r,     r,     r,  math.pi,        3 * math.pi / 2)
        ctx.close_path()
        ctx.fill()
        surface.flush()
        stride = surface.get_stride()
        arr = np.frombuffer(bytes(surface.get_data()), dtype=np.uint8).reshape((h, stride))[:, :w].copy()
        return Image.fromarray(arr)
    else:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(0, 0), (w - 1, h - 1)], radius=radius, fill=255
        )
        return mask


# Reusable 1×1 probe surface — textbbox needs an ImageDraw but doesn't read
# any pixels.  Allocated once at import time so per-call measurements don't
# pay an Image.new() round trip.
_PROBE = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


def _make_text_layer(
    xy: tuple[int, int],
    text: str,
    font,
    fill: tuple[int, int, int, int],
    *,
    blur: int = 0,
    tracking: int = 0,
) -> tuple[Image.Image, tuple[int, int]]:
    """
    Render *text* on a tightly-sized RGBA layer (just the glyph bbox plus
    blur padding) and apply the Gaussian blur inside this function.  Returns
    ``(layer, dest_xy)`` — alpha_composite the layer at dest_xy on the target
    image to place the glyph in the same position it would occupy if drawn at
    ``xy`` on a full-canvas layer.

    *tracking* adds that many pixels of extra spacing between every character
    (letter-spacing) so multi-digit ratings like "17" don't look cramped.  The
    glyphs are drawn one at a time when tracking is active; the top-left of the
    first glyph stays anchored exactly where the non-tracked draw would place
    it, so all five render passes line up.

    Why the tight layer: the badge previously created a full 500×750 RGBA layer
    per pass and blurred the whole thing.  The actual ink occupies maybe 60×60
    pixels.  GaussianBlur cost is proportional to area, so rendering on the
    glyph bbox cuts blur time roughly 30–50× per pass.
    """
    bb = _PROBE.textbbox((0, 0), text, font=font)
    # 3× sigma covers ~99% of a Gaussian — anything beyond that contributes
    # less than 1% intensity and is safely cropped at the layer edge.
    pad = blur * 3 if blur else 0
    glyph_h = int(bb[3] - bb[1])

    if tracking and len(text) > 1:
        advances = [_PROBE.textlength(ch, font=font) for ch in text]
        glyph_w  = int(sum(advances) + tracking * (len(text) - 1))
        layer    = Image.new("RGBA", (glyph_w + 2 * pad, glyph_h + 2 * pad), (0, 0, 0, 0))
        draw     = ImageDraw.Draw(layer)
        cx       = float(pad - bb[0])
        for ch, adv in zip(text, advances):
            draw.text((cx, pad - bb[1]), ch, font=font, fill=fill)
            cx += adv + tracking
    else:
        glyph_w = int(bb[2] - bb[0])
        layer   = Image.new("RGBA", (glyph_w + 2 * pad, glyph_h + 2 * pad), (0, 0, 0, 0))
        # Draw so the glyph ink lands at (pad, pad)..(pad + glyph_w, pad + glyph_h)
        ImageDraw.Draw(layer).text(
            (pad - bb[0], pad - bb[1]),
            text, font=font, fill=fill,
        )

    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))

    # Compositing the layer at this offset puts the ink where the original
    # full-canvas (xy + bb[0..1]) draw would have placed it.
    dest = (int(xy[0] + bb[0] - pad), int(xy[1] + bb[1] - pad))
    return layer, dest


def _composite_at(target: Image.Image, layer: Image.Image, dest: tuple[int, int]) -> None:
    """
    alpha_composite with negative-coordinate support.

    PIL's alpha_composite refuses negative dest coordinates.  When the glyph
    is in the top-left corner with a big glow padding, the dest can be slightly
    negative — we handle that by clipping the source rectangle so the on-canvas
    portion still composites correctly.
    """
    dx, dy = dest
    sx = max(0, -dx)
    sy = max(0, -dy)
    dx = max(0, dx)
    dy = max(0, dy)
    if sx >= layer.width or sy >= layer.height:
        return  # entirely off the left/top edge
    if dx >= target.width or dy >= target.height:
        return  # entirely off the right/bottom edge
    target.alpha_composite(layer, dest=(dx, dy), source=(sx, sy))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def draw_quality_age_badge(
    image: Image.Image,
    age_rating: int | None,
    quality_tokens: Sequence[str],
    *,
    anchor_x_ratio: float = 0.040,
    anchor_y_ratio: float = 0.036,
    badge_height: int = 54,
    always_silver: bool = False,
) -> None:
    """
    Render the age rating as a large typographic number in the top-left corner.
    Colour is determined by the quality tier derived from *quality_tokens*,
    unless *always_silver* is True, in which case the silver tier is always used
    regardless of quality (mode 3 — age rating only, no quality dependency).
    If no age rating is available the badge is skipped entirely.

    Visual layers (back → front):
      1. Wide ambient glow      — very soft, large-radius blur for luminance halo
      2. Tight drop shadow      — small offset + moderate blur for depth
      3. Primary numeral        — full-opacity face colour
      4. Highlight pass         — near-white overlay at low opacity, shifted
                                  slightly up-left, for an inner-light illusion
    """
    if age_rating is None:
        return

    W, H   = image.size
    colors = _TIERS["silver"] if always_silver else _tier(_score_points(quality_tokens))

    age_text  = str(age_rating)
    font_size = max(16, int(badge_height * 1.0))
    font      = _font(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"), font_size)

    # Letter-spacing so multi-character ratings (e.g. "17") aren't cramped.
    tracking = int(font_size * 0.10) if len(age_text) > 1 else 0

    ax = int(W * anchor_x_ratio)
    ay = int(H * anchor_y_ratio)

    # Measure text bounds once
    probe = ImageDraw.Draw(image)
    bb    = probe.textbbox((0, 0), age_text, font=font)
    tx    = int(ax - bb[0])
    ty    = int(ay - bb[1])

    # ── 1. Ambient glow ───────────────────────────────────────────────────
    # Large, very-soft blur centred on the glyph — creates a luminance halo
    # that belongs to the numeral rather than the surface beneath it.
    glow_blur = max(font_size // 4, 8)
    glow_layer, glow_dest = _make_text_layer(
        (tx, ty), age_text, font, colors["glow"], blur=glow_blur, tracking=tracking,
    )
    _composite_at(image, glow_layer, glow_dest)

    # Second, slightly tighter pass at higher opacity for a warm core to the glow
    glow_core_color = (*colors["glow"][:3], min(255, colors["glow"][3] + 40))
    core_layer, core_dest = _make_text_layer(
        (tx, ty), age_text, font, glow_core_color, blur=glow_blur // 2, tracking=tracking,
    )
    _composite_at(image, core_layer, core_dest)

    # ── 2. Drop shadow ────────────────────────────────────────────────────
    shadow_offset = max(1, font_size // 16)   # tighter than before for elegance
    shadow_blur   = max(2, font_size // 10)
    shadow_layer, shadow_dest = _make_text_layer(
        (tx + shadow_offset, ty + shadow_offset),
        age_text, font, colors["shadow"], blur=shadow_blur, tracking=tracking,
    )
    _composite_at(image, shadow_layer, shadow_dest)

    # ── 3. Primary numeral ────────────────────────────────────────────────
    primary_layer, primary_dest = _make_text_layer(
        (tx, ty), age_text, font, colors["primary"], tracking=tracking,
    )
    _composite_at(image, primary_layer, primary_dest)

    # ── 4. Highlight / inner-light pass ───────────────────────────────────
    # A slightly up-left shifted copy in near-white at low opacity creates the
    # illusion of light catching the top-left edge of the numeral — no container
    # needed; the effect belongs entirely to the glyph itself.
    hl_offset = max(1, font_size // 22)
    hl_layer, hl_dest = _make_text_layer(
        (tx - hl_offset, ty - hl_offset),
        age_text, font, colors["highlight"],
        blur=max(1, font_size // 30), tracking=tracking,
    )
    _composite_at(image, hl_layer, hl_dest)


# ---------------------------------------------------------------------------
# Mode 4 — Accent bar
# ---------------------------------------------------------------------------
# A small vertical rounded pill in the top-left corner whose colour reflects
# the quality tier.  No text — purely decorative / at-a-glance indicator.
#
# Visual layers (back → front):
#   1. Wide ambient glow  — soft blur matching the numeral badge style
#   2. Bar body           — solid fill at the primary tier colour
#   3. Highlight pass     — near-white overlay at low opacity across the left
#                           half, giving a subtle sheen without looking fake

def draw_tier_bar(
    image: Image.Image,
    quality_tokens: Sequence[str],
    *,
    anchor_x_ratio: float = 0.040,
    anchor_y_ratio: float = 0.030,
    bar_w_ratio: float = 0.008,   # bar width as fraction of poster width (narrow)
    bar_h_ratio: float = 0.028,   # bar height as fraction of poster height (tall)
    min_bar_w: int = 3,
    min_bar_h: int = 20,
    bar_height: int | None = None,  # explicit pixel height; overrides bar_h_ratio when set
) -> None:
    """
    Render a small vertical rounded accent pill in the top-left corner whose
    fill colour reflects the quality tier derived from *quality_tokens*.
    When *bar_height* is provided it is used directly as the bar's pixel height,
    allowing the configurator's Height control to apply to mode 4.
    """
    pts    = _score_points(quality_tokens)
    colors = _tier(pts)

    W, H = image.size
    x = int(W * anchor_x_ratio)
    y = int(H * anchor_y_ratio)
    bw = max(min_bar_w, int(W * bar_w_ratio))
    bh = bar_height if bar_height is not None else max(min_bar_h, int(H * bar_h_ratio))
    radius = bw // 2  # pill-shaped: radius driven by the narrow dimension

    # ── 1. Ambient glow ───────────────────────────────────────────────────
    pad        = bw * 5
    glow_size  = (bw + pad * 2, bh + pad * 2)
    glow_layer = Image.new("RGBA", glow_size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).rounded_rectangle(
        [pad, pad, pad + bw, pad + bh],
        radius=radius,
        fill=(*colors["glow"][:3], min(255, colors["glow"][3] + 20)),
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(bw * 1.8))
    image.alpha_composite(glow_layer, dest=(x - pad, y - pad))

    # ── 2. Bar body ───────────────────────────────────────────────────────
    bar_mask  = _cairo_pill_mask(bw, bh, radius)
    bar_alpha = colors["primary"][3]
    if bar_alpha < 255:
        bar_mask = bar_mask.point(lambda v: v * bar_alpha // 255)
    bar_strip = Image.new("RGBA", (bw, bh), colors["primary"][:3] + (0,))
    bar_strip.putalpha(bar_mask)
    # Composite at the offset instead of via a full-canvas transparent layer —
    # same result (transparent pixels are a no-op), a fraction of the pixels.
    # This is the form the glow above already uses.
    image.alpha_composite(bar_strip, dest=(x, y))

    # ── 3. Highlight sheen ────────────────────────────────────────────────
    hl_w    = max(1, bw // 2)
    hl_fill = (*colors["highlight"][:3], min(255, colors["highlight"][3] + 20))
    hl_mask = _cairo_pill_mask(hl_w, bh, radius)
    if hl_fill[3] < 255:
        hl_mask = hl_mask.point(lambda v: v * hl_fill[3] // 255)
    hl_strip = Image.new("RGBA", (hl_w, bh), hl_fill[:3] + (0,))
    hl_strip.putalpha(hl_mask)
    image.alpha_composite(hl_strip, dest=(x, y))


# ---------------------------------------------------------------------------
# Mode 6 — corner bookmark
# ---------------------------------------------------------------------------

# Shape of the fold, all as fractions of the requested size:
_BM_TIP_RADIUS = 0.18    # rounding on the two tips that meet the poster edges
_BM_INNER_BOW  = 0.04    # outward bulge of the inner edge — the folded-ribbon read
_BM_RIM        = 0.12    # width of the rim light running along the inner edge
_BM_CORNER_LIT = 0.18    # strength of the corner-to-inner-edge lighting gradient


def _convex_sdf(points: Sequence[tuple[float, float]], w: int, h: int) -> np.ndarray:
    """Signed distance (negative inside) from every pixel centre to a CCW convex path."""
    ys, xs = np.mgrid[0:h, 0:w]
    px = xs + 0.5
    py = ys + 0.5
    nearest = np.full((h, w), np.inf)
    inside = np.ones((h, w), bool)
    for i, (ax, ay) in enumerate(points):
        bx, by = points[(i + 1) % len(points)]
        ex, ey = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        seg = ex * ex + ey * ey
        t = np.clip((wx * ex + wy * ey) / seg, 0.0, 1.0) if seg else 0.0
        dx, dy = wx - t * ex, wy - t * ey
        nearest = np.minimum(nearest, dx * dx + dy * dy)
        inside &= (ex * wy - ey * wx) >= 0
    dist = np.sqrt(nearest)
    return np.where(inside, -dist, dist)


def _bookmark_core(leg: float, radius: float, samples: int = 24) -> list[tuple[float, float]]:
    """The path that becomes the fold once it is dilated by `radius`.

    Dilation is what rounds the two tips, so the core stops a radius short of the
    poster edges and `leg` stays the true reach of the finished mark.  The outer
    corner instead runs well past the origin: dilating a point that is already
    off-canvas keeps the corner square, so the mark stays flush with the poster
    edges however much the client rounds them.
    """
    out = -3.0 * radius
    tip = leg - radius
    top  = (tip, 0.0)
    left = (0.0, tip)

    # Inner edge as a quadratic Bezier bowing away from the corner.
    mid_x, mid_y = (top[0] + left[0]) / 2.0, (top[1] + left[1]) / 2.0
    ctrl = (mid_x + _BM_INNER_BOW * leg, mid_y + _BM_INNER_BOW * leg)

    points = [(out, out), top]
    for i in range(1, samples + 1):
        t = i / samples
        u = 1.0 - t
        points.append((
            u * u * top[0] + 2 * u * t * ctrl[0] + t * t * left[0],
            u * u * top[1] + 2 * u * t * ctrl[1] + t * t * left[1],
        ))
    return points


def draw_quality_corner_bookmark(
    image: Image.Image,
    quality_tokens: Sequence[str],
    *,
    bookmark_size: int = 16,
    side: str = "left",
) -> None:
    """Render a small tier-coloured fold attached to a poster top corner.

    ``side="right"`` mirrors it into the top-right corner, for when a diagonal
    sash has taken the top-left.
    """
    W, H = image.size
    if W < 2 or H < 2:
        return

    size = min(max(6, int(bookmark_size)), W, H)
    colors = _tier(_score_points(quality_tokens))
    spill = max(2, round(size * 0.45))
    canvas = min(size + spill, W, H)

    radius = max(0.75, _BM_TIP_RADIUS * size)
    sdf = _convex_sdf(_bookmark_core(float(size), radius), canvas, canvas)
    # Coverage from the distance field: antialiasing without a supersampled draw.
    cover = np.clip(radius - sdf + 0.5, 0.0, 1.0)

    ys, xs = np.mgrid[0:canvas, 0:canvas]
    face = np.array(colors["primary"][:3], dtype=float)
    highlight = np.array(colors["highlight"][:3], dtype=float)

    # Corner-lit gradient: brightest where the fold meets the corner, settling
    # into the flat tier colour by the time it reaches the inner edge.
    lit = (1.0 - np.clip((xs + ys + 1.0) / (2.0 * size), 0.0, 1.0)) ** 2 * _BM_CORNER_LIT
    rgb = face * np.ones((canvas, canvas, 1)) * (1 - lit[..., None]) + highlight * lit[..., None]
    alpha = cover * (colors["primary"][3] / 255.0)

    # Rim light hugging the inner edge — reads as the lit lip of a folded ribbon.
    rim_w = _BM_RIM * size
    rim = np.clip(1.0 - np.abs((radius - sdf) - rim_w) / rim_w, 0.0, 1.0) * cover
    rgb = rgb * (1 - rim[..., None] * 0.45) + highlight * (rim[..., None] * 0.45)
    alpha = np.clip(alpha + rim * (colors["highlight"][3] / 255.0) * 0.9, 0.0, 1.0)

    mark = Image.fromarray(
        np.dstack([rgb.clip(0, 255), alpha * 255]).astype(np.uint8),
    )

    def _blurred(color, peak_alpha: float, blur: float) -> Image.Image:
        layer = np.dstack([
            np.ones((canvas, canvas, 1)) * np.array(color[:3], dtype=float),
            cover * peak_alpha,
        ]).astype(np.uint8)
        return Image.fromarray(layer).filter(
            ImageFilter.GaussianBlur(max(1.0, size * blur))
        )

    # A restrained halo carries over the existing quality-notch language, and a
    # tight contact shadow keeps the fold legible against pale artwork.
    below = _blurred(colors["glow"], colors["glow"][3], 0.11)
    shadow = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    offset = max(1, round(size * 0.05))
    shadow.alpha_composite(
        _blurred(colors["shadow"], min(140, colors["shadow"][3]), 0.07),
        dest=(offset, offset),
    )

    composite = Image.alpha_composite(Image.alpha_composite(below, shadow), mark)
    if side == "right":
        image.alpha_composite(composite.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
                              dest=(W - canvas, 0))
    else:
        image.alpha_composite(composite, dest=(0, 0))
