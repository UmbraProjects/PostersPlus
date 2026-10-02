#tmdb.py
import asyncio
import colorsys
import hashlib
import io
from contextvars import ContextVar
import logging
import re
import time
from datetime import date as _date, datetime as _datetime, time as _time
from urllib.parse import urlsplit
import httpx
import numpy as np

import cinemeta
import tvdb

logger = logging.getLogger(__name__)
from PIL import Image, ImageFilter

# SVG title-logo support — TMDB serves many of its highest-voted logos as SVG.
# Soft import so the service still runs (PNG-only) if cairosvg is unavailable.
try:
    import cairosvg as _cairosvg
    _HAS_CAIROSVG = True
except Exception:
    _HAS_CAIROSVG = False


def svg_logo_supported() -> bool:
    """True when SVG title logos can be rasterised (cairosvg is importable)."""
    return _HAS_CAIROSVG


def _rasterize_svg(svg_bytes: bytes, target_w: int = 1000) -> "Image.Image | None":
    """Render SVG bytes to an RGBA PIL image at target_w px wide, or None on failure."""
    if not _HAS_CAIROSVG:
        return None
    try:
        png_bytes = _cairosvg.svg2png(bytestring=svg_bytes, output_width=target_w)
        return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"SVG logo rasterise failed: {exc}")
        return None

from cache import (
    get_cached_trending_snapshot,
    get_cached_trending_snapshot_entry,
    set_cached_trending_snapshot,
    expire_trending_snapshot,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_metadata,
    set_cached_tmdb_metadata,
    get_cached_release_status,
    set_cached_release_status,
    get_cached_movie_release_info,
    set_cached_movie_release_info,
    release_status_expiry,
    get_cached_tvdb_json,
    set_cached_tvdb_json,
    delete_cached_tvdb_json,
    get_cached_badge_facts,
    set_cached_badge_facts,
)

