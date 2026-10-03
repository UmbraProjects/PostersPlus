"""Graphic badges (badge_display_mode 7): quality marks and the US certificate,
in up to four groups, each a row at its own anchor (see Group; laid out by
main._draw_graphic_badges).

Nothing trademarked ships in the repo.  The marks are fetched once from
Wikimedia Commons, pinned by SHA-1 so an edit upstream can never change a
poster (a re-upload is looked past to the pinned revision, see fetch_pinned),
and kept in the cache volume:

  Dolby Vision 2021 logo   public domain (below the threshold of originality)
  Dolby Cinema 2021 logo   public domain; only its letters are used
  DTS X B&W                CC BY-SA 4.0, CinemaLover24680 — credited in README.md

Dolby publishes no "Dolby / ATMOS" or "Dolby / VISION • ATMOS" lockup on
Commons, so both are composed from the two 2021 lockups: the "DD Dolby" row
as drawn, and the lower line set from their own capitals (VISION and CINEMA
between them hold every letter but T, which is E's top arm on I's stem).

Everything else — resolution, HDR, the certificate — is text in a rounded box,
after Nuvio TV's certificate chip, so it needs no artwork at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from pxscale import px, pxr

logger = logging.getLogger(__name__)

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
ASSET_DIR = "/app/cache/commons"
# Wikimedia asks automated clients to identify themselves.
_USER_AGENT = "PostersPlus (https://github.com/UmbraProjects/PostersPlus)"


@dataclass(frozen=True)
class _CommonsFile:
    title: str   # file name on Commons, without "File:"
    sha1: str    # of the exact revision these marks were built against


_FILES = {
    "dolby_vision": _CommonsFile("Dolby Vision 2021 logo.svg", "9749fa31647c8b3c7a0d72c5ad9369536e5e83a8"),
    "dolby_cinema": _CommonsFile("Dolby Cinema 2021 logo.svg", "5590e86e314385886d105d12def956ea831d53b9"),
    "dts_x":        _CommonsFile("DTS X B&W.png",              "1b94e717779fd22128d1eec89f28bafdc962a0aa"),
}


def _asset_path(key: str) -> str:
    f = _FILES[key]
    return os.path.join(ASSET_DIR, f"{f.sha1}{os.path.splitext(f.title)[1]}")


def commons_url(title: str) -> str:
    """The latest revision of a Commons file.  It moves when the file is
    re-uploaded, so a pinned hash can stop matching; see fetch_pinned."""
    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + title.replace(" ", "_")


_COMMONS_API = "https://commons.wikimedia.org/w/api.php"


class PinnedGone(Exception):
    """Neither the URL nor the file's Commons history serves the pinned bytes."""


async def fetch_pinned(client, url: str, sha1: str, commons_title: str | None = None) -> bytes:
    """The file at ``url``, verified against ``sha1``.  When upstream now
    serves something else and the file is on Commons, the file's revision
    history is searched for the pinned hash: a superseded revision stays at an
    archive URL that never changes, so a re-upload doesn't lose the mark.

    Raises PinnedGone when no revision matches (retrying won't help) and any
    other exception for a failure that may pass (network, 429, 5xx)."""
    resp = await client.get(url, headers={"User-Agent": _USER_AGENT}, follow_redirects=True, timeout=15)
    resp.raise_for_status()
    if hashlib.sha1(resp.content).hexdigest() == sha1:
        return resp.content
    if commons_title is None:
        raise PinnedGone(url)
    api = await client.get(_COMMONS_API, headers={"User-Agent": _USER_AGENT}, timeout=15, params={
        "action": "query", "titles": "File:" + commons_title, "prop": "imageinfo",
        "iiprop": "sha1|url", "iilimit": "50", "format": "json", "formatversion": "2"})
    api.raise_for_status()
    pages = (api.json().get("query") or {}).get("pages") or [{}]
    for info in pages[0].get("imageinfo") or []:
        if info.get("sha1") == sha1 and info.get("url"):
            old = await client.get(info["url"], headers={"User-Agent": _USER_AGENT},
                                   follow_redirects=True, timeout=15)
            old.raise_for_status()
            if hashlib.sha1(old.content).hexdigest() == sha1:
                return old.content
    raise PinnedGone(url)


def assets_ready() -> bool:
    return all(os.path.exists(_asset_path(k)) or k in _gone for k in _FILES)


_fetch_lock = asyncio.Lock()
# A failed file is left alone this long, so a slow, throttling (Commons
# answers a burst with 429) or unreachable host isn't asked on every render.
_RETRY_AFTER = 600.0
_failed_at: dict[str, float] = {}
# Files no revision of which matches the pin: drawn without until restart.
_gone: set[str] = set()


async def ensure_assets(client) -> bool:
    """Download any missing Commons file.  Cheap once they are all on disk;
    a failed download leaves that mark out rather than failing the render,
    and is retried after _RETRY_AFTER.  True once nothing is left to fetch."""
    if assets_ready():
        return True
    async with _fetch_lock:
        os.makedirs(ASSET_DIR, exist_ok=True)
        fetched = False
        for key, f in _FILES.items():
            path = _asset_path(key)
            if (os.path.exists(path) or key in _gone
                    or time.monotonic() - _failed_at.get(key, -_RETRY_AFTER) < _RETRY_AFTER):
                continue
            try:
                body = await fetch_pinned(client, commons_url(f.title), f.sha1, f.title)
            except PinnedGone:
                # No revision on Commons is the reviewed one.  Keep drawing
                # without it rather than put an unreviewed file on every poster.
                _gone.add(key)
                logger.warning(f"Graphic badges: no revision of {f.title} matches its pinned SHA-1; skipped")
                continue
            except Exception as exc:
                _failed_at[key] = time.monotonic()
                logger.warning(f"Graphic badges: Commons fetch failed for {f.title}: {exc}")
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
            fetched = True
            logger.info(f"Graphic badges: cached {f.title}")
        if fetched:
            _marks.cache_clear()
    return assets_ready()


# ---------------------------------------------------------------------------
# Marks, as white-on-transparent alpha at a fixed working height
# ---------------------------------------------------------------------------

_WORK_H = 600   # raster height of the Dolby lockups everything is cut from


def _svg_alpha(path: str) -> np.ndarray:
    import cairosvg
    png = cairosvg.svg2png(url=path, output_height=_WORK_H)
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]


