"""
Landscape (16:9) poster rendering.

Deliberately a separate renderer rather than a mode inside ``build_poster``.
Almost every anchor in the portrait layout is keyed to *width* — the diagonal
sash, the badge row, the rating bar, the logo box — and on a canvas that is
twice as wide and 40% shorter every one of them lands wrong.  The portrait
vocabulary does not survive the aspect change, so this file owns its own.

Layout, all fractions of the canvas:

    +--------------------------------------------------+
    |  [badge]                              [badge]    |   top_left / top_right
    |                                                  |
    |                                                  |
    |......................vignette....................|   band, _BAND_RATIO h
    |  LOGO  (or title)              Genre | Yr | 87   |
    +--------------------------------------------------+

Three rules govern the whole thing:

  * **Sizes key off height, positions off both.**  A width-derived font on a
    1000x563 canvas is nearly three times its optical size on 500x750.
  * **Both top corners stay clear of anything load-bearing.**  Stremio draws its
    watched check and hover-dismiss top-left, Nuvio draws its watched badge
    top-right; each takes roughly 11% of width by 20% of height.  The badge is
    placed inside that zone only because the user asked for it — it is a glass
    pill, so a small circle overlapping its leading corner stays readable.
  * **Baselines sit above 0.85 h**, clearing Stremio's continue-watching
    progress bar.

The tinted vignette is the one part of the portrait system that transfers
unchanged, and improves: its colour ramp already runs left-to-right across the
band, so twice the width gives it twice the runway.  Its helpers live in
main.py and are imported at call time — the same late-import idiom tvdb.py uses
for tmdb internals — to keep this module free of a circular import.
"""
from __future__ import annotations

import colorsys
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from i18n import translate_genre, translate_sash, upper_label

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# --- Layout constants (fractions of the canvas) ------------------------------

# Bottom vignette height.  Was 0.40, the portrait "medium" band; on a frame
# that is 16:9 the same fraction starts a third of the way up the subjects,
# and with a strong tint the band's onset read as a wash over the content
# rather than a base under the text.  The band is a base for the text row,
# not a box the logo has to fit inside — the logo has its own ceiling
# (_LOGO_MAX_H) and its own shadow, so it may stand above the band's edge.
_BAND_RATIO      = 0.45
_BAND_ALPHA      = 212    # peak alpha at the very bottom row
# Power applied to the smoothstep (see _band_ramp).  1.0 is the plain S-curve
# with its midpoint halfway down the band; above it the darkness gathers lower
# and the top half of the band goes nearly clear.  The top edge stays
# invisible at any value — that is the point of the curve, not of this knob.
_BAND_GAMMA      = 1.4

# Absolute cell counts the tint sampler works in.  The portrait defaults (64/24)
# describe a 500px-wide band; at 1000px each cell would cover twice the content,
# so the local-colour end of the blur slider would go coarse exactly where it
# wants to be sharper.
_TINT_COLUMNS    = 96
_RAMP_COLUMNS    = 36

_SIDE_PAD        = 0.055  # left inset for the logo / badge
_RIGHT_PAD       = 0.045  # right inset for the info strip
# Shared bottom baseline for the logo and the info strip — the logo's ink
# bottom and the text baseline, which is where the two align optically.
#
# Anchored low on purpose.  The band's alpha ramps to full at the very bottom
# row, so anything sitting high in it is being asked to read against the weakest
# part of the only thing put there to support it.  This leaves a ~6% margin
# below the text, which is about where the ink stops once descenders are drawn.
_BASELINE        = 0.925

_LOGO_MAX_W      = 0.42   # keeps the logo out of the info strip's half
# Independent of the band on purpose.  It used to be capped at the band's top
# edge as well, which made the logo a function of the vignette: lowering the
# band to 0.25 h shrank every height-bound logo to 0.155 h, unreadable on a
# TV.  A stacked logo now rises above a shallow band on its drop shadow.
_LOGO_MAX_H      = 0.30

# Logo drop shadow: a diffuse pool rather than a hard offset copy, so a
# wordmark lifts off a light patch of the band without a second outline.
_LOGO_SHADOW_ALPHA = 200
_LOGO_SHADOW_BLUR  = 9.0
_LOGO_SHADOW_DX    = 2
_LOGO_SHADOW_DY    = 5
# The info strip's shadow: tighter than the logo's, because at text size a
# 9px pool reads as a smudge rather than a lift.
_INFO_SHADOW_ALPHA = 170
_INFO_SHADOW_BLUR  = 5.0

