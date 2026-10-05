# trending_rank.py
#
# The trending rank drawn as its own mark rather than as a sash label, chosen
# with trending_style:
#
#   number — a large silver-to-white numeral in the top corner, the way Apple TV
#            numbers its Top Shows row
#   ribbon — a dark bookmark ribbon hanging from the top edge, the rank on it
#
# Either one frees the sash for the next label in the user's priority list, so
# a title can read "#3" and "New Season" at once.  Both are laid out as ratios
# of the canvas width, so every poster width draws the same mark.
import math
import os
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import fonts
from i18n import visual

_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf")

STYLES = ("sash", "number", "ribbon")
# What the ribbon's label says for each kind of title, as sashLabels keys.
KIND_LABELS = {"movie": "Film", "series": "Series", "anime": "Anime"}
# The ribbon's own charcoal, then the notch's styles.
RIBBON_STYLES = ("charcoal", "frosted", "black", "silver", "gold")
# The notch's silver and gold trim.
_TRIM = {"silver": (192, 192, 200), "gold": (212, 175, 55)}

# Drawn at SS× on a layer just big enough for the mark, then box-reduced:
# anti-aliased edges for the ribbon's point and the numeral's gradient.
_SS = 4

# Number: the digits' ink height, and its inset from the top and side, as
# fractions of the poster width.
_NUM_H      = 0.19
_NUM_INSET  = 0.065
# Silver at the foot of the numeral, white at its head.
_NUM_TOP    = (255, 255, 255)
_NUM_BOTTOM = (168, 172, 180)

# Ribbon: its width for one or two digits, its inset from the side (clear of
# the rounded corner most apps clip posters with), and the body's height and
# the depth of the notch cut into its foot, as fractions of its width.
_RIB_W      = 0.13
_RIB_INSET  = 0.06
_RIB_BODY   = 1.55
_RIB_NOTCH  = 0.26
_RIB_DIGIT  = 0.46   # digit ink height, of the ribbon width
# With a label under the rank: the extra body it takes, and its capitals'
# ink height, both of the ribbon width.
_RIB_LABEL_BAND = 0.36
_RIB_LABEL_CAP  = 0.13
# The label's baseline sits this far above the notch's point, of the ribbon
# width: room to breathe above the point, the rank close above it.
_RIB_LABEL_GAP  = 0.28


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)


def _digit_font(ink_h: float) -> tuple[ImageFont.FreeTypeFont, int]:
    """Inter at the size whose digits are *ink_h* tall, and that size."""
    probe = _font(200)
    t = probe.getbbox("0123456789", anchor="ls")
    size = max(8, round(200 * ink_h / (t[3] - t[1])))
    return _font(size), size