def _runs(on: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of each run of True."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], on.astype(np.int8), [0]))))
    return list(zip(edges[::2], edges[1::2]))


def _rows(a: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """A two-row lockup split into (top row, bottom row, gap between them)."""
    rows = _runs(a.max(axis=1) > 20)
    top, bottom = rows[0], rows[-1]
    return a[top[0]:top[1]], a[bottom[0]:bottom[1]], int(bottom[0] - top[1])


def _glyphs(row: np.ndarray) -> tuple[list[np.ndarray], int]:
    """A one-line row cut into letters, plus its median letter spacing."""
    cols = _runs(row.max(axis=0) > 20)
    gaps = [cols[i + 1][0] - cols[i][1] for i in range(len(cols) - 1)]
    return [row[:, s:e] for s, e in cols], int(np.median(gaps))


def _fit_height(g: np.ndarray, h: int) -> np.ndarray:
    if g.shape[0] == h:
        return g
    return np.asarray(Image.fromarray(g).resize((max(1, round(g.shape[1] * h / g.shape[0])), h),
                                                Image.Resampling.LANCZOS))


def _set_line(glyphs: list[np.ndarray | None], gap: int) -> np.ndarray:
    """Letters side by side at ``gap``; None is a word space either side of a
    bullet, a little tighter than a letter gap so the bullet binds the words."""
    h = max(g.shape[0] for g in glyphs if g is not None)
    # (counted by identity: list.count would compare None against arrays)
    width = (sum(g.shape[1] + gap for g in glyphs if g is not None)
             + int(gap * 0.6) * sum(g is None for g in glyphs))
    line = np.zeros((h, width), dtype=np.uint8)
    x = 0
    for g in glyphs:
        if g is None:
            x += int(gap * 0.6)
            continue
        line[:, x:x + g.shape[1]] = np.maximum(line[:, x:x + g.shape[1]], g)
        x += g.shape[1] + gap
    return line[:, :x - gap]


# The lower line of every Dolby lockup, reset from the source letters.  Dolby's
# own 2021 lockup sets it at 0.46 of the DD mark's height with 0.41 cap
# heights of letter spacing — a thin, airy line under a heavy "Dolby" that at
# badge size reads as a smudge, and leaves the wordmark carrying all the
# weight.  Larger and tighter balances the two lines.
_LOWER_CAP   = 0.55   # cap height, of the DD mark's height
_LOWER_TRACK = 0.28   # letter spacing, of the cap height


def _lockup(top: np.ndarray, glyphs: list[np.ndarray | None], cap: int, row_gap: int) -> np.ndarray:
    """"DD Dolby" over a line set from ``glyphs`` (all ``cap`` tall; None is
    a word space round a bullet), at _LOWER_CAP / _LOWER_TRACK, centred, and
    narrowed to the top row's width if it would overhang it."""
    line = _set_line(glyphs, max(1, round(cap * _LOWER_TRACK)))
    dd_h = int(_runs(top[:, :_runs(top.max(axis=0) > 20)[0][1]].max(axis=1) > 20)[0][1])
    scale = min(_LOWER_CAP * dd_h / cap, top.shape[1] / line.shape[1])
    line = np.asarray(Image.fromarray(line).resize(
        (max(1, round(line.shape[1] * scale)), max(1, round(line.shape[0] * scale))), Image.Resampling.LANCZOS))
    out = np.zeros((top.shape[0] + row_gap + line.shape[0], top.shape[1]), dtype=np.uint8)
    out[:top.shape[0]] = top
    x = (out.shape[1] - line.shape[1]) // 2
    out[top.shape[0] + row_gap:, x:x + line.shape[1]] = line
    return out


def _dts_letters(a: np.ndarray) -> np.ndarray:
    """The "dts" of the DTS:X mark, without its X.  At the row's full height
    the whole mark outweighs everything beside it, and shrinking it breaks the
    row's shared top and bottom line; the letters alone hold both.  They are
    one joined shape, and the X's arm reaches back under the "s", so they are
    cut apart as shapes rather than at a column."""
    import cv2
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a > 100).astype(np.uint8))
    first = min(range(1, n), key=lambda i: stats[i][0])
    x, y, w, h = stats[first][:4]
    # Grown a pixel so the anti-aliased rim below the threshold comes along.
    keep = cv2.dilate((labels == first).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return np.where(keep, a, 0)[y:y + h, x:x + w]


@lru_cache(maxsize=1)
def _marks() -> dict[str, np.ndarray]:
    """Every mark whose source file is on disk, keyed by name."""
    marks: dict[str, np.ndarray] = {}
    if os.path.exists(_asset_path("dolby_vision")):
        try:
            vision = _svg_alpha(_asset_path("dolby_vision"))
            top, bottom, row_gap = _rows(vision)
            (V, I, S, _, O, N), _ = _glyphs(bottom)
            cap = bottom.shape[0]
            marks["DV"] = _lockup(top, [V, I, S, I, O, N], cap, row_gap)
            if os.path.exists(_asset_path("dolby_cinema")):
                _, cinema_bottom, _ = _rows(_svg_alpha(_asset_path("dolby_cinema")))
                cin, _ = _glyphs(cinema_bottom)
                _C, _I, _N, E, M, A = (_fit_height(g, bottom.shape[0]) for g in cin)
                stroke = I.shape[1]
                T = np.zeros((bottom.shape[0], E.shape[1]), dtype=np.uint8)
                T[:, :] = np.where(np.arange(bottom.shape[0])[:, None] < stroke, 255, 0)
                cx = (T.shape[1] - stroke) // 2
                T[:, cx:cx + stroke] = np.maximum(T[:, cx:cx + stroke], I)
                d = int(stroke * 1.5)
                dot = Image.new("L", (d * 4, d * 4), 0)
                ImageDraw.Draw(dot).ellipse([0, 0, d * 4 - 1, d * 4 - 1], fill=255)
                bullet = np.zeros((bottom.shape[0], d), dtype=np.uint8)
                top_off = (bottom.shape[0] - d) // 2
                bullet[top_off:top_off + d] = np.asarray(dot.resize((d, d), Image.Resampling.LANCZOS))
                atmos = [A, T, M, O, S]
                marks["ATMOS"] = _lockup(top, atmos, cap, row_gap)
                marks["DV+ATMOS"] = _lockup(top, [V, I, S, I, O, N, None, bullet, None, *atmos], cap, row_gap)
        except Exception as exc:
            logger.error(f"Graphic badges: Dolby lockups failed: {exc}")
    if os.path.exists(_asset_path("dts_x")):
        try:
            # Black mark on an opaque white plate: its shape is the darkness.
            rgb = np.asarray(Image.open(_asset_path("dts_x")).convert("L"), dtype=np.float32)
            a = (255 - rgb).clip(0, 255).astype(np.uint8)
            marks["DTSX"] = _dts_letters(a)
        except Exception as exc:
            logger.error(f"Graphic badges: DTS:X mark failed: {exc}")
    return marks


@lru_cache(maxsize=128)
def _mark(name: str, h: int) -> Image.Image | None:
    a = _marks().get(name)
    if a is None:
        return None
    alpha = Image.fromarray(a).resize((max(1, round(a.shape[1] * h / a.shape[0])), h),
                                      Image.Resampling.LANCZOS)
    out = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
    out.putalpha(alpha)
    return out


# ---------------------------------------------------------------------------
# Text boxes
# ---------------------------------------------------------------------------

_INK = (238, 238, 238)
# Sized to carry the same weight as the Dolby lockups beside them.
_BOX_TEXT   = 0.62    # font size, of the box height
_BOX_PAD    = 0.72    # total horizontal padding, of the box height
_BOX_RADIUS = 0.24    # corner radius, of the box height


@lru_cache(maxsize=128)
def _box(text: str, h: int, filled: bool) -> Image.Image:
    """Nuvio TV's certificate chip: text in a thin rounded outline.  ``filled``
    knocks the text out of a solid light box instead, for resolution, so it
    reads as a different kind of fact from the outlined ones beside it."""
    ss = 4
    # 500-wide rounding (pxscale), snapped to whole pixels for the image.
    font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), px(h * _BOX_TEXT) * ss)
    w = round(px(font.getlength(text) / ss + h * _BOX_PAD))
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    bw = max(1, round(h * 0.07)) * ss
    # Edge to edge: PIL strokes an outline inwards, so no inset is needed, and
    # one would leave the box's ink a pixel short of the marks beside it.
    box = [0, 0, w * ss - 1, h * ss - 1]
    if filled:
        d.rounded_rectangle(box, radius=px(h * _BOX_RADIUS * ss), fill=(*_INK, 235))
        # Cut the label out of the plate so the poster shows through it.
        cut = Image.new("L", im.size, 0)
        ImageDraw.Draw(cut).text((w * ss / 2, h * ss / 2), text, font=font, fill=255, anchor="mm")
        im.putalpha(Image.fromarray(np.minimum(np.asarray(im.getchannel("A")),
                                               255 - np.asarray(cut))))
    else:
        d.rounded_rectangle(box, radius=px(h * _BOX_RADIUS * ss), outline=(*_INK, 235), width=bw)
        d.text((w * ss / 2, h * ss / 2), text, font=font, fill=(*_INK, 245), anchor="mm")
    return im.reduce(ss)