_BADGE_TOP       = 0.075
_BADGE_FONT      = 0.048  # was 0.042; pill scaled up ~15% with its padding
_BADGE_PAD_X     = 25
_BADGE_PAD_Y     = 13
# Soft drop shadow under the glass pill, the same idea as the logo's: a top
# corner is bare art, and a light pill on a light sky had nothing to stand off.
_BADGE_SHADOW_ALPHA = 150
_BADGE_SHADOW_BLUR  = 0.28   # Gaussian radius as a fraction of pill height
_BADGE_SHADOW_DY    = 0.14   # downward offset, likewise

_INFO_FONT       = 0.058  # "Genre • Year • Score" strip

# Fallback title, used when a title has no logo.  A range rather than a size:
# it is set as large as fits and stepped down before anything is cut, because a
# title is content and losing it should be the last resort.  Two lines are
# allowed for the same reason — the logo box is 0.30 h and one line of text uses
# about a third of that, so the second line is free and lands the text nearer the
# optical weight of the logos it shares a row with.
_TITLE_FONT_MAX  = 0.085
_TITLE_FONT_MIN  = 0.050
_TITLE_FONT_STEP = 0.005
_TITLE_LINE      = 1.12   # line height as a multiple of font size
_TITLE_MAX_LINES = 2

_MUTED           = (255, 255, 255, 195)   # was 170; lifted with the shadow
_SEPARATOR       = (255, 255, 255, 90)

# Black, not a colour of its own.  The panel already carries the poster's hue,
# and any tinted border competes with it — a gold one disappeared outright on
# posters whose dominant colour was itself gold.  Black reads as an edge against
# every panel the art can produce.
_BORDER_RGB      = (0, 0, 0)
_BORDER_RATIO    = 0.045  # hairline width as a fraction of pill height
_BORDER_ALPHA    = 200
_BORDER          = False  # borderless: the lift below is what separates it

# Borderless lift.  The panel takes the colour of the art directly under it and
# raises its Value, so it reads as a lit surface sitting above that art rather
# than a hole cut into it.  Whichever of the two lifts is larger wins: the
# multiplier carries mid-tones, the addend rescues near-black backings that a
# multiplier would leave black.  Saturation eases off slightly — a lit surface
# scatters, so holding full chroma reads as paint rather than glass.
_LIFT_MUL        = 1.85
_LIFT_ADD        = 0.34
_LIFT_SAT        = 0.82
_LIFT_OPACITY    = 0.86  # frost layer alpha; higher than the bordered pill used
# Minimum luma the panel has to stand off its backing by, 0-255.  Lifting alone
# cannot always reach it: a backing that is already bright has no headroom left,
# and the panel lands on the same tone it is sitting on.  Where that happens the
# same colour is taken downward instead — still the art's own hue, still no
# border, just separated in the direction that had room.
_MIN_SEPARATION  = 30.0
_DROP_MUL        = 0.45
_DROP_SUB        = 0.28

# How the badge takes the poster's colour — see _glass_pill.  "match" holds the
# art's own lightness, so a dark poster keeps a dark panel; True is the frosted
# notch's reference mode, which lifts Value and always lands light.
_LANDSCAPE_FROST_MODE: bool | str = "match"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(_FONTS_DIR, name), max(1, size))


def _band_ramp(band_h: int) -> np.ndarray:
    """Per-row alpha of the bottom band, top row first.

    The portrait band's ``1 - (1 - t) ** k`` starts at full slope: the first
    row inside the band is already darker than the row above it by the same
    step as every row after, and on a short canvas the eye reads that kink as
    a line ruled across the art — the classic Mach band.  On a 2:3 poster the
    onset is spread over enough rows to pass; here it is not, and no setting
    of height or strength hides it, because the kink is in the curve's shape.

    So the band uses a smoothstep instead: zero slope at its top edge, so the
    art simply starts to deepen with no row to point at, and zero slope at the
    bottom, where the alpha settles at its peak under the text.  ``_BAND_GAMMA``
    then gathers the darkness lower without reintroducing the kink — the top
    stays flat at any power, only the middle moves.
    """
    t = np.linspace(0.0, 1.0, band_h, dtype=np.float32)
    smooth = t * t * (3.0 - 2.0 * t)
    return (smooth ** _BAND_GAMMA * _BAND_ALPHA).astype(np.uint8)


