"""Graphic badges (badge_display_mode 7): quality marks and the US certificate,
in up to three groups, each a row at its own anchor (see Group; laid out by
main._draw_graphic_badges).

Nothing trademarked ships in the repo.  The marks are fetched once from
Wikimedia Commons, pinned by SHA-1 so an edit upstream can never change a
poster, and kept in the cache volume:

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
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def assets_ready() -> bool:
    return all(os.path.exists(_asset_path(k)) for k in _FILES)


_fetch_lock = asyncio.Lock()


async def ensure_assets(client) -> bool:
    """Download any missing Commons file.  Cheap once they are all on disk;
    a failed download leaves that mark out rather than failing the render,
    and is retried on the next request."""
    if assets_ready():
        return True
    async with _fetch_lock:
        os.makedirs(ASSET_DIR, exist_ok=True)
        for key, f in _FILES.items():
            path = _asset_path(key)
            if os.path.exists(path):
                continue
            url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + f.title.replace(" ", "_")
            try:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT},
                                        follow_redirects=True, timeout=15)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning(f"Graphic badges: Commons fetch failed for {f.title}: {exc}")
                continue
            if hashlib.sha1(resp.content).hexdigest() != f.sha1:
                # Commons now serves a different revision.  Keep drawing without
                # it rather than put an unreviewed file on every poster.
                logger.warning(f"Graphic badges: {f.title} no longer matches its pinned SHA-1; skipped")
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(resp.content)
            os.replace(tmp, path)
            logger.info(f"Graphic badges: cached {f.title}")
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
    font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), int(h * _BOX_TEXT) * ss)
    w = int(font.getlength(text) / ss + h * _BOX_PAD)
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    bw = max(1, round(h * 0.07)) * ss
    # Edge to edge: PIL strokes an outline inwards, so no inset is needed, and
    # one would leave the box's ink a pixel short of the marks beside it.
    box = [0, 0, w * ss - 1, h * ss - 1]
    if filled:
        d.rounded_rectangle(box, radius=int(h * _BOX_RADIUS * ss), fill=(*_INK, 235))
        # Cut the label out of the plate so the poster shows through it.
        cut = Image.new("L", im.size, 0)
        ImageDraw.Draw(cut).text((w * ss / 2, h * ss / 2), text, font=font, fill=255, anchor="mm")
        im.putalpha(Image.fromarray(np.minimum(np.asarray(im.getchannel("A")),
                                               255 - np.asarray(cut))))
    else:
        d.rounded_rectangle(box, radius=int(h * _BOX_RADIUS * ss), outline=(*_INK, 235), width=bw)
        d.text((w * ss / 2, h * ss / 2), text, font=font, fill=(*_INK, 245), anchor="mm")
    return im.reduce(ss)


# ---------------------------------------------------------------------------
# Network and studio logos (TMDB)
# ---------------------------------------------------------------------------

LOGO_DIR = "/app/cache/company_logos"

# Studios shown on the studio badge, by TMDB company id: ones people know and
# whose TMDB logo still reads as a white mark at badge height.  Picked by
# rendering each one; DreamWorks, Paramount, 20th Century, Toho, DC Studios,
# Bad Robot, Studio Ghibli, Syncopy and New Line (too long to stay legible
# at its fitted size) were left out as illegible there.
STUDIOS = {
    3: "Pixar", 1: "Lucasfilm", 420: "Marvel Studios", 7505: "Marvel",
    6125: "Walt Disney Animation Studios", 2: "Walt Disney Pictures", 6704: "Illumination",
    11537: "LAIKA", 3172: "Blumhouse", 41077: "A24", 56: "Amblin", 174: "Warner Bros.",
    33: "Universal", 2251: "Sony Pictures Animation", 297: "Aardman", 127929: "Searchlight",
    43: "Fox Searchlight", 10146: "Focus Features", 90733: "NEON", 13184: "Annapurna",
    81: "Plan B", 5: "Columbia", 1632: "Lionsgate", 9383: "Blue Sky",
    128064: "DC Films", 923: "Legendary",
}
# A film has no network on TMDB; one made by a streamer's own studio arm gets
# that streamer's network logo.  Company id -> network id.  Only as good as
# TMDB's company lists: a streamer that only distributed a film (Glass Onion
# lists T-Street alone) isn't there, and there's no distributor data to use.
STREAMER_NETWORKS = {
    178464: 213, 198834: 213, 185004: 213,   # Netflix (US, GB, JP) -> Netflix
    145174: 213,                             # Netflix International Pictures -> Netflix
    194232: 2552,                            # Apple Studios -> Apple TV
    210099: 1024,                            # Amazon MGM Studios -> Prime Video
    7429: 49,                                # HBO Films -> HBO
}


@dataclass(frozen=True)
class Logo:
    kind: str        # "network" | "company"
    id: int
    path: str        # TMDB logo path


def pick_logos(facts: dict | None, media_type: str) -> tuple[Logo | None, Logo | None, int | None]:
    """(network, studio, streamer network id) for a title's badge facts.  TV
    takes its first network with a logo; a film, the network of the first
    streamer studio that made it — whose logo path the caller looks up, as it
    isn't in the film's own data (the third value).  The studio is the first
    of the title's production companies on the curated list."""
    if not facts:
        return None, None, None
    network, streamer = None, None
    if media_type in ("tv", "series"):
        network = next((Logo("network", n["id"], n["logo_path"])
                        for n in facts.get("networks", []) if n.get("logo_path")), None)
    else:
        streamer = next((STREAMER_NETWORKS[c["id"]] for c in facts.get("companies", [])
                         if c["id"] in STREAMER_NETWORKS), None)
    studio = next((Logo("company", c["id"], c["logo_path"]) for c in facts.get("companies", [])
                   if c["id"] in STUDIOS and c.get("logo_path")), None)
    return network, studio, streamer


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


