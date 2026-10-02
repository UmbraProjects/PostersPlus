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
import dataclasses

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import fonts
from i18n import translate_genre, translate_sash, upper_label, visual


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

# Optional top band (landscape_vignette_top): the bottom band's colour, faded
# down from the top edge.  Shallower and lighter than the bottom one — it backs
# a corner badge at most, not a row of text.
_TOP_BAND_RATIO  = 0.30
_TOP_BAND_ALPHA  = 170

# Centred logo: the gap between its bottom and the top of the info line under it.
_STACK_GAP       = 0.035
# Where landscape_info_pos may put the "Genre • Year • Score" line; anything
# else is "auto" (see build_landscape).
_INFO_POSITIONS  = ("bottom_left", "bottom_center", "bottom_right",
                    "top_left", "top_center", "top_right")
# A logo in the top row (landscape_logo_pos=top_*) hangs from the badge line
# and is held a little shorter: it shares the top with the client's own marks.
_LOGO_MAX_H_TOP  = 0.24
# Between the logo and a badge stacked on it (above in the bottom row, below
# in the top row), or moved out of its way.
_BADGE_LOGO_GAP  = 0.045

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
    """Paint the bottom band, tinted from the art when the user asked for it,
    and the tinted top band when landscape_vignette_top asks for one.

    ``art`` is the pre-vignette snapshot: sampling ``image`` would just return
    the darkness a previous pass painted.

    ``source`` overrides the band's own colour choice with a colour decided
    elsewhere (the badge's, under "vignette follows badge").  Returns the tint
    the band was painted from, or None when it was left plain black, so the
    badge can follow it the other way round.
    """
    from main import (
        _fog_pick, _fog_faces, _vignette_tint_band, _vignette_frost_band, _vignette_level_band,
        _vignette_composite, _vignette_fog_ramp, _VIGNETTE_SAT_FULL, _VIGNETTE_MATCH_MIN_CONF,
        _VIGNETTE_SEAM_H,
    )

    width, height = image.size
    band_h = max(1, int(height * _BAND_RATIO))
    band_y = height - band_h

    ramp = Image.fromarray(
        np.broadcast_to(_band_ramp(band_h)[:, np.newaxis], (band_h, width)).copy(),
    )

    box = (0, band_y, width, height)
    top_h = max(1, int(height * _TOP_BAND_RATIO))
    want_top = bool(getattr(cfg, "landscape_vignette_top", False))
    tinted = None
    painted = None
    cover = None
    tint = None
    if cfg.vignette_poster_color_bottom or want_top:
        if source is not None:
            # Handed a colour: paint with it outright.  Confidence is the
            # badge's business, and it has already committed to this hue.
            tint, conf, second = tuple(float(c) for c in source), 1.0, None
        else:
            # The same pick the portrait bottom band makes (see _fog_pick), so
            # "Blend Into Nearby Art" means the same thing on both shapes: the
            # art this band covers, plus its seam, counts extra.  One pick for
            # both bands, as portrait does: the bottom band's when it is
            # tinted, else the top band's own.
            cover_box = ((0, max(0, band_y - int(height * _VIGNETTE_SEAM_H)), width, height)
                         if cfg.vignette_poster_color_bottom
                         else (0, 0, width, min(height, top_h + int(height * _VIGNETTE_SEAM_H))))
            tint, conf, second, cover = _fog_pick(
                art, cover_box, cfg.vignette_color_local, cfg.vignette_color_ramp, _fog_faces(art),
            )
    if tint is not None:
        # Same derivation the portrait bands use: levelling follows
        # whichever of saturation / blur is asking for more of it.
        slider = min(1.0, max(0.0, cfg.vignette_color_saturation) / _VIGNETTE_SAT_FULL)
        level = max(slider, min(1.0, max(0.0, cfg.vignette_color_blur)))
        if want_top:
            # Painted first: the two bands never overlap (0.30 + 0.45 < 1).
            top_alpha = _vignette_fog_ramp(top_h, _TOP_BAND_ALPHA, rising=False)
            top_ramp = Image.fromarray(np.broadcast_to(
                np.round(top_alpha).astype(np.uint8)[:, np.newaxis], (top_h, width)).copy())
            top_box = (0, 0, width, top_h)
            _vignette_frost_band(image, top_box, top_ramp, cfg.vignette_color_blur)
            _vignette_level_band(image, top_box, top_ramp, level)
            _vignette_composite(image, 0, _vignette_tint_band(
                art, top_box, tint, conf,
                cfg.vignette_color_saturation, cfg.vignette_color_blur,
                second, cfg.vignette_color_lightness,
                columns=_TINT_COLUMNS, ramp_columns=_RAMP_COLUMNS,
                cover_lightness=cover, style=cfg.vignette_color_style,
            ), top_alpha)
        if cfg.vignette_poster_color_bottom:
            # Only a colour the band actually shows is one the badge may
            # follow — the same bar the portrait notch's match uses.  A band
            # that came out near black, or at saturation 0, is black.
            if conf >= _VIGNETTE_MATCH_MIN_CONF and slider > 0:
                painted = tint
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