# ---------------------------------------------------------------------------
# Frosted quality chips (badge_quality_style=frosted)
# ---------------------------------------------------------------------------

# The quality badges, each on a chip of the frosted notch's glass: the poster
# under it blurred beneath the frost tint, the mark or label in the tint's
# ink.  The chip is the row's height, so a Dolby lockup inside it is smaller
# than the bare mark — set to carry the same weight as the box labels.
QUALITY_STYLES = ("solid", "frosted")
DEFAULT_QUALITY_STYLE = "solid"
_CHIP_MARK  = 0.68    # a mark's height inside its chip, of the chip height
_CHIP_PAD   = 0.50    # a mark's total horizontal padding, of the chip height
_CHIP_FROST = 0.78    # the tint layer's opacity when no other is given


def _frost_look(tint: tuple[float, float, float], opacity: float | None) -> str:
    """"rgb:r,g,b" for a frost tint, with "@alpha" (0-255) after it when the
    tint layer's opacity is set: the sash's, so the chips and disc read as
    the same glass as the frosted notch beside them."""
    look = "rgb:" + ",".join(str(int(round(c))) for c in tint[:3])
    if opacity is not None:
        look += f"@{round(255 * min(1.0, max(0.0, opacity)))}"
    return look


def _parse_look(look: str) -> tuple[tuple[int, int, int] | None, int]:
    """(tint, tint alpha) of a look: the tint None for "auto"."""
    alpha = round(255 * _CHIP_FROST)
    if "@" in look:
        look, a = look.split("@", 1)
        alpha = int(a)
    if look.startswith("rgb:"):
        return tuple(int(c) for c in look[4:].split(",")), alpha
    return None, alpha


def quality_style(value: str | None) -> str | None:
    """badge_quality_style as given; None if unknown."""
    v = (value or "").strip().lower()
    return v if v in QUALITY_STYLES else None


def quality_look(style: str, tint: tuple[float, float, float] | None = None,
                 opacity: float | None = None) -> str | None:
    """What row_items takes for the quality badges: None for the solid ones,
    else the frost tint ("rgb:r,g,b", then "@alpha" with an *opacity*), or
    "auto" while it isn't sampled (the layout pass: a chip is the same size
    either way)."""
    if style != "frosted":
        return None
    return _frost_look(tint, opacity) if tint is not None else "auto"


def _chip_ink(content: str, h: int) -> Image.Image | None:
    """The chip's contents as white ink on a clear sheet the chip's size:
    a mark ("mark:DV") or a label, centred.  None for a mark not on disk."""
    if content.startswith("mark:"):
        mark = _mark(content[5:], max(1, round(h * _CHIP_MARK)))
        if mark is None:
            return None
        w = round(px(pxr(mark.width) + h * _CHIP_PAD))
        im = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        im.alpha_composite(mark, ((w - mark.width) // 2, (h - mark.height) // 2))
        return im
    return _chip_label(content, h)


@lru_cache(maxsize=128)
def _chip_label(content: str, h: int) -> Image.Image:
    ss = 4
    font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), px(h * _BOX_TEXT) * ss)
    w = round(px(font.getlength(content) / ss + h * _BOX_PAD))
    im = Image.new("RGBA", (w * ss, h * ss), (255, 255, 255, 0))
    ImageDraw.Draw(im).text((w * ss / 2, h * ss / 2), content, font=font,
                            fill=(255, 255, 255, 255), anchor="mm")
    return im.reduce(ss)


@lru_cache(maxsize=64)
def _chip_mask(w: int, h: int) -> Image.Image:
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1],
                                        radius=px(h * _BOX_RADIUS * ss), fill=255)
    return m.reduce(ss)