def _shadow(layer: Image.Image, blur: float, alpha: int) -> Image.Image:
    """A black copy of *layer*'s alpha, blurred, for a soft drop shadow."""
    a = layer.getchannel("A").point(lambda v: v * alpha // 255)
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(a)
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def number_box(width: int, scale: float = 1.0) -> tuple[int, int]:
    """(inset, bottom) of the numeral's band, in pixels of a *width*-wide
    poster: where to look for room beside it."""
    return round(_NUM_INSET * width), round((_NUM_INSET + _NUM_H * scale) * width)


def _number_layout(w: int, text: str, max_w: float | None, scale: float) -> tuple:
    """(font, track, ink_w, ink_h, pad, glyphs, adv) of the numeral at SS:
    each digit's ink box and advance too.  Shared by the drawing and its
    footprint."""
    ink_h = _NUM_H * scale * w
    if max_w is not None:
        font, _ = _digit_font(ink_h)
        natural = font.getlength(text) * 0.97
        if natural > max_w:
            ink_h *= max(0.6, max_w / natural)
    font, _ = _digit_font(ink_h * _SS)
    # Tighten the digits a touch: display numerals at this size read loose.
    track = -round(font.size * 0.03)
    glyphs = [font.getbbox(ch, anchor="ls") for ch in text]
    adv = [font.getlength(ch) for ch in text]
    x0 = min(0, glyphs[0][0])
    ink_w = sum(adv[:-1]) + track * (len(text) - 1) + glyphs[-1][2] - x0
    ink_h = -min(b[1] for b in glyphs)
    pad = round(0.04 * w * _SS)          # room for the shadow's blur
    return font, track, ink_w, ink_h, pad, glyphs, adv


def _number_origin(w: int, iw: float, right: bool, top: int | None,
                   center_x: float | None = None, align: str = "center") -> tuple[float, int]:
    """(x, y) of the numeral's ink box: in a top corner, or with *top* given,
    its ink starting at *top*, centred on *center_x* (the poster's middle by
    default) and kept inside the corner insets.  *align* "left" or "right"
    puts that edge of the ink at *center_x* instead, flush with a chip
    nearer the edge than the insets."""
    inset = round(_NUM_INSET * w)
    if top is not None:
        cx = w / 2 if center_x is None else center_x
        if align in ("left", "right"):
            x = cx if align == "left" else cx - iw
            return min(max(x, 0), w - iw), top
        return min(max(cx - iw / 2, inset), w - inset - iw), top
    return (w - inset - iw if right else inset), inset


def draw_rank_number(image: Image.Image, rank: int, right: bool = False,
                     max_w: float | None = None, scale: float = 1.0,
                     top: int | None = None, center_x: float | None = None,
                     align: str = "center") -> Image.Image:
    """The rank as a large silver numeral in the top-left (or top-right) corner.

    *scale* sizes it against its default.  *max_w* caps its width in pixels,
    for when a notch sits beside it: the digits shrink to fit, down to 60 % of
    their size and no further.  *top* hangs it there instead, centred on
    *center_x* (under the notch, wherever that is), or with *align* "left" /
    "right" with that edge there.
    """
    w = image.width
    text = str(rank)
    font, track, ink_w, ink_h, pad, glyphs, adv = _number_layout(w, text, max_w, scale)
    x0 = min(0, glyphs[0][0])

    lw, lh = round(ink_w) + 2 * pad, round(ink_h) + 2 * pad
    mask = Image.new("L", (lw, lh), 0)
    md = ImageDraw.Draw(mask)
    x = pad - x0
    for i, ch in enumerate(text):
        md.text((x, pad + ink_h), ch, font=font, fill=255, anchor="ls")
        x += adv[i] + track

    # Vertical silver-to-white gradient over the ink, brightest at the head.
    grad = Image.new("RGBA", (1, lh))
    for y in range(lh):
        t = min(1.0, max(0.0, (y - pad) / max(1, ink_h)))
        t = t ** 1.4
        grad.putpixel((0, y), tuple(round(_NUM_TOP[i] * (1 - t) + _NUM_BOTTOM[i] * t) for i in range(3)) + (255,))
    numeral = grad.resize((lw, lh))
    numeral.putalpha(mask)
    numeral = numeral.reduce(_SS)

    shadow = _shadow(numeral, 0.012 * w, 150)
    pad1 = pad / _SS
    ix, iy = _number_origin(w, numeral.width - 2 * pad1, right, top, center_x, align)
    nx = round(ix - pad1)
    ny = round(iy - pad1)
    off = max(1, round(0.004 * w))

    result = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
    _paste(result, shadow, nx, ny + off)
    _paste(result, numeral, nx, ny)
    return result.convert(image.mode) if image.mode != "RGBA" else result


def _spaced(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
            font: ImageFont.FreeTypeFont, track: float, fill) -> None:
    """*text* from its left baseline at *xy*, *track* px between letters."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch) + track


def _spaced_width(text: str, font: ImageFont.FreeTypeFont, track: float) -> float:
    return sum(font.getlength(ch) for ch in text) + track * max(0, len(text) - 1)


def _ribbon_body(style: str, size: tuple[int, int], yb: float, region: Image.Image | None,
                 tint: tuple[int, int, int] | None, frost_opacity: float) -> Image.Image:
    """The ribbon's fill at SS, before its shape is cut: the notch's surface
    for each of its styles, plus the ribbon's own charcoal."""
    lw, lh = size
    if style == "frosted" and region is not None and tint is not None:
        # The poster under the ribbon, blurred, with the frost colour laid over
        # it at the notch's opacity.
        body = region.resize(size, Image.Resampling.BILINEAR).convert("RGBA")
        frost = Image.new("RGBA", size, (*tint, round(255 * frost_opacity)))
        return Image.alpha_composite(body, frost)
    if style == "black":
        return Image.new("RGBA", size, (10, 10, 12, 230))
    grad = Image.new("RGBA", (1, lh))
    for y in range(lh):
        t = min(1.0, y / max(1.0, yb))
        if style in ("silver", "gold"):
            # The notch's dark body: near-black, lifting a little mid-way.
            c = round(4 + 10 * math.sin(t * math.pi))
            grad.putpixel((0, y), (c, c, min(255, round(c * 1.3)), 235))
        else:
            # Charcoal, a shade lighter at the top.
            c = round(46 * (1 - t) + 20 * t)
            grad.putpixel((0, y), (c, c, c + 2, 235))
    return grad.resize(size)


def _ribbon_geometry(w: int, rank: int, right: bool, label: bool, scale: float,
                     corner: bool, top_inset: int) -> tuple:
    """(widen, rib_w, body_h, pad, grow, lift, rx): the ribbon's size and
    where its layer goes, shared by the drawing and its footprint."""
    widen = 1 + 0.28 * max(0, len(str(rank)) - 2)
    rib_w = _RIB_W * scale * w * widen
    body_h = rib_w * (_RIB_BODY + (_RIB_LABEL_BAND if label else 0))
    pad = round(0.03 * w)                # room for the shadow's blur
    grow, lift = max(0, top_inset), min(0, top_inset)
    inset = 0 if corner else _RIB_INSET * w
    rx = round(w - inset - rib_w - pad) if right else round(inset - pad)
    return widen, rib_w, body_h, pad, grow, lift, rx


# How far past a mark its drop shadow still reads, of the poster width: the
# part of the shadow a badge is kept clear of, like the mark itself.
_SHADE = 0.016


def ribbon_footprint(w: int, rank: int, right: bool = False, label: bool = False,
                     scale: float = 1.0, corner: bool = False,
                     top_inset: int = 0) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """(body, extent) of the ribbon draw_rank_ribbon draws with these
    arguments, as (x0, y0, x1, y1) pixel boxes: the ribbon down to the tips
    of its notch, with as much of its shadow as reads, and everything the
    drawing touches, the shadow's faint tail included."""
    _, rib_w, body_h, pad, grow, lift, rx = _ribbon_geometry(w, rank, right, label, scale,
                                                             corner, top_inset)
    lw, lh = round(rib_w + 2 * pad), round(grow + body_h + pad)
    off = max(1, round(0.004 * w))
    shade = _SHADE * w
    body = (round(rx + pad - shade), 0, round(rx + pad + rib_w + shade),
            max(0, round(lift + grow + body_h + shade)))
    return body, (rx, 0, rx + lw + off, max(0, lift + lh + off))


def number_footprint(w: int, rank: int, right: bool = False, max_w: float | None = None,
                     scale: float = 1.0, top: int | None = None,
                     center_x: float | None = None,
                     align: str = "center") -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """(body, extent) of the numeral draw_rank_number draws with these
    arguments, as ribbon_footprint gives them for the ribbon."""
    _font, _track, ink_w, ink_h, pad, _glyphs, _adv = _number_layout(w, str(rank), max_w, scale)
    iw, ih = round(ink_w / _SS), round(ink_h / _SS)
    x0, y0 = _number_origin(w, iw, right, top, center_x, align)
    x0 = round(x0)
    shade = round(_SHADE * w)
    body = (x0 - shade, y0 - shade, x0 + iw + shade, y0 + ih + shade)
    grow = round(pad / _SS) + max(1, round(0.004 * w))
    return body, (max(0, x0 - grow), max(0, y0 - grow), min(w, x0 + iw + grow), y0 + ih + grow)


def draw_rank_ribbon(image: Image.Image, rank: int, right: bool = False,
                     label: str | None = None, scale: float = 1.0,
                     corner: bool = False, style: str = "charcoal",
                     tint_rgb: tuple[float, float, float] | None = None,
                     frost_opacity: float = 0.75, frost_saturation: float = 1.2,
                     frost_reference: bool | str = False,
                     text_color: tuple[int, int, int] | None = None,
                     top_inset: int = 0) -> Image.Image:
    """The rank on a ribbon hanging from the top edge, its foot cut into a
    notch: just in from the top-left (or top-right) corner, or with *corner*,
    nested right into it.

    *label* ("FILM", "SERIES", ...) goes in small letter-spaced capitals under
    the rank, the ribbon growing to hold it.  *scale* sizes the whole ribbon
    against its default.  *style* is one of RIBBON_STYLES: its own charcoal,
    or any of the notch's, drawn as the notch draws it — frosted from the same
    colour sample (*tint_rgb*) and settings, silver and gold with their trim.
    *text_color* overrides the label colour except on frosted, which picks
    dark or light ink for its panel, as the notch does.
    *top_inset* is the primary client's top-edge inset in pixels, the one
    the notch takes: a client that crops the poster's top edge would cut into
    the ribbon, so it grows upwards by that much, and still meets the edge
    where nothing is cropped.  Negative raises it off the top instead.
    """
    if style not in RIBBON_STYLES:
        style = "charcoal"
    w = image.width
    text = str(rank)
    widen, rib_w, body_h, pad, grow, lift, rx = _ribbon_geometry(
        w, rank, right, bool(label), scale, corner, top_inset)
    notch = rib_w * _RIB_NOTCH

    S = _SS
    lw, lh = round(rib_w + 2 * pad), round(grow + body_h + pad)
    layer = Image.new("RGBA", (lw * S, lh * S), (0, 0, 0, 0))
    # Top edge flush with the poster's (layer row 0), so it hangs from it;
    # the design proper starts *top* below it, under the client's crop.
    x0, x1 = pad * S, (pad + rib_w) * S
    top = grow * S
    yb, yn = top + body_h * S, top + (body_h - notch) * S
    outline = [(x0, 0), (x1, 0), (x1, yb), ((x0 + x1) / 2, yn), (x0, yb)]

    tint = region = None
    if style == "frosted":
        from awards import _frosted_tint, dominant_frost_rgb
        src = image.convert("RGBA")
        region = src.crop((rx, lift, rx + lw, lift + lh)).filter(
            ImageFilter.GaussianBlur(max(2.0, 0.12 * rib_w)))
        tint = _frosted_tint(*(tint_rgb if tint_rgb is not None else dominant_frost_rgb(src)),
                             saturation=frost_saturation, reference=frost_reference)

    mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)
    body = _ribbon_body(style, layer.size, yb, region, tint, frost_opacity)
    body_a = 255 if style == "frosted" else (230 if style == "black" else 235)
    body.putalpha(Image.eval(mask, lambda v: v * body_a // 255))
    layer.alpha_composite(body)

    # An edge down the sides and round the notch; none on top, where it meets
    # the poster edge, nor down a side nested against the poster's.  Charcoal
    # has a faint hairline, silver and gold the notch's trim; frosted and pure
    # black have none, like their notches.
    edge = {"charcoal": ((255, 255, 255, 46), 0.0035),
            "silver":   ((*_TRIM["silver"], 215), 0.007),
            "gold":     ((*_TRIM["gold"], 215), 0.007)}.get(style)
    if edge is not None:
        rim = outline[1:] + outline[:1]
        if corner:
            rim = outline[2:] + outline[:1] if right else outline[1:]
        # Twice the width: it is centred on the outline, and only the inner
        # half survives the cut below.
        ImageDraw.Draw(layer).line(rim, fill=edge[0], width=max(S, round(2 * edge[1] * rib_w / _RIB_W * S)),
                                   joint="curve")
        layer.putalpha(ImageChops.darker(layer.getchannel("A"), mask))

    if style == "frosted":
        from awards import _frost_ink
        ink_rgb, num_a, label_a = _frost_ink(*tint), 245, 225
    elif style == "black":
        ink_rgb, num_a, label_a = text_color or (210, 210, 218), 245, 215
    else:
        ink_rgb, num_a, label_a = text_color or (255, 255, 255), 255, 200

    draw = ImageDraw.Draw(layer)
    cx_mid = (x0 + x1) / 2
    num_bottom = yn
    if label:
        # Capitals whose ink is _RIB_LABEL_CAP of the width, narrowed to fit
        # a long word ("PELÍCULA", "MFULULIZO") inside the ribbon's sides.
        # In the label font, like every other label; the numeral stays Inter.
        label = visual(label)
        cap_h = _RIB_LABEL_CAP * rib_w * S / widen ** 0.5
        probe = fonts.label_font(200)
        hb = probe.getbbox("H", anchor="ls")
        lfont = fonts.label_font(max(6, round(200 * cap_h / (hb[3] - hb[1]))))
        track = lfont.size * 0.08
        fit = 0.80 * rib_w * S
        span = _spaced_width(label, lfont, track)
        if span > fit:
            lfont = fonts.label_font(max(6, round(lfont.size * fit / span)))
            track = lfont.size * 0.08
            span = _spaced_width(label, lfont, track)
        band = _RIB_LABEL_BAND * rib_w * S
        base = yn - _RIB_LABEL_GAP * rib_w * S
        _spaced(draw, (cx_mid - span / 2, base), label, lfont, track, (*ink_rgb, label_a))
        num_bottom = yn - band

    font, _ = _digit_font(_RIB_DIGIT * rib_w * S / widen ** 0.5)
    ink = font.getbbox(text, anchor="ls")
    fit = 0.74 * rib_w * S
    if ink[2] - ink[0] > fit:
        font = _font(max(6, round(font.size * fit / (ink[2] - ink[0]))))
        ink = font.getbbox(text, anchor="ls")
    cx = cx_mid - (ink[0] + ink[2]) / 2
    cy = (top + num_bottom) / 2 - (ink[1] + ink[3]) / 2 + (0.04 * rib_w * S if label else 0)
    draw.text((cx, cy), text, font=font, fill=(*ink_rgb, num_a), anchor="ls")

    ribbon = layer.reduce(S)
    shadow = _shadow(ribbon, 0.012 * w, 140)

    result = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
    _paste(result, shadow, rx + max(1, round(0.004 * w)), lift + max(1, round(0.004 * w)))
    _paste(result, ribbon, rx, lift)
    return result.convert(image.mode) if image.mode != "RGBA" else result


def _paste(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    """alpha_composite *src* at (x, y), clipped to *dst* (which it rejects when
    the offset is negative or the layer runs off the edge)."""
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if sx1 > sx0 and sy1 > sy0:
        dst.alpha_composite(src, (x + sx0, y + sy0), (sx0, sy0, sx1, sy1))