# A logo that is mostly one solid block (a badge, a shield: Marvel Studios'
# red box, ABC's disc) carries its lettering as lighter colour inside it, which
# a plain white mark would lose.  Those have their light parts cut out — but
# only where the light parts are a minority of the block: a logo that is
# itself light lettering (Marvel's wordmark, STARZ) would otherwise vanish.
_KNOCKOUT_FILL = 0.55
_KNOCKOUT_LIGHT = (0.03, 0.5)


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
    block = solid[y0:y1, x0:x1]
    light = (lum[y0:y1, x0:x1] > 170) & block
    light_share = light.sum() / max(1, block.sum())
    if block.mean() > _KNOCKOUT_FILL and _KNOCKOUT_LIGHT[0] < light_share < _KNOCKOUT_LIGHT[1]:
        alpha = alpha * np.clip((200 - lum) / 80, 0, 1)
    return alpha[y0:y1, x0:x1].astype(np.uint8)


# Logos come in every shape, so they're sized by area rather than height:
# each is scaled to cover about the area of a box _LOGO_AREA_W row heights
# wide and one high, which shrinks a long wordmark (Lionsgate, Netflix) and
# lets a compact emblem (HBO, A24) fill the row — no taller than the row, so
# the shared top and bottom line holds, and no wider than _LOGO_MAX_W.
_LOGO_AREA_W = 2.6
_LOGO_MAX_W  = 3.5


def logo_size(shape: tuple[int, int], row_h: int) -> tuple[int, int]:
    """(width, height) for a logo of ``shape`` (h, w) in a row ``row_h`` tall."""
    aspect = shape[1] / shape[0]
    h = min(row_h, (row_h * row_h * _LOGO_AREA_W / aspect) ** 0.5)
    w = min(h * aspect, row_h * _LOGO_MAX_W)
    h = w / aspect
    return max(1, round(w)), max(2, round(h))


@lru_cache(maxsize=128)
def _logo_mark(logo: Logo, h: int) -> Image.Image | None:
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
    m = Image.fromarray(a).resize(logo_size(a.shape, h), Image.Resampling.LANCZOS)
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
SLOTS = ("video", "audio", "res", "cert", "network", "studio")
# The slots that show stream quality; the rest come from TMDB alone, so a
# layout without any of these needs no quality source at all.
QUALITY_SLOTS = ("video", "audio", "res")
MAX_ITEMS = 4
DEFAULT_GROUP1 = "chip:4:video,audio,res,cert"
# A group's size is the row height in the units badge_height uses (20 matches
# the side chip); its spacing, the space between its badges, is a fraction of
# the poster's width.
DEFAULT_SIZE, SIZE_RANGE = 20, (10, 60)
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


