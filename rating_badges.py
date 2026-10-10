"""Rating provider badges: each chosen provider's own score behind its logo,
in place of the ★ and the single weighted score (rating_badges=imdb,tomatoes).

Drawn by three of the rating display modes — Clean (the badges replace the
star), Minimalist (small icons before each score) and the frosted Bar
(after its year and genre, in the room they leave) — and not by the Rating
Bar, whose score is the bar itself.

Nothing trademarked ships in the repo.  Each mark is fetched once, pinned by
SHA-1 so an edit upstream can never change a poster (a Commons re-upload is
looked past to the pinned revision), and kept in the cache volume beside the
graphic badges' marks:

  IMDb, Rotten Tomatoes (Tomatometer fresh / rotten, Popcornmeter up / down),
  Metacritic, Letterboxd, Trakt, MyAnimeList, AniList
      Wikimedia Commons, all public domain (below the threshold of originality)
  TMDB    TMDB's own stacked square logo, from its logos & attribution page
  Kitsu   Simple Icons (CC0)
  Roger Ebert   Material Symbols' thumb_up (Apache-2.0): RogerEbert.com
          publishes no mark, so its badge is Siskel & Ebert's thumbs-up
  Posters+  the weighted score itself, as the service's own serif "P" (the
          favicon, which ships in static/), gold in a gold ring.  A "+" beside
          it had to shrink the pair to fit the disc and the P lost its weight

Most are set round, so the row reads as a line of discs rather than a mix of
wordmarks: IMDb's lettering on its yellow, AniList's and MyAnimeList's
lettering on their own plate colours, TMDB's logo on its navy, Kitsu's glyph
on its orange, Letterboxd's dots on its slate, Trakt's glyph on its own
red-to-purple gradient and the thumb in gold on black.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from config import SCORE_NORMALISERS
from graphic_badges import ASSET_DIR, PinnedGone, _runs, commons_url, fetch_pinned

logger = logging.getLogger(__name__)

# Every source a badge can be drawn for, in the configurator's default order.
PROVIDERS = (
    "pplus", "imdb", "tomatoes", "popcorn", "metacritic", "metacriticuser", "letterboxd",
    "trakt", "tmdb", "rogerebert", "myanimelist", "anilist", "kitsu",
)
_MAX_BADGES = 6

SCALES = ("native", "normalized")
# "color" draws each site's own colours; "mono" draws every badge in the text
# colour beside it (a solid shape with the logo cut out), so a tinted vignette
# or a light frosted bar can't clash with a logo's colours.
STYLES = ("color", "mono")


@dataclass(frozen=True)
class _Source:
    url: str
    sha1: str   # of the exact file these marks were built against
    ext: str
    commons_title: str | None = None   # searched for the pinned revision if the URL moves on


def _commons(title: str, sha1: str) -> _Source:
    return _Source(commons_url(title), sha1, os.path.splitext(title)[1], title)


_FILES = {
    "imdb":        _commons("IMDB Logo 2016.svg",                    "0a1b5cd02f8aecdf4b9bbf1bda2950487e5c29c0"),
    "rt_fresh":    _commons("Rotten Tomatoes.svg",                   "5099a1d8f0dccfa0584f25387d08ecc1434cb375"),
    # Mono only: the tomato with "RT" on it, so the letters can be cut out
    # and a fresh tomato still reads as Rotten Tomatoes in one colour.
    "rt_lettered": _commons("Rotten Tomatoes alternative logo.svg",  "bcadeaaf2bd2b4f1421cbf5b1e8fe318e17856d0"),
    "rt_rotten":   _commons("Rotten Tomatoes rotten.svg",            "4b2867d30f31941105f80f544acf1e283946e4e8"),
    "rt_upright":  _commons("Rotten Tomatoes positive audience.svg", "63bbcf6deb1bf8ec8268052e1cc41681794fed93"),
    "rt_spilled":  _commons("Rotten Tomatoes negative audience.svg", "83ecbf269edf953837f6c930af510043e42e3964"),
    "metacritic":  _commons("Metacritic logo Roundel.svg",           "f36dd4ffb2045f91a84ae91cc73dd1fd33369cde"),
    "letterboxd":  _commons("Letterboxd 2018 logo (vertical).svg",   "3c22c25f6e1ce3a26c0bd59e7f70729ddbaddb6b"),
    "trakt":       _commons("Trakt.tv-favicon.svg",                  "87ee5c6b8e0e4370837299905c1f105f32b23843"),
    "myanimelist": _commons("MyAnimeList favicon.svg",               "a4ef3a65aaed28a1e32f7c070230427f1e3ca8b6"),
    "anilist":     _commons("AniList logo.svg",                      "9ca4ba567100a290d007d43c6beae38728061909"),
    "tmdb":        _Source("https://www.themoviedb.org/assets/2/v4/logos/v2/"
                           "blue_square_1-5bdc75aaebeb75dc7ae79426ddd9be3b2be1e342510f8202baf6bffa71d7f5c4.svg",
                           "bbdb45a8e78cf26bb19bc994a4553efcee54f38c", ".svg"),
    "kitsu":       _Source("https://cdn.jsdelivr.net/npm/simple-icons@16.32.0/icons/kitsu.svg",
                           "317e69e7dcc993124443042f080eff50b0179401", ".svg"),
    "thumb_up":    _Source("https://cdn.jsdelivr.net/npm/@material-symbols/svg-700@0.47.5/rounded/thumb_up-fill.svg",
                           "7f052d1f2827686651bc2f22bdc105dedb48ca07", ".svg"),
}

# Rotten Tomatoes' own thresholds: a Tomatometer of 60 or more is fresh, and
# a Popcornmeter of 60 or more an upright bucket.  (Certified Fresh has no
# free mark to draw.)
_RT_FRESH = 60


def _mark_key(provider: str, value, mono: bool = False) -> str | None:
    """The file a provider's badge is drawn from, for this score."""
    if provider == "tomatoes":
        if value >= _RT_FRESH:
            return "rt_lettered" if mono else "rt_fresh"
        return "rt_rotten"
    if provider == "popcorn":
        return "rt_upright" if value >= _RT_FRESH else "rt_spilled"
    if provider in ("metacritic", "metacriticuser"):
        return "metacritic"
    if provider == "rogerebert":
        return "thumb_up"
    return provider