def _draw_vignette(image: Image.Image, art: Image.Image, cfg,
                   source: tuple[float, float, float] | None = None,
                   ) -> tuple[float, float, float] | None:
    """Paint the bottom band, tinted from the art when the user asked for it.

    ``art`` is the pre-vignette snapshot: sampling ``image`` would just return
    the darkness a previous pass painted.

    ``source`` overrides the band's own colour choice with a colour decided
    elsewhere (the badge's, under "vignette follows badge").  Returns the tint
    the band was painted from, or None when it was left plain black, so the
    badge can follow it the other way round.
    """
    from main import (
        _fog_pick, _fog_faces, _vignette_tint_band, _vignette_frost_band, _vignette_level_band,
        _vignette_composite, _VIGNETTE_SAT_FULL, _VIGNETTE_MATCH_MIN_CONF,
        _VIGNETTE_SEAM_H,
    )

    width, height = image.size
    band_h = max(1, int(height * _BAND_RATIO))
    band_y = height - band_h

    ramp = Image.fromarray(
        np.broadcast_to(_band_ramp(band_h)[:, np.newaxis], (band_h, width)).copy(),
    )

    box = (0, band_y, width, height)
    tinted = None
    painted = None
    cover = None
    if cfg.vignette_poster_color_bottom:
        if source is not None:
            # Handed a colour: paint with it outright.  Confidence is the
            # badge's business, and it has already committed to this hue.
            tint, conf, second = tuple(float(c) for c in source), 1.0, None
        else:
            # The same pick the portrait bottom band makes (see _fog_pick), so
            # "Blend Into Nearby Art" means the same thing on both shapes: the
            # art this band covers, plus its seam, counts extra.
            tint, conf, second, cover = _fog_pick(
                art, (0, max(0, band_y - int(height * _VIGNETTE_SEAM_H)), width, height),
                cfg.vignette_color_local, cfg.vignette_color_ramp, _fog_faces(art),
            )
        if tint is not None:
            # Same derivation the portrait bands use: levelling follows
            # whichever of saturation / blur is asking for more of it.
            slider = min(1.0, max(0.0, cfg.vignette_color_saturation) / _VIGNETTE_SAT_FULL)
            # Only a colour the band actually shows is one the badge may
            # follow — the same bar the portrait notch's match uses.  A band
            # that came out near black, or at saturation 0, is black.
            if conf >= _VIGNETTE_MATCH_MIN_CONF and slider > 0:
                painted = tint
            level = max(slider, min(1.0, max(0.0, cfg.vignette_color_blur)))
            _vignette_frost_band(image, box, ramp, cfg.vignette_color_blur)
            _vignette_level_band(image, box, ramp, level)
            tinted = _vignette_tint_band(
                art, box, tint, conf,
                cfg.vignette_color_saturation, cfg.vignette_color_blur,
                second, cfg.vignette_color_lightness,
                columns=_TINT_COLUMNS, ramp_columns=_RAMP_COLUMNS,
                cover_lightness=cover, style=cfg.vignette_color_style,
            )

    if tinted is None:
        tinted = Image.new("RGBA", (width, band_h), (0, 0, 0, 0))
        tinted.putalpha(ramp)
        image.paste(tinted, (0, band_y), mask=tinted)
    else:
        # Dithered, like the portrait bands — see _vignette_composite.
        _vignette_composite(image, band_y, tinted, _band_ramp(band_h).astype(np.float32))
    return painted