def _frost_chip(content: str, h: int, look: str) -> Image.Image | None:
    """The placeholder the row is laid out with (a smoked chip); draw_row
    swaps in the glass once it knows what is underneath (_resolve_chip)."""
    ink = _chip_ink(content, h)
    if ink is None:
        return None
    im = Image.new("RGBA", ink.size, (20, 20, 24, 0))
    im.putalpha(_chip_mask(*ink.size).point(lambda a: a * 150 // 255))
    im.alpha_composite(ink)
    im.info["frost_chip"] = (look, ink)     # the ink, so it is cut only once
    return im


def _glass(image: Image.Image, x: int, y: int, w: int, h: int,
           tint: tuple[int, int, int] | None,
           alpha: int = round(255 * _CHIP_FROST)) -> tuple[Image.Image, tuple[int, int, int]] | None:
    """The frosted notch's glass for a (w, h) badge at (x, y): the art under
    it blurred beneath the tint at *alpha*, unmasked.  ``tint`` None
    samples one from that art.  Returns (glass, tint), or None off-canvas."""
    box = (max(0, x), max(0, y), min(image.width, x + w), min(image.height, y + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    under = image.crop(box).convert("RGB")
    if tint is None:
        from awards import _frosted_tint, dominant_frost_rgb
        tint = tuple(int(round(c)) for c in _frosted_tint(*dominant_frost_rgb(under)))
    glass = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glass.paste(under.filter(ImageFilter.GaussianBlur(max(2.0, h * 0.35))).convert("RGBA"),
                (box[0] - x, box[1] - y))
    frost = Image.new("RGBA", (w, h), (*tint, 0))
    frost.putalpha(Image.new("L", (w, h), alpha))
    return Image.alpha_composite(glass, frost), tint


def _resolve_chip(image: Image.Image, im: Image.Image, x: int, y: int) -> Image.Image:
    """The frosted chip for where it lands at (x, y) on ``image``."""
    from awards import _frost_ink
    look, ink = im.info["frost_chip"]
    w, h = im.size
    made = _glass(image, x, y, w, h, *_parse_look(look))
    if made is None:
        return im
    glass, tint = made
    glass.putalpha(_chip_mask(w, h))
    tinted = Image.new("RGBA", ink.size, (*_frost_ink(*tint), 0))
    tinted.putalpha(ink.getchannel("A"))
    glass.alpha_composite(tinted)
    return glass


# ---------------------------------------------------------------------------
# Cinema badge: the film is in cinemas (or not out yet) and not at home
# ---------------------------------------------------------------------------

# One disc the row's height: the day the film reaches home ("OCT" over "16")
# when that is dated; otherwise a popcorn bucket for a film in cinemas, or a
# clapperboard for one still in production.
#
# Its look (badge_cinema_style): "auto" takes its tone from what it lands on,
# as a dark-on-dark logo is lightened — white to silver over dark art, black
# to grey over light — and "frosted" is the frosted notch's glass, the poster
# under it blurred beneath the frost tint.  The old popcorn colours (timing,
# red, black, white) are read as "auto".
CINEMA_STYLES = ("auto", "frosted")
DEFAULT_CINEMA_STYLE = "auto"
_LEGACY_CINEMA_STYLES = ("timing", "red", "black", "white")

_DISC_LIGHT = ((252, 252, 252), (184, 187, 194), (26, 26, 30))    # top, bottom, face
_DISC_DARK = ((74, 74, 80), (12, 12, 14), (242, 242, 242))
# Mean luma under the disc below which it takes the light tone.
_DISC_LIGHT_BELOW = 128
_ICON = 0.56   # the popcorn or clapper, of the disc's size


# English whatever the poster's language: three capitals read at badge size
# in any script the row is drawn in.
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def cinema_style(value: str | None) -> str | None:
    """badge_cinema_style as given, legacy colours as "auto"; None if unknown."""
    v = (value or "").strip().lower()
    if v in _LEGACY_CINEMA_STYLES:
        return "auto"
    return v if v in CINEMA_STYLES else None


@dataclass(frozen=True)
class CinemaRun:
    """A film not yet out at home: "Cinema" or "Production", and the day of
    its first digital (or disc) release when one is dated."""
    status: str
    home_date: date | None = None


def cinema_ink(style: str, run: CinemaRun | None,
               tint: tuple[float, float, float] | None = None,
               opacity: float | None = None) -> str | None:
    """The badge's key for ``row_items`` ("disc|<look>|<face>"), or None when
    there is no badge.  The look is "auto" or the frost tint ("rgb:r,g,b"),
    "auto" too for a frosted disc whose tint isn't sampled yet (the layout
    pass: same size either way); an *opacity* follows the tint as "@alpha".  The face is the date ("OCT 16"), else
    "popcorn" or "clapper"."""
    if run is None:
        return None
    look = _frost_look(tint, opacity) if style == "frosted" and tint is not None else "auto"
    if run.home_date is not None:
        face = f"{_MONTHS[run.home_date.month - 1]} {run.home_date.day}"
    else:
        face = "clapper" if run.status == "Production" else "popcorn"
    return f"disc|{look}|{face}"


def wants_frost(style: str) -> bool:
    """Whether the cinema badge needs the frost tint sampled."""
    return style == "frosted"


@lru_cache(maxsize=16)
def _disc_mask(h: int) -> Image.Image:
    ss = 4
    m = Image.new("L", (h * ss, h * ss), 0)
    ImageDraw.Draw(m).ellipse([0, 0, h * ss - 1, h * ss - 1], fill=255)
    return m.resize((h, h), Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def _popcorn_alpha(size: int) -> Image.Image:
    """A popcorn bucket ``size`` square: a tapered bucket with two stripes
    cut down it under a heap of kernels.  Drawn for this size rather than
    traced from the full popcorn mark, whose fine lines grey over below
    ~20 px: the gaps are held to at least a pixel and the level edges put
    on whole pixels (drawn 4x on the final grid and box-reduced)."""
    ss = 4
    u = size / 100
    px1 = 100 / size                             # one final pixel, in units

    def snap(v):
        return round(v * u) * ss

    def P(pts):
        return [(x * u * ss, y * u * ss) for x, y in pts]

    im = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(im)
    # Kernels: overlapping rounds heaped over the bucket's mouth.
    for cx, cy, r in ((22, 34, 13), (40, 22, 14), (60, 22, 14), (78, 34, 13), (50, 34, 14)):
        d.ellipse([(cx - r) * u * ss, (cy - r) * u * ss, (cx + r) * u * ss, (cy + r) * u * ss], fill=255)
    rim = snap(42) / ss / u
    gap = max(4, px1)
    # A clear line between the heap and the bucket.
    d.rectangle([0, snap(42) - max(ss, snap(gap)), size * ss, snap(42) - 1], fill=0)
    d.rectangle([0, snap(42), size * ss, size * ss], fill=0)
    bottom = snap(98) / ss / u
    d.polygon(P([(12, rim), (88, rim), (76, bottom), (24, bottom)]), fill=255)
    # Two stripes, following the taper.
    w = max(5, px1 * 1.1) / 2
    for top_x, bot_x in ((37, 42), (63, 58)):
        d.polygon(P([(top_x - w, rim), (top_x + w, rim), (bot_x + w, bottom), (bot_x - w, bottom)]), fill=0)
    return im.reduce(ss)


@lru_cache(maxsize=8)
def _clapper_alpha(size: int) -> Image.Image:
    """A clapperboard ``size`` square: a board under a striped hinge bar, its
    striped arm raised off it.  Its level edges are put on whole pixels (it
    is drawn 4x on the final grid and box-reduced, never resampled), so only
    the slants are soft."""
    import math
    ss = 4
    u = size / 100                              # final pixels per unit

    def snap(v):                                # a unit value onto the pixel grid, in 4x
        return round(v * u) * ss

    im = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(im)

    def P(pts):
        return [(x * u * ss, y * u * ss) for x, y in pts]

    # Fewer, wider stripes when small, so each still clears a pixel.
    step = 22 if size >= 24 else 30
    starts = range(6 + step - 6, 94, step)

    left, right = snap(6) / ss / u, snap(94) / ss / u
    # Board.
    d.rounded_rectangle([snap(6), snap(54), snap(94) - 1, snap(92) - 1], radius=max(ss, snap(7)), fill=255)
    # Hinge bar, stripes slanting right.
    top, bot = snap(38) / ss / u, snap(50) / ss / u
    d.rectangle([snap(6), snap(38), snap(94) - 1, snap(50) - 1], fill=255)
    for x in starts:
        d.polygon(P([(x, top), (x + 9, top), (x + 3, bot), (x - 6, bot)]), fill=0)
    # Arm, raised about its left end.
    t = math.radians(-18)
    ox, oy = left, 34

    def rot(pts):
        return [(ox + (x - ox) * math.cos(t) - (y - oy) * math.sin(t),
                 oy + (x - ox) * math.sin(t) + (y - oy) * math.cos(t)) for x, y in pts]

    d.polygon(P(rot([(left, 22), (right, 22), (right, 34), (left, 34)])), fill=255)
    for x in starts:
        d.polygon(P(rot([(x, 22), (x + 9, 22), (x + 3, 34), (x - 6, 34)])), fill=0)
    return im.reduce(ss)


@lru_cache(maxsize=64)
def _disc_face(face: str, h: int, ink: tuple[int, int, int]) -> Image.Image:
    """What the disc says, ``h`` square, in ``ink``: the month over the day,
    or the popcorn / clapper glyph."""
    if face in ("popcorn", "clapper"):
        size = max(1, round(h * _ICON))
        alpha = _popcorn_alpha(size) if face == "popcorn" else _clapper_alpha(size)
        out = Image.new("RGBA", (h, h), (*ink, 0))
        # Trimmed to its ink and placed on whole pixels, never resampled.
        alpha = alpha.crop(alpha.getbbox() or (0, 0, alpha.width, alpha.height))
        glyph = Image.new("RGBA", alpha.size, (*ink, 0))
        glyph.putalpha(alpha)
        out.alpha_composite(glyph, ((h - alpha.width) // 2, (h - alpha.height) // 2))
        return out
    month, day = face.split(" ")
    ss = 4
    d_ss = h * ss
    im = Image.new("RGBA", (d_ss, d_ss), (*ink, 0))
    d = ImageDraw.Draw(im)
    font_path = os.path.join(_FONTS_DIR, "Inter-Bold.ttf")
    mon_font = ImageFont.truetype(font_path, px(h * 0.24) * ss or ss)
    day_font = ImageFont.truetype(font_path, px(h * 0.49) * ss or ss)
    # The two lines as one block centred on the disc, by their ink (caps and
    # figures, so no descenders): month cap-height, a gap, then the day.
    mon_h = -d.textbbox((0, 0), month, font=mon_font, anchor="ls")[1]
    day_h = -d.textbbox((0, 0), day, font=day_font, anchor="ls")[1]
    gap = h * 0.05 * ss
    top = (d_ss - (mon_h + gap + day_h)) / 2
    # Letter-spaced a touch, as small caps usually are.
    track = h * 0.02 * ss
    widths = [d.textlength(ch, font=mon_font) for ch in month]
    x = (d_ss - (sum(widths) + track * (len(month) - 1))) / 2
    for ch, cw in zip(month, widths):
        d.text((x, top + mon_h), ch, font=mon_font, fill=(*ink, 245), anchor="ls")
        x += cw + track
    d.text((d_ss / 2, top + mon_h + gap + day_h), day, font=day_font, fill=(*ink, 250), anchor="ms")
    return im.resize((h, h), Image.Resampling.LANCZOS)


@lru_cache(maxsize=64)
def _gradient_disc(face: str, h: int, light: bool) -> Image.Image:
    top, bottom, ink = _DISC_LIGHT if light else _DISC_DARK
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    rgb = (np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t).repeat(h, axis=1)
    im = Image.fromarray(rgb.astype(np.uint8)).convert("RGBA")
    im.putalpha(_disc_mask(h))
    im.alpha_composite(_disc_face(face, h, ink))
    return im


def _cinema_disc(look: str, face: str, h: int) -> Image.Image:
    """The placeholder the row is laid out with (the dark disc); draw_row
    swaps in the real one once it knows what is underneath (_resolve_disc)."""
    im = _gradient_disc(face, h, False).copy()
    im.info["cinema_disc"] = (look, face)
    return im


def _resolve_disc(image: Image.Image, im: Image.Image, x: int, y: int) -> Image.Image:
    """The cinema disc for where it lands at (x, y) on ``image``."""
    look, face = im.info["cinema_disc"]
    h = im.height
    box = (max(0, x), max(0, y), min(image.width, x + h), min(image.height, y + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return im
    if look.startswith("rgb:"):
        from awards import _frost_ink
        glass, tint = _glass(image, x, y, h, h, *_parse_look(look))
        glass.putalpha(_disc_mask(h))
        glass.alpha_composite(_disc_face(face, h, _frost_ink(*tint)))
        return glass
    under = image.crop(box).convert("RGB")
    luma = float(np.asarray(under.convert("L"), dtype=np.float32).mean())
    return _gradient_disc(face, h, luma < _DISC_LIGHT_BELOW)


def _cinema_mark(key: str, h: int) -> Image.Image | None:
    """The cinema slot's mark for a cinema_ink key."""
    _, look, face = key.split("|", 2)
    return _cinema_disc(look, face, h)


# ---------------------------------------------------------------------------
# Network and studio logos (TMDB)
# ---------------------------------------------------------------------------

LOGO_DIR = "/app/cache/company_logos"

# Studios shown on the studio badge, by TMDB company id: ones people know and
# whose TMDB logo still reads as a white mark at badge height.  The first set
# was picked by rendering each one; DreamWorks, Paramount, 20th Century, Toho,
# DC Studios, Bad Robot, Studio Ghibli, Syncopy and New Line (too long to stay
# legible at its fitted size) were left out as illegible there.  The second
# set's ids were checked against TMDB's company pages but not yet rendered at
# badge size; drop any that turn out not to read.  TMDB often keeps a studio
# under several ids (Lionsgate / Lions Gate Films), so each one it uses is listed.
STUDIOS = {
    3: "Pixar", 1: "Lucasfilm", 420: "Marvel Studios", 7505: "Marvel",
    6125: "Walt Disney Animation Studios", 2: "Walt Disney Pictures", 6704: "Illumination",
    11537: "LAIKA", 3172: "Blumhouse", 41077: "A24", 56: "Amblin", 174: "Warner Bros.",
    33: "Universal", 2251: "Sony Pictures Animation", 297: "Aardman", 127929: "Searchlight",
    43: "Fox Searchlight", 10146: "Focus Features", 90733: "NEON", 13184: "Annapurna",
    81: "Plan B", 5: "Columbia", 1632: "Lionsgate", 9383: "Blue Sky",
    128064: "DC Films", 923: "Legendary",
    # Checked ids, not yet rendered.
    35: "Lions Gate Films", 21: "Metro-Goldwyn-Mayer", 14: "Miramax", 491: "Summit Entertainment",
    41: "Orion Pictures", 559: "TriStar Pictures", 9195: "Touchstone Pictures",
    79: "Village Roadshow Pictures", 10163: "Working Title", 82819: "Skydance",
    179999: "Skydance Animation", 694: "StudioCanal", 6705: "Film4", 204957: "MUBI",
    23948: "Cartoon Saloon", 84493: "Studio Ponoc", 5542: "Toei Animation",
    5438: "Kyoto Animation", 5887: "ufotable", 21444: "MAPPA",
}
# A film has no network on TMDB; one made by a streamer's own studio arm gets
# that streamer's network logo.  Company id -> network id.  Only as good as
# TMDB's company lists: a streamer that only distributed a film (Glass Onion
# lists T-Street alone) isn't there, and there's no distributor data to use.
STREAMER_NETWORKS = {
    178464: 213, 198834: 213, 185004: 213,   # Netflix (US, GB, JP) -> Netflix
    145174: 213,                             # Netflix International Pictures -> Netflix
    171251: 213,                             # Netflix Animation Studios -> Netflix
    194232: 2552, 14801: 2552,               # Apple Studios, Apple -> Apple TV
    210099: 1024, 20580: 1024,               # Amazon MGM Studios, Amazon Studios -> Prime Video
    7429: 49, 3268: 49, 14914: 49,           # HBO Films, HBO, HBO Documentary Films -> HBO
}


# Networks drawn with another network's logo, where their own doesn't
# survive the white-mark conversion: Fox Kids' letters sit in a thick comic
# outline that joins them into one blob.  Network id -> network id.
NETWORK_STAND_INS = {
    2686: 19,                                # Fox Kids -> FOX
}
# A network or studio held to one of its TMDB logos, by path.  TMDB has given
# HBO two over the years (purple, and the sharper black one), and titles whose
# facts were cached at different times would otherwise show either.
PINNED_LOGOS = {
    ("network", 49): "/tuomPhY2UtuPTqqFnKMVHvSb724.png",   # HBO, the black one
}


@dataclass(frozen=True)
class Logo:
    kind: str        # "network" | "company"
    id: int
    path: str        # TMDB logo path


def make_logo(kind: str, ident: int, path: str) -> Logo:
    """A Logo, held to its PINNED_LOGOS path where it has one."""
    return Logo(kind, ident, PINNED_LOGOS.get((kind, ident), path))


def pick_logos(facts: dict | None, media_type: str) -> tuple[Logo | None, Logo | None, int | None]:
    """(network, studio, network id to look up) for a title's badge facts.
    TV takes its first network with a logo; a film, the network of the first
    streamer studio that made it.  Where the network's logo path isn't in the
    title's own data — a film's streamer, or a network drawn with a stand-in's
    logo (NETWORK_STAND_INS) — the caller looks it up by the third value.  The
    studio is the first of the title's production companies on the curated
    list."""
    if not facts:
        return None, None, None
    network, lookup = None, None
    if media_type in ("tv", "series"):
        first = next((n for n in facts.get("networks", []) if n.get("logo_path")), None)
        if first and first["id"] in NETWORK_STAND_INS:
            lookup = NETWORK_STAND_INS[first["id"]]
        elif first:
            network = make_logo("network", first["id"], first["logo_path"])
    else:
        lookup = next((STREAMER_NETWORKS[c["id"]] for c in facts.get("companies", [])
                       if c["id"] in STREAMER_NETWORKS), None)
    studio = next((make_logo("company", c["id"], c["logo_path"]) for c in facts.get("companies", [])
                   if c["id"] in STUDIOS and c.get("logo_path")), None)
    return network, studio, lookup


def _logo_file(logo: Logo) -> str:
    stem = os.path.splitext(os.path.basename(logo.path))[0]
    return os.path.join(LOGO_DIR, f"{logo.kind}_{logo.id}_{stem}.png")


async def ensure_logo(client, logo: Logo | None) -> None:
    """Download a network or studio logo once, as a PNG.  SVG logos are
    rasterised here so rendering never needs cairosvg for them.  A failure
    leaves the badge out and is retried on the next request."""
    if logo is None or os.path.exists(_logo_file(logo)):
        return
    try:
        if logo.path.endswith(".svg"):
            import cairosvg
            resp = await client.get(f"https://image.tmdb.org/t/p/original{logo.path}", timeout=15)
            resp.raise_for_status()
            png = cairosvg.svg2png(bytestring=resp.content, output_height=300)
        else:
            resp = await client.get(f"https://image.tmdb.org/t/p/w500{logo.path}", timeout=15)
            resp.raise_for_status()
            png = resp.content
        Image.open(io.BytesIO(png)).verify()
    except Exception as exc:
        logger.warning(f"Graphic badges: {logo.kind} logo {logo.id} fetch failed: {exc}")
        return
    os.makedirs(LOGO_DIR, exist_ok=True)
    tmp = _logo_file(logo) + ".part"
    with open(tmp, "wb") as fh:
        fh.write(png)
    os.replace(tmp, _logo_file(logo))


# A shape that is mostly one solid block (a badge, a shield: Marvel Studios'
# red box, ABC's disc, SBT's colour wheel, Toei's cat) carries its lettering as another
# colour inside it, which a plain white mark would lose.  Those have the
# lettering cut out, found against the block's own median brightness, never
# a fixed one: a bright orange or yellow block is the block, not lettering.
#
# Lettering is one even colour on one side of the block, lighter (white on
# red) or darker (black on a white disc).  So only one side is cut — the more
# neutral one when both stand out, as white letters do on SBT's wheel, whose
# dark purples stand out too — and of that side only what is near its colour:
# its white or black when it has one (_KNOCKOUT_NEUTRAL), else its median
# colour.  The wheel's yellows and purples are block.  Only where that is a
# minority of the block (_KNOCKOUT_SHARE) and differs by more than shading
# does (_KNOCKOUT_GAP): a logo that is all lettering (Marvel's wordmark,
# STARZ) has nothing to cut.  And only parts the block holds inside it: one
# that reaches the logo's outside edge (Fox Kids' yellow X, HBO Max's "max")
# is part of the mark's own shape, and stays.  Warner Bros.' shield is just
# over half gold (rim, letters, banner), so its blue field, inside the rim,
# is what goes.
_KNOCKOUT_FILL = 0.55
_KNOCKOUT_SHARE = (0.03, 0.65)
_KNOCKOUT_GAP = 60
_KNOCKOUT_NEUTRAL = 40      # chroma (max - min channel) under which a colour is white, grey or black
_KNOCKOUT_NEAR = 60         # RGB distance within which a pixel is the lettering's colour
_KNOCKOUT_MIN_PART = 0.05   # share of the logo's ink a shape needs to be judged on its own


def _enclosed(marks: np.ndarray, block: np.ndarray) -> np.ndarray:
    """The parts of ``marks`` the block surrounds: not joined to the logo's
    outside (what isn't block) except through other marks."""
    import cv2
    passable = np.pad(marks | ~block, 1, constant_values=True).astype(np.uint8)
    _, labels = cv2.connectedComponents(passable, connectivity=4)
    return marks & (labels[1:-1, 1:-1] != labels[0, 0])


def _lettering(rgb: np.ndarray, lum: np.ndarray, block: np.ndarray):
    """(mask, colour) of the lettering a solid block holds, or (None, None)."""
    differs = (np.abs(lum - np.median(lum[block])) > _KNOCKOUT_GAP) & block
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    sides = [m for m in (differs & (lum > np.median(lum[block])),
                         differs & (lum < np.median(lum[block]))) if m.any()]
    if not sides:
        return None, None
    side = min(sides, key=lambda m: float(chroma[m].mean()))
    neutral = side & (chroma < _KNOCKOUT_NEUTRAL)
    pick = neutral if neutral.sum() >= 0.3 * side.sum() else side
    colour = np.median(rgb[pick], axis=0)
    near = (np.linalg.norm(rgb - colour, axis=2) < _KNOCKOUT_NEAR) & side
    return _enclosed(near, block), colour


def logo_alpha(im: Image.Image) -> np.ndarray:
    """A TMDB logo as the alpha of a white mark, cropped to its ink."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    alpha = a[..., 3]
    lum = a[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if alpha.min() > 250:
        # No transparency: the logo sits on a plate; its shape is whatever
        # differs from the plate's colour (read off the border).
        plate = np.median(np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]]))
        alpha = np.clip(np.abs(lum - plate) * 3, 0, 255)
    solid = alpha > 128
    if not solid.any():
        return np.zeros((1, 1), dtype=np.uint8)
    rows, cols = np.flatnonzero(solid.any(axis=1)), np.flatnonzero(solid.any(axis=0))
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    alpha, lum, block = alpha[y0:y1, x0:x1], lum[y0:y1, x0:x1], solid[y0:y1, x0:x1]
    # Each separate shape is judged on its own, so an emblem over a wordmark
    # (Toei's cat over "TOEI ANIMATION") has the emblem's lettering and
    # details cut even though the logo as a whole is mostly empty.
    import cv2
    n, labels, stats, _ = cv2.connectedComponentsWithStats(block.astype(np.uint8), connectivity=8)
    rgb = a[y0:y1, x0:x1, :3]
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < _KNOCKOUT_MIN_PART * block.sum() or area <= _KNOCKOUT_FILL * w * h:
            continue
        box = (slice(y, y + h), slice(x, x + w))
        part = labels[box] == i
        marks, colour = _lettering(rgb[box], lum[box], part)
        if marks is None or not _KNOCKOUT_SHARE[0] < marks.sum() / part.sum() < _KNOCKOUT_SHARE[1]:
            continue
        # Faded across the edge between block and lettering, by distance from
        # the lettering's colour, so anti-aliased letters keep a clean
        # outline; nothing further off is touched.
        near = cv2.dilate(marks.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        dist = np.linalg.norm(rgb[box] - colour, axis=2)
        rest = near & part & ~marks
        ref = max(1.0, float(np.median(dist[rest]))) if rest.any() else 255.0
        alpha[box] = np.where(near, alpha[box] * np.clip((dist / ref - 0.25) / 0.5, 0, 1), alpha[box])
    return alpha.astype(np.uint8)


# Logos come in every shape and weight, so they're sized by how much they
# weigh on the row rather than by height: each is scaled to cover about the
# area of a box _LOGO_AREA_W row heights wide and one high, which shrinks a
# long wordmark (Lionsgate, Netflix) and grows a compact emblem (HBO, A24).
# That area is then weighed by the logo's ink: a solid block (Marvel Studios'
# box) holds far more than an outline or a thin wordmark in the same box, so
# it is drawn smaller and the light one larger — half way to equal ink
# (_LOGO_FILL_POWER), within _LOGO_FILL_LIMITS, since a hairline logo blown up
# to a solid one's ink would tower over the row.
#
# Every logo then fits a box _LOGO_MAX_W rows wide and _LOGO_MAX_H high, so
# its footprint is predictable wherever a group puts it.  The caps are what
# size the two ends: compact emblems (abc, Universal) meet the height, long
# wordmarks (TV TOKYO, TOKYO MX) the width.  A heavy logo's box shrinks with
# its ink too, or a solid disc (abc, TNT) would fill the same box as an
# outline emblem (Warner Bros., Universal) and look far bigger.  Tuned on a
# sheet of real TMDB logos (tools/logo_sheet.py): aspect 2-4 wordmarks
# (NETFLIX, CBS, PIXAR) are the reference the ends were pulled towards.
# ``scale`` (badge_logo_scale) multiplies the result.
_LOGO_AREA_W = 2.6
_LOGO_MAX_W  = 3.8
_LOGO_MAX_H  = 1.15
_LOGO_FILL   = 0.45            # the ink share of a typical logo's box
_LOGO_FILL_POWER  = 0.5
_LOGO_FILL_LIMITS = (0.75, 1.5)
LOGO_SCALE_DEFAULT, LOGO_SCALE_RANGE = 1.0, (0.5, 2.0)


def logo_size(shape: tuple[int, int], row_h: int, fill: float = _LOGO_FILL,
              scale: float = 1.0) -> tuple[int, int]:
    """(width, height) for a logo of ``shape`` (h, w) whose ink covers
    ``fill`` of its box, in a row ``row_h`` tall."""
    aspect = shape[1] / shape[0]
    unit = row_h * scale
    weight = (_LOGO_FILL / max(fill, 1e-3)) ** _LOGO_FILL_POWER
    weight = min(_LOGO_FILL_LIMITS[1], max(_LOGO_FILL_LIMITS[0], weight))
    box = unit * min(1.0, weight ** 0.5)    # a heavy logo's box shrinks with it
    h = min(box * _LOGO_MAX_H, (unit * unit * _LOGO_AREA_W * weight / aspect) ** 0.5)
    w = min(h * aspect, box * _LOGO_MAX_W)
    h = w / aspect
    return max(1, round(w)), max(2, round(h))


@lru_cache(maxsize=128)
def _logo_mark(logo: Logo, h: int, scale: float = 1.0) -> Image.Image | None:
    path = _logo_file(logo)
    if not os.path.exists(path):
        return None
    try:
        a = logo_alpha(Image.open(path))
    except Exception as exc:
        logger.error(f"Graphic badges: {logo.kind} logo {logo.id} unreadable: {exc}")
        return None
    if a.shape[0] < 2:
        return None
    fill = float(a.mean()) / 255
    m = Image.fromarray(a).resize(logo_size(a.shape, h, fill, scale), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", m.size, (255, 255, 255, 0))
    out.putalpha(m)
    return out


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

# A group is a list of badge slots drawn as one row at one anchor.  Its order
# is both the order across the row and the order they survive in: the max
# keeps the first N present, and a row too wide for its space loses from the
# end.  Serialised as "anchor:max:slot,slot,...[:size[:spacing]]" — see
# parse_group.  The anchor is a named corner, or "x,y,align" for a custom
# position: x and y are
# fractions of the poster, y the row's centre line, and align (l / c / r) says
# which part of the row sits at x — its left edge (the row grows rightwards),
# its centre, or its right edge (grows leftwards).  That edge stays put as
# badges come and go.  Without an align, the nearer edge of the poster decides.
ANCHORS = ("chip", "tl", "tr", "bl", "br", "above_logo", "below_logo")
# Centred on the title logo (or fallback title text), wherever it landed.
LOGO_ANCHORS = ("above_logo", "below_logo")
SLOTS = ("video", "audio", "res", "cert", "network", "studio", "cinema")
# The slots that show stream quality; the rest come from TMDB alone, so a
# layout without any of these needs no quality source at all.
QUALITY_SLOTS = ("video", "audio", "res")
MAX_ITEMS = 4
DEFAULT_GROUP1 = "chip:4:video,audio,res,cert"
# The request parameters holding the groups, in drawing order.
GROUP_PARAMS = ("badge_group1", "badge_group2", "badge_group3", "badge_group4")
# A group's size is the row height in the units badge_height uses (20 matches
# the side chip; the default is a touch larger); its spacing, the space between its badges, is a fraction of
# the poster's width.
DEFAULT_SIZE, SIZE_RANGE = 22, (10, 60)
DEFAULT_SPACING, SPACING_RANGE = 0.028, (0.0, 0.08)


@dataclass(frozen=True)
class Group:
    anchor: str                                 # a named anchor, or "custom"
    max_items: int
    slots: tuple[str, ...]
    xy: tuple[float, float] | None = None       # the custom position
    align: str = "l"                            # custom: which part of the row is at x
    size: int = DEFAULT_SIZE
    spacing: float = DEFAULT_SPACING


ALIGNS = ("l", "c", "r")


def _parse_xy(raw: str) -> tuple[tuple[float, float], str] | None:
    parts = raw.split(",")
    if len(parts) not in (2, 3):
        return None
    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not all(0.0 <= v <= 1.0 for v in (x, y)):   # also rejects nan
        return None
    if len(parts) == 3:
        if parts[2] not in ALIGNS:
            return None
        align = parts[2]
    else:
        align = "l" if x < 0.5 else ("r" if x > 0.5 else "c")
    return (round(x, 3), round(y, 3)), align


def parse_group(raw: str | None) -> Group | None:
    """A group from its URL spelling, or None for off / unreadable.  Unknown
    slots are skipped and repeats dropped; the max, size and spacing are
    clamped to their ranges."""
    parts = (raw or "").strip().lower().split(":")
    if not 3 <= len(parts) <= 5:
        return None
    size, spacing = DEFAULT_SIZE, DEFAULT_SPACING
    try:
        if len(parts) >= 4:
            size = int(max(SIZE_RANGE[0], min(SIZE_RANGE[1], round(float(parts[3])))))
        if len(parts) == 5:
            spacing = float(parts[4])
            if spacing != spacing:  # nan
                return None
            spacing = round(max(SPACING_RANGE[0], min(SPACING_RANGE[1], spacing)), 3)
    except (ValueError, OverflowError):
        return None
    custom = _parse_xy(parts[0]) if "," in parts[0] else None
    if custom is None and parts[0] not in ANCHORS:
        return None
    try:
        max_items = max(1, min(MAX_ITEMS, int(parts[1])))
    except ValueError:
        return None
    slots: list[str] = []
    for slot in parts[2].split(","):
        slot = slot.strip()
        if slot in SLOTS and slot not in slots:
            slots.append(slot)
    if not slots:
        return None
    if custom:
        return Group("custom", max_items, tuple(slots), custom[0], custom[1], size, spacing)
    return Group(parts[0], max_items, tuple(slots), size=size, spacing=spacing)


def format_group(group: Group | None) -> str:
    if group is None:
        return ""
    anchor = (f"{group.xy[0]:g},{group.xy[1]:g},{group.align}" if group.xy else group.anchor)
    spec = f"{anchor}:{group.max_items}:{','.join(group.slots)}"
    if group.spacing != DEFAULT_SPACING:
        return f"{spec}:{group.size}:{group.spacing:g}"
    return spec if group.size == DEFAULT_SIZE else f"{spec}:{group.size}"


def groups_use_quality(cfg) -> bool:
    """Whether any of a request config's groups shows a quality badge."""
    return any(slot in QUALITY_SLOTS for g in cfg_groups(cfg) for slot in g.slots)


def cfg_groups(cfg) -> list[Group]:
    """The groups a request config draws (resolve_groups over GROUP_PARAMS)."""
    return resolve_groups(*(getattr(cfg, name) for name in GROUP_PARAMS))


def resolve_groups(*raw: str | None) -> list[Group]:
    """The groups a request draws, in drawing order.  A slot assigned to two
    groups stays in the first."""
    taken: set[str] = set()
    groups: list[Group] = []
    for r in raw:
        g = parse_group(r)
        if g is None:
            continue
        slots = tuple(s for s in g.slots if s not in taken)
        taken.update(slots)
        if slots:
            groups.append(Group(g.anchor, g.max_items, slots, g.xy, g.align, g.size, g.spacing))
    return groups


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

# Every badge is exactly the row's unit height, ink top to ink bottom — marks
# cropped to their ink, boxes drawn edge to edge — so the row shares one top
# and one bottom line.  Network and studio logos are the exception: sized by
# area and ink around that height (see logo_size), since their shapes vary so
# much, and centred on the row.

_US_CERTS = {"G", "PG", "PG-13", "R", "NC-17",
             "TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA"}


def row_items(tokens: list[str], certification: str | None, age_rating: int | None,
              unit_h: int, slots: tuple[str, ...] = SLOTS,
              show_quality: bool = True,
              network: Logo | None = None, studio: Logo | None = None,
              cinema: str | None = None,
              quality_look: str | None = None,
              logo_scale: float = LOGO_SCALE_DEFAULT) -> list[tuple[str, Image.Image]]:
    """(slot, image) for each of ``slots`` this title has, in that order.
    Quality marks only when ``show_quality`` (the minimum-quality gate); the
    certificate always.  ``cinema`` is the cinema badge's key (cinema_ink), None
    for a title that is out at home.  ``quality_look`` (quality_look) puts the
    quality marks on frosted chips, and ``logo_scale`` (badge_logo_scale)
    multiplies the network and studio logos' size.  Dolby Vision and Atmos in the same group
    share the combined mark, in the video slot's place."""
    t = set(tokens) if show_quality else set()
    dolby_h = unit_h
    combined = ("video" in slots and "audio" in slots and "DV" in t and "ATMOS" in t
                and _mark("DV+ATMOS", dolby_h) is not None)

    def mark(name: str):
        if quality_look:
            return _frost_chip("mark:" + name, unit_h, quality_look)
        return _mark(name, dolby_h)

    def box(text: str):
        # Filled: solid enough to hold its own beside the Dolby marks;
        # outlined boxes are left to the certificate.
        if quality_look:
            return _frost_chip(text, unit_h, quality_look)
        return _box(text, unit_h, True)

    def video():
        if combined:
            return mark("DV+ATMOS")
        if "DV" in t:
            return mark("DV")
        if "HDR10+" in t:
            return box("HDR10+")
        if "HDR10" in t:
            return box("HDR10")
        return None

    def audio():
        if combined:
            return None
        if "ATMOS" in t:
            return mark("ATMOS")
        if "DTSX" in t:
            return mark("DTSX")
        return None

    def res():
        if "4K" in t:
            return box("4K")
        if "1080P" in t:
            return box("HD")
        return None

    def cert():
        c = (certification or "").strip().upper()
        if c in _US_CERTS:
            return _box(c, unit_h, False)
        if age_rating:
            return _box(f"{int(age_rating)}+", unit_h, False)
        return None

    build = {"video": video, "audio": audio, "res": res, "cert": cert,
             "network": lambda: _logo_mark(network, unit_h, logo_scale) if network else None,
             "studio": lambda: _logo_mark(studio, unit_h, logo_scale) if studio else None,
             "cinema": lambda: _cinema_mark(cinema, unit_h) if cinema else None}
    items = [(slot, build[slot]()) for slot in slots]
    return [(slot, im) for slot, im in items if im is not None]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def row_width(items: list[tuple[str, Image.Image]], gap: float) -> float:
    # In 500-wide units (pxscale), as draw_row advances; rounding each item's
    # width at a larger canvas would otherwise add up along the row.
    return sum(pxr(im.width) for _, im in items) + gap * max(0, len(items) - 1)


def fit(items: list[tuple[str, Image.Image]], budget: int, gap: int) -> list[tuple[str, Image.Image]]:
    """The longest prefix of ``items`` no wider than ``budget``."""
    items = list(items)
    while items and row_width(items, gap) > budget:
        items.pop()
    return items


# A row whose network or studio logo doesn't fit on its own line, beside what
# is already there (Minimalist's genre and year, say), has the logo shrunk in
# these steps before the group is moved off that line — so a long wordmark
# (TOKYO MX) gives a little size rather than its place.
LOGO_SHRINK_STEPS = (0.9, 0.8, 0.7, 0.6)


def has_logo(items: list[tuple[str, Image.Image]]) -> bool:
    return any(slot in ("network", "studio") for slot, _ in items)


def fit_shrinking(build, budget: int, gap: int, scale: float) -> list[tuple[str, Image.Image]] | None:
    """``build(logo_scale)``'s items, all of them no wider than ``budget``,
    their logos at ``scale`` or shrunk as far as LOGO_SHRINK_STEPS goes; None
    when even that doesn't fit them all."""
    for step in (1.0, *LOGO_SHRINK_STEPS):
        items = build(scale * step)
        if items and row_width(items, gap) <= budget:
            return items
    return None


def free_run(occupied_cols: np.ndarray, right: bool, margin: int) -> int:
    """How far a row can run in from the ``right`` (or left) margin before it
    meets an occupied column."""
    cols = occupied_cols[::-1] if right else occupied_cols
    cols = cols[margin:]
    hit = np.flatnonzero(cols)
    return int(hit[0]) if hit.size else len(cols)


def _shadowed(im: Image.Image) -> tuple[Image.Image, int]:
    """The mark over a soft shadow of itself, for legibility on light art."""
    pad = max(2, im.height // 5)
    sheet = Image.new("L", (im.width + 2 * pad, im.height + 2 * pad), 0)
    sheet.paste(im.getchannel("A").point(lambda v: v * 120 // 255), (pad, pad))
    sheet = sheet.filter(ImageFilter.GaussianBlur(max(1.0, im.height * 0.07)))
    lift = max(1, im.height // 30)
    out = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    out.putalpha(sheet)
    out.alpha_composite(im, (pad, pad - lift))
    return out, pad


def draw_row(image: Image.Image, items: list[tuple[str, Image.Image]], *,
             left_x: int, center_y: float, gap: int) -> None:
    """``items`` in a row from ``left_x``, centred on ``center_y``.  Fitting
    is the caller's job (see fit)."""
    x = left_x
    for _, im in items:
        if "cinema_disc" in im.info:
            im = _resolve_disc(image, im, round(x), int(round(center_y - im.height / 2)))
        elif "frost_chip" in im.info:
            im = _resolve_chip(image, im, round(x), int(round(center_y - im.height / 2)))
        shadow, pad = _shadowed(im)
        sx, sy = round(x) - pad, int(round(center_y - im.height / 2)) - pad
        cl, ct = max(0, -sx), max(0, -sy)
        image.alpha_composite(shadow.crop((cl, ct, shadow.width, shadow.height)), (sx + cl, sy + ct))
        x += pxr(im.width) + gap