# The Posters+ mark is built from the repo's own favicon, so it has nothing
# to fetch.
_FAVICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "favicon.png")
_LOCAL_MARKS = {"pplus": _FAVICON}


def _asset_path(key: str) -> str:
    if key in _LOCAL_MARKS:
        return _LOCAL_MARKS[key]
    f = _FILES[key]
    return os.path.join(ASSET_DIR, f"{f.sha1}{f.ext}")


def _keys_for(providers, style: str = "color") -> set[str]:
    keys: set[str] = set()
    for p in providers:
        if p == "tomatoes":
            keys |= {"rt_lettered" if style == "mono" else "rt_fresh", "rt_rotten"}
        elif p == "popcorn":
            keys |= {"rt_upright", "rt_spilled"}
        elif _mark_key(p, 0) not in _LOCAL_MARKS:
            keys.add(_mark_key(p, 0))
    return keys


def assets_ready(providers, style: str = "color") -> bool:
    """Every mark the providers draw is on disk, or known not to be coming
    (no revision upstream matches its pin), so nothing is left to wait for."""
    return all(os.path.exists(_asset_path(k)) or k in _gone for k in _keys_for(providers, style))


_fetch_lock = asyncio.Lock()
# A failed file is left alone this long, so an unreachable or throttling host
# (Commons answers a burst with 429) isn't asked again on every request.
_RETRY_AFTER = 600.0
_failed_at: dict[str, float] = {}
# Marks no upstream revision of which matches the pin.  Their badges are left
# out until restart, and the poster is cached as usual: waiting can't help.
_gone: set[str] = set()


