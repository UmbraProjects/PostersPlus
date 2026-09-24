#tmdb.py
import asyncio
import colorsys
import hashlib
import io
import logging
import re
import time
from datetime import date as _date, datetime as _datetime, time as _time
from urllib.parse import urlsplit
import httpx
import numpy as np

import cinemeta

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
    set_cached_trending_snapshot,
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
    TRENDING_SOURCE_MOVIE,
    TRENDING_SOURCE_TV,
    TRENDING_SOURCE_MAX_ITEMS,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def normalise_poster(image: Image.Image) -> Image.Image:
    target_w, target_h = POSTER_WIDTH, POSTER_HEIGHT
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


def ensure_light_logo(logo: Image.Image,
                      lum_threshold: float = 0.2,
                      sat_threshold: float = 0.25,
                      light_lum: float = 0.6,
                      light_frac_min: float = 0.05,
                      card_coverage_max: float = 0.6) -> Image.Image:
    """
    Whiten a logo's pixels *only* when we are confident it is a dark, achromatic
    wordmark that would otherwise be invisible on a dark poster — and leave every
    other logo completely untouched. Doing nothing is always preferable to a
    recolour that could make the logo worse.

    The asset this primarily guards against is a logo that is a *filled dark card
    with light text baked in* (e.g. white "JURY DUTY" letters on a solid black
    rectangle). Averaging the luminance of every opaque pixel — the naive test —
    is dominated by the dark card, mislabels the asset "dark", and blanket-whitens
    it into a solid white block, erasing the text. Two complementary structural
    guards catch that before any recolour:

      • Light-content guard — if a non-trivial share of the solid pixels are
        already light, the logo carries its own legible content (light text,
        free-standing or on a dark card) and reads fine on a dark poster. This
        is the signal that tells a "black card + white text" asset (has a light
        population) apart from plain "black text" (has none).

      • Card guard — if the solid pixels fill most of their own bounding box, the
        logo is a filled card/emblem rather than glyphs on transparency.
        Whitening it would produce a solid block, so never touch it. This backs
        up the light-content guard for the dark-card / dark-or-no-text case,
        where there is no light population to detect.

    Only after both guards pass do the original tests apply — the ink must be
    dark (low mean luminance) and achromatic (low saturation), so coloured or
    branded logos keep their hues. Colour statistics are computed over *solid*
    pixels (alpha >= 128) so a soft anti-aliased fringe can't skew them; the
    recolour itself still covers the full visible mask (alpha > 30) to keep
    edge anti-aliasing intact.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)
    alpha = rgba[:, :, 3]

    # Analyse only solidly-opaque pixels so a semi-transparent AA halo can't
    # skew the luminance/saturation/coverage statistics below.
    solid = alpha >= 128
    if not solid.any():
        return logo  # nothing solid to analyse — leave as-is

    r = rgba[:, :, 0][solid]
    g = rgba[:, :, 1][solid]
    b = rgba[:, :, 2][solid]
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0  # per-pixel 0–1

    # Guard 1 — the logo already carries light content (white text, on a card
    # or free-standing), so it already reads on a dark poster. Leave it alone.
    light_frac = float((lum >= light_lum).mean())
    if light_frac >= light_frac_min:
        logger.debug(
            f"ensure_light_logo: skip (light content {light_frac:.0%} >= "
            f"{light_frac_min:.0%}) — already legible on dark"
        )
        return logo

    # Guard 2 — a filled card/emblem fills most of its bounding box. Whitening
    # it would produce a solid block, so never touch it.
    ys, xs = np.nonzero(solid)
    bbox_area = (int(ys.max()) - int(ys.min()) + 1) * (int(xs.max()) - int(xs.min()) + 1)
    coverage = float(solid.sum()) / bbox_area if bbox_area else 0.0
    if coverage >= card_coverage_max:
        logger.debug(
            f"ensure_light_logo: skip (coverage {coverage:.0%} >= "
            f"{card_coverage_max:.0%}) — filled card/shape, not a wordmark"
        )
        return logo

    # Original gates — only whiten genuinely dark, achromatic ink.
    avg_lum = float(lum.mean())
    if avg_lum > lum_threshold:
        return logo  # already light enough

    # Saturation = (max - min) / max per pixel (HSV definition).
    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    coloured = max_c > 0
    if coloured.any():
        avg_sat = float((((max_c - min_c) / np.where(coloured, max_c, 1.0)) * coloured).mean())
    else:
        avg_sat = 0.0
    if avg_sat > sat_threshold:
        return logo  # coloured/branded logo — preserve original hues

    logger.debug(
        f"ensure_light_logo: whitening dark wordmark "
        f"(light={light_frac:.0%}, coverage={coverage:.0%}, "
        f"avg_lum={avg_lum:.2f}, avg_sat={avg_sat:.2f})"
    )
    visible = alpha > 30
    out = rgba.copy()
    out[:, :, 0][visible] = 255
    out[:, :, 1][visible] = 255
    out[:, :, 2][visible] = 255
    return Image.fromarray(out.astype(np.uint8), "RGBA")


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
    max_h = min(int(height * LOGO_MAX_H_RATIO), LOGO_ABS_MAX_H)
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
    return Image.fromarray(rgba, "RGBA")


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
            "original_poster_path":  meta.get("original_poster_path"),
            "poster_langs":          meta.get("poster_langs", {}),
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
            "append_to_response": "images,credits,external_ids",
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

    set_cached_tmdb_metadata(
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
        original_poster_path=original_poster_path,
        poster_langs=poster_langs,
        imdb_id=imdb_id,
        tmdb_release_date=tmdb_release_date,
        last_air_date=last_air_date,
        next_episode=next_episode,
        last_episode=last_episode,
        seasons=seasons,
    )

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
        "original_poster_path": original_poster_path,
        "poster_langs":         poster_langs,
        "imdb_id":              imdb_id,
        "tmdb_release_date":    tmdb_release_date,
        "last_air_date":        last_air_date,
        "next_episode":         next_episode,
        "last_episode":         last_episode,
        "seasons":              seasons,
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
    if is_absolute_art(path):
        return hashlib.sha256(path.encode()).hexdigest()[:16]
    return path.strip("/")


def _id_token(tmdb_id: str) -> str:
    # A stand-in id ("kitsu:12345") carries a colon; replaced so the key is a
    # portable filename on every filesystem. Numeric TMDB ids are unchanged.
    return tmdb_id.replace(":", "_")


def poster_image_cache_key(tmdb_id: str, media_type: str, poster_path: str) -> str:
    return f"{media_type}_{_id_token(tmdb_id)}_{_art_token(poster_path)}"


def backdrop_image_cache_key(tmdb_id: str, backdrop_path: str, avoid_text: bool) -> str:
    # Carries the crop-logic version so changing the crop algorithm invalidates
    # previously-cached crops instead of serving the old framing.
    return (
        f"backdrop_{_id_token(tmdb_id)}_{_art_token(backdrop_path)}_{_CROP_VERSION}"
        + ("_ta" if avoid_text else "")
    )


def landscape_image_cache_key(tmdb_id: str, backdrop_path: str) -> str:
    return (
        f"landscape_{_id_token(tmdb_id)}_{_art_token(backdrop_path)}"
        f"_{LANDSCAPE_WIDTH}x{LANDSCAPE_HEIGHT}"
    )


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
    cached_bytes = get_cached_tmdb_poster(poster_cache_key)

    if cached_bytes:
        logger.info(f"Poster cache hit for {tmdb_id}")
        # Stored as JPEG RGB — convert to RGBA for the compositing pipeline
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    if _is_absolute:
        logger.info(f"External API Call: Requested poster art for {tmdb_id}")
        img_resp = await client.get(poster_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested poster from TMDB for {tmdb_id}")
        img_resp = await client.get(f"https://image.tmdb.org/t/p/w500{poster_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
    image = normalise_poster(image)

    # Save as JPEG RGB (no alpha needed for base poster; restoring alpha on load is free)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(poster_cache_key, buf.getvalue())

    return image


# Bumped whenever the backdrop crop logic changes, so cached crops from the old
# algorithm are invalidated rather than served.
#   v2 = face-aware cropping
#   v3 = focus a single face when subjects are too far apart to both fit
#   v4 = derive text avoidance from PP-OCR polygons
#   v5 = cube confidence in the face "prominence" weight so a large
#        low-confidence false-positive blob can no longer outrank a smaller,
#        genuinely-confident face purely on bounding-box size (see
#        face_detect.detect_faces docstring — observed on TMDB 450545)
_CROP_VERSION = "v5"


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
    cached_bytes = get_cached_tmdb_poster(cache_key)

    if cached_bytes:
        logger.info(f"TMDB backdrop cache hit for {tmdb_id}")
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    # w1280 gives enough resolution to crop to a quality portrait
    if is_absolute_art(backdrop_path):
        logger.info(f"External API Call: Requested backdrop art for {tmdb_id}")
        img_resp = await client.get(backdrop_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested backdrop from TMDB for {tmdb_id}")
        img_resp = await client.get(f"https://image.tmdb.org/t/p/w1280{backdrop_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")

    # The crop runs CPU-heavy face/text inference, so do it in the thread pool;
    # running it inline would stall the event loop and delay unrelated requests.
    image = await asyncio.get_running_loop().run_in_executor(
        None, _crop_and_normalise_backdrop, image, tmdb_id, avoid_text
    )

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())

    return image


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
    cached_bytes = get_cached_tmdb_poster(cache_key)

    if cached_bytes:
        logger.info(f"TMDB landscape cache hit for {tmdb_id}")
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (LANDSCAPE_WIDTH, LANDSCAPE_HEIGHT):
            image = normalise_landscape(image)
        return image

    if is_absolute_art(backdrop_path):
        logger.info(f"External API Call: Requested landscape backdrop art for {tmdb_id}")
        img_resp = await client.get(backdrop_path, follow_redirects=True)
    else:
        logger.info(f"External API Call: Requested landscape backdrop from TMDB for {tmdb_id}")
        img_resp = await client.get(f"https://image.tmdb.org/t/p/w1280{backdrop_path}")
    img_resp.raise_for_status()
    image = normalise_landscape(Image.open(io.BytesIO(img_resp.content)).convert("RGBA"))

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())

    return image


def _crop_and_normalise_backdrop(image: Image.Image, tmdb_id: str,
                                 avoid_text: bool) -> Image.Image:
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

    return normalise_poster(image)


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
    cached_bytes = get_cached_tmdb_logo(cache_key)

    if cached_bytes:
        logger.info(f"Metahub logo cache hit for {imdb_id}")
        return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")

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

    try:
        logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"Metahub logo parse failed for {imdb_id}: {exc}")
        return None

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(cache_key, buf.getvalue())

    return logo


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


# Priorities whose fallback tail is "text-forward": once the language buckets
# are exhausted we prefer TMDB English → Metahub → language-neutral, and finally
# a rendered text title, rather than dropping to a neutral/wrong-language logo.
TEXT_FORWARD_PRIORITIES = frozenset({
    "native_text",
    "native_custom_text",
    "native_custom_original_text",
})


def image_language_order(
    logo_language: str,
    original_language: str | None,
    logo_priority: str,
    secondary_language: str | None = None,
) -> list[str]:
    """Return the distinct language buckets to try, in priority order.

    *secondary_language* is a user's second preferred language ("custom").  It is
    only consulted by the ``native_custom_*`` priorities; a blank value there
    degrades those modes to ``native_text`` / ``native_original`` respectively
    (the falsy filter below drops it), so the field is safe to leave unset.
    """
    if logo_priority == "original_native":
        languages = [original_language, logo_language]
    elif logo_priority == "native_if_original_english":
        languages = (
            [logo_language, "en", original_language]
            if original_language == logo_language
            else ["en", original_language]
        )
    elif logo_priority == "native_text":
        languages = [logo_language]
    elif logo_priority == "native_custom_text":
        languages = [logo_language, secondary_language]
    elif logo_priority == "native_custom_original_text":
        languages = [logo_language, secondary_language, original_language]
    else:
        languages = [logo_language, original_language]

    return list(dict.fromkeys(language for language in languages if language))


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

    Two language-specific buckets are weighed first, in an order set by
    *logo_priority*:
      • "native"   — a logo in the requested language (logo_language).
      • "original" — a logo in the content's own original language
                     (original_language); helps foreign titles that only ship
                     a native-language logo on TMDB.
      logo_priority:
        "native_original" (default) → native, then original
        "original_native"           → original, then native
        "native_if_original_english" → native when the content is native,
                                        otherwise English, then original
        "native_text"               → native only, then English before neutral
                                       fallback (skip original-language logos)
        "native_custom_text"          → native, then the secondary_language
                                       ("custom"), then English/neutral/text
        "native_custom_original_text" → native, secondary_language, original,
                                       then English/neutral/text

    After the priority buckets, the common fallbacks apply:
      → TMDB English logo, Metahub, then neutral logo for native_text
      → TMDB language-neutral logo, then English logo for other priorities
      → Metahub CDN logo for other priorities (images.metahub.space)
      → None (caller may render the translated title as text instead).

    All results are cached locally so repeat requests never hit external APIs.
    """
    # Accept PNG always; accept SVG too when we can rasterise it (cairosvg).
    # TMDB's highest-voted logo is frequently an SVG, so excluding them would
    # silently fall back to a lower-quality raster or a text title.
    _exts = (".png", ".svg") if _HAS_CAIROSVG else (".png",)
    _cand = [lg for lg in logos if lg["file_path"].lower().endswith(_exts)]

    language_buckets = {
        language: [lg for lg in _cand if _image_matches_language(lg, language)]
        for language in image_language_order(
            logo_language, original_language, logo_priority, secondary_language
        )
    }
    neutral   = [lg for lg in _cand if lg.get("iso_639_1") in (None, "")]
    english   = [lg for lg in _cand if _image_matches_language(lg, "en")]

    candidates = []
    for language in language_buckets:
        if language_buckets[language]:
            candidates = language_buckets[language]
            break

    if logo_priority in TEXT_FORWARD_PRIORITIES:
        if not candidates and english:
            candidates = english
        if not candidates and use_metahub and imdb_id:
            metahub_logo = await _fetch_metahub_logo(client, imdb_id)
            if metahub_logo is not None:
                return metahub_logo
        if not candidates and neutral:
            candidates = neutral
    else:
        for bucket in (neutral, english):
            if not candidates and bucket:
                candidates = bucket

    candidates = sorted(candidates, key=_logo_rank_key(prefer_wide), reverse=True)

    if not candidates:
        # No TMDB logo at all — try Metahub before giving up (unless the caller
        # has asked to skip it, e.g. to slot another source in between).
        if use_metahub and imdb_id:
            return await _fetch_metahub_logo(client, imdb_id)
        return None

    logo_path = candidates[0]["file_path"]
    is_svg    = logo_path.lower().endswith(".svg")

    logo_cache_key = logo_path.strip('/').replace('/', '_')
    cached_bytes = get_cached_tmdb_logo(logo_cache_key)

    if cached_bytes:
        logger.info("TMDB logo cache hit")
        logo = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        return logo

    # SVGs are served at "original" (the sized w500 path doesn't apply to vector);
    # rasters use w500 which is plenty for our ≤~440px rendered width.
    _size = "original" if is_svg else "w500"
    resp = await client.get(f"https://image.tmdb.org/t/p/{_size}{logo_path}")
    logger.info(f"External API Call: Requested logo from TMDB")
    resp.raise_for_status()

    if is_svg:
        logo = _rasterize_svg(resp.content)
        if logo is None:
            # Rasterise failed — fall back to Metahub, then None.
            logger.warning(f"SVG logo unusable for {imdb_id} — trying Metahub fallback")
            return await _fetch_metahub_logo(client, imdb_id) if (use_metahub and imdb_id) else None
    else:
        logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(logo_cache_key, buf.getvalue())

    return logo


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