# The dark pills (landscape_badge_style), after portrait's black / silver /
# gold notch: a near-black body, or the notch's dark vertical gradient with a
# silver or gold rim, and a light label.
_DARK_INK   = {"black": (210, 210, 218), "silver": (255, 255, 255), "gold": (255, 255, 255)}
_TRIM       = {"silver": (192, 192, 200), "gold": (212, 175, 55)}


def _dark_pill(image: Image.Image, box: tuple[int, int, int, int], style: str,
               ink: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """A black / silver / gold pill at ``box``.  Returns its ink colour:
    ``ink`` (landscape_badge_text_color) when given, else the style's own."""
    from ratings import _cairo_pill_mask

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return ink or _DARK_INK.get(style, (255, 255, 255))
    mask = _cairo_pill_mask(w, h, h // 2)
    if style == "black":
        body = Image.new("RGBA", (w, h), (10, 10, 12, 0))
        body.putalpha(mask.point(lambda a: a * 230 // 255))
    else:
        t = np.linspace(0, 1, h, dtype=np.float32)
        dark = (4 + 10 * np.sin(t * np.pi))
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        arr[:, :, 0] = arr[:, :, 1] = dark.astype(np.uint8)[:, None]
        arr[:, :, 2] = np.minimum(255, dark * 1.3).astype(np.uint8)[:, None]
        body = Image.fromarray(arr)
        body.putalpha(mask.point(lambda a: a * 235 // 255))
        bw = max(1, round(h * 0.06))
        inner = Image.new("L", (w, h), 0)
        inner.paste(_cairo_pill_mask(max(1, w - 2 * bw), max(1, h - 2 * bw), max(1, (h - 2 * bw) // 2)),
                    (bw, bw))
        trim = Image.new("RGBA", (w, h), (*_TRIM[style], 0))
        trim.putalpha(ImageChops.subtract(mask, inner).point(lambda a: a * 215 // 255))
        body = Image.alpha_composite(body, trim)
    _drop_shadow(image, mask, x0, y0 + int(h * _BADGE_SHADOW_DY),
                 h * _BADGE_SHADOW_BLUR, _BADGE_SHADOW_ALPHA)
    image.alpha_composite(body, (x0, y0))
    return ink or _DARK_INK.get(style, (255, 255, 255))


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
                source: tuple[float, float, float] | None = None,
                logo_align: str = "left", logo_baseline: int | None = None,
                logo_box: tuple[int, int, int, int] | None = None,
                logo_top_row: bool = False,
                obstacles: tuple = ()) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)
    # User scale on top of the tuned size: a pill legible on a monitor is
    # not necessarily legible from a sofa.  Padding scales with the type so
    # the pill keeps its proportions rather than growing a thick rim.
    scale = max(0.1, float(getattr(cfg, "landscape_badge_scale", 1.0) or 1.0))
    text = visual(text)

    if plain:
        # The stacked slot sits inside the band, so the glass would be a second
        # surface doing a job the vignette has already done.  Set at the info
        # strip's size and on its baseline, so the two read as one bottom row
        # rather than as a label that happens to be near some metadata.
        _plain_font = fonts.label_font(max(1, int(height * _INFO_FONT * scale)))
        _px = _slot_x(width, draw.textlength(text, font=_plain_font), logo_align)
        draw.text((_px, int(height * _BASELINE) if logo_baseline is None else logo_baseline), text,
                  font=_plain_font, fill=(255, 255, 255, 242), anchor="ls")
        return

    font = fonts.label_font(max(1, int(height * _BADGE_FONT * scale)))
    tw = draw.textlength(text, font=font)
    th = int(height * _BADGE_FONT * scale)
    pad_x, pad_y = round(_BADGE_PAD_X * scale), round(_BADGE_PAD_Y * scale)
    bw, bh = int(tw + pad_x * 2), int(th + pad_y * 2)

    gap = int(height * _BADGE_LOGO_GAP)
    if position == "logo" and logo_top_row:
        # A top-row logo carries its badge underneath, aligned the way it is;
        # with no logo drawn the badge takes the slot at the top itself.
        x = _slot_x(width, bw, logo_align)
        y = logo_box[3] + gap if logo_box else int(height * _BADGE_TOP)
    elif position == "top_right":
        x, y = width - int(width * _RIGHT_PAD) - bw, int(height * _BADGE_TOP)
    elif position in ("bottom_left", "bottom_right"):
        # Standing on the shared baseline, in from that side.
        x = int(width * _SIDE_PAD) if position == "bottom_left" else width - int(width * _RIGHT_PAD) - bw
        y = int(height * _BASELINE) - bh
    elif position == "logo":
        # Stacked above the logo, aligned the way it is.  With no logo drawn —
        # original art, which carries its own title treatment — there is nothing
        # to stack on, so the badge takes the logo's slot itself.
        x = _slot_x(width, bw, logo_align)
        y = (int(height * _BASELINE) if logo_baseline is None else logo_baseline) - bh
        if logo_height:
            y -= logo_height + int(height * 0.045)
    else:  # top_left
        x, y = int(width * _SIDE_PAD), int(height * _BADGE_TOP)
    # Whatever it lands on — a logo or the info line in its corner — it moves
    # off, away from its edge: down past it at the top, up over it at the
    # bottom.  A few passes, since clearing one can land it on the next.
    down = y < height / 2
    for _ in range(4):
        hit = next((b for b in obstacles if b and x < b[2] and x + bw > b[0]
                    and y < b[3] and y + bh > b[1]), None)
        if hit is None:
            break
        y = hit[3] + gap if down else hit[1] - gap - bh

    style = getattr(cfg, "landscape_badge_style", "glass")
    if style in _DARK_INK:
        ink = _dark_pill(image, (x, y, x + bw, y + bh), style,
                         getattr(cfg, "landscape_badge_text_color", None))
    else:
        ink = _glass_pill(image, (x, y, x + bw, y + bh), art, cfg, source=source)
    draw.text((x + pad_x, y + pad_y - round(2 * scale)), text, font=font,
              fill=(*ink, 245))


def _slot_x(width: int, w: float, align: str) -> int:
    """Left edge of something ``w`` wide in the logo slot: in from the left
    (the default), in from the right, or centred."""
    if align == "right":
        return width - int(width * _RIGHT_PAD) - int(w)
    if align == "center":
        return int((width - w) / 2)
    return int(width * _SIDE_PAD)


def _draw_logo(image: Image.Image, logo: Image.Image, align: str = "left",
               baseline: int | None = None, top: int | None = None,
               scale: float = 1.0) -> tuple[int, int, int]:
    """Bottom-anchored on ``baseline`` (the shared one by default), or hung
    from ``top`` in the top row, and placed by ``align`` (landscape_logo_pos).
    ``scale`` (landscape_logo_scale) sizes the box it is fitted to.
    Returns (drawn height, left x, right x) — the edges are what the info
    strip keeps clear of (see _draw_info_strip)."""
    width, height = image.size

    alpha = logo.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 32 else 0).getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    if logo.width <= 0 or logo.height <= 0:
        return 0, 0, 0

    max_h = int(height * (_LOGO_MAX_H if top is None else _LOGO_MAX_H_TOP) * scale)
    fit = min(int(width * _LOGO_MAX_W * scale) / logo.width, max(1, max_h) / logo.height)
    drawn = logo.resize((max(1, round(logo.width * fit)),
                         max(1, round(logo.height * fit))), Image.Resampling.LANCZOS)

    x = _slot_x(width, drawn.width, align)
    if top is not None:
        y = top
    else:
        y = (int(height * _BASELINE) if baseline is None else baseline) - drawn.height

    # Black ink vanishes into a dark band; lighten it as the portrait does.
    under = np.asarray(image.crop((max(0, x), max(0, y), min(width, x + drawn.width),
                                   min(height, y + drawn.height))).convert("RGB"),
                       dtype=np.float32)
    if under.size and _luma(under.reshape(-1, 3).mean(axis=0)) / 255.0 < 0.40:
        from tmdb import ensure_light_logo
        drawn = ensure_light_logo(drawn)

    # Soft drop shadow so a white wordmark survives a light patch in the band.
    # Built on a padded canvas (see _drop_shadow): blurring the logo's own
    # alpha on a canvas exactly its size clamps at the edges, and wherever the
    # ink reaches its bounding box the blur smears into a straight-edged slab
    # — the shadow came out as a box drawn around the logo.
    _drop_shadow(image, drawn.getchannel("A"), x + _LOGO_SHADOW_DX, y + _LOGO_SHADOW_DY,
                 _LOGO_SHADOW_BLUR, _LOGO_SHADOW_ALPHA)
    image.alpha_composite(drawn, (x, y))
    return drawn.height, x, x + drawn.width


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


def _draw_title(image: Image.Image, title: str, align: str = "left",
                baseline: int | None = None, top: int | None = None,
                scale: float = 1.0) -> tuple[int, int, int]:
    """Bottom-anchored text stand-in for a missing logo, aligned like it.

    Shares the logo's box, and returns (drawn height, left x, right x) the same
    way, so a badge stacked above it clears the text rather than landing on it
    and the info strip knows how far the text actually reaches.
    """
    width, height = image.size
    draw = ImageDraw.Draw(image)

    max_w = int(width * _LOGO_MAX_W * scale)
    max_h = int(height * (_LOGO_MAX_H if top is None else _LOGO_MAX_H_TOP) * scale)
    # The title is in the poster's language, which the label font may not
    # have (a Hebrew title under Inter).
    font_path = fonts.font_for_text(fonts.label_path(), title)

    # Largest size that fits, one line preferred over two at every size — a
    # single line beside a logo reads better than a wrapped one a size larger.
    chosen: tuple[object, list[str], int] | None = None
    ratio = _TITLE_FONT_MAX
    while ratio >= _TITLE_FONT_MIN - 1e-9 and chosen is None:
        size = max(1, int(height * ratio * scale))
        font = fonts.truetype(font_path, size)
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
        size = max(1, int(height * _TITLE_FONT_MIN * scale))
        font = fonts.truetype(font_path, size)
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
    lines = [visual(line) for line in lines]   # wrapped in reading order, drawn in visual
    if top is not None:
        # Hung from the top: the first line's ascent starts at ``top``.
        baseline = top + font.getmetrics()[0] + line_h * (len(lines) - 1)
    elif baseline is None:
        baseline = int(height * _BASELINE)
    widest = max(draw.textlength(line, font=font) for line in lines)
    left = _slot_x(width, widest, align)
    # Each line sits on the block's own side: ragged right when left-aligned,
    # ragged left when right-aligned, centred on the block when centred.
    anchor, x = {"right": ("rs", left + widest), "center": ("ms", left + widest / 2)}.get(
        align, ("ls", left))
    for i, line in enumerate(reversed(lines)):
        draw.text((x, baseline - i * line_h), line,
                  font=font, fill=(255, 255, 255, 245), anchor=anchor)

    ascent = font.getmetrics()[0]
    return line_h * (len(lines) - 1) + ascent, left, left + int(widest)


def _draw_info_strip(image: Image.Image, genre_label: str,
                     release_year: str | None, score, scale: float = 1.0,
                     logo_right: int | None = None,
                     out_of_10: bool = False,
                     star: bool = False,
                     align: str = "right",
                     logo_left: int | None = None,
                     bounds: tuple[float, float] | None = None,
                     baseline: int | None = None,
                     top: int | None = None,
                     order: str = "",
                     rating_items: list[tuple[str, float]] | None = None,
                     badge_scale: str = "native",
                     badge_style: str = "color") -> tuple[int, int, int, int] | None:
    """`Genre • Year • 87`, right-aligned on the shared baseline.

    ``order`` (meta_order) rearranges the three, "year,genre,rating" and so on.

    ``align`` is the side it hangs from: "right" beside a left logo (the
    default), "left" beside a right one, "center" under a centred one.
    ``bounds`` (left x, right x) is the room it may use, in place of keeping
    clear of ``logo_left`` / ``logo_right``.  It sits on ``baseline`` (the
    shared one by default), or hangs from ``top``.  Returns the box its text
    takes (left, top of the caps, right, baseline), or None when nothing is
    drawn — what a stacked logo or a badge keeps clear of.

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

    ``star`` labels the score the way Clean does on a portrait: the separator
    in front of it becomes a ★ (`Genre • Year ★ 87`), or a lone score gets
    one of its own.  The star is the text's colour, not the separator's —
    it names the number rather than dividing the row.

    ``rating_items`` (landscape_rating_badges) puts those sites' scores,
    each behind its logo, where the score was, in ``badge_scale`` and
    ``badge_style`` (rating_badges.rating_run); the ★ goes, as the badges
    stand in for it.  Short of room they drop from the end, after the genre
    and the year, keeping the first.
    """
    width, height = image.size
    scale = max(0.1, float(scale or 1.0))
    font = fonts.label_font(max(1, int(height * _INFO_FONT * scale)))
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

    # A run is a list of pieces (rating_badges), told from a text by type.
    run = None
    if rating_items:
        import rating_badges

        def measure(text: str) -> float:
            return draw.textlength(text, font=font)

        def build_run(items) -> list:
            return rating_badges.rating_run(items, max(1, int(height * _INFO_FONT * scale)),
                                            badge_scale, out_of_10, badge_style)
        run = build_run(rating_items)
        score_text = score_text or "run"     # the part has to exist to hold it

    # The score takes the same weight as the genre and the year rather than a
    # score-banded colour.  Here the three are one line of metadata, and one
    # member of it changing hue per title breaks the row instead of ranking it.
    fields = {"genre": genre_label or None, "year": str(release_year) if release_year else None,
              "score": score_text}
    keys = ["genre", "year", "score"]
    if order:
        rank = order.replace("rating", "score").split(",")
        keys.sort(key=rank.index)
    # Each entry kept by field, told apart by identity: a year and a score
    # can read the same.
    part_of = {k: (fields[k], _MUTED) for k in keys if fields[k]}
    parts: list[tuple[str, tuple[int, int, int, int]]] = list(part_of.values())
    if not parts:
        return None
    score_part = part_of.get("score")

    sep = "  •  "
    star_sep = "  ★ "

    def segments(items) -> list[tuple[str, tuple[int, int, int, int]]]:
        # The row as drawn, separators included, left to right.
        out = []
        for i, part in enumerate(items):
            text, fill = part
            if star and part is score_part and run is None:
                out.append(("★ " if i == 0 else star_sep, _MUTED))
            elif i:
                out.append((sep, _SEPARATOR))
            out.append((run if run is not None and part is score_part else visual(text), fill))
        return out

    def width_of(text) -> float:
        if isinstance(text, list):
            return rating_badges.run_width(text, measure)
        return draw.textlength(text, font=font)

    def total(items) -> float:
        return sum(width_of(t) for t, _ in segments(items))

    # Everything on the logo's side of the info strip belongs to the logo; if
    # the two would meet, shed the genre first, then the year, before shrinking
    # any type.  Centred, the strip has the row to itself.
    if bounds is not None:
        # Centred, it may slide off the canvas centre into the middle of the
        # room it has (beside a side logo), so all of that room counts.
        lo, hi = bounds
        limit = hi - lo
    elif align == "left":
        right = (width * (1 - _RIGHT_PAD - _LOGO_MAX_W)) if logo_left is None else logo_left
        limit = right - width * _SIDE_PAD - width * 0.03
    elif align == "center":
        limit = width * (1 - _SIDE_PAD - _RIGHT_PAD)
    else:
        left = width * (_SIDE_PAD + _LOGO_MAX_W) if logo_right is None else logo_right
        limit = width * (1 - _RIGHT_PAD) - left - width * 0.03
    while len(parts) > 1 and total(parts) > limit:
        # The genre goes first, then the year, wherever they stand.
        shed = next(part_of[k] for k in ("genre", "year", "score")
                    if k in part_of and any(p is part_of[k] for p in parts))
        parts = [p for p in parts if p is not shed]
    while run is not None and len(rating_items) > 1 and total(parts) > limit:
        rating_items = rating_items[:-1]
        run = build_run(rating_items)

    # Drawn on a layer of its own so the strip's ink can cast one shadow —
    # the same pool the logo gets, for the same reason: the band is the
    # strip's only backing, and on light art it can be thin where the text
    # sits.  Compositing the layer afterwards keeps the text itself crisp.
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    if align == "left":
        x = int(width * _SIDE_PAD) + total(parts)
    elif align == "center":
        x = (width + total(parts)) / 2
        if bounds is not None and (x > bounds[1] or x - total(parts) < bounds[0]):
            x = (bounds[0] + bounds[1] + total(parts)) / 2
    else:
        x = width - int(width * _RIGHT_PAD)
    x_right = x
    ascent = font.getmetrics()[0]
    if top is not None:
        baseline = top + ascent
    elif baseline is None:
        baseline = int(height * _BASELINE)
    for text, fill in reversed(segments(parts)):
        x -= width_of(text)
        if isinstance(text, list):
            # draw_run takes the text's top, as ImageDraw.text does by default.
            rating_badges.draw_run(layer, ldraw, text, x, baseline - ascent, font, fill, measure)
        else:
            ldraw.text((x, baseline), text, font=font, fill=fill, anchor="ls")
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
    return int(x), baseline - ascent, int(x_right), baseline


# Graphic badge rows (badge_display_mode 7).  A group's size is in the portrait
# poster's units, where 22 sits beside the side chip; here it is scaled to sit
# beside the glass pill, whose text is _BADGE_FONT of the height.
_GB_UNIT         = 0.062   # row height at the default size, of the height
_GB_SEARCH       = 0.35    # how far a corner row may move from its corner, of the height


def _draw_graphic_badges(image: Image.Image, before: np.ndarray, cfg, tokens: list[str],
                         certification: str | None, age_rating: int | None,
                         logos: tuple, logo_box: tuple[int, int, int, int] | None,
                         badge_position: str | None, quality_look: str | None = None) -> None:
    """The graphic badge groups, laid out the portrait way (main._draw_graphic_badges)
    against the landscape furniture: top rows share the glass pill's centre
    line, "chip" takes the top corner the pill leaves free, and every row moves
    away from its corner until it clears what is already drawn."""
    import graphic_badges
    from graphic_badges import DEFAULT_SIZE
    from main import _draw_custom_group, _draw_logo_group, _occupied_cols, _score_points

    width, height = image.size
    left_margin, right_margin = int(width * _SIDE_PAD), int(width * _RIGHT_PAD)
    clear = int(width * 0.02)
    show_quality = bool(tokens) and _score_points(tokens) >= cfg.badge_min_score
    scale = max(0.1, float(getattr(cfg, "landscape_badge_scale", 1.0) or 1.0))
    pill_h = int(height * _BADGE_FONT * scale) + 2 * round(_BADGE_PAD_Y * scale)
    top_line = int(height * _BADGE_TOP) + pill_h / 2

    for group in graphic_badges.cfg_groups(cfg):
        unit = max(8, round(height * _GB_UNIT * group.size / DEFAULT_SIZE))
        # Spacing is a fraction of a portrait width; keyed to height here, as
        # every size on this canvas is.  At the default it is the portrait gap.
        gap = int(height * group.spacing * 0.9)
        items = graphic_badges.row_items(tokens, certification, age_rating, unit,
                                         group.slots, show_quality, *logos,
                                         quality_look=quality_look)[:group.max_items]
        if not items:
            continue
        if group.xy is not None:
            _draw_custom_group(image, items, group.xy, group.align, gap)
            continue
        now = np.asarray(image)
        if group.anchor in graphic_badges.LOGO_ANCHORS:
            _draw_logo_group(image, now, before, items, group.anchor, logo_box,
                             left_margin, clear, gap, unit)
            continue
        if group.anchor == "chip":
            top, right = True, badge_position != "top_right"
        else:
            top, right = group.anchor[0] == "t", group.anchor[1] == "r"
        margin = right_margin if right else left_margin
        half = max(im.height for _, im in items) / 2 + clear / 2
        start = top_line if top else height - (height - int(height * _BASELINE)) - unit / 2
        step, offset = max(2, unit // 3), 0.0
        while offset <= height * _GB_SEARCH:
            cy = start + offset if top else start - offset
            cols = _occupied_cols(now, before, max(0, int(cy - half)), min(height, int(cy + half) + 1))
            fitted = graphic_badges.fit(items, graphic_badges.free_run(cols, right, margin) - clear, gap)
            if fitted:
                row_w = graphic_badges.row_width(fitted, gap)
                graphic_badges.draw_row(image, fitted, center_y=cy, gap=gap,
                                        left_x=width - margin - row_w if right else margin)
                break
            offset += step


def build_landscape(image: Image.Image, score: int | str, genre: str, cfg, *args, **kwargs) -> Image.Image:
    """Render the landscape poster, its labels in cfg's label font
    (fonts.label_font_scope).  See _build_landscape."""
    with fonts.label_font_scope(cfg.label_font, cfg.logo_language):
        return _build_landscape(image, score, genre, cfg, *args, **kwargs)


def _build_landscape(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta=None,
    release_year: str | None = None,
    quality_tokens: list[str] | None = None,
    age_rating: int | None = None,
    certification: str | None = None,
    badge_logos: tuple = (None, None),
    cinema_run=None,
    ratings: dict | None = None,
    **_ignored,
) -> Image.Image:
    """Render the landscape poster.  Mirrors ``build_poster``'s call shape so the
    request pipeline can swap one for the other; extra kwargs it does not use
    are accepted and dropped.  Quality shows only as graphic badges."""
    from main import pick_sash, _greyscale_wanted, _score_points

    image = image.convert("RGBA")
    # Greyscale for "not available", as the portrait does it: before anything
    # samples the art, so the band and the pill take their colour from the
    # greyed art too and nothing on the poster says "in colour".
    # The bands go black, as a greyscaled portrait's do: grey art has no
    # colour to give them, and the colourless fallback is a deep blue.  The
    # bottom one is the plain band; a top band, which is only ever tinted, is
    # tinted black.
    greyed = _greyscale_wanted(cfg, discovery_meta, quality_tokens,
                               getattr(cfg, "landscape_greyscale", False))
    if greyed:
        image = image.convert("L").convert("RGBA")
        cfg = dataclasses.replace(cfg, vignette_poster_color_bottom=False)
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
    band_tint = _draw_vignette(image, art, cfg, source=(0.0, 0.0, 0.0) if greyed else shared)
    badge_source = band_tint if link == "badge_follows_vignette" else shared
    # What the overlays are measured against, for the graphic badges to lay
    # themselves out around them.
    graphic = bool(getattr(cfg, "landscape_graphic_badges", False))
    before = np.asarray(image).copy() if graphic else None

    # What belongs in the logo slot was decided upstream, where the art actually
    # got picked: a logo, or a title to stand in for one, or neither when the
    # chosen art already carries its own title treatment.  Re-deriving that from
    # cfg.landscape_art is what this used to do, and it was wrong in exactly the
    # cases that matter — `original` falling back to the neutral backdrop or to
    # the genre canvas passes a title precisely because that art has none, and
    # suppressing it produced a completely untitled render.
    #
    # Two slots, each a row (top / bottom) and a column (left / centre /
    # right): the logo's (landscape_logo_pos) and the info line's
    # (landscape_info_pos).  "auto" puts the line where it has always gone:
    # opposite a bottom-row logo, under a centred one, and on a top logo's side
    # of the bottom row.  Sharing a slot, the two stack — the logo standing on
    # the line at the bottom, the line hanging under the logo at the top.
    width, height = image.size
    pos = getattr(cfg, "landscape_logo_pos", "left")
    logo_row, align = ("top", pos[4:]) if pos.startswith("top_") else ("bottom", pos)
    if align not in ("left", "right", "center"):
        align = "left"
    info_pos = getattr(cfg, "landscape_info_pos", "auto")
    auto_info = info_pos not in _INFO_POSITIONS
    if auto_info:
        info_row = "bottom"
        info_col = align if logo_row == "top" else {"left": "right", "right": "left"}.get(align, "center")
    else:
        info_row, info_col = info_pos.split("_")
    stacked = (info_row, info_col) == (logo_row, align)
    top_row = logo_row == "top"
    edge_gap = width * 0.03
    full = (width * _SIDE_PAD, width * (1 - _RIGHT_PAD))

    # Hiding the rating is passed as "there is no score": the strip already
    # drops a missing one along with its separator, which is exactly the result
    # wanted here, and the same switch reads the same way in either shape.
    def _strip(**where):
        return _draw_info_strip(
            image,
            "" if cfg.hide_genre else (translate_genre(genre, cfg.logo_language) or genre),
            None if cfg.hide_year else release_year,
            None if cfg.hide_rating else score,
            scale=getattr(cfg, "landscape_info_scale", 1.0),
            out_of_10=getattr(cfg, "landscape_score_out_of_10", False),
            star=getattr(cfg, "landscape_score_star", False),
            align=info_col, order=getattr(cfg, "meta_order", ""),
            rating_items=rating_items, badge_scale=getattr(cfg, "rating_badge_scale", "native"),
            badge_style=getattr(cfg, "rating_badge_style", "color"), **where)

    # The sites' own scores in place of the weighted one, as portrait's
    # rating badges (rating_badges, rating_badge_max).
    rating_items = None
    if getattr(cfg, "landscape_rating_badges", False) and cfg.rating_badges and ratings and not cfg.hide_rating:
        import rating_badges
        rating_items = rating_badges.entries(ratings, cfg.rating_badges, score)
        if getattr(cfg, "rating_badge_max", 0):
            rating_items = rating_items[:cfg.rating_badge_max]
        rating_items = rating_items or None

    info_box = None
    logo_baseline = None
    logo_scale = max(0.5, min(1.5, float(getattr(cfg, "landscape_logo_scale", 1.0) or 1.0)))
    logo_top = int(height * _BADGE_TOP) if top_row else None
    if stacked and not top_row:
        info_box = _strip(bounds=full)
        if info_box:
            logo_baseline = info_box[1] - int(height * _STACK_GAP)
    logo_height, logo_left, logo_right = 0, None, None
    if logo is not None:
        logo_height, logo_left, logo_right = _draw_logo(image, logo, align, logo_baseline, logo_top,
                                                        logo_scale)
    elif fallback_title:
        # Height comes back for the same reason it does from the logo: a
        # badge stacked above needs something to clear.
        logo_height, logo_left, logo_right = _draw_title(image, fallback_title, align,
                                                         logo_baseline, logo_top, logo_scale)
    logo_box = None
    if logo_height and top_row:
        logo_box = (logo_left, logo_top, logo_right, logo_top + logo_height)
    elif logo_height:
        _base = int(height * _BASELINE) if logo_baseline is None else logo_baseline
        logo_box = (logo_left, _base - logo_height, logo_right, _base)

    if stacked and top_row:
        info_box = _strip(bounds=full, top=(logo_box[3] if logo_box else logo_top)
                          + (int(height * _STACK_GAP) if logo_box else 0))
    elif not stacked:
        where = {"top": int(height * _BADGE_TOP)} if info_row == "top" else {}
        if info_row != logo_row:
            info_box = _strip(bounds=full, **where)
        else:
            # The logo's side of the row is the logo's.  With nothing drawn
            # there (original art carries its own title) the row is all the
            # line's: keeping clear of a logo that isn't there only cost it
            # the genre.
            lo, hi = full
            if logo_box:
                if info_col == "left" or (info_col == "center" and align == "right"):
                    hi = min(hi, logo_box[0] - edge_gap) if align != "left" else hi
                if info_col == "right" or (info_col == "center" and align == "left"):
                    lo = max(lo, logo_box[2] + edge_gap) if align != "right" else lo
            info_box = _strip(bounds=(lo, hi), **where)

    badge_position = None
    if cfg.sash_mode != "hidden" and discovery_meta is not None:
        sash_result = pick_sash(discovery_meta, cfg.sash_priority)
        if sash_result is not None:
            label, _sash_type = sash_result
            label = upper_label(translate_sash(label, cfg.logo_language), cfg.logo_language)
            # A win wears portrait's Winner Star (sash_winner_star) as a ★.
            if getattr(cfg, "landscape_winner_star", False) and _sash_type == "win":
                label = f"★ {label}"
            position = getattr(cfg, "landscape_badge_pos", "top_left")
            badge_position = position
            _draw_badge(image, label, position, art, cfg, logo_height=logo_height,
                        # Only a stacked badge with an empty logo slot lands
                        # inside the band.  Stacked over a logo or a title it
                        # often clears the band's top edge, and the top corners
                        # are bare art, so those keep the glass that makes them
                        # readable.  Keyed on what was drawn, not on the mode
                        # that was asked for, for the same reason as above.
                        plain=(logo_height == 0 and position == "logo" and not top_row
                               and not (info_row == "bottom" and info_col == align)),
                        source=badge_source,
                        logo_align=align, logo_baseline=logo_baseline,
                        logo_box=logo_box, logo_top_row=top_row,
                        obstacles=(logo_box, info_box))

    # Drawn last because they lay themselves out around everything else.
    if graphic:
        import graphic_badges
        tint = None
        if ((cinema_run is not None and graphic_badges.wants_frost(cfg.badge_cinema_style))
                or (graphic_badges.wants_frost(cfg.badge_quality_style) and quality_tokens
                    and graphic_badges.groups_use_quality(cfg)
                    and _score_points(quality_tokens) >= cfg.badge_min_score)):
            from awards import dominant_frost_rgb, _frosted_tint
            tint = _frosted_tint(*(badge_source or dominant_frost_rgb(art)),
                                 saturation=cfg.sash_badge_frost_saturation, reference=cfg.frost_reference)
        badge_logos = (*badge_logos[:2], graphic_badges.cinema_ink(cfg.badge_cinema_style, cinema_run, tint,
                                                                     cfg.sash_badge_frost_opacity))
        _draw_graphic_badges(image, before, cfg, quality_tokens or [], certification, age_rating,
                             badge_logos, logo_box, badge_position,
                             graphic_badges.quality_look(cfg.badge_quality_style, tint,
                                                         cfg.sash_badge_frost_opacity))

    return image