async def ensure_assets(client, providers, style: str = "color") -> bool:
    """Download any missing mark the request's providers draw.  Cheap once
    they are on disk; a failed download leaves that badge out rather than
    failing the render.  True when nothing they need is still to come."""
    if assets_ready(providers, style):
        return True
    async with _fetch_lock:
        os.makedirs(ASSET_DIR, exist_ok=True)
        fetched = False
        for key in sorted(_keys_for(providers, style)):
            path = _asset_path(key)
            if (os.path.exists(path) or key in _gone
                    or time.monotonic() - _failed_at.get(key, -_RETRY_AFTER) < _RETRY_AFTER):
                continue
            f = _FILES[key]
            try:
                body = await fetch_pinned(client, f.url, f.sha1, f.commons_title)
            except PinnedGone:
                # Upstream now serves a different file.  Keep drawing without
                # it rather than put an unreviewed logo on every poster.
                _gone.add(key)
                logger.warning(f"Rating badges: {key} no longer matches its pinned SHA-1; skipped")
                continue
            except Exception as exc:
                _failed_at[key] = time.monotonic()
                logger.warning(f"Rating badges: fetch failed for {key}: {exc}")
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
            fetched = True
            logger.info(f"Rating badges: cached {key}")
        if fetched:
            _mark_rgba.cache_clear()
            badge.cache_clear()
    return assets_ready(providers, style)


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

_WORK_H = 400   # raster height every mark is cut from


def _crop(a: np.ndarray) -> np.ndarray:
    on = a[..., 3] > 8
    if not on.any():
        return a[:1, :1]
    rows, cols = np.flatnonzero(on.any(axis=1)), np.flatnonzero(on.any(axis=0))
    return a[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _svg_rgba(path: str, h: int = _WORK_H) -> np.ndarray:
    import cairosvg
    png = cairosvg.svg2png(url=path, output_height=h)
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))


def _svg_image(path: str, h: int = _WORK_H) -> Image.Image:
    return Image.fromarray(np.ascontiguousarray(_crop(_svg_rgba(path, h))))


def _plate_ink(im: Image.Image) -> tuple[tuple[int, int, int], Image.Image]:
    """An app-icon mark split into its plate colour (read off the corner) and
    the lettering on it, in its own colours.  A mark with nothing on its plate
    comes back whole."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    plate = a[2, 2, :3]
    dist = np.abs(a[..., :3] - plate).sum(axis=-1)
    alpha = np.clip((dist - 40) * 3, 0, 255) * (a[..., 3] / 255)
    if (alpha > 128).sum() < 20:
        return tuple(int(c) for c in plate), im
    ink = a.copy()
    ink[..., 3] = alpha
    return tuple(int(c) for c in plate), Image.fromarray(np.ascontiguousarray(_crop(ink.astype(np.uint8))))


def _tint(mark: Image.Image, rgb) -> Image.Image:
    out = Image.new("RGBA", mark.size, (*rgb, 0))
    out.putalpha(mark.getchannel("A"))
    return out


_SS = 4   # supersampling for the plates' edges


def _plate(fill, shape: str = "disc", ring=None, ring_w: float = 0.07) -> Image.Image:
    """A _WORK_H plate: a disc, or a rounded square; *ring* draws an outer
    ring of that colour *ring_w* of the diameter wide."""
    n = _WORK_H * _SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def shape_at(inset, colour):
        box = (inset, inset, n - 1 - inset, n - 1 - inset)
        if shape == "disc":
            d.ellipse(box, fill=colour)
        else:
            d.rounded_rectangle(box, radius=round((n - 2 * inset) * 0.24), fill=colour)
    if ring is not None:
        shape_at(0, (*ring, 255))
        shape_at(round(n * ring_w), (*fill, 255))
    else:
        shape_at(0, (*fill, 255))
    return im.resize((_WORK_H, _WORK_H), Image.Resampling.LANCZOS)


def _gradient_disc(c0, c1) -> Image.Image:
    """A disc shaded from *c0* at the bottom left to *c1* at the top right."""
    t = np.add.outer(np.arange(_WORK_H)[::-1], np.arange(_WORK_H)) / (2 * (_WORK_H - 1))
    rgb = (np.asarray(c0, np.float32) * (1 - t[..., None]) + np.asarray(c1, np.float32) * t[..., None])
    out = Image.fromarray(np.dstack([rgb, np.full(t.shape, 255, np.float32)]).astype(np.uint8))
    out.putalpha(_plate((0, 0, 0)).getchannel("A"))
    return out


def _white_ink(im: Image.Image) -> Image.Image:
    """The white parts of a mark, as a white mark."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    alpha = np.clip((a[..., :3].min(axis=-1) - 150) * 2.5, 0, 255) * (a[..., 3] / 255)
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return Image.fromarray(np.ascontiguousarray(_crop(np.asarray(out))))