def trending_source_url(media_type: str) -> str:
    """Configured trending source for *media_type*, or "" when unset."""
    return TRENDING_SOURCE_TV if media_type in ("tv", "series") else TRENDING_SOURCE_MOVIE


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
    url = _normalise_trending_url(trending_source_url(media_type))
    if not url:
        return "tmdb"
    return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


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


def _parse_trending_payload(payload, media_type: str) -> list[str]:
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

    wanted = "show" if media_type in ("tv", "series") else "movie"
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
        if raw is None or not str(raw).isdigit():
            continue
        order = item.get("rank") if ranked else None
        rows.append((float(order) if isinstance(order, (int, float)) else position, str(raw)))

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

    try:
        logger.info(f"External API Call: trending source for {media_type} ({shown})")
        # Redirects are followed here specifically: an operator pastes whatever
        # their browser showed them, and a host that answers on a canonical
        # form should not read as a broken config.
        resp = await client.get(resolved, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        ids = _parse_trending_payload(resp.json(), media_type)
    except Exception as exc:
        _trending_source_failed_at[media_type] = time.monotonic()
        logger.error(
            f"Trending source fetch failed ({shown}): {exc} — "
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


async def fetch_trending_rank(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> int | None:

    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    source_sig = trending_source_signature(endpoint)

    snapshot = get_cached_trending_snapshot(endpoint, source_sig)

    if snapshot is None:
        inflight_event = _trending_inflight.get(endpoint)
        if inflight_event is not None:
            await inflight_event.wait()
            snapshot = get_cached_trending_snapshot(endpoint, source_sig)
        
        if snapshot is None:
            event_to_set = asyncio.Event()
            _trending_inflight[endpoint] = event_to_set
            
            try:
                # An operator-configured source replaces TMDB's list entirely.
                # A configured-but-broken source serves no ranks, so the sash
                # goes quiet instead of falling back to TMDB and looking like
                # the config worked.
                source_ids = await fetch_trending_source_ids(client, endpoint)
                if source_ids is not None:
                    if not source_ids:
                        # Deliberately NOT cached.  Writing an empty snapshot
                        # would pin "no trending" for the full TTL on what is
                        # usually a transient fetch failure; the source's own
                        # retry cooldown already stops this from re-fetching per
                        # request.  Nothing to rank against this time round.
                        return None
                    rankings = {
                        entry_id: position
                        for position, entry_id in enumerate(source_ids, start=1)
                    }
                else:
                    logger.info("External API Call: Refreshing TMDB trending snapshot (pages 1-5 concurrent)")

                    async def _fetch_page(page: int) -> list[dict]:
                        resp = await client.get(
                            f"https://api.themoviedb.org/3/trending/{endpoint}/day",
                            params={"api_key": tmdb_key, "page": page},
                        )
                        resp.raise_for_status()
                        return resp.json().get("results", [])

                    try:
                        pages = await asyncio.gather(*(_fetch_page(page) for page in range(1, 6)))
                    except Exception as exc:
                        logger.error(f"TMDB trending fetch error: {exc}")
                        return None

                    rankings = {}
                    rank = 1
                    for results in pages:
                        for item in results:
                            rankings[str(item["id"])] = rank
                            rank += 1

                set_cached_trending_snapshot(endpoint, rankings, source_sig)
                snapshot = rankings
            finally:
                event_to_set.set()
                _trending_inflight.pop(endpoint, None)

    rank = snapshot.get(str(tmdb_id))

    if rank:
        logger.info(f"Trending rank for {tmdb_id}: #{rank}")

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
    sources = dict(zip(
        ("movie", "tv"),
        await asyncio.gather(
            fetch_trending_source_ids(client, "movie"),
            fetch_trending_source_ids(client, "tv"),
        ),
    ))

    # The ranking snapshot and the warm list are the same fetch.  Writing it here
    # is what makes the scheduled refresh actually refresh the ranks: without it
    # the warm cycle pulled the operator's list, threw the order away, and the
    # next /poster request fetched the identical list again to rebuild it — and
    # a title warmed this cycle could be rendered against yesterday's ranks.
    # An empty list means the fetch failed; leave the existing snapshot alone.
    for _media_type, _ids in sources.items():
        if _ids:
            set_cached_trending_snapshot(
                _media_type,
                {entry_id: position for position, entry_id in enumerate(_ids, start=1)},
                trending_source_signature(_media_type),
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


async def tmdb_find_by_imdb(
    client: httpx.AsyncClient,
    imdb_id: str,
    tmdb_key: str,
    media_type_hint: str | None = None,
) -> dict | None:
    """
    Resolve an IMDB id (``tt...``) to a TMDB id via TMDB's /find endpoint.

    Returns ``{"tmdb_id": str, "media_type": "movie"|"tv"}``, preferring a
    result matching *media_type_hint* when both movie and tv results are
    present, or ``None`` if TMDB has no match for either. Raises
    ``IdResolveError`` when the lookup itself failed, so a caller can tell an
    outage from an unknown title and not cache the former.
    """
    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": tmdb_key, "external_source": "imdb_id"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise IdResolveError(f"TMDB find failed for {imdb_id}: {exc}") from exc

    movie_results = data.get("movie_results") or []
    tv_results    = data.get("tv_results") or []

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


def _compute_movie_status_from_dates(
    theatrical_date: _date | None,
    digital_date: _date | None,
    physical_date: _date | None,
    tmdb_status: str | None,
    premiere_date: _date | None = None,
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
        if (
            CINEMA_MAX_AGE_YEARS > 0
            and theatrical_date is not None
            and (today - theatrical_date).days > CINEMA_MAX_AGE_YEARS * 365
        ):
            return "Streaming"
        else:
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
    return release_status_expiry(info.get("status"), upcoming_dates=upcoming)


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


async def fetch_movie_release_info(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    tmdb_status: str | None,
) -> dict | None:
    """Cached TMDB movie release-date facts used by release-status and freshness sashes."""
    cache_key = f"movie_{tmdb_id}"
    cached = get_cached_movie_release_info(cache_key)
    if cached and _release_info_is_current(cached):
        cached["status"] = _compute_movie_status_from_dates(
            _parse_tmdb_date(cached.get("theatrical_date")),
            _parse_tmdb_date(cached.get("digital_date")),
            _parse_tmdb_date(cached.get("physical_date")),
            tmdb_status,
            _parse_tmdb_date(cached.get("premiere_date")),
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
    if tmdb_status in _pre_release:
        info["status"] = "Production"
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
    )

    info = {
        "status": result,
        "theatrical_date": earliest_theatrical.isoformat() if earliest_theatrical else None,
        "digital_date": earliest_digital.isoformat() if earliest_digital else None,
        "physical_date": earliest_physical.isoformat() if earliest_physical else None,
        "digital_latest_date": latest_digital.isoformat() if latest_digital else None,
        "premiere_date": earliest_premiere.isoformat() if earliest_premiere else None,
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
    info = await fetch_movie_release_info(client, tmdb_id, tmdb_key, tmdb_status) or {}
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
    soonest, window = min(upcoming)
    return soonest.isoformat(), window


async def fetch_release_status(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str,
    tmdb_status: str | None,
) -> str | None:
    """
    Determine the current release status for the info sash.

    TV shows: mapped from the TMDB ``status`` field (already fetched as part
    of poster metadata, so no extra API call is needed).  That mapping wins over
    the cached row, which exists only for requests that arrive without a status.

    Movies: consults ``/movie/{id}/release_dates`` to determine whether the
    film is on physical media (Physical), digital/streaming (Streaming), still
    theatrical-only (Cinema), or not yet released (Production).  The dates are
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
    info = await fetch_movie_release_info(client, tmdb_id, tmdb_key, tmdb_status)
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
    # raised Height slider can't let tall logos take over the poster.
    max_h = min(int(height * max_h_ratio), LOGO_ABS_MAX_H)

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
    eff_max_h = max_h                       # hard height ceiling (already ≤ LOGO_ABS_MAX_H)

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
            new_h  = min(eff_max_h, float(LOGO_ABS_MAX_H), new_h * factor)
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