from config import (
    POSTER_WIDTH,
    POSTER_HEIGHT,
    LANDSCAPE_WIDTH,
    LANDSCAPE_HEIGHT,
    LOGO_MAX_W_RATIO,
    LOGO_MAX_H_RATIO,
    LOGO_BOTTOM_RATIO,
    LOGO_CONTRAST_RESCUE,
    LOGO_STRETCH_DISABLED,
    LOGO_STRETCH_FACTOR,
    DEBUG_LOGO_SIZING,
    TMDB_POSTER_MIN_VOTES,
    TMDB_POSTER_MAX_SCORE_DROP,
    CINEMA_MAX_AGE_YEARS,
    CINEMA_ASSUMED_DIGITAL_DAYS,
    CINEMA_POPULAR_VOTES,
    CINEMA_POPULAR_DIGITAL_DAYS,
    TRENDING_SOURCE_MOVIE,
    TRENDING_SOURCE_TV,
    TRENDING_SOURCE_ANIME,
    TRENDING_SOURCE_ANIME_MOVIE,
    TRENDING_SOURCE_MAX_ITEMS,
    TRENDING_HIDE_UNRELEASED,
    TRENDING_FETCH_COUNT,
    TRENDING_BROAD_FETCH_COUNT,
    CINEMETA_ENABLED,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

# The portrait canvas this request renders at.  500x750 unless the request asks
# for the larger size (RequestConfig.poster_width); get_poster sets it once and
# every art fetcher below reads it, so the right TMDB size is fetched, cached
# under its own key and fitted to the right canvas.  A ContextVar rather than a
# parameter threaded through each fetcher: it follows the request's task and the
# tasks it spawns.  It does NOT reach run_in_executor threads, so the code that
# normalises in the thread pool takes the size as an argument.
_POSTER_CANVAS: ContextVar[tuple[int, int]] = ContextVar(
    "poster_canvas", default=(POSTER_WIDTH, POSTER_HEIGHT)
)

# Canvas widths a request may ask for, and the TMDB poster size fetched for
# each — the smallest TMDB rendition at least as wide as the canvas.  Above
# 780 that is the original (usually 2000x3000), shrunk to the canvas here.
# Measured cost per render vs 500 (2026-09-26): 780 ~2x, 1000 ~3x, 1500 ~7x,
# 2000 ~12x in CPU and file size; 2000 renders peaked near 1.75 GiB on one
# worker.  The larger sizes are here to be tried, not yet to be offered.
POSTER_WIDTHS = {500: "w500", 780: "w780", 1000: "original", 1500: "original", 2000: "original"}


def poster_canvas() -> tuple[int, int]:
    return _POSTER_CANVAS.get()


def set_poster_canvas(width: int) -> None:
    """Render this request's portrait art at *width* (a POSTER_WIDTHS key), 2:3."""
    _POSTER_CANVAS.set((width, width * POSTER_HEIGHT // POSTER_WIDTH))


def _canvas_suffix(size: tuple[int, int]) -> str:
    """Cache-key suffix for art cached at *size*.  Empty at the standard size,
    so every key cached before sizes existed stays valid."""
    return "" if size == (POSTER_WIDTH, POSTER_HEIGHT) else f"_{size[0]}x{size[1]}"


def normalise_poster(image: Image.Image, size: tuple[int, int] | None = None) -> Image.Image:
    target_w, target_h = size or poster_canvas()
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


# Black-ink lightening (ensure_light_logo).  A pixel is "black ink" below
# _INK_V_BLACK on its brightest channel, and fades out of the recolour between
# that and _INK_V_RAMP so a dark-to-colour gradient has no seam.  Brightest
# channel rather than luminance: pure blue or red has a luminance as low as
# black's but reads on a dark poster, while a near-black with a faint tint
# stays dark on every channel.
_INK_V_BLACK = 90.0
_INK_V_RAMP  = 130.0
# The ink has to be most of the logo's solid pixels...
_INK_MIN_FRAC = 0.5
# ...and has to sit on transparency, not against the logo's own content: grown
# by _INK_GROW px, it may run into at most this share of opaque non-black
# pixels.  Black outlines round a coloured fill, black cards behind white text
# and black shadows under a light face all fail it.
_INK_MAX_ENCLOSE = 0.25
_INK_GROW = 2


def ensure_light_logo(logo: Image.Image) -> Image.Image:
    """
    Lighten a logo's black ink so it reads on a dark poster, and leave every
    other logo untouched.  Only the near-black pixels change: their lightness
    is flipped (black → white, dark grey → light grey, navy → pale blue) with
    hue kept, while coloured accents and light parts keep their own colours —
    BEASTARS keeps its red B, black·ish its green "ish".

    Two gates decide whether a logo is black ink at all (see _INK_MIN_FRAC and
    _INK_MAX_ENCLOSE): the black has to be most of the logo, and it has to
    border transparency rather than the logo's own coloured or light parts.
    A black outline round a coloured fill, a black card behind white letters,
    or a black extrusion under a grey face is structure the logo relies on,
    so such logos are left as they are.  Statistics use solid pixels
    (alpha >= 128); the recolour covers every visible pixel (alpha > 30) so
    anti-aliased edges lighten with the ink.
    """
    rgba = np.asarray(logo.convert("RGBA"), dtype=np.float32)
    alpha = rgba[:, :, 3]
    solid = alpha >= 128
    n_solid = int(solid.sum())
    if n_solid < 20:
        return logo

    rgb = rgba[:, :, :3]
    v = rgb.max(axis=2)
    ink = solid & (v < _INK_V_BLACK)
    ink_frac = float(ink.sum()) / n_solid
    if ink_frac < _INK_MIN_FRAC:
        return logo

    grown = ink.copy()
    for _ in range(_INK_GROW):
        step = grown.copy()
        step[1:] |= grown[:-1]
        step[:-1] |= grown[1:]
        step[:, 1:] |= grown[:, :-1]
        step[:, :-1] |= grown[:, 1:]
        grown = step
    touch_other = int((grown & solid & (v >= _INK_V_RAMP)).sum())
    touch_clear = int((grown & ~solid).sum())
    enclose = touch_other / max(1, touch_other + touch_clear)
    if enclose > _INK_MAX_ENCLOSE:
        logger.debug(
            f"ensure_light_logo: skip (ink {ink_frac:.0%}, borders its own "
            f"content {enclose:.0%}) — outline, card or shadow"
        )
        return logo

    # HLS lightness flip, per pixel, keeping hue and HLS saturation.
    c = rgb / 255.0
    hi = c.max(axis=2)
    lo = c.min(axis=2)
    span = hi - lo
    light = (hi + lo) / 2
    sat = np.where(span > 0, span / np.maximum(1e-6, 1 - np.abs(2 * light - 1)), 0.0)
    flipped = 1 - light
    chroma = (1 - np.abs(2 * flipped - 1)) * sat
    rel = np.where(span[..., None] > 0,
                   (c - lo[..., None]) / np.maximum(1e-6, span)[..., None], 0.5)
    lit = (flipped - chroma / 2)[..., None] + rel * chroma[..., None]

    weight = np.clip((_INK_V_RAMP - v) / (_INK_V_RAMP - _INK_V_BLACK), 0.0, 1.0)
    weight = (weight * (alpha > 30))[..., None]
    out = rgba.copy()
    out[:, :, :3] = (weight * lit + (1 - weight) * c) * 255.0
    logger.debug(f"ensure_light_logo: lightening black ink (ink {ink_frac:.0%}, "
                 f"borders its own content {enclose:.0%})")
    return Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8))


# Experimental contrast-rescue tuning.  Lower = more conservative (only recolour
# when the logo and background colours are very close).  Set to 0 to disable.
LOGO_CONTRAST_MIN = 0.25   # normalised RGB distance (0–1) below which we recolour
# Logos whose internal colour spread exceeds this are left alone — multi-colour
# logos (Mario) or outline+fill logos (Archer) rely on their own colours for
# legibility and would be ruined by a flat recolour.
LOGO_COLOR_VARIANCE_MAX = 0.16
# When a flat logo is recoloured, default to white and only switch to black on
# genuinely light backgrounds (white reads well on most posters).
LOGO_DARK_TEXT_LUM = 0.66   # background luminance above which black is used
# In the mid-luminance band (where plain white/black are weakest) a flat logo
# may instead be recoloured to the COMPLEMENTARY hue of the background, forced
# to an extreme value for guaranteed luminance contrast.  Only used when the
# background has a clear dominant hue — greyscale backgrounds fall back to
# white/black.  Set the band to (0, 0) to disable accents entirely.
LOGO_ACCENT_LUM_BAND = (0.40, 0.66)   # bg-luminance window for accent colours
LOGO_ACCENT_MIN_SAT  = 0.25           # bg must be at least this saturated

# Logos are normalised to one overall size (the geometric mean of the Width and
# Height caps), then clamped to those caps preserving aspect ratio.  Both caps
# are HARD ceilings — the configured ratios are the true maximums.  PIVOT is the
# width:height ratio treated as "neutral" (a typical title logo is wider than
# tall); it's used only to label logo orientation in the sizing telemetry.
LOGO_ASPECT_PIVOT = 2.8    # neutral aspect (wider → "wide", narrower → "tall")
# Absolute pixel ceiling on rendered logo height — a hard stop so a tall, only
# moderately-wide logo can never dominate the poster, regardless of the Height
# ratio slider or aspect flex.  ~25 % of a 750 px poster.
LOGO_ABS_MAX_H = 170
# Single-axis fill stretch: a slim logo whose under-cap dimension would leave it
# looking lost may be stretched up to this factor toward its cap (width OR
# height, never both).  Height stays bounded by LOGO_ABS_MAX_H.  Env-tunable via
# LOGO_STRETCH_FACTOR; skipped entirely when LOGO_STRETCH_DISABLED is set.
LOGO_FILL_STRETCH = LOGO_STRETCH_FACTOR
# The height stretch only fires when the logo's clamped height is below this
# fraction of its height cap — i.e. only genuinely short/slim logos are lifted,
# while normally-proportioned logos are left at their true aspect ratio.
LOGO_FILL_HEIGHT_TRIGGER = 0.6


def logo_centre_y(height: int, bottom_ratio: float = LOGO_BOTTOM_RATIO) -> int:
    """
    Vertical centre line that composite_logo aligns logos to.  Exposed so the
    fallback title-text renderer can sit on the exact same line, keeping logo
    and text posters visually consistent.
    """
    max_h = min(int(height * LOGO_MAX_H_RATIO), LOGO_ABS_MAX_H * height // POSTER_HEIGHT)
    return int(height - int(height * bottom_ratio) - max_h / 2)


def _recolor_target(bg_rgb: tuple[float, float, float],
                    bg_lum: float) -> tuple[tuple[int, int, int], str]:
    """
    Choose the colour to recolour a flat logo to, given the background under it.

    Returns (rgb, label).  In the mid-luminance band, a saturated background
    yields the complementary hue pushed to an extreme value (dark accent over a
    lighter bg, light accent over a darker bg) so contrast stays high while the
    tint ties to the poster.  Outside the band, or on greyscale backgrounds,
    falls back to white (default) or black (very light backgrounds).
    """
    r, g, b = bg_rgb[0] / 255, bg_rgb[1] / 255, bg_rgb[2] / 255
    h, s, _v = colorsys.rgb_to_hsv(r, g, b)

    lo, hi = LOGO_ACCENT_LUM_BAND
    if lo < hi and lo <= bg_lum <= hi and s >= LOGO_ACCENT_MIN_SAT:
        comp_h = (h + 0.5) % 1.0
        comp_v = 0.30 if bg_lum >= 0.50 else 0.95   # opposite side of bg luminance
        cr, cg, cb = colorsys.hsv_to_rgb(comp_h, 0.85, comp_v)
        return (int(cr * 255), int(cg * 255), int(cb * 255)), "accent"

    if bg_lum > LOGO_DARK_TEXT_LUM:
        return (20, 20, 20), "black"
    return (255, 255, 255), "white"


def _logo_color_stats(logo: Image.Image) -> tuple[tuple[float, float, float], float] | None:
    """
    Return ((mean_r, mean_g, mean_b), variance) for the logo's opaque pixels.

    variance is the mean normalised RGB distance of pixels from the mean colour
    (0–1).  Low → flat single-colour logo (safe to recolour); high → multi-colour
    or outline+fill logo whose own colours carry its legibility.
    Returns None when the logo has no opaque pixels.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)
    vis = rgba[:, :, 3] > 64
    if not vis.any():
        return None
    rgb  = rgba[:, :, :3][vis]                       # N×3
    mean = rgb.mean(axis=0)
    var  = float(np.sqrt(((rgb - mean) ** 2).sum(axis=1)).mean() / 441.673)
    return (float(mean[0]), float(mean[1]), float(mean[2])), var


def _recolor_logo_solid(logo: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Force all visible logo pixels to a solid colour, preserving alpha."""
    rgba = np.array(logo.convert("RGBA"))
    vis = rgba[:, :, 3] > 30
    rgba[:, :, 0][vis] = rgb[0]
    rgba[:, :, 1][vis] = rgb[1]
    rgba[:, :, 2][vis] = rgb[2]
    return Image.fromarray(rgba)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def tmdb_metadata_cache_key(
    endpoint: str, tmdb_id: str, logo_language: str, secondary_language: str = ""
) -> str:
    selection_sig = (
        f"p{TMDB_POSTER_MIN_VOTES}"
        f"d{TMDB_POSTER_MAX_SCORE_DROP:g}"
    )
    base = f"{endpoint}_{tmdb_id}_{logo_language}_{selection_sig}"
    # A secondary preferred language changes the image set fetched from TMDB, so
    # it must key separately.  Suffixed (not inlined) so existing single-language
    # cache entries keep their key and don't all miss on deploy.
    return f"{base}_s{secondary_language}" if secondary_language else base


# Minimum null-language posters before a runner-up is kept as an alternate.
TEXTLESS_ALT_MIN_POSTERS = 6


# Candidates kept per pool for the random top-5 pick (poster_pick=random).
POSTER_POOL_SIZE = 5


def _rank_textless_posters(posters: list[dict]) -> list[dict]:
    """Textless posters in pick order: _select_textless_poster's choice first,
    then well-voted competitive art, then competitive, then the rest."""
    if not posters:
        return []
    best = _select_textless_poster(posters)
    top_rating = max(float(p.get("vote_average") or 0) for p in posters)

    def _key(poster: dict):
        rating = float(poster.get("vote_average") or 0)
        votes = int(poster.get("vote_count") or 0)
        competitive = rating >= top_rating - TMDB_POSTER_MAX_SCORE_DROP
        voted = votes >= TMDB_POSTER_MIN_VOTES
        return (poster is not best, not (competitive and voted), not competitive,
                -rating, -votes)

    return sorted(posters, key=_key)


def _select_textless_poster(posters: list[dict]) -> dict | None:
    """Prefer sufficiently voted art without accepting a large score downgrade."""
    if not posters:
        return None

    def _rating(poster: dict) -> float:
        return float(poster.get("vote_average") or 0)

    def _votes(poster: dict) -> int:
        return int(poster.get("vote_count") or 0)

    best_rating = max(_rating(poster) for poster in posters)
    competitive = [
        poster for poster in posters
        if _rating(poster) >= best_rating - TMDB_POSTER_MAX_SCORE_DROP
    ]
    voted = [
        poster for poster in competitive
        if _votes(poster) >= TMDB_POSTER_MIN_VOTES
    ]
    return max(
        voted or competitive,
        key=lambda poster: (_rating(poster), _votes(poster)),
    )


# TMDB files TV under one "Sci-Fi & Fantasy" genre (10765) where films get two
# (878, 14), so every fantasy series printed as "Sci-Fi".  The show's TMDB
# keywords, fetched in the same details call, nearly always say which it is:
# counted on a sample of the 140 most-voted shows in the genre, they settled
# ~85% and read right on ~90% of those.  Terms match at word starts; a
# trailing "*" also takes longer words (dystopia/dystopian).
_SCIFI_TERMS = (
    "science fiction", "sci-fi", "space", "alien", "extraterrestrial",
    "spaceship", "spacecraft", "starship", "time travel", "time machine",
    "robot", "android", "cyborg", "artificial intelligence", "cyberpunk",
    "dystopi*", "futuristic", "future", "clone", "cloning", "virtual reality",
    "mecha", "genetic*", "mutant", "mutation", "post-apocalyp*", "planet",
    "galaxy", "interstellar", "virus", "scientist", "experiment",
    "simulation", "nanotech*", "teleport*", "multiverse",
)
_FANTASY_TERMS = (
    "fantasy", "magic", "dragon", "wizard", "witch*", "sorcer*", "mytholog*",
    "myth", "fairy", "fairy tale", "elf", "elves", "supernatural", "vampire",
    "werewolf", "ghost", "demon", "angel", "curse", "medieval", "sword*",
    "legend", "folklore", "spirit", "afterlife", "hell", "heaven", "god",
    "immortal*", "prophecy", "occult", "necromanc*", "shapeshift*",
)


def _term_pattern(terms: tuple[str, ...]) -> "re.Pattern[str]":
    parts = [
        re.escape(t[:-1]) + r"\w*" if t.endswith("*") else re.escape(t) + r"s?"
        for t in terms
    ]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


_SCIFI_RE = _term_pattern(_SCIFI_TERMS)
_FANTASY_RE = _term_pattern(_FANTASY_TERMS)

TV_SCIFI_FANTASY = 10765
_SCIFI, _FANTASY = 878, 14


def _scifi_or_fantasy(keywords: list[str], imdb_genres: list[str] | None = None) -> int | None:
    """878 or 14 for a show TMDB files as 10765, or None when nothing
    decides it.  Keywords first; IMDb's genres only break a tie, because
    IMDb keeps three per title and most of these shows spend them on
    Action/Adventure/Drama."""
    sci = sum(1 for k in keywords if _SCIFI_RE.search(k.lower()))
    fan = sum(1 for k in keywords if _FANTASY_RE.search(k.lower()))
    if sci != fan:
        return _SCIFI if sci > fan else _FANTASY
    imdb = {g.strip().lower() for g in imdb_genres or ()}
    has_sci = bool(imdb & {"sci-fi", "science fiction"})
    has_fan = "fantasy" in imdb
    if has_sci != has_fan:
        return _SCIFI if has_sci else _FANTASY
    return None


async def _split_tv_scifi_fantasy(
    client: httpx.AsyncClient,
    genre_ids: list[int],
    keywords: list[str],
    imdb_id: str | None,
) -> list[int]:
    """Replace 10765 with 878 or 14 where the show says which.  Unresolved
    shows keep 10765, which still reads "Sci-Fi" as before."""
    if TV_SCIFI_FANTASY not in genre_ids:
        return genre_ids
    pick = _scifi_or_fantasy(keywords)
    if pick is None and imdb_id and CINEMETA_ENABLED:
        # Cached for a week and keyless; only reached on a keyword tie.
        meta = await cinemeta.fetch_cinemeta_meta(client, imdb_id, "tv")
        if meta:
            pick = _scifi_or_fantasy([], meta.get("genres") or meta.get("genre") or [])
    if pick is None:
        return genre_ids
    out: list[int] = []
    for gid in genre_ids:
        gid = pick if gid == TV_SCIFI_FANTASY else gid
        if gid not in out:
            out.append(gid)
    return out


# TMDB's TV genre list has no Horror at all, so American Horror Story read
# "Fantasy".  main.py gives a show Horror from MDBList's genres (they ride on
# the ratings call already made) whenever it has them; this is the answer for
# the cache, used until MDBList has spoken and by servers without a key.
# Checked on the 300 most-voted shows against MDBList, TVDB and Cinemeta
# (Horror where two of three agree): a "horror" keyword caught 74% of the 19
# horror shows at 67% precision, the extras debatable (Black Mirror, iZombie);
# slasher/zombie/haunt added nothing.  TVDB tags Horror twice as freely as the
# others (Death Note, Twin Peaks), Cinemeta read 89% / 94%, so a show with
# no keywords asks Cinemeta first and TVDB only when Cinemeta can't answer.
TV_HORROR = 27
_HORROR_KEYWORD_RE = re.compile(r"\bhorror\b")


async def _tv_is_horror(
    client: httpx.AsyncClient,
    keywords: list[str],
    imdb_id: str | None,
    tvdb_id: int | str | None,
) -> bool | None:
    """Whether a TMDB TV show is horror, short of MDBList: its keywords when it
    has any, else Cinemeta's genres (IMDb's), else TVDB's (with a key).  None
    when it can't be told because Cinemeta was unreachable and TVDB didn't
    answer, so the caller doesn't cache "no" for the week."""
    if keywords:
        return any(_HORROR_KEYWORD_RE.search(k.lower()) for k in keywords)
    names: list | None = None
    cinemeta_down = False
    if imdb_id and CINEMETA_ENABLED:
        # Cached for a week and keyless; the same document the Sci-Fi/Fantasy
        # tie-break reads.
        meta = await cinemeta.fetch_cinemeta_meta(client, imdb_id, "tv")
        if meta:
            names = meta.get("genres") or meta.get("genre") or []
        else:
            # A real miss is negatively cached; an outage leaves nothing.
            cinemeta_down = get_cached_tvdb_json(cinemeta._cache_key(imdb_id, "tv")) is None
    if names is None and tvdb_id and tvdb.tvdb_enabled():
        # Last, because TVDB tags Horror freely; it still knows shows that
        # Cinemeta doesn't (no IMDb id, or no entry).
        try:
            record = await tvdb._fetch_record(client, int(tvdb_id), "series")
        except Exception as exc:
            logger.warning(f"TVDB genre lookup failed for tvdb_id={tvdb_id}: {exc}")
            record = None
        if record:
            names = [g.get("name") for g in record.get("genres") or [] if isinstance(g, dict)]
    if names is None and cinemeta_down:
        return None
    return any((n or "").strip().lower() == "horror" for n in names or ())


async def fetch_poster_metadata(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
    logo_language: str = "en",
    secondary_language: str = "",
) -> tuple[list[int], bool, list[dict], str | None, str, str, str | None, dict]:
    """
    Fetch (or return cached) TMDB metadata, including credits,
    production_companies, and original_language for discovery sash logic.

    Returns:
        (genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data)
    """
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    # Key by logo_language too: the images fetched (logos + posters) depend on it,
    # so a title cached under one language must not be served to another without
    # that language's art.  Each language gets its own correctly-fetched entry.
    metadata_cache_key = tmdb_metadata_cache_key(
        endpoint, tmdb_id, logo_language, secondary_language
    )

    meta = get_cached_tmdb_metadata(metadata_cache_key)

    if meta:
        logger.info(f"TMDB metadata cache hit for {tmdb_id}")
        tmdb_data = {
            "credits":               meta.get("credits", {}),
            "production_companies":  meta.get("production_companies", []),
            "original_language":     meta.get("original_language"),
            "original_title":        meta.get("original_title"),
            "runtime":               meta.get("runtime"),
            "number_of_seasons":     meta.get("number_of_seasons"),
            "number_of_episodes":    meta.get("number_of_episodes"),
            "tmdb_status":           meta.get("tmdb_status"),
            "vote_count":            meta.get("vote_count"),
            "vote_average":          meta.get("vote_average"),
            "text_backdrop_path":    meta.get("text_backdrop_path"),
            "alt_poster_path":       meta.get("alt_poster_path"),
            "original_poster_path":  meta.get("original_poster_path"),
            "poster_langs":          meta.get("poster_langs", {}),
            "poster_pools":          meta.get("poster_pools", {}),
            "imdb_id":               meta.get("imdb_id"),
            "tmdb_release_date":     meta.get("tmdb_release_date"),
            "last_air_date":         meta.get("last_air_date"),
            "next_episode":          meta.get("next_episode"),
            "last_episode":          meta.get("last_episode"),
            "seasons":               meta.get("seasons", []),
        }
        return (
            meta["genre_ids"],
            meta["is_textless"],
            meta["logos"],
            meta["release_year"],
            meta["title"],
            meta["poster_path"],
            meta.get("backdrop_path"),
            tmdb_data,
        )

    # Build include_image_language so TMDB returns:
    #   null  — language-neutral entries (TMDB's signal for textless/unspecified)
    #   en    — English (logos + fallback posters)
    #   logo_language — non-English logo candidates when requested
    # For regional locales (fr-fr), TMDB image rows are still language-tagged
    # with iso_639_1=fr and iso_3166_1=FR, so the API request must include the
    # base language too. The later selector remains strict and rejects fr-CA for
    # a fr-fr request.
    # Note: null-language ≠ guaranteed text-free; TMDB uses it for both truly
    # textless art and posters where the language simply wasn't catalogued.
    _img_langs = ",".join(_tmdb_include_image_languages(logo_language, secondary_language))

    logger.info(f"External API Call: Requested meta from TMDB for {tmdb_id}")
    resp = await client.get(
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
        params={
            "api_key": tmdb_key,
            # Keywords settle TV's merged Sci-Fi & Fantasy genre (see
            # _split_tv_scifi_fantasy); films have the two apart already.
            "append_to_response": "images,credits,external_ids"
                                  + (",keywords" if endpoint == "tv" else ""),
            "include_image_language": _img_langs,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    # imdb_id from external_ids — used by cache warming to look up MDBList
    # ratings/awards without a separate API call. TV's external_ids also
    # includes imdb_id (the show's IMDb entry).
    imdb_id = (data.get("external_ids") or {}).get("imdb_id") or None

    original_title = data.get("original_title") or data.get("original_name")

    title = (
        data.get("title")
        or data.get("name")
        or data.get("original_title")
        or data.get("original_name")
        or "Unknown Title"
    )

    raw_date = data.get("release_date") or data.get("first_air_date") or ""
    release_year: str | None = raw_date[:4] if len(raw_date) >= 4 else None

    images    = data.get("images", {})
    posters   = images.get("posters", [])
    logos     = images.get("logos", [])
    backdrops = images.get("backdrops", [])

    # iso_639_1 is None (JSON null) for most textless entries;
    # older TMDB records occasionally use "" (empty string) for the same thing.
    textless = [p for p in posters if p.get("iso_639_1") in (None, "")]

    if textless:
        best = _select_textless_poster(textless)
        poster_path = best["file_path"]
        is_textless = True
    else:
        poster_path = data.get("poster_path")
        is_textless = False

    # Runner-up textless poster, tried once when the pick turns out to have
    # burned-in text.  Only for large pools, where one uploader can't own most
    # of the textless set.  The scan runs on the request path (the backdrop
    # fallback still follows if it fails), so it's never more than one.
    alt_poster_path: str | None = None
    if len(textless) >= TEXTLESS_ALT_MIN_POSTERS:
        alt_poster_path = _select_textless_poster(
            [p for p in textless if p is not best]
        )["file_path"]

    if not poster_path:
        logger.warning(f"No poster image on TMDB for tmdb_id={tmdb_id} — fallback canvas will be served")
        is_textless = False  # no art, no point fetching logos
        # poster_path stays None; get_poster will generate a fallback canvas

    # TMDB's primary poster (title/logo baked into the art).  Captured separately
    # from the textless selection above so "original art" mode can serve it as-is
    # — skipping our own logo — even when a textless poster also exists.
    original_poster_path = data.get("poster_path")

    # Best backdrop — only consider null/unspecified language entries, which are
    # the ones TMDB marks as language-neutral (almost always textless).
    # Backdrops with an explicit language tag frequently have title text burned in,
    # so we ignore them entirely rather than risk a borked crop.
    # backdrop_path stays None if no null-language backdrop exists, which suppresses
    # the backdrop fallback path in main.py.
    backdrop_candidates = [b for b in backdrops if b.get("iso_639_1") in (None, "")]
    if backdrop_candidates:
        best_backdrop = max(backdrop_candidates, key=lambda x: x.get("vote_average", 0))
        backdrop_path: str | None = best_backdrop["file_path"]
    else:
        backdrop_path = None

    # Best TEXT-bearing (language-tagged) backdrop — the last-resort landscape
    # source for titles with no textless poster or backdrop.  Only used by the
    # text-aware crop rescue (gated behind TEXTLESS_TEXT_DETECTION) which crops
    # away the title text; never used by the default pipeline.
    _text_backdrops = [b for b in backdrops if b.get("iso_639_1") not in (None, "")]
    text_backdrop_path: str | None = (
        max(_text_backdrops, key=lambda x: x.get("vote_average", 0))["file_path"]
        if _text_backdrops else None
    )

    genre_ids            = [g["id"] for g in data.get("genres", [])]
    _genres_unsettled    = False
    if endpoint == "tv":
        # TV keywords sit under "results" (films use "keywords").
        _kw = [k.get("name") or "" for k in (data.get("keywords") or {}).get("results", [])]
        genre_ids = await _split_tv_scifi_fantasy(client, genre_ids, _kw, imdb_id)
        _horror = await _tv_is_horror(
            client, _kw, imdb_id, (data.get("external_ids") or {}).get("tvdb_id"),
        )
        if _horror:
            genre_ids.append(TV_HORROR)
        _genres_unsettled = _horror is None
    credits              = data.get("credits", {})
    production_companies = data.get("production_companies", [])
    original_language    = data.get("original_language")
    runtime              = data.get("runtime")
    number_of_seasons    = data.get("number_of_seasons")
    number_of_episodes   = data.get("number_of_episodes")
    tmdb_status          = data.get("status")   # e.g. "Released", "In Production", "Returning Series"
    vote_count           = data.get("vote_count")
    # The title's own aggregate score, straight from the same details call
    # already made for genre/year/credits — no extra API request. 0-10 scale,
    # same as TMDB's UI. Lets the "tmdb" rating weight be sourced without
    # MDBList when tmdb_rating_source=direct (see main._merge_direct_tmdb_rating).
    vote_average         = data.get("vote_average")
    tmdb_release_date    = raw_date or None
    last_air_date        = data.get("last_air_date")
    next_episode         = data.get("next_episode_to_air") or None
    last_episode         = data.get("last_episode_to_air") or None
    seasons              = data.get("seasons") or []

    # If the content's original language wasn't included in the initial image
    # request (e.g. a Romanian show fetched by an English-language user), TMDB
    # won't return native-language logos.  Do a cheap supplemental /images call
    # so we can cache those logos alongside the rest.  Skipped when the original
    # language is already covered by _img_langs (en or user's logo_language).
    # Fire when the original-language logos OR posters aren't already covered —
    # original-art mode needs the original-language poster (e.g. the Spanish
    # poster for a Spanish film) to honour poster-language priority.
    _covered = {logo_language, "en"}
    if secondary_language:
        _covered.add(secondary_language)
    _have_orig_logos   = any(lg.get("iso_639_1") == original_language for lg in logos)
    _have_orig_posters = any(p.get("iso_639_1")  == original_language for p in posters)
    if (
        original_language
        and original_language not in _covered
        and not (_have_orig_logos and _have_orig_posters)
    ):
        try:
            logger.info(
                f"Fetching supplemental {original_language} images for {tmdb_id}"
            )
            supp = await client.get(
                f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/images",
                params={
                    "api_key":                tmdb_key,
                    "include_image_language": original_language,
                },
            )
            if supp.status_code == 200:
                _supp = supp.json()
                supp_logos   = _supp.get("logos", [])
                supp_posters = _supp.get("posters", [])
                logos   = logos + supp_logos
                posters = posters + supp_posters
                logger.info(
                    f"Added {len(supp_logos)} {original_language} logo(s) and "
                    f"{len(supp_posters)} poster(s) for {tmdb_id}"
                )
        except Exception as exc:
            logger.warning(f"Supplemental image fetch failed for {tmdb_id}: {exc}")

    # Original-art mode picks a TEXTUAL poster by language at RENDER time (so it
    # honours the request's native language, not the fetch-time one). Store the
    # best language-tagged poster per locale key (e.g. fr-fr) and base language
    # (e.g. fr), excluding null/"" textless entries.
    poster_langs: dict[str, str] = {}
    _poster_best_vote: dict[str, float] = {}
    for _p in posters:
        _pv = _p.get("vote_average") or 0
        for _pl in _image_language_keys(_p):
            if _pl not in poster_langs or _pv > _poster_best_vote[_pl]:
                poster_langs[_pl] = _p["file_path"]
                _poster_best_vote[_pl] = _pv

    # Top candidates for the random pick (poster_pick=random): textless in pick
    # order, and per language key by rating.  poster_langs above stays the
    # single best, so a random pool never changes the default pick.
    _lang_pools: dict[str, list[str]] = {}
    for _p in sorted(posters, key=lambda p: -(p.get("vote_average") or 0)):
        for _pl in _image_language_keys(_p):
            _pool = _lang_pools.setdefault(_pl, [])
            if len(_pool) < POSTER_POOL_SIZE:
                _pool.append(_p["file_path"])
    poster_pools = {
        "textless": [
            p["file_path"] for p in _rank_textless_posters(textless)[:POSTER_POOL_SIZE]
        ],
        "langs": _lang_pools,
        # Languages with a text-bearing backdrop, so an operator's per-language
        # landscape choice (art_overrides) knows where TMDB already has one.
        # Rows cached before this was kept lack it; see main's landscape hook.
        "backdrop_langs": sorted({
            key for b in _text_backdrops for key in _image_language_keys(b)
        }),
    }

    # A show whose Horror couldn't be told (Cinemeta down) isn't cached: the
    # render is provisional, and the next one asks again.
    if not _genres_unsettled:
        await asyncio.to_thread(
            set_cached_tmdb_metadata,
            metadata_cache_key,
            title,
            release_year,
            genre_ids,
            is_textless,
            poster_path,
            logos,
            credits=credits,
            production_companies=production_companies,
            original_language=original_language,
            original_title=original_title,
            runtime=runtime,
            number_of_seasons=number_of_seasons,
            number_of_episodes=number_of_episodes,
            backdrop_path=backdrop_path,
            tmdb_status=tmdb_status,
            vote_count=vote_count,
            vote_average=vote_average,
            text_backdrop_path=text_backdrop_path,
            alt_poster_path=alt_poster_path,
            original_poster_path=original_poster_path,
            poster_langs=poster_langs,
            poster_pools=poster_pools,
            imdb_id=imdb_id,
            tmdb_release_date=tmdb_release_date,
            last_air_date=last_air_date,
            next_episode=next_episode,
            last_episode=last_episode,
            seasons=seasons,
        )
    else:
        logger.info(f"TMDB metadata for tv {tmdb_id} not cached: Horror unsettled (Cinemeta unreachable)")

    tmdb_data = {
        "credits":              credits,
        "production_companies": production_companies,
        "original_language":    original_language,
        "original_title":       original_title,
        "runtime":              runtime,
        "number_of_seasons":    number_of_seasons,
        "number_of_episodes":   number_of_episodes,
        "tmdb_status":          tmdb_status,
        "vote_count":           vote_count,
        "vote_average":         vote_average,
        "text_backdrop_path":   text_backdrop_path,
        "alt_poster_path":      alt_poster_path,
        "original_poster_path": original_poster_path,
        "poster_langs":         poster_langs,
        "poster_pools":         poster_pools,
        "imdb_id":              imdb_id,
        "tmdb_release_date":    tmdb_release_date,
        "last_air_date":        last_air_date,
        "next_episode":         next_episode,
        "last_episode":         last_episode,
        "seasons":              seasons,
        "genres_unsettled":     _genres_unsettled,
    }

    return genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data


# ---------------------------------------------------------------------------
# Art cache keys
#
# Shared by the fetchers below and by main.py's deferred text-detection queue,
# which has to name the cached image a fetcher wrote without re-deriving the
# scheme. Absolute urls (anime providers, Cinemeta/Metahub) are hashed because
# they contain characters that don't belong in a filename; TMDB paths are used
# as-is so every existing cache entry keeps its key.
# ---------------------------------------------------------------------------

def is_absolute_art(path: str | None) -> bool:
    return bool(path) and path.startswith(("http://", "https://"))


def _art_token(path: str) -> str:
    # An operator's stored image ("custom:<hash>.jpg") is hashed like a url.
    if is_absolute_art(path) or path.startswith("custom:"):
        return hashlib.sha256(path.encode()).hexdigest()[:16]
    return path.strip("/")


def _id_token(tmdb_id: str) -> str:
    # A stand-in id ("kitsu:12345") carries a colon; replaced so the key is a
    # portable filename on every filesystem. Numeric TMDB ids are unchanged.
    return tmdb_id.replace(":", "_")


def poster_image_cache_key(tmdb_id: str, media_type: str, poster_path: str) -> str:
    # Stremio asks for "series", the warmer and TMDB say "tv": one file either way.
    kind = "tv" if media_type == "series" else media_type
    return f"{kind}_{_id_token(tmdb_id)}_{_art_token(poster_path)}{_canvas_suffix(poster_canvas())}"


def backdrop_image_cache_key(tmdb_id: str, backdrop_path: str, avoid_text: bool) -> str:
    # Carries the crop-logic version so changing the crop algorithm invalidates
    # previously-cached crops instead of serving the old framing.
    return (
        f"backdrop_{_id_token(tmdb_id)}_{_art_token(backdrop_path)}_{_CROP_VERSION}"
        + ("_ta" if avoid_text else "")
        + _canvas_suffix(poster_canvas())
    )


def landscape_image_cache_key(tmdb_id: str, backdrop_path: str) -> str:
    return (
        f"landscape_{_id_token(tmdb_id)}_{_art_token(backdrop_path)}"
        f"_{LANDSCAPE_WIDTH}x{LANDSCAPE_HEIGHT}"
    )


def _cached_art(cache_key: str, size: tuple[int, int], fit) -> "Image.Image | None":
    """A cached art file as RGBA, fitted to *size* when it was stored at
    another, or None on a miss.  Blocking (a file read and a decode); run
    through asyncio.to_thread, which carries the request's canvas along."""
    cached_bytes = get_cached_tmdb_poster(cache_key)
    if not cached_bytes:
        return None
    image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
    if image.size != size:
        image = fit(image)
    return image


def _store_art(cache_key: str, image: Image.Image) -> None:
    """Cache *image* as JPEG q92 RGB (no alpha needed for base art; restoring
    it on load is free).  Blocking: an encode and an fsync'd write."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())


class BlankArtError(Exception):
    """The image CDN answered 200 with a flat single-colour image."""


def _is_blank_art(content: bytes) -> bool:
    """True for an image with no detail at all — a flat colour field.

    TMDB's CDN now and then serves an all-black JPEG of the right size for a
    rendition (seen for w780 posters: 5-7 KB, every pixel 0), while the other
    sizes of the same image are fine.  Cached, the black one became the
    title's art at that canvas size for the whole cache window.
    Real posters are never this flat; a draft decode keeps the check cheap.
    """
    try:
        image = Image.open(io.BytesIO(content))
        image.draft("L", (64, 64))
        image = image.convert("L")
        image.thumbnail((64, 64))
        low, high = image.getextrema()
        return high - low < 4
    except Exception:
        return False   # an undecodable body fails later, in the caller


async def _get_art(client: httpx.AsyncClient, url: str, **kwargs) -> bytes:
    """Download image bytes, rejecting a blank body (see _is_blank_art).

    A TMDB url whose rendition comes back blank is asked for again at another
    size: the blank one tends to stick to a single rendition for a while, and
    every caller resizes to its canvas anyway.  All of them blank raises
    BlankArtError, so nothing blank is ever cached."""
    urls = [url]
    m = re.match(r"(https://image\.tmdb\.org/t/p/)([a-z0-9]+)(/.+)\Z", url)
    if m:
        urls += [f"{m.group(1)}{size}{m.group(3)}"
                 for size in ("original", "w780", "w500") if size != m.group(2)][:2]
    for candidate in urls:
        resp = await client.get(candidate, **kwargs)
        resp.raise_for_status()
        if not _is_blank_art(resp.content):
            return resp.content
        logger.warning(f"Blank image from {candidate}")
    raise BlankArtError(url)


async def fetch_poster_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    poster_path: str,
) -> Image.Image:
    """
    Fetch and cache the base poster image.

    Disk cache format is JPEG (q=92 RGB) rather than PNG:
      - ~4-5x faster decode on cache hit
      - ~5x smaller on disk
      - Imperceptible quality difference for photographic poster art
    The image is returned as RGBA so the compositing pipeline can use
    alpha_composite throughout without mode-checking.
    """
    # Anime providers (AniList/Kitsu) and Cinemeta hand us an absolute CDN url
    # rather than a TMDB path.  Detect that and fetch it directly; the
    # cache/normalise/return path below is identical either way.
    _is_absolute = is_absolute_art(poster_path)
    poster_cache_key = poster_image_cache_key(tmdb_id, media_type, poster_path)
    # Decoding, resizing and encoding are off the loop: at the larger canvases
    # a LANCZOS resize alone is a few hundred ms, and every request in the
    # worker would wait on it.
    image = await asyncio.to_thread(_cached_art, poster_cache_key, poster_canvas(), normalise_poster)
    if image is not None:
        logger.info(f"Poster cache hit for {tmdb_id}")
        return image

    def _decode_and_store(content: bytes) -> Image.Image:
        image = normalise_poster(Image.open(io.BytesIO(content)).convert("RGBA"))
        _store_art(poster_cache_key, image)
        return image

    if poster_path.startswith("custom:"):
        # An operator's pasted or uploaded image, already on disk.
        from art_overrides import custom_art_bytes
        content = await asyncio.to_thread(custom_art_bytes, poster_path)
        if content is None:
            raise FileNotFoundError(f"custom art {poster_path} is missing")
        return await asyncio.to_thread(_decode_and_store, content)
    if _is_absolute:
        logger.info(f"External API Call: Requested poster art for {tmdb_id}")
        content = await _get_art(client, poster_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested poster from TMDB for {tmdb_id}")
        _tmdb_size = POSTER_WIDTHS.get(poster_canvas()[0], "w500")
        content = await _get_art(client, f"https://image.tmdb.org/t/p/{_tmdb_size}{poster_path}")

    return await asyncio.to_thread(_decode_and_store, content)


# Bumped whenever the backdrop crop logic changes, so cached crops from the old
# algorithm are invalidated rather than served.
#   v2 = face-aware cropping
#   v3 = focus a single face when subjects are too far apart to both fit
#   v4 = derive text avoidance from PP-OCR polygons
#   v5 = cube confidence in the face "prominence" weight so a large
#        low-confidence false-positive blob can no longer outrank a smaller,
#        genuinely-confident face purely on bounding-box size (see
#        face_detect.detect_faces docstring — observed on TMDB 450545)
#   v6 = retry face detection at half size when none are found, for close-ups
#        too large for YuNet at native size (TMDB 1751701)
_CROP_VERSION = "v6"


def _face_crop_left(image: Image.Image, crop_w: int) -> "int | None":
    """
    Best left-edge x for a portrait crop that keeps detected faces in frame, or
    None when no faces are found / detection is unavailable (caller then falls
    back to the saliency crop).

    If every face fits within the crop window, the window is centred on their
    combined bounding box so all stay framed.  If the faces are too far apart to
    fit, the crop focuses on the single most prominent face (largest × most
    confident) rather than splitting the difference and slicing each in half.
    """
    try:
        from face_detect import detect_faces
    except Exception:
        return None
    faces = detect_faces(image)
    if not faces:
        return None
    w = image.width
    if crop_w >= w:
        return 0

    lefts  = [cx - fw / 2.0 for cx, fw, _ in faces]
    rights = [cx + fw / 2.0 for cx, fw, _ in faces]
    extent = max(rights) - min(lefts)

    if extent <= crop_w:
        # All faces fit — centre on their bounding-box midpoint.
        target_cx = (min(lefts) + max(rights)) / 2.0
    else:
        # Too far apart to keep both — focus on the most prominent face.
        target_cx = max(faces, key=lambda f: f[2])[0]

    left = int(round(target_cx - crop_w / 2))
    return max(0, min(w - crop_w, left))


def _saliency_crop_left(image: Image.Image, crop_w: int,
                        text_penalty=None, text_weight: float = 1.8) -> int:
    """
    Find the best left-edge x-coordinate for a portrait crop of a landscape image.

    Uses three complementary saliency signals combined into a per-column profile,
    then picks the crop window with the highest score.  A mild Gaussian centre
    bias acts as a tiebreaker when the scene is uniform so the result never drifts
    to an arbitrary edge.

    Signals (all computed on a 320 px-wide thumbnail for speed):

    1. Skin-tone mask  — HSV-based detection of warm pinkish-orange hues that
       reliably indicate human (and many animated) characters.  Strong weight
       (×4) because it's the most semantically meaningful signal for movie art.

    2. Center-surround saliency  — Difference of two Gaussian blurs at different
       radii (fine ≈ 4 % of width, coarse ≈ 20 % of width).  Finds blobs that
       are locally distinct from their surroundings — faces, figures, bright
       objects — rather than just any edge or texture.  Weight ×2.

    3. Saturation  — Subjects tend to be more saturated than blurred/desaturated
       backgrounds.  Lightweight secondary signal (×0.5).

    Vertical weighting: upper 65 % of frame gets 2× weight because characters'
    faces and torsos live in the top half; floors and landscape fill the bottom.

    Centre bias: ≈10 % of peak score — gentle enough not to override clear signal
    but prevents chaotic results on uniformly textured frames.
    """
    w, h = image.size
    if crop_w >= w:
        return 0

    # --- Downsample for speed ------------------------------------------------
    SMALL_W = 320
    scale   = min(1.0, SMALL_W / w)
    sw      = max(1, int(w * scale))
    sh      = max(1, int(h * scale))
    scrop_w = max(1, int(crop_w * scale))

    small = image.resize((sw, sh), Image.Resampling.LANCZOS).convert("RGB")
    rgb   = np.array(small, dtype=np.float32) / 255.0   # H × W × 3, [0,1]
    r, g, b = rgb[:,:,0], rgb[:,:,1], rgb[:,:,2]

    # --- Skin-tone mask (HSV) ------------------------------------------------
    # Compute V, S, H in numpy without scipy.
    cmax  = np.maximum(np.maximum(r, g), b)
    cmin  = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    v = cmax
    s = np.zeros_like(cmax)
    np.divide(delta, cmax, out=s, where=cmax > 1e-5)

    # Hue in [0, 360)
    hue = np.zeros((sh, sw), dtype=np.float32)
    m_r = (cmax == r) & (delta > 1e-5)
    m_g = (cmax == g) & (delta > 1e-5)
    m_b = (cmax == b) & (delta > 1e-5)
    hue[m_r] = (60.0 * ((g[m_r] - b[m_r]) / delta[m_r])) % 360.0
    hue[m_g] =  60.0 *  (b[m_g] - r[m_g]) / delta[m_g] + 120.0
    hue[m_b] =  60.0 *  (r[m_b] - g[m_b]) / delta[m_b] + 240.0

    # Skin: hue in [0,25]∪[335,360], moderate saturation, reasonable brightness.
    skin = (
        ((hue <= 25.0) | (hue >= 335.0)) &
        (s >= 0.15) & (s <= 0.90) &
        (v >= 0.25)
    ).astype(np.float32)

    # --- Center-surround saliency (DoG) --------------------------------------
    grey_pil  = Image.fromarray((rgb @ np.array([0.2126, 0.7152, 0.0722]) * 255).clip(0,255).astype(np.uint8))
    r_fine    = max(1, int(sw * 0.04))
    r_coarse  = max(1, int(sw * 0.20))
    fine      = np.array(grey_pil.filter(ImageFilter.GaussianBlur(radius=r_fine)),   dtype=np.float32)
    coarse    = np.array(grey_pil.filter(ImageFilter.GaussianBlur(radius=r_coarse)), dtype=np.float32)
    dog       = np.abs(fine - coarse) / 255.0   # [0, 1]

    # --- Saturation layer ----------------------------------------------------
    sat = s   # already [0, 1]

    # --- Vertical weighting --------------------------------------------------
    # Upper 65 % of rows get a 2× boost; lower 35 % stay at 1×.
    vert = np.ones(sh, dtype=np.float32)
    vert[:int(sh * 0.65)] = 2.0

    # --- Combine -------------------------------------------------------------
    saliency = (skin * 4.0 + dog * 2.0 + sat * 0.5) * vert[:, np.newaxis]

    col_sal = saliency.sum(axis=0)   # shape (sw,)

    # --- Text avoidance ------------------------------------------------------
    # Subtract a penalty proportional to per-column text density so the chosen
    # crop window dodges burned-in title text.  text_penalty is a left→right
    # profile (any length) in [0,1]; we resample it to the thumbnail width.
    if text_penalty is not None and len(text_penalty) > 1 and col_sal.max() > 0:
        prof = np.asarray(text_penalty, dtype=np.float32)
        prof_resized = np.interp(
            np.linspace(0.0, 1.0, sw, dtype=np.float32),
            np.linspace(0.0, 1.0, len(prof), dtype=np.float32),
            prof,
        )
        col_sal = col_sal - prof_resized * col_sal.max() * text_weight

    # --- Sliding-window via cumulative sum -----------------------------------
    cum         = np.concatenate([[0.0], col_sal.cumsum()])
    n_positions = sw - scrop_w + 1
    if n_positions <= 1:
        return 0

    window_scores = cum[scrop_w:scrop_w + n_positions] - cum[:n_positions]

    # --- Gaussian centre bias (10 % of peak) ---------------------------------
    centre  = (n_positions - 1) / 2.0
    sigma   = n_positions * 0.35
    xs      = np.arange(n_positions, dtype=np.float32)
    bias    = np.exp(-0.5 * ((xs - centre) / sigma) ** 2)
    sal_max = window_scores.max()
    if sal_max > 0:
        bias *= sal_max * 0.10

    best_small_left = int((window_scores + bias).argmax())

    # --- Scale back and clamp ------------------------------------------------
    left = int(round(best_small_left / scale))
    return max(0, min(w - crop_w, left))


async def fetch_backdrop_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    backdrop_path: str,
    avoid_text: bool = False,
) -> Image.Image:
    """
    Fetch, saliency-crop, and cache a TMDB backdrop as a portrait poster.

    Backdrops are 16:9 landscape; we take the full height and cut a 2:3 strip
    whose horizontal position is chosen by gradient-magnitude saliency rather
    than always defaulting to the centre.  This keeps the main subject in frame
    when cinematographers frame wide shots off-centre.

    When *avoid_text* is set (text-detection feature on), PP-OCR polygons
    produce a profile that biases the crop away from burned-in title text.  Cached under the
    same JPEG scheme as regular posters (text-aware crops keyed separately).
    """
    # Bump _CROP_VERSION on any crop change — it is part of the key.
    cache_key = backdrop_image_cache_key(tmdb_id, backdrop_path, avoid_text)
    image = await asyncio.to_thread(_cached_art, cache_key, poster_canvas(), normalise_poster)
    if image is not None:
        logger.info(f"TMDB backdrop cache hit for {tmdb_id}")
        return image

    # w1280 (720 px tall) crops to a quality 500x750 portrait.  A larger canvas
    # needs more height than that, so it takes the original and shrinks it to
    # the canvas height first: the crop then works on no more pixels than the
    # poster needs, whatever size the original is.
    size = poster_canvas()
    _large = size[1] > 720
    if is_absolute_art(backdrop_path):
        logger.info(f"External API Call: Requested backdrop art for {tmdb_id}")
        content = await _get_art(client, backdrop_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested backdrop from TMDB for {tmdb_id}")
        content = await _get_art(
            client,
            f"https://image.tmdb.org/t/p/{'original' if _large else 'w1280'}{backdrop_path}",
        )

    # The decode, the downscale of an original, the crop (CPU-heavy face/text
    # inference) and the encode all run in the thread pool; inline they would
    # stall the event loop and delay unrelated requests.
    def _decode_crop_store(content: bytes) -> Image.Image:
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        if _large and image.height > size[1]:
            image = image.resize((round(image.width * size[1] / image.height), size[1]),
                                 Image.Resampling.LANCZOS, reducing_gap=2.0)
        image = _crop_and_normalise_backdrop(image, tmdb_id, avoid_text, size)
        _store_art(cache_key, image)
        return image

    return await asyncio.to_thread(_decode_crop_store, content)


async def fetch_cropped_art(
    client: httpx.AsyncClient,
    tmdb_id: str,
    path: str,
    crop,
) -> Image.Image:
    """An operator's hand-placed 2:3 crop (art_overrides.Crop) of *path* — a
    TMDB image path, an absolute url or a stored custom image — as a poster.
    No face or saliency detection: the operator has already framed it.
    Cached per image, crop and canvas."""
    size = poster_canvas()
    cache_key = (f"crop_{_id_token(tmdb_id)}_{_art_token(path)}_"
                 f"{hashlib.sha256(crop.token().encode()).hexdigest()[:10]}{_canvas_suffix(size)}")
    image = await asyncio.to_thread(_cached_art, cache_key, size, normalise_poster)
    if image is not None:
        logger.info(f"Cropped art cache hit for {tmdb_id}")
        return image

    if path.startswith("custom:"):
        from art_overrides import custom_art_bytes
        content = await asyncio.to_thread(custom_art_bytes, path)
        if content is None:
            raise FileNotFoundError(f"custom art {path} is missing")
    else:
        if is_absolute_art(path):
            url = path
        else:
            # A zoomed or large-canvas crop needs the pixels; w1280 (720 px
            # tall) is plenty for a full-height crop at the default canvas.
            url = f"https://image.tmdb.org/t/p/{'original' if (crop.zoom > 1 or size[1] > 720) else 'w1280'}{path}"
        logger.info(f"External API Call: Requested art to crop for {tmdb_id}")
        content = await _get_art(client, url, follow_redirects=True)

    def _decode_crop_store(data: bytes) -> Image.Image:
        source = Image.open(io.BytesIO(data)).convert("RGBA")
        image = normalise_poster(source.crop(crop.box(*source.size)), size)
        _store_art(cache_key, image)
        return image

    return await asyncio.to_thread(_decode_crop_store, content)


def normalise_landscape(image: Image.Image) -> Image.Image:
    """Fit-cover an image to the landscape canvas.

    Backdrops are already 16:9, so this is effectively a resize; the cover maths
    is kept so the odd 1.85:1 or 2:1 source is centred rather than squashed.
    """
    target_w, target_h = LANDSCAPE_WIDTH, LANDSCAPE_HEIGHT
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


async def fetch_landscape_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    backdrop_path: str,
) -> Image.Image:
    """
    Fetch a TMDB backdrop and fit it to the landscape canvas, uncropped.

    The portrait pipeline's expensive part — saliency and face detection to pick
    a 2:3 strip out of a 16:9 frame — has nothing to do here: the source and the
    target are the same shape, so the whole crop stage is skipped.

    Cached separately from the portrait backdrop crop of the same asset; the two
    are different images and must not share a key.
    """
    cache_key = landscape_image_cache_key(tmdb_id, backdrop_path)
    image = await asyncio.to_thread(
        _cached_art, cache_key, (LANDSCAPE_WIDTH, LANDSCAPE_HEIGHT), normalise_landscape
    )
    if image is not None:
        logger.info(f"TMDB landscape cache hit for {tmdb_id}")
        return image

    def _decode_and_store(content: bytes) -> Image.Image:
        image = normalise_landscape(Image.open(io.BytesIO(content)).convert("RGBA"))
        _store_art(cache_key, image)
        return image

    if backdrop_path.startswith("custom:"):
        # An operator's pasted or uploaded image, already on disk.
        from art_overrides import custom_art_bytes
        content = await asyncio.to_thread(custom_art_bytes, backdrop_path)
        if content is None:
            raise FileNotFoundError(f"custom art {backdrop_path} is missing")
        return await asyncio.to_thread(_decode_and_store, content)
    if is_absolute_art(backdrop_path):
        logger.info(f"External API Call: Requested landscape backdrop art for {tmdb_id}")
        content = await _get_art(client, backdrop_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested landscape backdrop from TMDB for {tmdb_id}")
        content = await _get_art(client, f"https://image.tmdb.org/t/p/w1280{backdrop_path}")

    return await asyncio.to_thread(_decode_and_store, content)


def _crop_and_normalise_backdrop(image: Image.Image, tmdb_id: str,
                                 avoid_text: bool,
                                 size: tuple[int, int] | None = None) -> Image.Image:
    """Synchronous backdrop crop (face-aware → saliency fallback) + normalise.
    Runs in the thread pool; all OpenCV inference is confined here."""
    # Optional text-density profile to steer the crop away from title text.
    _text_prof = None
    if avoid_text:
        try:
            from text_detect import text_column_profile
            _text_prof = text_column_profile(image)
        except Exception as exc:
            logger.warning(f"Backdrop text profile failed for {tmdb_id}: {exc}")

    # Crop full-height to a 2:3 strip.  Prefer a face-aware crop (robust on
    # people shots where warm/textured backgrounds fool the saliency heuristic);
    # fall back to saliency when no faces are detected.
    w, h   = image.size
    crop_w = int(h * 2 / 3)
    if crop_w < w:
        left = _face_crop_left(image, crop_w)
        if left is not None:
            logger.info(
                f"Backdrop face-aware crop for {tmdb_id}: "
                f"left={left} (centre would be {(w - crop_w) // 2}) of w={w}"
            )
        else:
            left = _saliency_crop_left(image, crop_w, text_penalty=_text_prof)
            logger.info(
                f"Backdrop saliency crop for {tmdb_id}: "
                f"left={left} (centre would be {(w - crop_w) // 2}) of w={w}"
                f"{' [text-aware]' if _text_prof is not None else ''}"
            )
        image = image.crop((left, 0, left + crop_w, h))

    # Explicit size: this runs in the thread pool, where the request's
    # poster_canvas() context does not follow.
    return normalise_poster(image, size or (POSTER_WIDTH, POSTER_HEIGHT))


def _cached_logo(cache_key: str) -> "Image.Image | None":
    """A cached logo as RGBA, or None.  Blocking; run via asyncio.to_thread."""
    cached_bytes = get_cached_tmdb_logo(cache_key)
    if not cached_bytes:
        return None
    return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")


def _store_logo(cache_key: str, logo: Image.Image) -> None:
    """Cache *logo* as PNG.  Blocking (an encode, ~15-55 ms, and a write)."""
    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(cache_key, buf.getvalue())


async def _fetch_metahub_logo(
    client: httpx.AsyncClient,
    imdb_id: str,
) -> Image.Image | None:
    """
    Fetch a title logo from the Metahub CDN (images.metahub.space).

    Metahub is the same CDN Cinemeta (Stremio's catalogue addon) uses for
    logo art.  It requires no authentication and caches aggressively
    (max-age ≈ 60 days server-side).  We use it as a final fallback when
    TMDB has no logo candidates for a given title.

    URL pattern: https://images.metahub.space/logo/medium/{imdb_id}/img
    """
    cache_key = f"metahub_logo_{imdb_id}"
    cached = await asyncio.to_thread(_cached_logo, cache_key)
    if cached is not None:
        logger.info(f"Metahub logo cache hit for {imdb_id}")
        return cached

    # Try medium first (smaller payload), fall back to large — some titles only
    # have a large-size entry on Metahub and the medium URL 404s.
    resp = None
    for size in ("medium", "large", "small"):
        url = f"https://images.metahub.space/logo/{size}/{imdb_id}/img"
        logger.info(f"External API Call: Requested logo from Metahub ({size}) for {imdb_id}")
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code == 404:
                logger.info(f"Metahub: no {size} logo for {imdb_id}")
                continue
            r.raise_for_status()
            resp = r
            break
        except httpx.HTTPStatusError as exc:
            logger.warning(f"Metahub logo fetch failed for {imdb_id} ({size}): {exc}")
        except Exception as exc:
            logger.warning(f"Metahub logo fetch error for {imdb_id} ({size}): {exc}")

    if resp is None:
        return None

    def _decode_and_store(content: bytes) -> Image.Image | None:
        try:
            logo = Image.open(io.BytesIO(content)).convert("RGBA")
        except Exception as exc:
            logger.warning(f"Metahub logo parse failed for {imdb_id}: {exc}")
            return None
        bbox = logo.getchannel("A").getbbox()
        if bbox:
            logo = logo.crop(bbox)
        _store_logo(cache_key, logo)
        return logo

    return await asyncio.to_thread(_decode_and_store, resp.content)


def _normalise_image_locale(value: str | None) -> str:
    return (value or "").strip().lower().replace("_", "-")


def _image_language_keys(image: dict) -> list[str]:
    language = _normalise_image_locale(image.get("iso_639_1"))
    if not language:
        return []
    region = _normalise_image_locale(image.get("iso_3166_1"))
    keys = [f"{language}-{region}"] if region else []
    keys.append(language)
    return list(dict.fromkeys(keys))


def _image_matches_language(image: dict, requested: str | None) -> bool:
    requested = _normalise_image_locale(requested)
    if not requested:
        return False
    keys = _image_language_keys(image)
    if "-" in requested:
        return requested in keys
    return requested in keys


def _tmdb_include_image_languages(
    logo_language: str | None, secondary_language: str | None = None
) -> list[str]:
    languages: list[str] = []
    for candidate in (logo_language, secondary_language):
        requested = _normalise_image_locale(candidate) or ""
        if requested and requested != "en":
            languages.append(requested)
            base = requested.split("-", 1)[0]
            if base and base != requested:
                languages.append(base)
    languages.extend(["en", "null"])
    return list(dict.fromkeys(languages))


# Logo language priority is an ordered list of sources, tried first to last:
#   native             — the request's logo_language
#   native_if_original — the same, but only when it is also the content's own
#                        original language (so a foreign title skips it)
#   custom             — the secondary preferred language
#   original           — the content's own original language
#   english            — a TMDB English logo, then the Metahub CDN (whose logos
#                        are English in practice)
#   neutral            — a TMDB logo tagged with no language, usually a symbol
#                        or a wordmark nobody labelled
#   text               — stop and draw the title as text
# A source left out is never used; with no "text" a title that runs out of
# logos gets no title at all.  "text" always ends the list: nothing after it
# could be reached.
LOGO_PRIORITY_SOURCES = (
    "native", "native_if_original", "custom", "original", "english", "neutral", "text",
)

# The named priorities the configurator offered before the list, kept as the
# canonical spelling of their order so existing URLs and composite cache keys
# are unchanged.  An order spelled out as a list that matches one of these is
# stored under its name, for the same reason.
LOGO_PRIORITY_PRESETS: dict[str, tuple[str, ...]] = {
    "native_original":             ("native", "original", "neutral", "english", "text"),
    "original_native":             ("original", "native", "neutral", "english", "text"),
    "native_if_original_english":  ("native_if_original", "english", "original", "neutral", "text"),
    "native_text":                 ("native", "english", "neutral", "text"),
    "native_custom_text":          ("native", "custom", "english", "neutral", "text"),
    "native_custom_original_text": ("native", "custom", "original", "english", "neutral", "text"),
}
DEFAULT_LOGO_PRIORITY = "native_original"


def parse_logo_priority(value: str | None) -> str | None:
    """Canonical form of a logo_priority parameter, or None when it is not one.

    Accepts a preset name or a comma-separated list of LOGO_PRIORITY_SOURCES.
    Unknown and repeated sources are dropped and the list ends at "text"; an
    order equal to a preset comes back as that preset's name."""
    value = (value or "").strip().lower()
    if value in LOGO_PRIORITY_PRESETS:
        return value
    sources: list[str] = []
    for token in value.split(","):
        token = token.strip()
        if token in LOGO_PRIORITY_SOURCES and token not in sources:
            sources.append(token)
            if token == "text":
                break
    if not sources:
        return None
    for name, preset in LOGO_PRIORITY_PRESETS.items():
        if tuple(sources) == preset:
            return name
    return ",".join(sources)


def logo_priority_sources(logo_priority: str) -> tuple[str, ...]:
    """The ordered sources a canonical logo_priority stands for."""
    preset = LOGO_PRIORITY_PRESETS.get(logo_priority)
    if preset is not None:
        return preset
    return tuple(logo_priority.split(",")) if logo_priority else \
        LOGO_PRIORITY_PRESETS[DEFAULT_LOGO_PRIORITY]


def logo_priority_uses_custom(logo_priority: str) -> bool:
    return "custom" in logo_priority_sources(logo_priority)


def logo_priority_draws_text(logo_priority: str) -> bool:
    """Whether a title with no logo falls back to its name drawn as text."""
    return "text" in logo_priority_sources(logo_priority)


def logo_language_steps(
    logo_language: str,
    original_language: str | None,
    logo_priority: str,
    secondary_language: str | None = None,
) -> list[str]:
    """The priority resolved against one title, as the steps to try in order:
    a language code for a language-tagged logo, "null" for a language-neutral
    one, and "metahub" for the Metahub CDN (which rides with English).  Ends
    before "text"; a source with no language to stand for (no secondary
    language, no known original language) is skipped, and a language already
    tried is not tried twice."""
    steps: list[str] = []
    for source in logo_priority_sources(logo_priority):
        if source == "text":
            break
        if source == "native":
            new = [logo_language]
        elif source == "native_if_original":
            new = [logo_language] if logo_language == original_language else []
        elif source == "custom":
            new = [secondary_language]
        elif source == "original":
            new = [original_language]
        elif source == "english":
            new = ["en", "metahub"]
        else:
            new = ["null"]
        steps.extend(step for step in new if step and step not in steps)
    return steps


def image_language_order(
    logo_language: str,
    original_language: str | None,
    logo_priority: str,
    secondary_language: str | None = None,
) -> list[str]:
    """The language codes of the priority, in order, for picking a language-
    tagged image (original-art posters): the logo steps without the language-
    neutral and Metahub ones."""
    return [
        step
        for step in logo_language_steps(
            logo_language, original_language, logo_priority, secondary_language
        )
        if step not in ("null", "metahub")
    ]


# Aspect ratio from which a logo counts as "wide" for the landscape layout.  Its
# logo box is 0.42 w by ~0.255 h, an aspect near 2.9: a stacked or square logo
# is capped by the height and lands small, a wordmark at 2.0 or above fills the
# box.  Below this the layout has to shrink the logo; above it, votes decide.
WIDE_LOGO_MIN_ASPECT = 2.0


def _logo_aspect(logo: dict) -> float:
    ratio = logo.get("aspect_ratio")
    if ratio:
        return float(ratio)
    w, h = logo.get("width") or 0, logo.get("height") or 0
    return (w / h) if (w and h) else 0.0


def _logo_rank_key(prefer_wide: bool):
    """Sort key for the winning language bucket, highest first: votes, or with
    ``prefer_wide`` the wide logos before the rest and votes within each."""
    def key(logo: dict) -> tuple[bool, float]:
        wide = prefer_wide and _logo_aspect(logo) >= WIDE_LOGO_MIN_ASPECT
        return (wide, logo.get("vote_average", 0) or 0)
    return key


def logo_step_available(logos: list[dict], step: str) -> bool:
    """Whether fetch_logo would find a TMDB logo at *step* (a language code
    or "null"), for deciding where an operator's logo override comes in."""
    _exts = (".png", ".svg") if _HAS_CAIROSVG else (".png",)
    for lg in logos:
        if not lg.get("file_path", "").lower().endswith(_exts):
            continue
        if (lg.get("iso_639_1") in (None, "")) if step == "null" \
                else _image_matches_language(lg, step):
            return True
    return False


async def fetch_logo(
    client: httpx.AsyncClient,
    logos: list[dict],
    logo_language: str = "en",
    imdb_id: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
    use_metahub: bool = True,
    secondary_language: str | None = None,
    prefer_wide: bool = False,
) -> Image.Image | None:
    """
    Fetch the best available logo for a title, with a Metahub CDN fallback.

    ``prefer_wide`` (the landscape layout) ranks a wide logo — aspect at or
    above WIDE_LOGO_MIN_ASPECT — ahead of any narrower one within the bucket
    that won, votes deciding among the wide ones.  Language still comes first:
    a wide logo in the wrong language is not preferred over a stacked one in
    the right language.

    The sources are tried in the order *logo_priority* gives them (see
    LOGO_PRIORITY_SOURCES and logo_language_steps): a TMDB logo in each
    language in turn, a language-neutral TMDB logo, and the Metahub CDN where
    English sits.  Metahub is skipped when *use_metahub* is False, so a caller
    can slot another provider in before it.  None when every source comes up
    empty; the caller decides whether that means a text title.

    All results are cached locally so repeat requests never hit external APIs.
    """
    # Accept PNG always; accept SVG too when we can rasterise it (cairosvg).
    # TMDB's highest-voted logo is frequently an SVG, so excluding them would
    # silently fall back to a lower-quality raster or a text title.
    _exts = (".png", ".svg") if _HAS_CAIROSVG else (".png",)
    _cand = [lg for lg in logos if lg["file_path"].lower().endswith(_exts)]

    candidates: list[dict] = []
    for step in logo_language_steps(
        logo_language, original_language, logo_priority, secondary_language
    ):
        if step == "metahub":
            if use_metahub and imdb_id:
                metahub_logo = await _fetch_metahub_logo(client, imdb_id)
                if metahub_logo is not None:
                    return metahub_logo
            continue
        if step == "null":
            candidates = [lg for lg in _cand if lg.get("iso_639_1") in (None, "")]
        else:
            candidates = [lg for lg in _cand if _image_matches_language(lg, step)]
        if candidates:
            break

    candidates = sorted(candidates, key=_logo_rank_key(prefer_wide), reverse=True)

    if not candidates:
        return None

    logo = await fetch_logo_image(client, candidates[0]["file_path"])
    if logo is None:
        # Rasterise failed — fall back to Metahub, then None.
        logger.warning(f"SVG logo unusable for {imdb_id} — trying Metahub fallback")
        return await _fetch_metahub_logo(client, imdb_id) if (use_metahub and imdb_id) else None
    return logo


async def fetch_logo_image(client: httpx.AsyncClient, logo_path: str) -> Image.Image | None:
    """One logo, alpha-trimmed and cached: a TMDB image path, or an absolute
    url (an operator's fanart.tv / TVDB pick).  None when an SVG can't be
    rasterised; an HTTP failure raises, as TMDB's always has."""
    is_svg = urlsplit(logo_path).path.lower().endswith(".svg")
    _absolute = is_absolute_art(logo_path)

    # A larger canvas draws the logo up to 0.75 of a wider poster, past w500's
    # 500 px, so it takes the original — shrunk to the canvas before it is
    # cached, so no render ever decodes a multi-thousand-pixel logo.
    _canvas = poster_canvas()
    _large = _canvas[0] > POSTER_WIDTH
    _custom = logo_path.startswith("custom:")
    logo_cache_key = (
        f"abs_{_art_token(logo_path)}" if (_absolute or _custom)
        else logo_path.strip('/').replace('/', '_')
    ) + _canvas_suffix(_canvas)
    cached = await asyncio.to_thread(_cached_logo, logo_cache_key)
    if cached is not None:
        logger.info("TMDB logo cache hit")
        return cached

    if _custom:
        from art_overrides import custom_art_bytes
        content = await asyncio.to_thread(custom_art_bytes, logo_path)
        if content is None:
            raise FileNotFoundError(f"custom logo {logo_path} is missing")
    elif _absolute:
        resp = await client.get(logo_path, follow_redirects=True)
        logger.info("External API Call: Requested chosen logo art")
    else:
        # SVGs are served at "original" (the sized w500 path doesn't apply to vector);
        # rasters use w500 which is plenty for our ≤~440px rendered width.
        _size = "original" if (is_svg or _large) else "w500"
        resp = await client.get(f"https://image.tmdb.org/t/p/{_size}{logo_path}")
        logger.info(f"External API Call: Requested logo from TMDB")
    if not _custom:
        resp.raise_for_status()
        content = resp.content

    # Rasterising, decoding, trimming and the PNG encode run off the loop.
    def _decode_and_store(content: bytes) -> Image.Image | None:
        if is_svg:
            logo = _rasterize_svg(content)
            if logo is None:
                return None
        else:
            logo = Image.open(io.BytesIO(content)).convert("RGBA")
        bbox = logo.getchannel("A").getbbox()
        if bbox:
            logo = logo.crop(bbox)
        if _large and (logo.width > _canvas[0] or logo.height > _canvas[1] // 3):
            logo.thumbnail((_canvas[0], _canvas[1] // 3), Image.Resampling.LANCZOS)
        _store_logo(logo_cache_key, logo)
        return logo

    return await asyncio.to_thread(_decode_and_store, content)


_trending_inflight: dict[str, asyncio.Event] = {}

# ---------------------------------------------------------------------------
# Operator-supplied trending sources
# ---------------------------------------------------------------------------

# An MDBList list page. The site serves the same list as JSON from a /json
# suffix with no API key, so a pasted human URL can be used directly.  The list
# path is captured whole rather than to a fixed depth: truncating it would point
# a deeper URL at a DIFFERENT list instead of failing, which is the one outcome
# worse than not accepting it.
_MDBLIST_LIST_RE = re.compile(
    r"^https?://(?:www\.)?mdblist\.com/lists/(?P<path>[^\s?#]+)", re.I
)


# On a movie or TV list's signature while anime is left to its own lists;
# changed whenever what counts as anime does, so stored lists are rebuilt.
_ANIME_SPLIT_MARK = "-anime-3"

# The anime lists, series then films: AniList's unless a source replaces it.
ANIME_ENDPOINTS = ("anime", "anime_movie")


def trending_source_url(media_type: str) -> str:
    """Configured trending source for *media_type*, or "" when unset."""
    if media_type == "anime":
        return TRENDING_SOURCE_ANIME
    if media_type == "anime_movie":
        return TRENDING_SOURCE_ANIME_MOVIE
    return TRENDING_SOURCE_TV if media_type in ("tv", "series") else TRENDING_SOURCE_MOVIE


def trending_kind(endpoint: str) -> str:
    """"tv" or "movie": what a trending list's TMDB ids name."""
    return "tv" if endpoint in ("tv", "series", "anime") else "movie"


def anime_split() -> bool:
    """Whether anime ranks on its own lists, and so leaves the movie and TV
    ones: while the trending catalogs addon serves those lists."""
    import config
    return config.TRENDING_CATALOGS_ENABLED


def sanitise_source_url(url: str) -> str:
    """Scheme, host and path only — safe to log.

    A trending source is an arbitrary operator-supplied URL, so it can carry an
    api key, a bearer token, a signed query, or basic-auth credentials in the
    userinfo.  Only the query and the userinfo are dropped, which leaves enough
    to identify which configured source a message is about.
    """
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return "<unparseable url>"
    host = parsed.hostname or ""
    if not host:
        return "<invalid url>"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def trending_source_signature(media_type: str) -> str:
    """Identity of the trending source, so a cached snapshot from a different
    one is discarded rather than served until its TTL lapses.

    Hashed rather than stored verbatim: this value is written to the cache DB,
    and the URL it identifies may contain credentials.  All the signature has to
    do is differ when the configured source differs.
    """
    # Hiding unreleased titles, or leaving anime to its own lists, makes a
    # different list from the same source: one stored without it is rebuilt.
    out = "+released" if TRENDING_HIDE_UNRELEASED else ""
    if out and media_type == "anime_movie":
        out += "-home"            # AniList's films checked against TMDB's dates too
    if media_type not in ANIME_ENDPOINTS and anime_split():
        out += _ANIME_SPLIT_MARK
    url = _normalise_trending_url(trending_source_url(media_type))
    if not url and media_type in ANIME_ENDPOINTS:
        return ("anilist" if media_type == "anime" else "anilist-films") + out
    if not url:
        return "tmdb" + out
    return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + out


def _normalise_trending_url(url: str) -> str:
    """Rewrite an MDBList list page to its JSON export; leave anything else be.

    The host is rebuilt canonically rather than echoed back: mdblist.com 301s
    both ``http://`` and ``www.`` to ``https://mdblist.com``, and the fetch that
    uses this needs the redirect not to matter.
    """
    url = url.strip()
    match = _MDBLIST_LIST_RE.match(url)
    if not match:
        return url
    path = match.group("path").strip("/")
    if path.endswith("/json"):
        path = path[: -len("/json")]
    if not path:
        return url
    return f"https://mdblist.com/lists/{path}/json"


# A source that just failed is not re-fetched on the next poster request: the
# rank lookup runs per title, so without this one bad URL means one outbound
# request (and one error line) per poster served.  Short enough that a blip
# clears well inside the snapshot TTL.
_TRENDING_SOURCE_RETRY_SECS = 300
_trending_source_failed_at: dict[str, float] = {}
# A failed trending read is tried once more after this pause before the list
# is given up on for the cooldown above.  Most failures are a blip (a timeout,
# a 5xx, a throttle), and giving up costs every poster its rank until the next
# attempt.
_TRENDING_RETRY_DELAY_SECS = 2.0


def _trending_item_details(item: dict) -> dict:
    """Name, year, IMDb id and poster path from one trending-list row, for the
    trending catalogs addon.  Covers TMDB's and MDBList's field names."""
    date = str(item.get("release_date") or item.get("first_air_date") or "")
    year = item.get("release_year") or item.get("year") or (date[:4] if date[:4].isascii() and date[:4].isdigit() else None)
    out = {
        "name": item.get("title") or item.get("name"),
        "year": str(year) if year else None,
        "imdb_id": item.get("imdb_id") if str(item.get("imdb_id") or "").startswith("tt") else None,
        "poster": item.get("poster_path") or item.get("poster"),
    }
    out = {k: v for k, v in out.items() if v}
    # TMDB's rows: Animation from Japan, for telling anime apart when the id
    # mapping doesn't know the title, and the language for when it does.
    lang = item.get("original_language")
    if isinstance(lang, str) and lang:
        out["lang"] = lang
    if 16 in (item.get("genre_ids") or []) and lang == "ja":
        out["anime"] = True
    # TMDB's vote count, for the cinema window TRENDING_HIDE_UNRELEASED
    # judges a film's release status by (cinema_window_days).
    if isinstance(item.get("vote_count"), int):
        out["votes"] = item["vote_count"]
    if "release_date" in item or "first_air_date" in item:
        # Kept even when empty: a row with the field and no date is a title
        # with no release date yet, which TRENDING_HIDE_UNRELEASED leaves out.
        out["date"] = date
    return out


def _parse_trending_payload(payload, media_type: str, details_out: dict | None = None) -> list[str]:
    """Extract an ordered list of TMDB ids from a trending payload.

    Handles the two shapes documented on TRENDING_SOURCE_MOVIE: TMDB's
    ``{"results": [...]}`` (ranked by array order) and MDBList's bare array
    (ranked by its own ``rank`` field, which is ascending but not contiguous —
    real lists step 1000, 2000, 3000).

    MDBList rows carry ``mediatype`` ("movie"/"show"), so a mixed list is
    filtered down to the type being asked for.  TMDB's own multi-type payloads
    use the same key with the same values, so one filter covers both.
    """
    if isinstance(payload, dict):
        items = payload.get("results")
        ranked = False
    else:
        items = payload
        ranked = True
    if not isinstance(items, list):
        return []

    wanted = "show" if trending_kind(media_type) == "tv" else "movie"
    rows: list[tuple[float, str]] = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("mediatype") or item.get("media_type") or "").lower()
        # "tv" appears where TMDB is the source, "show" where MDBList is.
        if kind:
            if kind in ("tv", "series"):
                kind = "show"
            if kind != wanted:
                continue
        raw = item.get("id", item.get("tmdb_id", item.get("tmdbid")))
        # Reject anything that is not a bare TMDB id — an IMDb id here means the
        # payload is keyed on a different id space and silently importing it
        # would produce a snapshot that never matches a request.
        if raw is None or not (str(raw).isascii() and str(raw).isdigit()):
            continue
        order = item.get("rank") if ranked else None
        rows.append((float(order) if isinstance(order, (int, float)) else position, str(raw)))
        if details_out is not None and str(raw) not in details_out:
            details_out[str(raw)] = _trending_item_details(item)

    rows.sort(key=lambda row: row[0])
    seen: set[str] = set()
    out: list[str] = []
    for _order, tmdb_id in rows:
        if tmdb_id not in seen:
            seen.add(tmdb_id)
            out.append(tmdb_id)
        if len(out) >= TRENDING_SOURCE_MAX_ITEMS:
            break
    return out


async def fetch_trending_source_ids(
    client: httpx.AsyncClient,
    media_type: str,
    details_out: dict | None = None,
) -> list[str] | None:
    """Ordered TMDB ids from the operator's trending source.

    Returns None when no source is configured (caller falls back to TMDB), and
    an empty list when a source IS configured but could not be used — the caller
    must treat that as "no trending this cycle" rather than reverting to TMDB,
    so a broken config is visible instead of silently working.
    """
    url = trending_source_url(media_type)
    if not url:
        return None
    resolved = _normalise_trending_url(url)
    # Only ever log the sanitised form: the configured URL can carry a token or
    # basic-auth credentials, and this runs on the request path.
    shown = sanitise_source_url(resolved)

    failed_at = _trending_source_failed_at.get(media_type)
    if failed_at is not None and time.monotonic() - failed_at < _TRENDING_SOURCE_RETRY_SECS:
        return []

    ids: list[str] = []
    error: Exception | None = None
    for attempt in (1, 2):
        error = None
        try:
            logger.info(f"External API Call: trending source for {media_type} ({shown})")
            # Redirects are followed here specifically: an operator pastes whatever
            # their browser showed them, and a host that answers on a canonical
            # form should not read as a broken config.
            resp = await client.get(resolved, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            ids = _parse_trending_payload(resp.json(), media_type, details_out)
        except Exception as exc:
            ids, error = [], exc
        if ids:
            break
        if attempt == 1:
            logger.warning(
                f"Trending source for {media_type} ({shown}) "
                f"{f'failed: {error}' if error else 'yielded no usable ids'} — retrying"
            )
            await asyncio.sleep(_TRENDING_RETRY_DELAY_SECS)
    if error is not None:
        _trending_source_failed_at[media_type] = time.monotonic()
        logger.error(
            f"Trending source fetch failed ({shown}): {error} — "
            f"no trending data will be served for {media_type} this cycle"
        )
        return []
    if not ids:
        _trending_source_failed_at[media_type] = time.monotonic()
        logger.error(
            f"Trending source for {media_type} ({shown}) yielded no usable "
            "TMDB ids — check the list is not empty and its entries carry "
            "numeric TMDB ids. No trending data will be served this cycle."
        )
        return []
    _trending_source_failed_at.pop(media_type, None)
    logger.info(f"Trending source for {media_type}: {len(ids)} titles from {shown}")
    return ids


async def _fetch_tmdb_trending_ids(
    client: httpx.AsyncClient, tmdb_key: str, endpoint: str,
    details_out: dict | None = None,
) -> list[str] | None:
    """TMDB's day-trending ids for *endpoint*, pages 1-5 (1-10 when unreleased
    titles or anime are left out, to have as many after), in rank order.

    The pages are fetched concurrently, and TMDB's list can shift between those
    requests, so a title can turn up on two pages. The first appearance wins
    and the ranks are numbered without gaps. Keeping the last appearance, as
    this used to, left rank numbers that no title held and put the titles
    around them a place out.
    """
    # Anime is a fifth of TV's list, and unreleased films a good share of
    # movies': twice the pages keeps the broad sash's ranks filled after.
    pages_n = 10 if (TRENDING_HIDE_UNRELEASED or anime_split()) else 5
    logger.info(f"External API Call: Refreshing TMDB trending snapshot (pages 1-{pages_n} concurrent)")

    async def _fetch_page(page: int) -> list[dict]:
        resp = await client.get(
            f"https://api.themoviedb.org/3/trending/{endpoint}/day",
            params={"api_key": tmdb_key, "page": page},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    try:
        pages = await asyncio.gather(*(_fetch_page(page) for page in range(1, pages_n + 1)))
    except Exception as exc:
        logger.error(f"TMDB trending fetch error: {exc}")
        return None

    seen: set[str] = set()
    ids: list[str] = []
    for results in pages:
        for item in results:
            entry_id = str(item["id"])
            if entry_id not in seen:
                seen.add(entry_id)
                ids.append(entry_id)
                if details_out is not None:
                    details_out[entry_id] = _trending_item_details(item)
    return ids


def _trending_cooling_down(key: str) -> bool:
    failed_at = _trending_source_failed_at.get(key)
    return failed_at is not None and time.monotonic() - failed_at < _TRENDING_SOURCE_RETRY_SECS


async def _read_twice(read, label: str):
    """*read()* once more after a short pause if the first gave nothing."""
    result = await read()
    if not result:
        logger.warning(f"Trending {label} list unreadable — retrying")
        await asyncio.sleep(_TRENDING_RETRY_DELAY_SECS)
        result = await read()
        if not result:
            logger.error(
                f"Trending {label} list unreadable twice — no ranks for "
                f"{_TRENDING_SOURCE_RETRY_SECS // 60} minutes"
            )
    return result


def _anime_list_tmdb_ids(kind: str) -> set[str]:
    """The TMDB ids (of *kind*, "tv" or "movie") of every title on the stored
    anime lists: AniList entries through the id mapping, and a custom
    source's TMDB ids as they are."""
    import anime_ids
    out: set[str] = set()
    anilist: set[int] = set()
    for endpoint in ANIME_ENDPOINTS:
        entry = get_cached_trending_snapshot_entry(endpoint, include_stale=True)
        for key in (entry[0] if entry else {}):
            if key.startswith("anilist:"):
                if key[8:].isdigit():
                    anilist.add(int(key[8:]))
            elif trending_kind(endpoint) == kind:
                out.add(key)
    _kitsu, tv, movie = anime_ids.ids_for_anilist(anilist)
    return out | {str(i) for i in (tv if kind == "tv" else movie)}


def _expire_overlapping_lists() -> None:
    """After an anime list is rebuilt: a movie or TV list still holding one of
    its titles is rebuilt too, on its next read, so no title is on two lists
    with two ranks.  The TMDB lists build the anime ones first, so this only
    happens when an anime list is refreshed on its own."""
    for endpoint in ("movie", "tv"):
        entry = get_cached_trending_snapshot_entry(endpoint)
        # None while a scheduled refresh rebuilds it anyway: nothing to check.
        if entry and set(entry[0]) & _anime_list_tmdb_ids(endpoint):
            logger.info(f"Trending {endpoint}: holds titles now on an anime list — rebuilding it")
            expire_trending_snapshot(endpoint)


def _without_anime(endpoint: str, ids: list[str], details: dict[str, dict],
                   on_anime_lists: "set[str] | None" = None) -> list[str]:
    """*ids* without the anime, which rank on the anime lists instead: every
    title on those lists (*on_anime_lists*, their TMDB ids), whatever its
    language, so no title is on two lists; and Japanese anime that isn't
    trending there, a TMDB row's Animation from Japan or what the id mapping
    knows as anime when the row is Japanese or says nothing of its language.
    Chinese and Korean animation off the anime lists stays where it is ranked.
    Numbered after this, so the ranks have no gaps."""
    import anime_ids
    kind = trending_kind(endpoint)
    on_anime_lists = on_anime_lists or set()

    def is_anime(i: str) -> bool:
        detail = details.get(i) or {}
        if i in on_anime_lists or detail.get("anime"):
            return True
        if detail.get("lang", "ja") != "ja":
            return False
        return bool(anime_ids.reverse_lookup(kind, i, detail.get("imdb_id")))

    kept = [i for i in ids if not is_anime(i)]
    if len(kept) < len(ids):
        logger.info(f"Trending {endpoint}: {len(ids) - len(kept)} anime title(s) left to the anime lists")
    return kept


# Titles checked at once while dropping unreleased ones from a trending list.
_RELEASED_BATCH = 20


async def _released_only(
    client: httpx.AsyncClient, tmdb_key: str, endpoint: str,
    ids: list[str], details: dict[str, dict],
) -> list[str]:
    """*ids* in order without the titles not out at home yet
    (TRENDING_HIDE_UNRELEASED), up to as many as the trending sashes rank.

    A film is out at home once it streams or is on disc somewhere: TMDB's
    release dates, the same ones its release-status sash reads, so "Cinema"
    and "Production" films are dropped.  An AniList film ("anilist:<id>") is
    judged by its TMDB film through the id mapping.  A series is out once its first
    episode has aired, by the date on its trending row.  A title that can't
    be checked (a failed fetch, a custom source's row with no date) is kept:
    a hiccup shouldn't empty the row."""
    limit = max(TRENDING_FETCH_COUNT, TRENDING_BROAD_FETCH_COUNT)
    today = _date.today()
    # AniList films are judged by the TMDB film the id mapping gives each, the
    # dates its poster's release badge reads; unmapped, one is kept on
    # AniList's word that it is out.
    import anime_ids
    tmdb_film = anime_ids.tmdb_films_for_anilist(
        {int(i[8:]) for i in ids if i.startswith("anilist:") and i[8:].isdigit()})

    async def out_at_home(entry_id: str) -> bool:
        detail = details.get(entry_id) or {}
        if entry_id.startswith("anilist:"):
            film = tmdb_film.get(int(entry_id[8:])) if entry_id[8:].isdigit() else None
            if film is None:
                return True
            entry_id, detail = str(film), {}
        if trending_kind(endpoint) == "tv":
            if "date" not in detail:
                return True
            aired = _parse_tmdb_date(detail["date"])
            return aired is not None and aired <= today
        if not tmdb_key:
            return True
        try:
            # A row without a vote count (AniList's, a custom source's) is
            # still trending, which is popularity enough for the long window.
            info = await fetch_movie_release_info(
                client, entry_id, tmdb_key, None,
                primary_release_date=detail.get("date"),
                vote_count=detail.get("votes", CINEMA_POPULAR_VOTES))
        except Exception as exc:
            logger.warning(f"Trending: release check failed for movie {entry_id}: {exc}")
            return True
        return info is None or info.get("status") not in ("Cinema", "Production")

    kept: list[str] = []
    checked = 0
    for start in range(0, len(ids), _RELEASED_BATCH):
        batch = ids[start:start + _RELEASED_BATCH]
        checked += len(batch)
        for entry_id, ok in zip(batch, await asyncio.gather(*(out_at_home(i) for i in batch))):
            if ok:
                kept.append(entry_id)
        if len(kept) >= limit:
            break
    dropped = checked - len(kept)
    if dropped:
        logger.info(f"Trending {endpoint}: left out {dropped} title(s) not out at home yet")
    return kept[:limit]


async def ensure_trending_snapshot(
    client: httpx.AsyncClient,
    tmdb_key: str,
    endpoint: str,
) -> "tuple[dict[str, int], float] | None":
    """The current (rankings, expires_at) for *endpoint*, fetched only if the
    stored snapshot has expired or came from another source.

    Concurrent callers share one fetch.  None when there is nothing to rank
    against: the source failed, or TMDB is the source and there is no key.
    """
    source_sig = trending_source_signature(endpoint)

    entry = get_cached_trending_snapshot_entry(endpoint, source_sig)
    if entry is not None:
        return entry

    inflight_event = _trending_inflight.get(endpoint)
    if inflight_event is not None:
        await inflight_event.wait()
        entry = get_cached_trending_snapshot_entry(endpoint, source_sig)
        if entry is not None:
            return entry

    event_to_set = asyncio.Event()
    _trending_inflight[endpoint] = event_to_set
    details: dict[str, dict] = {}
    try:
        if endpoint in ANIME_ENDPOINTS and not trending_source_url(endpoint):
            # AniList's list, for the trending catalogs addon.  A failed read
            # is not retried for a while: every anime poster would otherwise
            # try again, against a rate limit the anime art fetches share.
            if _trending_cooling_down(endpoint):
                return None
            from anime import fetch_anilist_trending
            films = endpoint == "anime_movie"
            # AniList calls a film released once it is in cinemas, so leaving
            # out what isn't home yet checks each one's TMDB dates (below) and
            # needs more of the list to have as many after.
            check_home = films and TRENDING_HIDE_UNRELEASED
            want = max(TRENDING_FETCH_COUNT, TRENDING_BROAD_FETCH_COUNT) * (2 if check_home else 1)
            ids = await _read_twice(lambda: fetch_anilist_trending(client, details, films=films, limit=want),
                                    f"AniList {endpoint}")
            if not ids:
                _trending_source_failed_at[endpoint] = time.monotonic()
                return None
            _trending_source_failed_at.pop(endpoint, None)
            if check_home:
                ids = await _released_only(client, tmdb_key, endpoint, ids, details)
            rankings = {entry_id: position for position, entry_id in enumerate(ids, start=1)}
            await asyncio.to_thread(set_cached_trending_snapshot, endpoint, rankings, source_sig, details)
            await asyncio.to_thread(_expire_overlapping_lists)
            return get_cached_trending_snapshot_entry(endpoint, source_sig) or (
                rankings, time.time() + 86400
            )

        # An operator-configured source replaces TMDB's list entirely.
        # A configured-but-broken source serves no ranks, so the sash
        # goes quiet instead of falling back to TMDB and looking like
        # the config worked.
        source_ids = await fetch_trending_source_ids(client, endpoint, details)
        if source_ids is not None:
            if not source_ids:
                # Deliberately NOT cached.  Writing an empty snapshot
                # would pin "no trending" for the full TTL on what is
                # usually a transient fetch failure; the source's own
                # retry cooldown already stops this from re-fetching per
                # request.  Nothing to rank against this time round.
                return None
            ids = source_ids
        else:
            if not tmdb_key:
                return None
            # Its own cooldown key: a custom source for this type is not in
            # play here, so the two never share one.
            cooldown_key = f"tmdb:{endpoint}"
            if _trending_cooling_down(cooldown_key):
                return None
            ids = await _read_twice(
                lambda: _fetch_tmdb_trending_ids(client, tmdb_key, endpoint, details),
                f"TMDB {endpoint}",
            )
            if not ids:
                _trending_source_failed_at[cooldown_key] = time.monotonic()
                return None
            _trending_source_failed_at.pop(cooldown_key, None)

        if endpoint not in ANIME_ENDPOINTS and anime_split():
            # The anime lists first, so their titles can be left off this one.
            for anime_endpoint in ANIME_ENDPOINTS:
                await ensure_trending_snapshot(client, tmdb_key, anime_endpoint)
            ids = _without_anime(endpoint, ids, details, _anime_list_tmdb_ids(trending_kind(endpoint)))
        if TRENDING_HIDE_UNRELEASED:
            ids = await _released_only(client, tmdb_key, endpoint, ids, details)
        rankings = {entry_id: position for position, entry_id in enumerate(ids, start=1)}
        await asyncio.to_thread(set_cached_trending_snapshot, endpoint, rankings, source_sig, details)
        if endpoint in ANIME_ENDPOINTS:
            await asyncio.to_thread(_expire_overlapping_lists)
        return get_cached_trending_snapshot_entry(endpoint, source_sig) or (
            rankings, time.time() + 86400
        )
    finally:
        event_to_set.set()
        _trending_inflight.pop(endpoint, None)


async def fetch_trending_rank_entry(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> "tuple[int | None, float | None]":
    """(rank, expires_at): the title's rank and when the snapshot it came from
    is replaced.  A poster that prints the rank is cached until then."""
    endpoint = (
        media_type if media_type in ANIME_ENDPOINTS
        else "tv" if media_type in ("tv", "series") else "movie"
    )
    entry = await ensure_trending_snapshot(client, tmdb_key, endpoint)
    if entry is None:
        return None, None
    snapshot, expires_at = entry

    rank = snapshot.get(str(tmdb_id))

    if rank:
        logger.info(f"Trending rank for {tmdb_id}: #{rank}")

    return rank, expires_at


async def fetch_anime_trending_rank_entry(
    client: httpx.AsyncClient,
    keys: list[str],
    tmdb_key: str,
    film: bool = False,
) -> "tuple[int | None, float | None]":
    """(rank, expires_at) on the anime series (or *film*) list: the best rank
    any of *keys* holds there.  The keys are every id the poster's title goes
    by — "anilist:<id>" for each AniList entry it maps to, and its TMDB id for
    a source that ranks by TMDB id."""
    entry = await ensure_trending_snapshot(client, tmdb_key, "anime_movie" if film else "anime")
    if entry is None:
        return None, None
    snapshot, expires_at = entry
    ranks = [snapshot[k] for k in keys if k in snapshot]
    rank = min(ranks) if ranks else None
    if rank:
        logger.info(f"Anime trending rank for {keys[0]}: #{rank}")
    return rank, expires_at


async def fetch_trending_rank(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> int | None:
    rank, _expires_at = await fetch_trending_rank_entry(client, tmdb_id, tmdb_key, media_type)
    return rank


async def fetch_trending_candidates(
    client: httpx.AsyncClient,
    tmdb_key: str,
    max_items: int = 500,
) -> list[dict]:
    """
    Build a deduped, ranked list of currently-trending titles for cache
    warming, by paginating TMDB's trending endpoint across movie/tv and
    day/week windows.

    Returns a list of dicts: ``{"tmdb_id": str, "media_type": "movie"|"tv"}``,
    ordered with the hottest (day-trending) titles first. Each (media_type,
    tmdb_id) pair appears at most once. May return fewer than *max_items* if
    TMDB's trending lists are exhausted first.
    """
    pages_per_list = max(1, (max_items + 19) // 20)  # 20 results per page

    async def _fetch_list(media_type: str, window: str) -> list[dict]:
        results: list[dict] = []
        for page in range(1, pages_per_list + 1):
            try:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/trending/{media_type}/{window}",
                    params={"api_key": tmdb_key, "page": page},
                )
                resp.raise_for_status()
                page_results = resp.json().get("results", [])
            except Exception as exc:
                logger.warning(f"Cache warm: trending fetch failed ({media_type}/{window} p{page}): {exc}")
                break
            if not page_results:
                break
            for item in page_results:
                results.append({"tmdb_id": str(item["id"]), "media_type": media_type})
        return results

    # Resolve each media type's custom source once. The day/week split is a TMDB
    # concept; a custom source is a single list, so it stands in for the "day"
    # pass and the "week" pass contributes nothing rather than duplicating it.
    source_details: dict[str, dict] = {"movie": {}, "tv": {}}
    sources = dict(zip(
        ("movie", "tv"),
        await asyncio.gather(
            fetch_trending_source_ids(client, "movie", source_details["movie"]),
            fetch_trending_source_ids(client, "tv", source_details["tv"]),
        ),
    ))

    # The ranking snapshot and the warm list are the same fetch.  Writing it here
    # is what makes the scheduled refresh actually refresh the ranks: without it
    # the warm cycle pulled the operator's list, threw the order away, and the
    # next /poster request fetched the identical list again to rebuild it — and
    # a title warmed this cycle could be rendered against yesterday's ranks.
    # An empty list means the fetch failed; leave the existing snapshot alone.
    #
    # Only an expired snapshot is replaced. Posters showing a rank are cached
    # until their snapshot expires, so replacing a current one would put new
    # ranks beside cached copies of the old ones. The warm loop keeps its own
    # schedule, so it would otherwise add a second daily turnover.
    for _media_type, _ids in sources.items():
        _sig = trending_source_signature(_media_type)
        if _ids and get_cached_trending_snapshot(_media_type, _sig) is None:
            await asyncio.to_thread(
                set_cached_trending_snapshot,
                _media_type,
                {entry_id: position for position, entry_id in enumerate(_ids, start=1)},
                _sig,
                source_details[_media_type],
            )

    async def _source_or_tmdb(media_type: str, window: str) -> list[dict]:
        ids = sources.get(media_type)
        if ids is None:
            return await _fetch_list(media_type, window)
        if window != "day":
            return []
        return [{"tmdb_id": tmdb_id, "media_type": media_type} for tmdb_id in ids]

    lists = await asyncio.gather(
        _source_or_tmdb("movie", "day"),
        _source_or_tmdb("tv", "day"),
        _source_or_tmdb("movie", "week"),
        _source_or_tmdb("tv", "week"),
    )

    # Round-robin merge so the result mixes movie/tv and prioritises the
    # day-trending lists before the week-trending ones, deduping as we go.
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for group in zip(*[l + [None] * (max(len(x) for x in lists) - len(l)) for l in lists]):
        for item in group:
            if item is None:
                continue
            key = (item["media_type"], item["tmdb_id"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
            if len(candidates) >= max_items:
                return candidates

    return candidates


async def fetch_popular_candidates(
    client: httpx.AsyncClient,
    tmdb_key: str,
    max_items: int = 500,
) -> list[dict]:
    """
    Build a deduped, ranked list of TMDB's "popular" titles for cache
    warming, by paginating the movie/tv popular endpoints.

    Unlike trending (day/week, very volatile), "popular" is a broad,
    slow-moving long-tail list — a useful complement to trending for cache
    warming since it covers steady-demand catalogue staples that trending
    alone would miss.

    Returns a list of dicts: ``{"tmdb_id": str, "media_type": "movie"|"tv"}``,
    each (media_type, tmdb_id) pair appearing at most once. May return fewer
    than *max_items* if TMDB's popular lists are exhausted first.
    """
    pages_per_list = max(1, (max_items + 19) // 20)  # 20 results per page

    async def _fetch_list(media_type: str) -> list[dict]:
        results: list[dict] = []
        for page in range(1, pages_per_list + 1):
            try:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/popular",
                    params={"api_key": tmdb_key, "page": page},
                )
                resp.raise_for_status()
                page_results = resp.json().get("results", [])
            except Exception as exc:
                logger.warning(f"Cache warm: popular fetch failed ({media_type} p{page}): {exc}")
                break
            if not page_results:
                break
            for item in page_results:
                results.append({"tmdb_id": str(item["id"]), "media_type": media_type})
        return results

    lists = await asyncio.gather(
        _fetch_list("movie"),
        _fetch_list("tv"),
    )

    # Round-robin merge so the result mixes movie/tv, deduping as we go.
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for group in zip(*[l + [None] * (max(len(x) for x in lists) - len(l)) for l in lists]):
        for item in group:
            if item is None:
                continue
            key = (item["media_type"], item["tmdb_id"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
            if len(candidates) >= max_items:
                return candidates

    return candidates


async def fetch_supplemental_candidates(
    client: httpx.AsyncClient,
    tmdb_key: str,
    max_items: int = 500,
) -> list[dict]:
    """
    Build a deduped, ranked list of cache-warming candidates from TMDB lists
    that trending/popular don't cover: critically-acclaimed catalogue staples
    (top rated) and titles currently airing/in theatres (now playing, on the
    air) — the kind of thing a user finds via a "Top Rated" or "Now Playing"
    catalog rather than trending/popular.

    Returns a list of dicts: ``{"tmdb_id": str, "media_type": "movie"|"tv"}``,
    each (media_type, tmdb_id) pair appearing at most once. May return fewer
    than *max_items* if these lists are exhausted first.
    """
    pages_per_list = max(1, (max_items + 19) // 20)  # 20 results per page

    async def _fetch_list(media_type: str, list_name: str) -> list[dict]:
        results: list[dict] = []
        for page in range(1, pages_per_list + 1):
            try:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{list_name}",
                    params={"api_key": tmdb_key, "page": page},
                )
                resp.raise_for_status()
                page_results = resp.json().get("results", [])
            except Exception as exc:
                logger.warning(f"Cache warm: {list_name} fetch failed ({media_type} p{page}): {exc}")
                break
            if not page_results:
                break
            for item in page_results:
                results.append({"tmdb_id": str(item["id"]), "media_type": media_type})
        return results

    lists = await asyncio.gather(
        _fetch_list("movie", "top_rated"),
        _fetch_list("tv", "top_rated"),
        _fetch_list("movie", "now_playing"),
        _fetch_list("tv", "on_the_air"),
    )

    # Round-robin merge across the four lists, deduping as we go.
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for group in zip(*[l + [None] * (max(len(x) for x in lists) - len(l)) for l in lists]):
        for item in group:
            if item is None:
                continue
            key = (item["media_type"], item["tmdb_id"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
            if len(candidates) >= max_items:
                return candidates

    return candidates


class IdResolveError(Exception):
    """The id lookup could not be completed (network, 5xx, bad key) — as
    opposed to a definite "no such title", which is a None result."""


class TmdbIdGone(IdResolveError):
    """TMDB answers 404 for this TMDB id: the entry was deleted, usually a
    duplicate merged into an older one. See mark_tmdb_id_gone."""


async def tmdb_find_by_imdb(
    client: httpx.AsyncClient,
    imdb_id: str,
    tmdb_key: str,
    media_type_hint: str | None = None,
    external_source: str = "imdb_id",
) -> dict | None:
    """
    Resolve an IMDB id (``tt...``) to a TMDB id via TMDB's /find endpoint.
    ``external_source="tvdb_id"`` looks up a TVDB id the same way.

    Returns ``{"tmdb_id": str, "media_type": "movie"|"tv"}``, preferring a
    result matching *media_type_hint* when both movie and tv results are
    present, or ``None`` if TMDB has no match for either. Raises
    ``IdResolveError`` when the lookup itself failed, so a caller can tell an
    outage from an unknown title and not cache the former.
    """
    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": tmdb_key, "external_source": external_source},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise IdResolveError(f"TMDB find failed for {imdb_id}: {exc}") from exc

    # A deleted entry can linger in /find for a while after its own page
    # 404s, and one that links a series' IMDb id would win the lookup again.
    movie_results = [r for r in data.get("movie_results") or []
                     if not tmdb_id_gone(str(r["id"]), "movie")]
    tv_results    = [r for r in data.get("tv_results") or []
                     if not tmdb_id_gone(str(r["id"]), "tv")]

    # Requests say "series"; TMDB says "tv". Unnormalised, a series whose
    # IMDb id some movie entry also claims (a duplicate someone added) was
    # resolved to that movie, and the answer kept for the id map's 90 days.
    if media_type_hint == "series":
        media_type_hint = "tv"
    if media_type_hint == "tv" and tv_results:
        return {"tmdb_id": str(tv_results[0]["id"]), "media_type": "tv"}
    if media_type_hint == "movie" and movie_results:
        return {"tmdb_id": str(movie_results[0]["id"]), "media_type": "movie"}
    if movie_results:
        return {"tmdb_id": str(movie_results[0]["id"]), "media_type": "movie"}
    if tv_results:
        return {"tmdb_id": str(tv_results[0]["id"]), "media_type": "tv"}
    return None


# Persisted IMDb -> TMDB id map. A /find call per request would double TMDB
# traffic and add a round-trip even to composite-cache hits, so the answer is
# kept for a long time — the mapping essentially never changes. A definite
# "TMDB has no record" is kept for a day so a run of requests for the same
# unlinked title doesn't re-ask; a failed lookup is never cached.
_IDMAP_VERSION = "v1"
_IDMAP_TTL_SECONDS = 90 * 86400
_IDMAP_MISS_TTL_SECONDS = 86400
_IDMAP_MISS = {"__miss__": True}


def _idmap_key(imdb_id: str, media_type: str) -> str:
    return f"idmap:{_IDMAP_VERSION}:imdb:{imdb_id}:{media_type}"


def _gone_key(tmdb_id: str, media_type: str) -> str:
    kind = "tv" if media_type in ("tv", "series") else "movie"
    return f"idmap:{_IDMAP_VERSION}:gone:{kind}:{tmdb_id}"


def tmdb_id_gone(tmdb_id: str, media_type: str) -> bool:
    """Whether TMDB answered 404 for this id lately (mark_tmdb_id_gone)."""
    return get_cached_tvdb_json(_gone_key(tmdb_id, media_type)) is not None


def forget_imdb_mapping_to(imdb_id: str, tmdb_id: str) -> None:
    """Drop the id-map rows that resolve *imdb_id* to *tmdb_id*."""
    for requested in ("movie", "tv", "series"):
        key = _idmap_key(imdb_id, requested)
        cached = get_cached_tvdb_json(key)
        if cached and cached.get("tmdb_id") == tmdb_id:
            delete_cached_tvdb_json(key)


def mark_tmdb_id_gone(tmdb_id: str, media_type: str, imdb_id: str | None = None) -> None:
    """Remember that TMDB 404s *tmdb_id*, and forget the id-map rows that led to it.

    TMDB deletes entries (a duplicate merged into the older one, a junk
    upload), but catalogs and our own id map keep pointing at them. Marked
    gone, a request carrying the id alongside an IMDb id renders from the
    IMDb id instead, and an IMDb id mapped to it is looked up afresh. Kept a
    day, like a /find miss, so an entry TMDB restores comes back.
    """
    kind = "tv" if media_type in ("tv", "series") else "movie"
    set_cached_tvdb_json(_gone_key(tmdb_id, kind), {"gone": True}, _IDMAP_MISS_TTL_SECONDS)
    delete_cached_tvdb_json(_reverse_idmap_key(tmdb_id, kind))
    if imdb_id:
        forget_imdb_mapping_to(imdb_id, tmdb_id)
    logger.warning(f"TMDB {kind}/{tmdb_id} is gone (404) — marked for a day"
                   + (f", id map for {imdb_id} cleared" if imdb_id else ""))


# Anthologies IMDb files as one series with a season per story, where TMDB
# lists each story as a show of its own — so /find has nothing to say for the
# IMDb id, or only sometimes. Installments are listed oldest first, and the
# IMDb id renders as the newest one that has premiered, the way IMDb's own page
# fronts the current season. Which one that is changes when a new story airs,
# so the answer is kept for a day rather than the id map's ninety.
ANTHOLOGY_INSTALLMENTS: dict[str, tuple[str, ...]] = {
    # Monster: Dahmer, Ed Gein, Lizzie Borden
    "tt13207736": ("113988", "286801", "299939"),
}
_ANTHOLOGY_TTL_SECONDS = 86400


def _anthology_key(imdb_id: str) -> str:
    return f"idmap:{_IDMAP_VERSION}:anthology:{imdb_id}"


async def _resolve_anthology(
    client: httpx.AsyncClient,
    imdb_id: str,
    installments: tuple[str, ...],
    tmdb_key: str,
    key: str,
) -> dict:
    """The newest installment of *imdb_id*'s anthology that has premiered.

    Asks TMDB for each installment's first air date, newest first, and stops at
    the first one already out. The oldest is taken on trust — it is only listed
    because it aired. A failed lookup skips that installment and leaves the
    answer uncached, so an outage never pins the anthology to an older story.
    """
    today = _date.today().isoformat()
    chosen = installments[0]
    complete = True
    for tmdb_id in reversed(installments[1:]):
        try:
            resp = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                params={"api_key": tmdb_key},
            )
            resp.raise_for_status()
            first_air = resp.json().get("first_air_date") or ""
        except Exception as exc:
            logger.warning(f"Anthology {imdb_id}: TMDB tv/{tmdb_id} lookup failed: {exc}")
            complete = False
            continue
        if first_air and first_air <= today:
            chosen = tmdb_id
            break

    result = {"tmdb_id": chosen, "media_type": "tv"}
    logger.info(f"Resolved anthology {imdb_id} -> TMDB tv/{chosen} (newest aired installment)")
    if complete:
        set_cached_tvdb_json(key, result, _ANTHOLOGY_TTL_SECONDS)
    return result


async def resolve_imdb_to_tmdb(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str,
    tmdb_key: str | None,
) -> dict | None:
    """
    The TMDB identity of an IMDb id, cached: ``{"tmdb_id", "media_type"}``.

    With a key, TMDB's /find is authoritative and may correct *media_type* —
    an IMDb id names one title, and TMDB knows which list it lives in. Without
    a key, Cinemeta's ``moviedb_id`` stands in (no key needed; the type is
    taken on trust). None means neither source has a TMDB id for it — the
    caller decides whether the Cinemeta spine can carry the title instead.
    Raises ``IdResolveError`` only when a keyed TMDB lookup failed outright.

    An IMDb id in ``ANTHOLOGY_INSTALLMENTS`` resolves, with a key, to its
    newest aired installment instead of going through /find.
    """
    anthology = ANTHOLOGY_INSTALLMENTS.get(imdb_id) if tmdb_key else None
    key = _anthology_key(imdb_id) if anthology else _idmap_key(imdb_id, media_type)
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        if cached.get("__miss__"):
            return None
        return {"tmdb_id": cached["tmdb_id"], "media_type": cached["media_type"]}

    # A library grid loading fires the same uncached id from many tiles at
    # once; the first lookup answers for all of them.
    inflight = _idmap_inflight.get(key)
    if inflight is not None:
        return await inflight
    fut: "asyncio.Future[dict | None]" = asyncio.get_running_loop().create_future()
    _idmap_inflight[key] = fut
    try:
        if anthology:
            result = await _resolve_anthology(client, imdb_id, anthology, tmdb_key, key)
        else:
            result = await _resolve_imdb_to_tmdb_uncached(client, imdb_id, media_type, tmdb_key, key)
    except BaseException as exc:
        fut.set_exception(exc)
        raise
    else:
        fut.set_result(result)
        return result
    finally:
        _idmap_inflight.pop(key, None)


_idmap_inflight: "dict[str, asyncio.Future]" = {}


def _reverse_idmap_key(tmdb_id: str, media_type: str) -> str:
    kind = "tv" if media_type in ("tv", "series") else "movie"
    return f"idmap:{_IDMAP_VERSION}:tmdb:{kind}:{tmdb_id}"


_REVERSE_IDMAP_RETRY_SECS = 60.0
_reverse_idmap_failed_at: dict[str, float] = {}


async def resolve_tmdb_to_imdb(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    tmdb_key: str,
) -> str | None:
    """The IMDb id TMDB links *tmdb_id* to, cached; None when it links none.

    What a request keeps as its IMDb id once a TMDB id is in charge: an
    anthology's installments are shows of their own on TMDB with no IMDb link
    (IMDb has one id for the whole anthology), so an IMDb id sent alongside one
    of them describes a different title and must not feed ratings, sashes or
    quality. Raises ``IdResolveError`` when the lookup itself failed.
    """
    key = _reverse_idmap_key(tmdb_id, media_type)
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        return cached.get("imdb_id") or None
    # Checked only past the cache: marking an id gone deletes its row.
    if tmdb_id_gone(tmdb_id, media_type):
        raise TmdbIdGone(f"TMDB {media_type}/{tmdb_id} is gone (404)")
    # A lookup that just failed isn't sent again for a while: during a TMDB
    # blip (a 429 above all) every request for the title would otherwise add
    # another call.
    failed_at = _reverse_idmap_failed_at.get(key)
    if failed_at is not None and time.monotonic() - failed_at < _REVERSE_IDMAP_RETRY_SECS:
        raise IdResolveError(f"TMDB external_ids for {tmdb_id} failed moments ago")

    inflight = _idmap_inflight.get(key)
    if inflight is not None:
        return await inflight
    fut: "asyncio.Future[str | None]" = asyncio.get_running_loop().create_future()
    _idmap_inflight[key] = fut
    try:
        kind = "tv" if media_type in ("tv", "series") else "movie"
        try:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/external_ids",
                params={"api_key": tmdb_key},
            )
            if resp.status_code == 404:
                mark_tmdb_id_gone(tmdb_id, kind)
                raise TmdbIdGone(f"TMDB {kind}/{tmdb_id} is gone (404)")
            resp.raise_for_status()
            imdb_id = (resp.json().get("imdb_id") or "").strip() or None
        except TmdbIdGone:
            raise
        except Exception as exc:
            if len(_reverse_idmap_failed_at) >= 10000:
                _reverse_idmap_failed_at.clear()
            _reverse_idmap_failed_at[key] = time.monotonic()
            raise IdResolveError(f"TMDB external_ids failed for {kind}/{tmdb_id}: {exc}") from exc
        _reverse_idmap_failed_at.pop(key, None)
        # A link TMDB adds later should be picked up, so "none" is kept only a day.
        set_cached_tvdb_json(
            key, {"imdb_id": imdb_id or ""},
            _IDMAP_TTL_SECONDS if imdb_id else _IDMAP_MISS_TTL_SECONDS,
        )
    except BaseException as exc:
        fut.set_exception(exc)
        raise
    else:
        fut.set_result(imdb_id)
        return imdb_id
    finally:
        _idmap_inflight.pop(key, None)


async def _resolve_imdb_to_tmdb_uncached(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str,
    tmdb_key: str | None,
    key: str,
) -> dict | None:
    result: dict | None = None
    if tmdb_key:
        # TMDB is authoritative for its own linkage: a title /find doesn't
        # return is unlinked, whatever an older Cinemeta document says.
        result = await tmdb_find_by_imdb(client, imdb_id, tmdb_key, media_type)
        if result is None:
            set_cached_tvdb_json(key, _IDMAP_MISS, _IDMAP_MISS_TTL_SECONDS)
            return None
        logger.info(
            f"Resolved {imdb_id} -> TMDB {result['media_type']}/{result['tmdb_id']} via /find"
        )
        set_cached_tvdb_json(
            _reverse_idmap_key(result["tmdb_id"], result["media_type"]),
            {"imdb_id": imdb_id}, _IDMAP_TTL_SECONDS,
        )
    else:
        cm_tmdb_id = await cinemeta.resolve_tmdb_id(client, imdb_id, media_type)
        if cm_tmdb_id is None:
            # Cinemeta may simply have been unreachable — never cache that as
            # a miss.
            return None
        result = {"tmdb_id": cm_tmdb_id, "media_type": media_type}
        logger.info(f"Resolved {imdb_id} -> TMDB {media_type}/{cm_tmdb_id} via Cinemeta")

    set_cached_tvdb_json(key, result, _IDMAP_TTL_SECONDS)
    return result


async def resolve_tvdb_to_tmdb(
    client: httpx.AsyncClient,
    tvdb_id: int,
    media_type: str,
    tmdb_key: str,
) -> dict | None:
    """The TMDB identity of a TVDB id, cached like the IMDb map: a request
    that carries only ``tvdb:<id>`` renders from TMDB whenever TMDB links
    the title.  None when it doesn't; raises ``IdResolveError`` on a failed
    lookup, which is never cached.

    Series only: TMDB keeps no TVDB ids for movies, so /find answers a TVDB
    id with TV results alone, and TVDB numbers its movies and series apart —
    a movie's id asked there would name whatever series shares the number.
    A movie goes to TVDB directly (None)."""
    if media_type not in ("tv", "series"):
        return None
    key = f"idmap:{_IDMAP_VERSION}:tvdb:series:{tvdb_id}"
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        return None if cached.get("__miss__") else cached
    result = await tmdb_find_by_imdb(client, str(tvdb_id), tmdb_key, "tv",
                                     external_source="tvdb_id")
    if result is not None and result["media_type"] != "tv":
        result = None
    if result is None:
        set_cached_tvdb_json(key, _IDMAP_MISS, _IDMAP_MISS_TTL_SECONDS)
        return None
    logger.info(f"Resolved tvdb:{tvdb_id} -> TMDB {result['media_type']}/{result['tmdb_id']} via /find")
    set_cached_tvdb_json(key, result, _IDMAP_TTL_SECONDS)
    return result


def _normalize_manifest_url(url: str) -> str:
    """Normalise a user-pasted addon install link to a manifest.json URL."""
    url = url.strip()
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    if not url.endswith("/manifest.json"):
        url = url.rstrip("/") + "/manifest.json"
    return url


async def fetch_catalog_candidates(
    client: httpx.AsyncClient,
    catalog_urls: list[str],
    tmdb_key: str,
    max_items_per_catalog: int = 100,
) -> list[dict]:
    """
    Build a deduped list of ``{"tmdb_id", "media_type"}`` candidates by
    fetching the catalogs exposed by the given Stremio addon manifest URLs,
    the same way a Stremio client would when a user opens that catalog.

    IMDB ids (the common case for Cinemeta-backed catalogs) are resolved to
    TMDB ids via TMDB's /find endpoint. ``tmdb:<id>`` ids are used directly.
    Any other id namespace (kitsu/mal/anilist/etc.) is skipped — there's no
    TMDB mapping for those, so warming can't cover that title.
    """
    if not catalog_urls or not tmdb_key:
        return []

    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    resolve_sem = asyncio.Semaphore(10)

    async def _resolve(meta_id: str, media_type: str) -> dict | None:
        if meta_id.startswith("tmdb:"):
            return {"tmdb_id": meta_id.split(":", 1)[1], "media_type": media_type}
        if meta_id.startswith("tt"):
            # Through the persisted id map, so a catalog re-warmed every cycle
            # costs one /find per title ever, and live imdb_id-only requests
            # for the same titles find the answer already there.
            async with resolve_sem:
                try:
                    return await resolve_imdb_to_tmdb(client, meta_id, media_type, tmdb_key)
                except IdResolveError as exc:
                    logger.warning(f"Cache warm: {exc}")
                    return None
        return None

    for raw_url in catalog_urls:
        manifest_url = _normalize_manifest_url(raw_url)
        try:
            resp = await client.get(manifest_url, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as exc:
            logger.warning(f"Cache warm: catalog manifest fetch failed for {manifest_url}: {exc}")
            continue

        base = manifest_url[: -len("/manifest.json")]
        catalogs = manifest.get("catalogs") or []
        if not catalogs:
            logger.warning(f"Cache warm: no catalogs in manifest {manifest_url}")
            continue

        for catalog in catalogs:
            cat_type = catalog.get("type")
            cat_id   = catalog.get("id")
            if not cat_type or not cat_id:
                continue

            metas: list[dict] = []
            while len(metas) < max_items_per_catalog:
                skip = len(metas)
                path = (
                    f"/catalog/{cat_type}/{cat_id}.json"
                    if skip == 0
                    else f"/catalog/{cat_type}/{cat_id}/skip={skip}.json"
                )
                try:
                    page_resp = await client.get(f"{base}{path}", timeout=15.0, follow_redirects=True)
                    page_resp.raise_for_status()
                    page_metas = page_resp.json().get("metas") or []
                except Exception as exc:
                    logger.warning(f"Cache warm: catalog fetch failed for {base}{path}: {exc}")
                    break
                if not page_metas:
                    break
                metas.extend(page_metas)

            metas = metas[:max_items_per_catalog]

            resolved = await asyncio.gather(*(
                _resolve(
                    meta.get("id", ""),
                    "tv" if meta.get("type") in ("series", "tv") else "movie",
                )
                for meta in metas
                if meta.get("id")
            ))

            added = 0
            for item in resolved:
                if item is None:
                    continue
                key = (item["media_type"], item["tmdb_id"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(item)
                added += 1

            logger.info(
                f"Cache warm: catalog {cat_type}/{cat_id} from {base} — "
                f"{len(metas)} items, {added} new candidates"
            )

    return candidates


def _parse_tmdb_date(value: str | None) -> _date | None:
    try:
        return _date.fromisoformat((value or "")[:10])
    except (TypeError, ValueError):
        return None


def cinema_window_days(vote_count: int | None) -> int:
    """Days a theatrical-only movie may stay "Cinema" with no digital date
    published before it is assumed to be streaming; 0 means no limit short of
    CINEMA_MAX_AGE_YEARS.

    TMDB is often slow to add a digital date and sometimes never does, so most
    films get the usual studio window.  A film with enough votes is one TMDB
    will keep current, and one that can play for months (Oppenheimer ran ~120
    days), so it gets the longer window.  An unknown vote count is not
    evidence of popularity."""
    try:
        votes = int(vote_count) if vote_count is not None else None
    except (TypeError, ValueError):
        votes = None
    popular = (CINEMA_POPULAR_VOTES > 0 and votes is not None
               and votes >= CINEMA_POPULAR_VOTES)
    return CINEMA_POPULAR_DIGITAL_DAYS if popular else CINEMA_ASSUMED_DIGITAL_DAYS


def _compute_movie_status_from_dates(
    theatrical_date: _date | None,
    digital_date: _date | None,
    physical_date: _date | None,
    tmdb_status: str | None,
    premiere_date: _date | None = None,
    vote_count: int | None = None,
) -> str:
    today = _date.today()
    has_physical = physical_date is not None and physical_date <= today
    has_digital = digital_date is not None and digital_date <= today
    has_theatrical = theatrical_date is not None and theatrical_date <= today

    if has_physical:
        return "Physical"
    elif has_digital:
        return "Streaming"
    elif has_theatrical:
        days_out = (today - theatrical_date).days
        if CINEMA_MAX_AGE_YEARS > 0 and days_out > CINEMA_MAX_AGE_YEARS * 365:
            return "Streaming"
        # A published digital date, even a future one, is the answer, so the
        # window is only a stand-in for a date nobody has given.
        window = cinema_window_days(vote_count)
        if digital_date is None and window > 0 and days_out > window:
            return "Streaming"
        return "Cinema"
    elif tmdb_status == "Released" and not any(
        d is not None and d > today
        for d in (theatrical_date, digital_date, physical_date, premiere_date)
    ):
        # "Released" with no dates at all is a film TMDB knows nothing else
        # about — assume it is out somewhere.  "Released" with only *future*
        # dates is TMDB flipping the flag early (it does, weeks ahead of a
        # limited theatrical run); the dates are the better witness.  A
        # festival premiere is not a release, so it never makes a title
        # "Cinema" above — but a future one is still proof it is not out.
        return "Streaming"
    else:
        return "Production"


def _release_info_expiry(info: dict) -> int:
    """Expiry for a release row, clamped to the title's next published date.

    TMDB usually knows a film's digital date before the film gets there, so a
    title "releasing soon" does not need predicting — it needs its cache row to
    expire on the day it moves.  Anything already in the past is ignored: it is
    baked into the status.
    """
    upcoming: list[int] = []
    today = _date.today()
    for key in ("theatrical_date", "digital_date", "physical_date", "premiere_date"):
        parsed = _parse_tmdb_date(info.get(key))
        if parsed is not None and parsed > today:
            upcoming.append(int(_datetime.combine(parsed, _time.min).timestamp()))
    status = info.get("status")
    # "Streaming" assumed from the cinema window (cinema_window_days) is a
    # guess standing in for a digital date TMDB hasn't published, so the row
    # keeps the Cinema tier's daily re-check until it does.  Past
    # CINEMA_MAX_AGE_YEARS nothing is expected any more.
    if status == "Streaming" and _theatrical_only_within_max_age(info, today):
        status = "Cinema"
    return release_status_expiry(status, upcoming_dates=upcoming)


def _theatrical_only_within_max_age(info: dict, today: _date) -> bool:
    """A row whose only past release is theatrical, younger than
    CINEMA_MAX_AGE_YEARS (or with that gate off)."""
    theatrical = _parse_tmdb_date(info.get("theatrical_date"))
    if theatrical is None or theatrical > today:
        return False
    for key in ("digital_date", "physical_date"):
        parsed = _parse_tmdb_date(info.get(key))
        if parsed is not None and parsed <= today:
            return False
    return CINEMA_MAX_AGE_YEARS <= 0 or (today - theatrical).days <= CINEMA_MAX_AGE_YEARS * 365


def _release_info_is_current(info: dict) -> bool:
    """Whether a cached release row was written by code that read every date
    type.  Rows predate the ``premiere_date`` field only if they were written
    when limited-theatrical (type 2) and premiere (type 1) dates were skipped;
    one that recorded no dates at all may simply have missed them, and sat at
    "Streaming" for a month on the strength of TMDB's early "Released" flag.
    A dated legacy row is trusted — it had a full release to key off."""
    if "premiere_date" in info:
        return True
    return any(info.get(k) for k in ("theatrical_date", "digital_date", "physical_date"))


def _never_looked(info: dict) -> bool:
    """A row written by the pre-release shortcut, without asking TMDB for
    dates: marked since this change, and before it, a row with no dates
    that isn't marked as having looked."""
    if info.get("pre_release_shortcut"):
        return True
    return (not info.get("dates_checked")
            and not any(info.get(k) for k in ("theatrical_date", "digital_date",
                                               "physical_date", "premiere_date")))


async def fetch_movie_release_info(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    tmdb_status: str | None,
    primary_release_date: str | None = None,
    vote_count: int | None = None,
) -> dict | None:
    """Cached TMDB movie release-date facts used by release-status and freshness sashes.

    *vote_count* (TMDB's) picks the film's cinema window — see
    cinema_window_days — for the ``status`` returned; the dates don't depend
    on it.

    A film TMDB still calls unreleased (Planned, In Production, ...) normally
    skips /release_dates: there is nothing out to date.  Given its primary
    release date, though, and that date still ahead, the dates are fetched
    after all — that date alone doesn't say whether the film opens in
    cinemas or streams (Animals: US theatrical and Netflix both Oct 9)."""
    cache_key = f"movie_{tmdb_id}"
    _primary = _parse_tmdb_date(primary_release_date)
    _look_anyway = _primary is not None and _primary > _date.today()
    cached = get_cached_movie_release_info(cache_key)
    if (cached and _release_info_is_current(cached)
            and not (_look_anyway and _never_looked(cached))):
        cached["status"] = _compute_movie_status_from_dates(
            _parse_tmdb_date(cached.get("theatrical_date")),
            _parse_tmdb_date(cached.get("digital_date")),
            _parse_tmdb_date(cached.get("physical_date")),
            tmdb_status,
            _parse_tmdb_date(cached.get("premiere_date")),
            vote_count,
        )
        return cached

    result: str | None = None
    info: dict[str, str | None] = {
        "status": None,
        "theatrical_date": None,
        "digital_date": None,
        "physical_date": None,
        "premiere_date": None,
    }

    _pre_release = {"In Production", "Post Production", "Planned", "Rumored"}
    if tmdb_status in _pre_release and not _look_anyway:
        info["status"] = "Production"
        # Marked, so a caller with a dated primary release can still look.
        info["pre_release_shortcut"] = True
        set_cached_movie_release_info(cache_key, info)
        return info
    if tmdb_status == "Cancelled":
        info["status"] = "Cancelled"
        set_cached_movie_release_info(cache_key, info)
        return info

    try:
        logger.info(f"External API Call: TMDB release_dates for movie {tmdb_id}")
        resp = await client.get(
            f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
            params={"api_key": tmdb_key},
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"fetch_movie_release_info failed for {tmdb_id}: {exc}")
        return None

    # Release-status decisions key off the EARLIEST date a film enters each
    # window (its first availability anywhere), so a title already streaming or
    # on disc in one region isn't held at "Cinema" just because a later regional
    # digital/physical date is still pending.  ``latest_digital`` is tracked
    # separately for the freshness "just added" sash, which wants the most
    # recent digital date rather than the first.
    earliest_theatrical: _date | None = None
    earliest_digital: _date | None = None
    latest_digital: _date | None = None
    earliest_physical: _date | None = None
    earliest_premiere: _date | None = None

    for entry in resp.json().get("results", []):
        for rd in entry.get("release_dates", []):
            rtype = rd.get("type")
            rdate = _parse_tmdb_date(rd.get("release_date"))
            if rdate is None:
                continue
            if rtype == 5:
                if earliest_physical is None or rdate < earliest_physical:
                    earliest_physical = rdate
            elif rtype in (4, 6):   # digital or TV broadcast
                if earliest_digital is None or rdate < earliest_digital:
                    earliest_digital = rdate
                # A TV broadcast says a film is out of cinemas, which is all the
                # status needs, but not that it was just added anywhere: Canal+
                # airing Point Break (1991) in September 2026 made it "New".
                if rtype == 4 and (latest_digital is None or rdate > latest_digital):
                    latest_digital = rdate
            elif rtype in (2, 3):   # theatrical, limited or wide — both are cinemas
                if earliest_theatrical is None or rdate < earliest_theatrical:
                    earliest_theatrical = rdate
            elif rtype == 1:        # festival / premiere — dated, but not a release
                if earliest_premiere is None or rdate < earliest_premiere:
                    earliest_premiere = rdate

    result = _compute_movie_status_from_dates(
        earliest_theatrical,
        earliest_digital,
        earliest_physical,
        tmdb_status,
        earliest_premiere,
        vote_count,
    )

    info = {
        "status": result,
        "theatrical_date": earliest_theatrical.isoformat() if earliest_theatrical else None,
        "digital_date": earliest_digital.isoformat() if earliest_digital else None,
        "physical_date": earliest_physical.isoformat() if earliest_physical else None,
        "digital_latest_date": latest_digital.isoformat() if latest_digital else None,
        "premiere_date": earliest_premiere.isoformat() if earliest_premiere else None,
        "dates_checked": True,
    }
    set_cached_movie_release_info(cache_key, info, _release_info_expiry(info))
    return info


# How long after a film's first release a digital date still counts as the
# film arriving at home ("Just Added" / "New") rather than a re-release.
JUST_ADDED_MAX_FILM_AGE_DAYS = 365


async def fetch_recent_movie_digital_release_date(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    tmdb_status: str | None,
    *,
    max_age_days: int = 14,
) -> str | None:
    """Return the most recent TMDB digital/TV release date when it is fresh."""
    info = await fetch_movie_release_info(client, tmdb_id, tmdb_key, tmdb_status)
    if not info:
        return None
    # "Just added" wants the most recent digital date; fall back to the plain
    # digital_date for cache entries written before that field was tracked.
    digital = _parse_tmdb_date(info.get("digital_latest_date") or info.get("digital_date"))
    if digital is None:
        return None
    age = (_date.today() - digital).days
    if not 0 <= age <= max_age_days:
        return None
    # A fresh digital date on a film first released years ago is a regional
    # re-release or a remaster, not a new film arriving at home — and rows
    # cached before TV broadcasts were left out still count those.
    first = min(filter(None, (_parse_tmdb_date(info.get(k)) for k in
                              ("theatrical_date", "digital_date", "physical_date"))))
    if (digital - first).days > JUST_ADDED_MAX_FILM_AGE_DAYS:
        return None
    return digital.isoformat()


# The status a movie moves to when each dated window opens — the second half
# of a dated sash ("Oct 16 Cinema"), so a viewer can tell a theatrical date
# from one they can actually watch at home.
_RELEASE_WINDOW_STATUS = {
    "theatrical_date": "Cinema",
    "digital_date":    "Streaming",
    "physical_date":   "Physical",
}


_SAME_DAY_RANK = {"Streaming": 0, "Physical": 1, "Cinema": 2}


async def fetch_upcoming_movie_release(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    tmdb_status: str | None,
    *,
    status: str | None,
    primary_release_date: str | None = None,
) -> tuple[str, str] | None:
    """The next published date a "Cinema" / "Production" movie moves on, as
    ``(YYYY-MM-DD, window)`` where *window* is the status it moves to —
    "Cinema", "Streaming" or "Physical".

    "Production" waits on its first release anywhere — theatrical, digital or
    disc, whichever TMDB has dated soonest.  The details endpoint's primary
    release date stands in when the release-dates row has none: the pre-release
    shortcut in fetch_movie_release_info never asks TMDB for them, and that
    date is all but always the theatrical one.  "Cinema" is already in
    theatres, so only the home dates count — its theatrical date would just
    restate the status.  Anything else has nothing to wait for.
    """
    if status not in ("Cinema", "Production"):
        return None
    info = await fetch_movie_release_info(client, tmdb_id, tmdb_key, tmdb_status,
                                          primary_release_date=primary_release_date) or {}
    keys = (
        ("theatrical_date", "digital_date", "physical_date")
        if status == "Production" else ("digital_date", "physical_date")
    )
    today = _date.today()
    upcoming: list[tuple[_date, str]] = []
    for key in keys:
        parsed = _parse_tmdb_date(info.get(key))
        if parsed is not None and parsed > today:
            upcoming.append((parsed, _RELEASE_WINDOW_STATUS[key]))
    if not upcoming and status == "Production":
        primary = _parse_tmdb_date(primary_release_date)
        if primary is not None and primary > today:
            upcoming.append((primary, "Cinema"))
    if not upcoming:
        return None
    # Two windows on the same day (a streamer's film opening in a few cinemas
    # the day it streams): the film reaches home that day, so the home window
    # names it, streaming first — not whichever sorts first alphabetically.
    soonest, window = min(upcoming, key=lambda u: (u[0], _SAME_DAY_RANK[u[1]]))
    return soonest.isoformat(), window


async def fetch_release_status(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str,
    tmdb_status: str | None,
    vote_count: int | None = None,
) -> str | None:
    """
    Determine the current release status for the info sash.

    TV shows: mapped from the TMDB ``status`` field (already fetched as part
    of poster metadata, so no extra API call is needed).  That mapping wins over
    the cached row, which exists only for requests that arrive without a status.

    Movies: consults ``/movie/{id}/release_dates`` to determine whether the
    film is on physical media (Physical), digital/streaming (Streaming), still
    theatrical-only (Cinema), or not yet released (Production).  A film
    theatrical-only for longer than its cinema window (cinema_window_days,
    picked by *vote_count*) with no digital date published reads Streaming.  The dates are
    cached in ``movie_release_info_cache`` with a per-row deadline — the status
    tier, or the film's next published release date when TMDB has told us one —
    and ``release_status_cache`` mirrors the status derived from them.

    Returns one of: "Physical" | "Streaming" | "Cinema" | "Production" |
                    "Returning" | "Ended" | "Cancelled" | None.
    """
    cache_key = f"{media_type}_{tmdb_id}"
    result: str | None = None
    info: dict | None = None

    if media_type in ("tv", "series"):
        # No extra API call — map the TMDB status field we already have.
        # "Ended" and "Cancelled" both mean the show has fully aired; assume
        # it's on streaming rather than showing a run-status label that says
        # nothing about where you can actually watch it.  "Cancelled" is kept
        # distinct so users know the story may be unresolved.
        _tv_map: dict[str, str] = {
            "Returning Series": "Airing",
            "In Production":    "Production",
            "Planned":          "Production",
            "Pilot":            "Production",
            "Ended":            "Ended",
            "Cancelled":        "Cancelled",
            "Canceled":         "Cancelled",
        }
        # Deliberately ahead of the cache read.  *tmdb_status* arrived with the
        # poster metadata this request already fetched, so it is both free and
        # newer than anything stored — and the cached value it replaces has a
        # 60-90 day tier behind it, which is long enough for a revived show to
        # sit at "Ended" for two months after TMDB says "Returning Series".
        # The cache still covers the case where no status came with the request.
        result = _tv_map.get(tmdb_status or "")
        cached = get_cached_release_status(cache_key)
        if not result:
            return cached
        # Only written when it actually moved (or its row lapsed), so this stays
        # a rare write rather than one per request.
        if cached != result:
            set_cached_release_status(cache_key, result)
        return result

    # The release-info row is the source of truth for movies: it is cached on
    # the same deadline, and a hit recomputes the status from the stored dates
    # rather than replaying a snapshot.  The status row used to short-circuit
    # this, which meant a row written from incomplete dates could sit for its
    # whole tier — "Streaming" is thirty days — shadowing the corrected answer.
    cached = get_cached_release_status(cache_key)
    info = await fetch_movie_release_info(client, tmdb_id, tmdb_key, tmdb_status,
                                          vote_count=vote_count)
    result = (info or {}).get("status")
    if not result:
        return cached

    # Mirrored for the cache stats and for requests that arrive while the
    # info fetch is failing; only written when it actually moved.
    if cached != result:
        # Movies carry published dates, so the status row can be told exactly
        # when it is next allowed to be wrong.
        set_cached_release_status(cache_key, result, _release_info_expiry(info))
    return result


# ---------------------------------------------------------------------------
# Logo rendering (onto poster)
# ---------------------------------------------------------------------------



def composite_logo(
    image: Image.Image,
    logo: Image.Image,
    *,
    max_w_ratio: float = LOGO_MAX_W_RATIO,
    max_h_ratio: float = LOGO_MAX_H_RATIO,
    bottom_ratio: float = LOGO_BOTTOM_RATIO,
    bottom_anchor: bool = False,
) -> None:
    width, height = image.size

    max_w = int(width  * max_w_ratio)
    # Height is bounded by BOTH the ratio and an absolute pixel ceiling, so a
    # raised Height slider can't let tall logos take over the poster.  The
    # ceiling is set for the 750-tall canvas and scales with a larger one.
    abs_max_h = LOGO_ABS_MAX_H * height // POSTER_HEIGHT
    max_h = min(int(height * max_h_ratio), abs_max_h)

    # ── Tight crop: ignore faint glow / halo / anti-alias pixels ──────────────
    # A plain getbbox() keys off ANY non-zero alpha, so baked-in soft shadows,
    # outer glows, or stray anti-aliased specks inflate the bounding box and
    # throw off the width-based normalisation below.  Threshold the alpha first
    # so only reasonably solid pixels define the box, then fall back to the full
    # alpha bbox if thresholding leaves nothing (e.g. a deliberately faint logo).
    alpha = logo.getchannel("A")
    solid = alpha.point(lambda a: 255 if a > 32 else 0)
    bbox  = solid.getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)

    lw, lh = logo.width, logo.height
    if lw <= 0 or lh <= 0:
        return

    # ── Normalise size by AREA, with hard caps on both axes ───────────────────
    # We target a constant geometric mean of the two caps (one overall size),
    # then clamp to the caps preserving aspect ratio.  BOTH caps are now hard
    # ceilings: the configured Width and Height ratios are the true maximums a
    # logo will ever reach.  Fill stretching may grow a slim logo UP TO a cap,
    # but never past it — so logos can't sprawl toward the borders.
    aspect = lw / lh

    # Orientation, kept for the sizing telemetry below: -1 (tall) .. +1 (wide).
    orient    = float(np.tanh(np.log(aspect / LOGO_ASPECT_PIVOT)))
    eff_max_w = max_w                       # hard width ceiling
    eff_max_h = max_h                       # hard height ceiling (already ≤ abs_max_h)

    # Overall size target comes from the BASE caps so the average logo size stays
    # consistent; the flex only relaxes the clamp for the dominant axis.
    target = (max_w * max_h) ** 0.5
    new_w  = target * (aspect ** 0.5)
    new_h  = target / (aspect ** 0.5)

    if new_w > eff_max_w:
        new_h *= eff_max_w / new_w
        new_w  = eff_max_w
    if new_h > eff_max_h:
        new_w *= eff_max_h / new_h
        new_h  = eff_max_h

    # Single-axis fill: after the aspect-preserving clamp, one dimension is
    # pinned to its cap and the other sits below it.  Stretch that under-cap
    # dimension toward its cap to give slim logos more presence.
    #
    # The HEIGHT stretch only fires for genuinely short logos (below the
    # trigger fraction of the cap) and its strength scales with HOW short the
    # logo is: one sitting right at the trigger gets ~1.0× (barely touched),
    # while a far-shorter logo ramps up toward the full LOGO_FILL_STRETCH.
    # This avoids over-stretching logos that only just qualify.
    if not LOGO_STRETCH_DISABLED and LOGO_FILL_STRETCH > 1.0:
        trigger_h = eff_max_h * LOGO_FILL_HEIGHT_TRIGGER
        if new_h < trigger_h:
            t      = (trigger_h - new_h) / trigger_h          # 0 at trigger → 1 near zero
            factor = 1.0 + t * (LOGO_FILL_STRETCH - 1.0)
            new_h  = min(eff_max_h, float(abs_max_h), new_h * factor)
        elif new_w < eff_max_w:
            new_w = min(eff_max_w, new_w * LOGO_FILL_STRETCH)

    # Logo sizing telemetry — gated behind DEBUG_LOGO_SIZING (off by default).
    if DEBUG_LOGO_SIZING:
        logger.info(
            f"LOGO SIZE: src={lw}x{lh} aspect={aspect:.2f} orient={orient:+.2f} "
            f"max_h={max_h} eff_max_h={eff_max_h:.0f} → final={int(new_w)}x{int(new_h)}"
        )

    logo = logo.resize((max(1, int(new_w)), max(1, int(new_h))), Image.Resampling.LANCZOS)

    # ── Position ─────────────────────────────────────────────────────────────
    # Two anchor modes:
    #
    # Centre (default): every logo shares a fixed vertical midline — the
    # midpoint of the tallest possible logo zone.  Tall logos bottom out at the
    # intended baseline; shorter logos float up to share the same centre.
    # Visually consistent for centred designs where logo size varies a lot.
    #
    # Bottom anchor (legacy): every logo's bottom edge is pinned to the same
    # baseline regardless of height, so logos only ever expand upward.  Useful
    # when the logo is placed low and a centred expansion would spill the top
    # edge into an overlay sitting above it.
    logo_x = round((width - logo.width) / 2)
    if bottom_anchor:
        baseline = height - int(height * bottom_ratio)
        logo_y   = baseline - logo.height
    else:
        centre_y = logo_centre_y(height, bottom_ratio)
        logo_y   = int(centre_y - logo.height / 2)

    # ── Background-aware legibility adjustments ──────────────────────────────
    # Sample the poster region the logo will cover (pure poster, sampled before
    # the paste) and derive its mean colour + luminance.
    cx1 = max(0, logo_x)
    cy1 = max(0, logo_y)
    cx2 = min(width,  logo_x + logo.width)
    cy2 = min(height, logo_y + logo.height)
    if cx2 > cx1 and cy2 > cy1:
        bg_arr = np.array(image.crop((cx1, cy1, cx2, cy2)).convert("RGB"),
                          dtype=np.float32)
        bg_r   = float(bg_arr[:, :, 0].mean())
        bg_g   = float(bg_arr[:, :, 1].mean())
        bg_b   = float(bg_arr[:, :, 2].mean())
        bg_lum = (0.2126 * bg_r + 0.7152 * bg_g + 0.0722 * bg_b) / 255.0

        # ── Experimental: contrast rescue ────────────────────────────────────
        # If the logo's average colour sits too close to the background's, the
        # title blends in (e.g. a red logo over a warm orange poster).  Recolour
        # it to white or black for guaranteed legibility — but ONLY when the
        # colour distance is small enough to be confident it's truly unreadable,
        # so well-contrasted logos are never touched.
        recoloured = False
        if LOGO_CONTRAST_RESCUE and LOGO_CONTRAST_MIN > 0:
            stats = _logo_color_stats(logo)
            if stats is not None:
                logo_rgb, variance = stats
                dist = (((logo_rgb[0] - bg_r) ** 2 +
                         (logo_rgb[1] - bg_g) ** 2 +
                         (logo_rgb[2] - bg_b) ** 2) ** 0.5) / 441.673  # 0–1
                if dist < LOGO_CONTRAST_MIN:
                    if variance > LOGO_COLOR_VARIANCE_MAX:
                        # Multi-colour / outline+fill logo — recolouring would
                        # destroy the internal contrast it relies on. Leave it.
                        logger.info(
                            f"Logo contrast rescue SKIPPED: dist={dist:.3f} but "
                            f"variance={variance:.3f} > {LOGO_COLOR_VARIANCE_MAX} "
                            f"(multi-colour logo preserved)"
                        )
                    else:
                        target, label = _recolor_target((bg_r, bg_g, bg_b), bg_lum)
                        logo = _recolor_logo_solid(logo, target)
                        recoloured = True
                        logger.info(
                            f"Logo contrast rescue: dist={dist:.3f} < "
                            f"{LOGO_CONTRAST_MIN}, variance={variance:.3f}, "
                            f"bg_lum={bg_lum:.2f} → recoloured to {label} "
                            f"rgb{target}"
                        )

        # Existing narrow rescue: whiten dark achromatic logos on dark posters.
        if not recoloured and bg_lum < 0.40:
            logo = ensure_light_logo(logo)

    image.paste(logo, (logo_x, logo_y), logo)


# ---------------------------------------------------------------------------
# US certificate (graphic badge row)
# ---------------------------------------------------------------------------

# Theatrical releases carry the certificate a title is known by; premieres and
# TV airings can carry a different one, or an empty string.
_CERT_RELEASE_ORDER = (3, 2, 4, 5, 6, 1)


def us_certification_from_release_dates(payload: dict) -> str:
    """The US certificate from a /movie/{id}/release_dates body, or ""."""
    for entry in payload.get("results") or []:
        if entry.get("iso_3166_1") != "US":
            continue
        dates = [d for d in entry.get("release_dates") or [] if (d.get("certification") or "").strip()]
        dates.sort(key=lambda d: _CERT_RELEASE_ORDER.index(d.get("type"))
                   if d.get("type") in _CERT_RELEASE_ORDER else len(_CERT_RELEASE_ORDER))
        return dates[0]["certification"].strip() if dates else ""
    return ""


def us_certification_from_content_ratings(payload: dict) -> str:
    """The US rating from a /tv/{id}/content_ratings body, or ""."""
    for entry in payload.get("results") or []:
        if entry.get("iso_3166_1") == "US":
            return (entry.get("rating") or "").strip()
    return ""


def _logo_entries(items: list[dict] | None) -> list[dict]:
    """Networks / production companies reduced to what a badge needs."""
    return [{"id": e.get("id"), "logo_path": e.get("logo_path")}
            for e in items or [] if e.get("id") is not None]


async def fetch_badge_facts(client: httpx.AsyncClient, tmdb_id: str, media_type: str,
                            tmdb_key: str | None) -> dict | None:
    """The graphic badges' facts about a title: its US certificate ("cert"),
    and its networks (TV) and production companies with their logo paths.
    One TMDB call per title per month — the details, with the release dates
    or content ratings appended.  None when it can't be fetched."""
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    cache_key = f"{endpoint}_{tmdb_id}"
    cached = get_cached_badge_facts(cache_key)
    if cached is not None:
        return cached
    if not tmdb_key or not (str(tmdb_id).isascii() and str(tmdb_id).isdigit()):
        return None
    append = "content_ratings" if endpoint == "tv" else "release_dates"
    try:
        logger.info(f"External API Call: TMDB badge facts for {endpoint} {tmdb_id}")
        resp = await client.get(f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
                                params={"api_key": tmdb_key, "append_to_response": append})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"Badge facts fetch failed for {endpoint} {tmdb_id}: {exc}")
        return None
    facts = {
        "cert": (us_certification_from_content_ratings(data.get("content_ratings") or {}) if endpoint == "tv"
                 else us_certification_from_release_dates(data.get("release_dates") or {})),
        "networks": _logo_entries(data.get("networks")),
        "companies": _logo_entries(data.get("production_companies")),
    }
    set_cached_badge_facts(cache_key, facts)
    return facts


async def fetch_network_logo_path(client: httpx.AsyncClient, network_id: int,
                                  tmdb_key: str | None) -> str | None:
    """A TV network's logo path, for a film from that network's studio arm."""
    cache_key = f"network_{network_id}"
    cached = get_cached_badge_facts(cache_key)
    if cached is not None:
        return cached.get("logo_path")
    if not tmdb_key:
        return None
    try:
        resp = await client.get(f"https://api.themoviedb.org/3/network/{network_id}",
                                params={"api_key": tmdb_key})
        resp.raise_for_status()
        path = resp.json().get("logo_path")
    except Exception as exc:
        logger.warning(f"Network logo fetch failed for {network_id}: {exc}")
        return None
    set_cached_badge_facts(cache_key, {"logo_path": path})
    return path