def groups_use_quality(*raw: str | None) -> bool:
    """Whether any of these groups shows a quality badge."""
    return any(slot in QUALITY_SLOTS for g in resolve_groups(*raw) for slot in g.slots)


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
# area within that height (see logo_size), since their shapes vary so much.

_US_CERTS = {"G", "PG", "PG-13", "R", "NC-17",
             "TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA"}


def row_items(tokens: list[str], certification: str | None, age_rating: int | None,
              unit_h: int, slots: tuple[str, ...] = SLOTS,
              show_quality: bool = True,
              network: Logo | None = None, studio: Logo | None = None) -> list[tuple[str, Image.Image]]:
    """(slot, image) for each of ``slots`` this title has, in that order.
    Quality marks only when ``show_quality`` (the minimum-quality gate); the
    certificate always.  Dolby Vision and Atmos in the same group share the
    combined mark, in the video slot's place."""
    t = set(tokens) if show_quality else set()
    dolby_h = unit_h
    combined = ("video" in slots and "audio" in slots and "DV" in t and "ATMOS" in t
                and _mark("DV+ATMOS", dolby_h) is not None)

    def video():
        if combined:
            return _mark("DV+ATMOS", dolby_h)
        if "DV" in t:
            return _mark("DV", dolby_h)
        # Filled like the resolution box: solid enough to hold its own beside
        # the Dolby marks; outlined boxes are left to the certificate.
        if "HDR10+" in t:
            return _box("HDR10+", unit_h, True)
        if "HDR10" in t:
            return _box("HDR10", unit_h, True)
        return None

    def audio():
        if combined:
            return None
        if "ATMOS" in t:
            return _mark("ATMOS", dolby_h)
        if "DTSX" in t:
            return _mark("DTSX", unit_h)
        return None

    def res():
        if "4K" in t:
            return _box("4K", unit_h, True)
        if "1080P" in t:
            return _box("HD", unit_h, True)
        return None

    def cert():
        c = (certification or "").strip().upper()
        if c in _US_CERTS:
            return _box(c, unit_h, False)
        if age_rating:
            return _box(f"{int(age_rating)}+", unit_h, False)
        return None

    build = {"video": video, "audio": audio, "res": res, "cert": cert,
             "network": lambda: _logo_mark(network, unit_h) if network else None,
             "studio": lambda: _logo_mark(studio, unit_h) if studio else None}
    items = [(slot, build[slot]()) for slot in slots]
    return [(slot, im) for slot, im in items if im is not None]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def row_width(items: list[tuple[str, Image.Image]], gap: int) -> int:
    return sum(im.width for _, im in items) + gap * max(0, len(items) - 1)


def fit(items: list[tuple[str, Image.Image]], budget: int, gap: int) -> list[tuple[str, Image.Image]]:
    """The longest prefix of ``items`` no wider than ``budget``."""
    items = list(items)
    while items and row_width(items, gap) > budget:
        items.pop()
    return items


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
    out = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    out.putalpha(sheet)
    out.alpha_composite(im, (pad, pad - max(1, im.height // 30)))
    return out, pad


def draw_row(image: Image.Image, items: list[tuple[str, Image.Image]], *,
             left_x: int, center_y: float, gap: int) -> None:
    """``items`` in a row from ``left_x``, centred on ``center_y``.  Fitting
    is the caller's job (see fit)."""
    x = left_x
    for _, im in items:
        shadow, pad = _shadowed(im)
        sx, sy = x - pad, int(round(center_y - im.height / 2)) - pad
        cl, ct = max(0, -sx), max(0, -sy)
        image.alpha_composite(shadow.crop((cl, ct, shadow.width, shadow.height)), (sx + cl, sy + ct))
        x += im.width + gap