def _luma(rgb) -> float:
    """Rec. 709 relative luminance, 0-255."""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _lift(rgb: tuple[float, float, float], backing: float) -> tuple[int, int, int]:
    """Move a colour off its backing in Value, keeping its hue.

    Up by preference — a lit surface above the art is the effect being aimed at.
    But a bright backing leaves nowhere to go: on a stadium crowd at luma 126 the
    lifted panel measured 128, a separation of 2, which the eye reads as a hole
    rather than a surface.  When the lift cannot clear ``_MIN_SEPARATION`` the
    same hue is taken down instead, which always has room because the floor is
    black.
    """
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    s *= _LIFT_SAT

    up = colorsys.hsv_to_rgb(h, s, min(1.0, max(v * _LIFT_MUL, v + _LIFT_ADD)))
    up = tuple(c * 255 for c in up)
    if _luma(up) - backing >= _MIN_SEPARATION:
        return tuple(round(c) for c in up)

    down = colorsys.hsv_to_rgb(h, s, max(0.0, min(v * _DROP_MUL, v - _DROP_SUB)))
    return tuple(round(c * 255) for c in down)


def _drop_shadow(image: Image.Image, mask: Image.Image, x: int, y: int,
                 radius: float, alpha: int) -> None:
    """Composite a blurred black copy of ``mask`` with its top-left at (x, y).

    The blur spills past the mask's own edges, so the shadow is built on a
    padded canvas and then clipped to the image — alpha_composite refuses a
    negative destination, which a pill in the top-left corner would produce.
    """
    pad = int(radius * 3) + 1
    sheet = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    sheet.paste(mask.point(lambda a: a * alpha // 255), (pad, pad))
    sheet = sheet.filter(ImageFilter.GaussianBlur(radius))
    sx, sy = x - pad, y - pad
    left, top = max(0, -sx), max(0, -sy)
    right  = min(sheet.width,  image.width  - sx)
    bottom = min(sheet.height, image.height - sy)
    if right <= left or bottom <= top:
        return
    sheet = sheet.crop((left, top, right, bottom))
    shadow = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    shadow.putalpha(sheet)
    image.alpha_composite(shadow, (sx + left, sy + top))


def _glass_pill(image: Image.Image, box: tuple[int, int, int, int],
                art: Image.Image, cfg,
                source: tuple[float, float, float] | None = None,
                ) -> tuple[int, int, int]:
    """Frosted pill carrying the poster's own colour.  Returns its ink colour.

    ``source`` replaces the colour the pill would sample for itself — the
    vignette's tint, when the two are linked.  It still goes through the lift,
    because the lift is what makes the pill legible on whatever it lands on:
    the link shares the hue, not the vignette's darkness.

    Same construction as the portrait frosted notch, and deliberately the same
    helpers: a blurred crop of what the pill sits on, under a tint layer whose
    colour comes from the art rather than from the crop.  Sampling the whole
    frame rather than the region under the pill is what keeps it agreeing with
    the vignette — a local sample would put a different colour under a top-left
    badge than under a bottom-left one on the same poster.

    ``_LANDSCAPE_FROST_MODE`` picks how that colour is used:
      "match" — the colour as it came, lightness included, floored short of
                black.  Keeps a dark poster dark, so the pill still reads as
                smoked glass rather than becoming a bright chip.
      True    — reference: the poster's true hue and saturation, lifted to a
                legibility floor.  Light panel, dark ink.
    """
    from awards import dominant_frost_rgb, _frosted_tint, _frost_ink
    from ratings import _cairo_pill_mask

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return (255, 255, 255)

    blurred = (image.crop(box).convert("RGB")
               .filter(ImageFilter.GaussianBlur(max(4, int(h * 0.35))))
               .convert("RGBA"))

    if _BORDER:
        tint = _frosted_tint(*dominant_frost_rgb(art),
                             cfg.sash_badge_frost_saturation, _LANDSCAPE_FROST_MODE)
        opacity = cfg.sash_badge_frost_opacity
    else:
        # Borderless: the colour comes from the art *under the pill* rather than
        # from the whole frame, because separation is a local judgement — what
        # matters is the panel standing off the pixels it actually covers.
        # dominant_frost_rgb's fallback handles the case where those pixels are
        # too dark or too washed to carry a hue, borrowing the frame's instead.
        backing = np.asarray(blurred.convert("RGB"), dtype=np.float32)
        base = source if source is not None else dominant_frost_rgb(image.crop(box), fallback=art)
        tint = _lift(base, _luma(backing.reshape(-1, 3).mean(axis=0)))
        opacity = _LIFT_OPACITY

    # Cairo rasterises at ANTIALIAS_BEST; PIL's rounded_rectangle has no
    # antialiasing at all, which on a hairline border is the difference between
    # an edge and a staircase.  The border is the difference of two masks rather
    # than a stroked outline, so both of its edges are smooth — stroking would
    # only smooth the outer one.
    mask = _cairo_pill_mask(w, h, h // 2)
    blurred.putalpha(mask)
    frost = Image.new("RGBA", (w, h), (*tint, 0))
    frost.putalpha(mask.point(lambda a: int(a * opacity)))
    # Shadow goes down after the glass has sampled the art beneath it, so the
    # frost doesn't blur its own shadow into a darker panel, and before the
    # pill, which covers the part of it that falls inside the outline.
    _drop_shadow(image, mask, x0, y0 + int(h * _BADGE_SHADOW_DY),
                 h * _BADGE_SHADOW_BLUR, _BADGE_SHADOW_ALPHA)
    image.alpha_composite(Image.alpha_composite(blurred, frost), (x0, y0))

    if _BORDER:
        bw = max(1, round(h * _BORDER_RATIO))
        iw, ih = max(1, w - 2 * bw), max(1, h - 2 * bw)
        inner = Image.new("L", (w, h), 0)
        inner.paste(_cairo_pill_mask(iw, ih, ih // 2), (bw, bw))
        ring = ImageChops.subtract(mask, inner)

        border = Image.new("RGBA", (w, h), (*_BORDER_RGB, 255))
        border.putalpha(ring.point(lambda a: a * _BORDER_ALPHA // 255))
        image.alpha_composite(border, (x0, y0))

    return _frost_ink(*tint)


def _draw_badge(image: Image.Image, text: str, position: str, art: Image.Image,
                cfg, logo_height: int = 0, plain: bool = False,
                source: tuple[float, float, float] | None = None) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)
    # User scale on top of the tuned size: a pill legible on a monitor is
    # not necessarily legible from a sofa.  Padding scales with the type so
    # the pill keeps its proportions rather than growing a thick rim.
    scale = max(0.1, float(getattr(cfg, "landscape_badge_scale", 1.0) or 1.0))

    if plain:
        # The stacked slot sits inside the band, so the glass would be a second
        # surface doing a job the vignette has already done.  Set at the info
        # strip's size and on its baseline, so the two read as one bottom row
        # rather than as a label that happens to be near some metadata.
        _plain_font = _font("Inter-Bold.ttf", int(height * _INFO_FONT * scale))
        draw.text((int(width * _SIDE_PAD), int(height * _BASELINE)), text,
                  font=_plain_font, fill=(255, 255, 255, 242), anchor="ls")
        return

    font = _font("Inter-Bold.ttf", int(height * _BADGE_FONT * scale))
    tw = draw.textlength(text, font=font)
    th = int(height * _BADGE_FONT * scale)
    pad_x, pad_y = round(_BADGE_PAD_X * scale), round(_BADGE_PAD_Y * scale)
    bw, bh = int(tw + pad_x * 2), int(th + pad_y * 2)

    if position == "top_right":
        x, y = width - int(width * _RIGHT_PAD) - bw, int(height * _BADGE_TOP)
    elif position == "logo":
        # Stacked above the logo, sharing its left edge.  With no logo drawn —
        # original art, which carries its own title treatment — there is nothing
        # to stack on, so the badge takes the bottom-left slot itself.
        x = int(width * _SIDE_PAD)
        y = int(height * _BASELINE) - bh
        if logo_height:
            y -= logo_height + int(height * 0.045)
    else:  # top_left
        x, y = int(width * _SIDE_PAD), int(height * _BADGE_TOP)

    ink = _glass_pill(image, (x, y, x + bw, y + bh), art, cfg, source=source)
    draw.text((x + pad_x, y + pad_y - round(2 * scale)), text, font=font,
              fill=(*ink, 245))


def _draw_logo(image: Image.Image, logo: Image.Image) -> tuple[int, int]:
    """Left-aligned, bottom-anchored. Returns (drawn height, right edge x) — the
    edge is what the info strip keeps clear of (see _draw_info_strip)."""
    width, height = image.size

    alpha = logo.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 32 else 0).getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    if logo.width <= 0 or logo.height <= 0:
        return 0, 0

    max_h = int(height * _LOGO_MAX_H)
    scale = min(int(width * _LOGO_MAX_W) / logo.width, max(1, max_h) / logo.height)
    drawn = logo.resize((max(1, round(logo.width * scale)),
                         max(1, round(logo.height * scale))), Image.Resampling.LANCZOS)

    x = int(width * _SIDE_PAD)
    y = int(height * _BASELINE) - drawn.height

    # Soft drop shadow so a white wordmark survives a light patch in the band.
    # Built on a padded canvas (see _drop_shadow): blurring the logo's own
    # alpha on a canvas exactly its size clamps at the edges, and wherever the
    # ink reaches its bounding box the blur smears into a straight-edged slab
    # — the shadow came out as a box drawn around the logo.
    _drop_shadow(image, drawn.getchannel("A"), x + _LOGO_SHADOW_DX, y + _LOGO_SHADOW_DY,
                 _LOGO_SHADOW_BLUR, _LOGO_SHADOW_ALPHA)
    image.alpha_composite(drawn, (x, y))
    return drawn.height, x + drawn.width


def _wrap(draw, text: str, font, max_w: float, max_lines: int) -> list[str] | None:
    """Greedy word wrap.  None when it will not fit in ``max_lines`` — including
    the case of a single word too long for one line, which no wrap can help."""
    words = text.split()
    if not words:
        return None
    lines, current = [], words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                return None
    lines.append(current)
    if any(draw.textlength(line, font=font) > max_w for line in lines):
        return None
    return lines


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    """Trim to fit, measuring *with* the ellipsis so the result is inside max_w.

    Whole words go first — "Marvelous…" reads as a title cut short, where the
    character-wise version, "Marvelous Mornin…", reads as a bug.  Characters are
    only cut when a single word is itself too long.
    """
    if draw.textlength(text, font=font) <= max_w:
        return text
    words = text.split()
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words) + "…"
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    stem = words[0] if words else text
    while stem and draw.textlength(stem + "…", font=font) > max_w:
        stem = stem[:-1]
    return f"{stem}…" if stem else ""


def _draw_title(image: Image.Image, title: str) -> tuple[int, int]:
    """Left-aligned, bottom-anchored text stand-in for a missing logo.

    Shares the logo's box, and returns (drawn height, right edge x) the same
    way, so a badge stacked above it clears the text rather than landing on it
    and the info strip knows how far the text actually reaches.
    """
    width, height = image.size
    draw = ImageDraw.Draw(image)

    max_w = int(width * _LOGO_MAX_W)
    max_h = int(height * _LOGO_MAX_H)

    # Largest size that fits, one line preferred over two at every size — a
    # single line beside a logo reads better than a wrapped one a size larger.
    chosen: tuple[object, list[str], int] | None = None
    ratio = _TITLE_FONT_MAX
    while ratio >= _TITLE_FONT_MIN - 1e-9 and chosen is None:
        size = max(1, int(height * ratio))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        for count in range(1, _TITLE_MAX_LINES + 1):
            if line_h * count > max_h:
                break
            lines = _wrap(draw, title, font, max_w, count)
            if lines is not None and len(lines) == count:
                chosen = (font, lines, line_h)
                break
        ratio -= _TITLE_FONT_STEP

    if chosen is None:
        # Nothing fits whole: set at the smallest size and cut the last line.
        size = max(1, int(height * _TITLE_FONT_MIN))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        count = max(1, min(_TITLE_MAX_LINES, int(max_h // line_h) or 1))
        lines = _wrap(draw, title, font, max_w, count) or []
        if len(lines) < count:
            # Rebuild greedily, keeping whatever fits, then trim the tail.
            words, lines, current = title.split(), [], ""
            for word in words:
                trial = f"{current} {word}".strip()
                if current and draw.textlength(trial, font=font) > max_w:
                    lines.append(current)
                    current = word
                    if len(lines) == count:
                        break
                else:
                    current = trial
            if len(lines) < count and current:
                lines.append(current)
        lines = lines[:count]
        if lines:
            consumed = len(" ".join(lines))
            remainder = title[consumed:].strip()
            if remainder:
                lines[-1] = f"{lines[-1]} {remainder}"
            # Every line, not only the last.  The greedy pass above appends a
            # word that is itself wider than the box untouched, and that word
            # can land on any line — trimming the tail alone left the overlong
            # one running off the canvas.  _ellipsize is a no-op on a line that
            # already fits, so the lines that were fine stay untouched.
            lines = [_ellipsize(draw, line, font, max_w) for line in lines]
        chosen = (font, lines or [_ellipsize(draw, title, font, max_w)], line_h)

    font, lines, line_h = chosen
    baseline = int(height * _BASELINE)
    x = int(width * _SIDE_PAD)
    for i, line in enumerate(reversed(lines)):
        draw.text((x, baseline - i * line_h), line,
                  font=font, fill=(255, 255, 255, 245), anchor="ls")

    ascent = font.getmetrics()[0]
    right = x + int(max(draw.textlength(line, font=font) for line in lines))
    return line_h * (len(lines) - 1) + ascent, right


def _draw_info_strip(image: Image.Image, genre_label: str,
                     release_year: str | None, score, scale: float = 1.0,
                     logo_right: int | None = None,
                     out_of_10: bool = False) -> None:
    """`Genre • Year • 87`, right-aligned on the shared baseline.

    Drawn right-to-left so the score stays pinned to the right edge whatever the
    genre string does, and the whole strip is measured before anything is drawn
    so a long genre can be dropped rather than colliding with the logo.

    ``scale`` (landscape_info_scale) sizes the text and its shadow together;
    the baseline and right edge stay put, so it grows up and to the left.

    ``logo_right`` is where the logo (or title) actually ends.  The strip keeps
    clear of that rather than of the widest a logo is ever allowed to be: with
    the maximum reserved, a narrow logo still cost the strip its genre, and at
    any enlarged size it lost it every time.  None falls back to the maximum.

    ``out_of_10`` prints the score the way portrait's out-of-10 switches do:
    one decimal ("8.7", "8.0"), with a bare "10" at the top.
    """
    width, height = image.size
    scale = max(0.1, float(scale or 1.0))
    font = _font("Inter-Bold.ttf", max(1, int(height * _INFO_FONT * scale)))
    draw = ImageDraw.Draw(image)

    # No rating is not a rating of nothing: a title MDBList has no score for
    # drops out of the row entirely, taking its separator with it, rather than
    # printing a placeholder that reads as a value.
    if isinstance(score, bool):
        score_text = None
    elif isinstance(score, int):
        score_text = str(score)
    elif isinstance(score, str) and score.strip().isdigit():
        score_text = score.strip()
    else:
        score_text = None
    if score_text and out_of_10:
        value = int(score_text)
        score_text = "10" if value >= 100 else f"{value / 10:.1f}"

    # The score takes the same weight as the genre and the year rather than a
    # score-banded colour.  Here the three are one line of metadata, and one
    # member of it changing hue per title breaks the row instead of ranking it.
    parts: list[tuple[str, tuple[int, int, int, int]]] = []
    if genre_label:
        parts.append((genre_label, _MUTED))
    if release_year:
        parts.append((str(release_year), _MUTED))
    if score_text:
        parts.append((score_text, _MUTED))
    if not parts:
        return

    sep = "  •  "
    sep_w = draw.textlength(sep, font=font)

    def total(items) -> float:
        return (sum(draw.textlength(t, font=font) for t, _ in items)
                + sep_w * max(0, len(items) - 1))

    # Everything left of the info strip belongs to the logo; if the two would
    # meet, shed the genre first, then the year, before shrinking any type.
    left = width * (_SIDE_PAD + _LOGO_MAX_W) if logo_right is None else logo_right
    limit = width * (1 - _RIGHT_PAD) - left - width * 0.03
    while len(parts) > 1 and total(parts) > limit:
        parts.pop(0)

    # Drawn on a layer of its own so the strip's ink can cast one shadow —
    # the same pool the logo gets, for the same reason: the band is the
    # strip's only backing, and on light art it can be thin where the text
    # sits.  Compositing the layer afterwards keeps the text itself crisp.
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    x = width - int(width * _RIGHT_PAD)
    baseline = int(height * _BASELINE)
    for i, (text, fill) in enumerate(reversed(parts)):
        tw = draw.textlength(text, font=font)
        ldraw.text((x - tw, baseline), text, font=font, fill=fill, anchor="ls")
        x -= tw
        if i < len(parts) - 1:
            x -= sep_w
            ldraw.text((x, baseline), sep, font=font, fill=_SEPARATOR, anchor="ls")
    ink = layer.getchannel("A")
    bbox = ink.getbbox()
    if bbox:
        # The strip is translucent, so a shadow straight under it shows
        # through the letters and reads as the text going darker rather than
        # standing out.  The shadow is built on its own layer and the ink
        # punched out of it, leaving only the halo around the glyphs.
        x0, y0, x1, y1 = bbox
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        _drop_shadow(shadow, ink.crop(bbox), x0 + _LOGO_SHADOW_DX, y0 + _LOGO_SHADOW_DY,
                     _INFO_SHADOW_BLUR * scale, _INFO_SHADOW_ALPHA)
        shadow.putalpha(ImageChops.multiply(shadow.getchannel("A"), ImageChops.invert(ink)))
        image.alpha_composite(shadow)
    image.alpha_composite(layer)


def build_landscape(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta=None,
    release_year: str | None = None,
    **_ignored,
) -> Image.Image:
    """Render the landscape poster.  Mirrors ``build_poster``'s call shape so the
    request pipeline can swap one for the other; extra kwargs it does not use
    (quality tokens, age rating) are accepted and dropped."""
    from main import pick_sash

    image = image.convert("RGBA")
    art = image.copy()          # pre-vignette snapshot for tint sampling

    # Colour link between the band and the badge.  Left alone, each samples
    # the art its own way — the band its seam, the pill the patch under it —
    # and on some art they land a hue apart.  "vignette_follows_badge" gives
    # both the whole-frame colour the pill was originally specified to use;
    # "badge_follows_vignette" hands the pill whatever the band chose.  Either
    # way only the hue is shared: the band still darkens it, the pill still
    # lifts it.  Nothing to link when the band is plain black.
    link = getattr(cfg, "landscape_color_link", "off")
    shared = None
    if link == "vignette_follows_badge" and cfg.vignette_poster_color_bottom:
        from awards import dominant_frost_rgb
        shared = tuple(float(c) for c in dominant_frost_rgb(art))
    band_tint = _draw_vignette(image, art, cfg, source=shared)
    badge_source = band_tint if link == "badge_follows_vignette" else shared

    # What belongs in the logo slot was decided upstream, where the art actually
    # got picked: a logo, or a title to stand in for one, or neither when the
    # chosen art already carries its own title treatment.  Re-deriving that from
    # cfg.landscape_art is what this used to do, and it was wrong in exactly the
    # cases that matter — `original` falling back to the neutral backdrop or to
    # the genre canvas passes a title precisely because that art has none, and
    # suppressing it produced a completely untitled render.
    logo_height, logo_right = 0, None
    if logo is not None:
        logo_height, logo_right = _draw_logo(image, logo)
    elif fallback_title:
        # Height comes back for the same reason it does from the logo: a
        # badge stacked above needs something to clear.
        logo_height, logo_right = _draw_title(image, fallback_title)

    # Hiding the rating is passed as "there is no score": the strip already
    # drops a missing one along with its separator, which is exactly the result
    # wanted here, and the same switch reads the same way in either shape.
    _draw_info_strip(image,
                     "" if cfg.hide_genre else (translate_genre(genre, cfg.logo_language) or genre),
                     release_year, None if cfg.hide_rating else score,
                     scale=getattr(cfg, "landscape_info_scale", 1.0),
                     logo_right=logo_right,
                     out_of_10=getattr(cfg, "landscape_score_out_of_10", False))

    if cfg.sash_mode != "hidden" and discovery_meta is not None:
        sash_result = pick_sash(discovery_meta, cfg.sash_priority)
        if sash_result is not None:
            label, _sash_type = sash_result
            position = getattr(cfg, "landscape_badge_pos", "top_left")
            _draw_badge(image, upper_label(translate_sash(label, cfg.logo_language), cfg.logo_language),
                        position, art, cfg, logo_height=logo_height,
                        # Only a stacked badge with an empty logo slot lands
                        # inside the band.  Stacked over a logo or a title it
                        # often clears the band's top edge, and the top corners
                        # are bare art, so those keep the glass that makes them
                        # readable.  Keyed on what was drawn, not on the mode
                        # that was asked for, for the same reason as above.
                        plain=(logo_height == 0 and position == "logo"),
                        source=badge_source)

    return image