def _on_plate(plate: Image.Image, mark: Image.Image, fit: float, dy: float = 0.0,
              mono: bool = False) -> Image.Image:
    """*mark* centred on *plate*, scaled so its diagonal is *fit* of the
    plate's width: a wide wordmark and a squat monogram then keep the same
    clearance from a circle's edge, which a width rule would not.

    *mono* cuts the mark out of a white plate instead, so whatever is behind
    the badge shows through the lettering."""
    scale = fit * plate.width / float(np.hypot(*mark.size))
    m = mark.resize((max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
                    Image.Resampling.LANCZOS)
    at = ((plate.width - m.width) // 2, round((plate.height - m.height) / 2 + dy * plate.height))
    if mono:
        cut = Image.new("L", plate.size, 0)
        cut.paste(m.getchannel("A"), at)
        alpha = (np.asarray(plate.getchannel("A"), np.float32)
                 * (1 - np.asarray(cut, np.float32) / 255))
        out = Image.new("RGBA", plate.size, (255, 255, 255, 0))
        out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
        return out
    out = plate.copy()
    out.alpha_composite(m, at)
    return out


def _silhouette(im: Image.Image, knock: str | None = None) -> Image.Image:
    """A white mark in *im*'s shape.  *knock* cuts out its near-white
    ("white") or near-black ("dark") parts, which carry a mark's detail:
    the lettering on a tomato, the stripes of a bucket, the disc behind
    Metacritic's "m"."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    alpha = a[..., 3].copy()
    if knock == "white":
        alpha *= np.clip((230 - a[..., :3].min(axis=-1)) / 40, 0, 1)
    elif knock == "dark":
        alpha *= np.clip((a[..., :3].max(axis=-1) - 50) / 40, 0, 1)
    # Specks standing apart from the mark — the lettered tomato's "™" —
    # read as dirt at badge size.
    import cv2
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a[..., 3] > 128).astype(np.uint8))
    if n > 2:
        big = stats[1:, cv2.CC_STAT_AREA].max()
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < big * 0.02:
                alpha[cv2.dilate((labels == i).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0] = 0
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return Image.fromarray(np.ascontiguousarray(_crop(np.asarray(out))))


_IMDB_YELLOW = (245, 197, 24)
_TMDB_NAVY   = (13, 37, 63)       # TMDB's primary dark blue
_LB_SLATE    = (32, 40, 48)       # Letterboxd's brand background
_KITSU_RGB   = (0xFD, 0x75, 0x5C)  # Simple Icons' brand colour for Kitsu
_EBERT_BODY  = (30, 30, 34)
_PPLUS_GOLD  = (252, 216, 4)       # the favicon's yellow
_PPLUS_BODY  = (23, 23, 28)
_EBERT_GOLD  = (212, 175, 55)


_WHITE = (255, 255, 255)

# What a mono badge cuts out of the marks that aren't set on a plate.
_MONO_KNOCK = {"rt_lettered": "white", "rt_upright": "white", "rt_spilled": "white",
               "metacritic": "dark"}


@lru_cache(maxsize=None)
def _mark_rgba(key: str, mono: bool = False) -> Image.Image | None:
    """A badge's mark at _WORK_H, or None when its file isn't on disk.  A
    mono mark is white, for draw_run to tint."""
    path = _asset_path(key)
    if not os.path.exists(path):
        return None
    try:
        if key == "imdb":
            # The lettering alone (the dark parts of the yellow box), on a
            # yellow disc with room around it.
            box = _svg_image(path)
            a = np.asarray(box).astype(np.int32)
            dark = (a[..., :3].sum(axis=-1) < 200) & (a[..., 3] > 128)
            letters = Image.fromarray(np.where(dark, 255, 0).astype(np.uint8))
            if dark.sum() < 20:
                return _on_plate(_plate(_IMDB_YELLOW), box, 0.72, mono=mono)
            mark = Image.new("RGBA", box.size, (0, 0, 0, 0))
            mark.putalpha(letters)
            return _on_plate(_plate(_IMDB_YELLOW), Image.fromarray(_crop(np.asarray(mark))), 0.66, mono=mono)
        if key == "tmdb":
            return _on_plate(_plate(_TMDB_NAVY), _svg_image(path), 0.72, mono=mono)
        if key == "letterboxd":
            # The three dots, without the wordmark set under them.
            a = _svg_rgba(path, _WORK_H * 3)
            top = _runs(a[..., 3].max(axis=1) > 8)[0]
            dots = Image.fromarray(np.ascontiguousarray(_crop(a[top[0]:top[1]])))
            if mono:
                # Its dots overlap, so cut out together they run into one
                # shape; three apart, a little smaller, still read as them.
                h = dots.height * _SS
                row = Image.new("RGBA", (round(h * 3.3), h), (0, 0, 0, 0))
                d = ImageDraw.Draw(row)
                for i in range(3):
                    cx, r = h / 2 + i * h * 1.15, h * 0.46
                    d.ellipse((cx - r, h / 2 - r, cx + r, h / 2 + r), fill=(255, 255, 255, 255))
                dots = row.resize((row.width // _SS, dots.height), Image.Resampling.LANCZOS)
            return _on_plate(_plate(_LB_SLATE), dots, 0.78, mono=mono)
        if key == "trakt":
            # Its white glyph on a disc shaded like the icon's own plate,
            # whose colours are read off two of its corners.
            icon = _svg_image(path)
            a = np.asarray(icon)
            inset = max(2, icon.width // 8)
            c0 = tuple(int(c) for c in a[-inset, inset, :3])
            c1 = tuple(int(c) for c in a[inset, -inset, :3])
            glyph = _white_ink(icon)
            return _on_plate(_gradient_disc(c0, c1), glyph if glyph.width >= 4 else icon, 0.72, mono=mono)
        if key == "kitsu":
            return _on_plate(_plate(_KITSU_RGB), _tint(_svg_image(path), _WHITE), 0.74, mono=mono)
        if key == "thumb_up":
            # Mono drops the gold ring: one colour has no ring to draw.
            return _on_plate(_plate(_WHITE) if mono else _plate(_EBERT_BODY, ring=_EBERT_GOLD),
                             _tint(_svg_image(path), _EBERT_GOLD), 0.66, dy=-0.01, mono=mono)
        if key == "pplus":
            return _on_plate(_plate(_WHITE) if mono else _plate(_PPLUS_BODY, ring=_PPLUS_GOLD),
                             Image.open(path).convert("RGBA"), 0.62, mono=mono)
        if key in ("myanimelist", "anilist"):
            plate, ink = _plate_ink(_svg_image(path))
            return _on_plate(_plate(plate), ink, 0.76, mono=mono)
        if mono:
            return _silhouette(_svg_image(path), _MONO_KNOCK.get(key))
        return _svg_image(path)
    except Exception as exc:
        logger.error(f"Rating badges: {key} mark failed: {exc}")
        return None


# Marks come in every shape, so they're sized by area rather than height,
# as the network and studio logos are: each covers about the area of a box
# _AREA_W row heights wide and one high — no taller than the row, so a square
# icon and IMDb's wide box carry the same weight — and no wider than _MAX_W.
_AREA_W = 1.25
_MAX_W  = 2.6


def _size(w: int, h: int, row_h: int) -> tuple[int, int]:
    aspect = w / h
    bh = min(row_h, (row_h * row_h * _AREA_W / aspect) ** 0.5)
    bw = min(bh * aspect, row_h * _MAX_W)
    return max(1, round(bw)), max(1, round(bw / aspect))


@lru_cache(maxsize=256)
def badge(provider: str, fresh: bool, row_h: int, mono: bool = False) -> Image.Image | None:
    """The badge for *provider* in a row *row_h* tall; *fresh* picks the
    Tomatometer / Popcornmeter state.  None when its mark isn't on disk."""
    src = _mark_rgba(_mark_key(provider, _RT_FRESH if fresh else 0, mono), mono)
    if src is None:
        return None
    return src.resize(_size(src.width, src.height, row_h), Image.Resampling.LANCZOS)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

# How each provider writes its own score (value as MDBList reports it).
_PERCENT = {"tomatoes", "popcorn", "trakt", "tmdb", "anilist", "kitsu"}


def _native(provider: str, value: float) -> str:
    if provider in _PERCENT:
        return f"{round(value)}%"
    if provider == "metacritic":
        return str(round(value))
    # IMDb, MyAnimeList and Metacritic users out of 10 (MDBList gives
    # MyAnimeList to one decimal, so no second one to show), Letterboxd out
    # of 5, Ebert out of 4.
    return f"{value:.1f}"


def score_text(provider: str, value: float, scale: str, out_of_10: bool) -> str:
    """*value* as the badge prints it: the provider's own form ("7.8", "92%",
    "3.9") or, normalised, the scale the weighted score is shown on.  The
    Posters+ score is already on that scale, so it prints as the mode would."""
    if provider == "pplus":
        score = round(value)
    elif scale == "native":
        return _native(provider, value)
    else:
        score = round(SCORE_NORMALISERS[provider](value))
    if out_of_10:
        return "10" if score >= 100 else f"{score / 10:.1f}"
    return str(score)


# Which titles a badge shows on: m(ovies), t(v), and anime split the same way,
# f(ilms) and s(eries).  Written after the provider ("imdb:mts") only when it
# is narrowed; a bare provider shows on all.  "a", from before anime was
# split, is both anime kinds, and is how both are still written.
KINDS = "mtfs"


def _kind_set(kinds: str) -> str:
    """*kinds* as written ("mta", "ts", ...) in KINDS' letters and order."""
    kinds = kinds.replace("a", "fs")
    return "".join(k for k in KINDS if k in kinds)


def _spell(kinds: str) -> str:
    """A KINDS-ordered set as written: both anime kinds as "a"."""
    return kinds.replace("fs", "a")


def _tokens(raw: str | None) -> list[tuple[str, str]]:
    """(provider, kinds) for each known provider, first mention kept, at most
    _MAX_BADGES."""
    seen: list[tuple[str, str]] = []
    for token in (raw or "").lower().replace(" ", "").split(","):
        provider, sep, kinds = token.partition(":")
        if provider in PROVIDERS and provider not in (p for p, _ in seen):
            seen.append((provider, _kind_set(kinds) if sep else KINDS))
    return seen[:_MAX_BADGES]


def parse_providers(raw: str | None) -> str:
    """rating_badges' providers in canonical spelling, without their kinds.
    "" when none."""
    return ",".join(p for p, _ in _tokens(raw))


def parse_kinds(raw: str | None) -> str:
    """The narrowed providers of rating_badges, canonically ("imdb:mt,kitsu:a",
    "letterboxd:mf"); "" when every badge shows on every kind of title."""
    return ",".join(f"{p}:{_spell(k)}" for p, k in _tokens(raw) if k != KINDS)


def for_kind(providers: str, kinds: str, kind: str) -> str:
    """``providers`` less those whose kinds (see parse_kinds) leave out
    ``kind`` — "m", "t", "f" (anime film) or "s" (anime series)."""
    if not kinds or not providers:
        return providers
    narrowed = {p: _kind_set(k) for p, k in (entry.split(":") for entry in kinds.split(","))}
    return ",".join(p for p in providers.split(",") if kind in narrowed.get(p, KINDS))


def entries(ratings: dict | None, providers: str, score=None) -> list[tuple[str, float]]:
    """(provider, value) for each chosen provider the title has a score from,
    in the chosen order; *score* is the weighted score, which "pplus" shows."""
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        ratings = {**(ratings or {}), "pplus": score}
    if not ratings or not providers:
        return []
    out = []
    for p in providers.split(","):
        value = ratings.get(p)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append((p, float(value)))
    return out


# ---------------------------------------------------------------------------
# Runs: a line of text and badges laid out left to right
# ---------------------------------------------------------------------------
#
# A run is a list of pieces — ("text", str), ("badge", Image) or ("gap", px) —
# so each mode can put the badges where its ★ was without knowing how they
# are sized or spaced.

_BADGE_ROW   = 0.95   # badge row height, of the font size
_SCORE_GAP   = 0.32   # badge → its score, of the font size
_ENTRY_GAP   = 0.55   # one score → the next badge, of the font size


def rating_run(items: list[tuple[str, float]], font_size: float, scale: str,
               out_of_10: bool, style: str = "color") -> list[tuple]:
    """The pieces for *items*.  A badge whose mark isn't on disk falls back
    to the provider's score alone rather than dropping the score.  A mono
    badge is a ("mono", white mark) piece, drawn in the text's colour."""
    mono = style == "mono"
    row_h = max(4, round(font_size * _BADGE_ROW))
    run: list[tuple] = []
    for i, (provider, value) in enumerate(items):
        if i:
            run.append(("gap", font_size * _ENTRY_GAP))
        im = badge(provider, value >= _RT_FRESH, row_h, mono)
        if im is not None:
            run += [("mono" if mono else "badge", im), ("gap", font_size * _SCORE_GAP)]
        run.append(("text", score_text(provider, value, scale, out_of_10)))
    return run


def run_width(run: list[tuple], measure) -> float:
    """Width of *run*, with *measure(text)* the width of a text piece."""
    total = 0.0
    for kind, v in run:
        total += measure(v) if kind == "text" else v.width if kind in ("badge", "mono") else v
    return total


def draw_run(image: Image.Image, draw: ImageDraw.ImageDraw, run: list[tuple], x: float,
             y: float, font, fill, measure) -> float:
    """Draw *run* with its text's top at *y* (as ImageDraw.text takes it) and
    every badge centred on the digits; a mono badge takes *fill*.  Returns
    the x it ended at."""
    l, t, r, b = font.getbbox("0")
    digit_cy = y + (t + b) / 2
    for kind, v in run:
        if kind == "text":
            draw.text((round(x), y), v, font=font, fill=fill)
            x += measure(v)
        elif kind in ("badge", "mono"):
            if kind == "mono":
                ink = tuple(fill) + (255,) * (4 - len(fill))
                tinted = Image.new("RGBA", v.size, ink[:3] + (0,))
                tinted.putalpha(Image.fromarray(
                    (np.asarray(v.getchannel("A"), np.float32) * ink[3] / 255).astype(np.uint8)))
                v = tinted
            image.alpha_composite(v, (round(x), round(digit_cy - v.height / 2)))
            x += v.width
        else:
            x += v
    return x
