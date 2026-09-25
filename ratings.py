#ratings.py
import logging
import math
import time
import httpx
import numpy as np

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import cairo as _cairo
    _HAS_CAIRO = True
except ImportError:
    _HAS_CAIRO = False
    logger.warning("pycairo not available — shape edges will use PIL (no antialiasing)")

from awards import (FETCH_FAILED, _FetchFailed, _RateLimited, dominant_frost_rgb,
                    _frost_ink, _frosted_tint)
from config import (
    ANIME_RATING_SOURCES,
    GENRE_MAP,
    GENRE_PRIORITY,
    SCORE_NORMALISERS,
    SCORE_GLOW_THRESHOLD,
    SCORE_GLOW_BLUR,
    SCORE_GLOW_ALPHA,
    RATING_MIN_VOTES,
)


_RATING_VOTE_KEYS = ("vote_count", "votes", "count", "rating_count", "ratings_count")


# ---------------------------------------------------------------------------
# MDBList daily quota tracking
# ---------------------------------------------------------------------------
# MDBList's limit is a per-key *daily* request quota (1000/day on the free
# tier, more on paid tiers), not a burst limit, and every response reports it:
#
#   x-ratelimit-limit: 1000
#   x-ratelimit-remaining: 647
#   x-ratelimit-reset: 1789948800     (epoch seconds, midnight UTC)
#
# Each fetch_rating call refreshes the snapshot for the key it used, so the
# cache warmer can stop spending the key before live traffic runs dry, and a
# quota 429 (which comes with no Retry-After) can cool the key down until the
# real reset instead of guessing.

class MDBListQuota:
    __slots__ = ("limit", "remaining", "reset_at", "observed_at")

    def __init__(self, limit: int | None, remaining: int | None, reset_at: float | None, observed_at: float):
        self.limit       = limit
        self.remaining   = remaining
        self.reset_at    = reset_at
        self.observed_at = observed_at

    def is_current(self, now: float | None = None) -> bool:
        """False once the quota window this snapshot describes has rolled over."""
        if self.reset_at is None:
            return True
        if now is None:
            now = time.time()
        return now < self.reset_at

    def __repr__(self):
        return f"MDBListQuota(limit={self.limit}, remaining={self.remaining}, reset_at={self.reset_at})"


# api key -> latest quota snapshot seen for it
MDBLIST_QUOTA: dict[str, MDBListQuota] = {}


def _header_int(headers, name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _record_mdblist_quota(mdblist_key: str, headers) -> MDBListQuota | None:
    """Refresh the per-key quota snapshot from a response's X-RateLimit-* headers."""
    limit     = _header_int(headers, "x-ratelimit-limit")
    remaining = _header_int(headers, "x-ratelimit-remaining")
    reset_raw = _header_int(headers, "x-ratelimit-reset")
    if limit is None and remaining is None and reset_raw is None:
        return None
    quota = MDBListQuota(limit, remaining, float(reset_raw) if reset_raw else None, time.time())
    MDBLIST_QUOTA[mdblist_key] = quota
    return quota


def mdblist_quota_remaining(mdblist_key: str, now: float | None = None) -> int | None:
    """Remaining daily requests for *mdblist_key*, or None when unknown / stale."""
    quota = MDBLIST_QUOTA.get(mdblist_key)
    if quota is None or quota.remaining is None or not quota.is_current(now):
        return None
    return quota.remaining


def _rating_vote_count(raw: dict) -> int | None:
    for key in _RATING_VOTE_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        try:
            return int(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

# MDBList's own release dates, kept beside the rating rather than in it.
#
# The record carries `released` (theatrical) and `released_digital`, and the
# digital date is the one fact the release-status sash cannot get anywhere
# else without a TMDB key: Cinemeta knows theatrical and disc dates only, so a
# film weeks into streaming stayed "Cinema" on a key-less instance.  Written
# on every MDBList answer into the generic JSON cache — the rating tuple and
# the rating cache row are unpacked in enough places that threading a sixth
# field through them was the wrong cost for two dates.  A record with no
# digital date yet is kept only briefly, so the date is picked up once MDBList
# learns it.
_MDBLIST_DATES_TTL_KNOWN   = 90 * 86400
_MDBLIST_DATES_TTL_PENDING = 3 * 86400


def _mdblist_dates_key(media_id: str, media_type: str) -> str:
    kind = "show" if media_type in ("tv", "series") else "movie"
    return f"mdblist_dates:{kind}:{media_id}"


def remember_mdblist_release_dates(media_id: str, media_type: str, data: dict) -> None:
    from cache import set_cached_tvdb_json
    if media_type in ("tv", "series"):
        return
    released = str(data.get("released") or "")[:10] or None
    digital  = str(data.get("released_digital") or "")[:10] or None
    if not released and not digital:
        return
    try:
        set_cached_tvdb_json(
            _mdblist_dates_key(media_id, media_type),
            {"released": released, "released_digital": digital},
            _MDBLIST_DATES_TTL_KNOWN if digital else _MDBLIST_DATES_TTL_PENDING,
        )
    except Exception as exc:   # a cache hiccup must not cost the rating
        logger.warning(f"Could not cache MDBList release dates for {media_id}: {exc}")


def mdblist_release_dates(media_id: str | None, media_type: str) -> dict | None:
    """``{"released", "released_digital"}`` remembered for a movie, or None."""
    if not media_id or media_type in ("tv", "series"):
        return None
    from cache import get_cached_tvdb_json
    try:
        return get_cached_tvdb_json(_mdblist_dates_key(media_id, media_type))
    except Exception:
        return None


async def fetch_rating(
    client: httpx.AsyncClient,
    mdblist_key: str,
    genre_ids: list[int],
    media_type: str = "movie",
    *,
    media_id: str,
    provider: str = "imdb",
    movie_weights: dict | None = None,
    tv_weights: dict | None = None,
) -> "tuple[dict | str, str, str | None, list[dict], int | None] | _FetchFailed | _RateLimited":
    """
    Returns ``(ratings_dict, genre, release_date, keywords, age_rating)`` on
    success, or ``FETCH_FAILED`` on a network / API error.

    MDBList serves the same record under several id namespaces, so *provider*
    selects the route ("imdb" or "tmdb") and *media_id* is the id in that
    namespace. A title TMDB has no IMDb link for still has ratings, awards,
    keywords and an age rating here — it just has to be asked for by TMDB id.

    *media_id* and *provider* are keyword-only, and the old positional id
    argument is gone: a call site that still passed an IMDb id positionally
    would otherwise have silently become the API key.
    """

    genre = "Unknown"
    for gid in GENRE_PRIORITY:
        if gid in genre_ids:
            genre = GENRE_MAP[gid]
            break

    mdb_type = "show" if media_type in ("tv", "series") else "movie"

    try:
        logger.info(
            "External API Call: Requested ratings+keywords from MDBlist for "
            f"{provider}/{media_id}"
        )
        resp = await client.get(
            f"https://api.mdblist.com/{provider}/{mdb_type}/{media_id}",
            params={"apikey": mdblist_key, "append_to_response": "keyword"},
            timeout=10.0,
        )
    except Exception as exc:
        logger.error(f"MDblist request error for {media_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED

    quota = _record_mdblist_quota(mdblist_key, resp.headers)

    # 429 is either the daily quota or the per-IP burst limit; 503 is how the
    # burst limit first shows itself (a run of 503s, then 429 + Retry-After
    # for every key on the address). Both go back as _RateLimited so the
    # caller pauses instead of walking the network-failure ladder.
    if resp.status_code in (429, 503):
        retry_after: float | None = None
        raw = resp.headers.get("retry-after")
        if raw:
            try:
                # Most APIs send Retry-After as an integer seconds value.
                # HTTP-date format also exists but is uncommon for JSON APIs;
                # we don't try to parse it — caller will fall back to default.
                parsed = float(raw)
                if parsed > 0:
                    retry_after = parsed
            except ValueError:
                pass
        # Only treat the 429 as quota exhaustion when MDBList says the key is
        # actually empty; a 429 with requests still remaining (or none of the
        # quota headers at all) is the burst throttle, and parking the key
        # until midnight for it would be wrong.
        reset_at = None
        if (
            resp.status_code == 429
            and quota and quota.reset_at
            and (quota.remaining is None or quota.remaining <= 0)
        ):
            reset_at = quota.reset_at
        logger.warning(
            f"MDblist {'rate-limited' if resp.status_code == 429 else 'refused (503)'} "
            f"for {media_id} (retry-after={retry_after}, quota={quota})"
        )
        return _RateLimited(retry_after, reset_at=reset_at)

    if resp.status_code == 404:
        logger.info(f"MDblist 404 for {provider}/{media_id} — title not found, returning empty result")
        return {}, genre, None, [], None

    if resp.status_code != 200:
        logger.warning(f"MDblist error {resp.status_code} for {provider}/{media_id}")
        return FETCH_FAILED

    data         = resp.json()
    release_date = data.get("released")
    keywords: list[dict] = data.get("keywords") or []
    remember_mdblist_release_dates(media_id, media_type, data)

    age_rating: int | None = data.get("age_rating") or None
    if age_rating is not None:
        try:
            age_rating = int(age_rating)
        except (ValueError, TypeError):
            age_rating = None

    ratings_dict: dict[str, float] = {}
    for r in data.get("ratings", []):
        source = (r.get("source") or "").lower()
        value  = r.get("value")
        if source not in SCORE_NORMALISERS or value is None:
            continue

        vote_count = _rating_vote_count(r)
        if source != "rogerebert" and vote_count is not None and vote_count < RATING_MIN_VOTES:
            logger.info(
                f"Skipping {source} rating for {media_id}: "
                f"vote_count={vote_count} < {RATING_MIN_VOTES}"
            )
            continue

        ratings_dict[source] = value

    return ratings_dict, genre, release_date, keywords, age_rating


# ---------------------------------------------------------------------------
# Score colour
# ---------------------------------------------------------------------------

CustomScorePalette = list[tuple[int, tuple[int, int, int]]]


def parse_custom_score_palette(raw: str | None) -> CustomScorePalette | None:
    if not raw:
        return None
    out: dict[int, tuple[int, int, int]] = {}
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        raw_score, raw_hex = part.split(":", 1)
        try:
            score = max(0, min(100, int(round(float(raw_score.strip())))))
        except (TypeError, ValueError):
            continue
        hex_value = raw_hex.strip().lstrip("#")
        if len(hex_value) != 6:
            continue
        try:
            rgb = (
                int(hex_value[0:2], 16),
                int(hex_value[2:4], 16),
                int(hex_value[4:6], 16),
            )
        except ValueError:
            continue
        out[score] = rgb
    if not out:
        return None
    return sorted(out.items())


def _darken(rgb: tuple[int, int, int], amount: float = 0.72) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * amount))) for c in rgb)


def _score_color_custom(
    score: int,
    custom_palette: CustomScorePalette | None,
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    if not custom_palette:
        return None
    score = max(0, min(int(score), 100))
    selected = custom_palette[0][1]
    for threshold, rgb in custom_palette:
        if score < threshold:
            break
        selected = rgb
    return selected, _darken(selected)


def _score_color(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if score < 50:
        return (255, 80, 80), (160, 40, 40)
    elif score < 70:
        return (255, 210, 90), (200, 150, 40)
    elif score < 85:
        return (120, 255, 160), (40, 170, 90)
    else:
        return (190, 140, 255), (186, 85, 211)


def _score_color_alt(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Six-band alternative: dark red → red → dark amber → yellow → dark green → bright green."""
    if score < 17:    # dark red
        return (180, 30,  30),  (120, 15,  15)
    elif score < 34:  # red
        return (255, 70,  70),  (200, 45,  45)
    elif score < 50:  # dark amber
        return (200, 130, 20),  (150, 90,  10)
    elif score < 67:  # yellow
        return (255, 215, 60),  (210, 165, 30)
    elif score < 84:  # dark green
        return (50,  160, 80),  (25,  110, 50)
    else:             # bright green
        return (110, 245, 150), (60,  190, 100)


def _score_color_metal(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Four-band metal palette mirroring the quality-tier badge colours: grey → bronze → silver → gold."""
    if score < 50:    # grey
        return (140, 140, 148), (90,  90,  98)
    elif score < 70:  # bronze
        return (210, 120,  50), (150, 80,  25)
    elif score < 85:  # silver
        return (218, 224, 240), (155, 165, 195)
    else:             # gold
        return (255, 210,  60), (200, 150,  25)


def score_color_for_mode(
    score: int,
    color_mode: int = 0,
    custom_palette: CustomScorePalette | None = None,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if color_mode == 3:
        custom = _score_color_custom(score, custom_palette)
        if custom is not None:
            return custom
    return {1: _score_color_alt, 2: _score_color_metal}.get(color_mode, _score_color)(score)


def _cairo_pill_mask(w: int, h: int, radius: int) -> Image.Image:
    """
    Return an antialiased greyscale pill mask (PIL 'L' mode) for use as an
    alpha mask when compositing solid-colour or gradient fills.

    Uses cairo's vector rasteriser (ANTIALIAS_BEST) when available so edges
    are smooth at any size.  Falls back to a plain PIL rounded_rectangle when
    pycairo is not installed — identical to the previous behaviour.
    """
    if _HAS_CAIRO:
        r = min(radius, w / 2, h / 2)
        surface = _cairo.ImageSurface(_cairo.FORMAT_A8, w, h)
        ctx = _cairo.Context(surface)
        ctx.set_antialias(_cairo.ANTIALIAS_BEST)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        # Rounded-rectangle path built from four arcs
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


def _soften(rgb: tuple[int, int, int], amount: float = 0.9) -> tuple[int, int, int]:
    r, g, b = rgb
    return (
        int(r * amount + 255 * (1 - amount)),
        int(g * amount + 255 * (1 - amount)),
        int(b * amount + 255 * (1 - amount)),
    )


# ---------------------------------------------------------------------------
# Score bar  (horizontal)
# ---------------------------------------------------------------------------

def draw_score_bar(
    image: Image.Image,
    score: int | str,
    *,
    bottom_margin: int = 30,
    side_margin: int = 70,
    glow_threshold: int = SCORE_GLOW_THRESHOLD,
    glow_blur: int = SCORE_GLOW_BLUR,
    glow_alpha: int = SCORE_GLOW_ALPHA,
    glow_color: tuple[int, int, int] | str | None = None,
    color_mode: int = 0,
    custom_palette: CustomScorePalette | None = None,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return
    score = max(0, min(int(score), 100))
    W, H = image.size
    bar_h  = max(8, round(H * 0.012))
    x0, x1 = side_margin, W - side_margin
    y1, y0  = H - bottom_margin, H - bottom_margin - bar_h
    bar_w   = x1 - x0
    fill_w  = int(bar_w * (score / 100))
    radius  = min(bar_h // 2, 8)

    # ── Track (background pill) ───────────────────────────────────────────
    # Drawn before the early-return so score=0 still shows an empty track
    # rather than no bar at all (which would be visually indistinguishable
    # from "no rating available").
    track_mask = _cairo_pill_mask(bar_w, bar_h, radius)
    track_mask = track_mask.point(lambda v: v * 45 // 255)   # scale to fill alpha
    track_strip = Image.new("RGBA", (bar_w, bar_h), (255, 255, 255, 0))
    track_strip.putalpha(track_mask)
    # Composite the strip where it belongs rather than pasting it into a
    # full-canvas transparent layer first: fully transparent pixels contribute
    # nothing to an alpha composite, so the result is identical and the work is
    # proportional to the bar (360x9) instead of the whole poster (500x750).
    image.alpha_composite(track_strip, dest=(x0, y0))

    if fill_w <= 0:
        return

    left_color, right_color = score_color_for_mode(score, color_mode, custom_palette)
    left_color  = _soften(left_color,  0.90)
    right_color = _soften(right_color, 0.90)

    # ── Filled segment — numpy gradient, no Python pixel loop ────────────
    # Build an (bar_h × fill_w) RGB array by interpolating left→right colour.
    t = np.linspace(0, 1, fill_w, dtype=np.float32)               # (fill_w,)
    r_ch = (left_color[0] * (1 - t) + right_color[0] * t).astype(np.uint8)
    g_ch = (left_color[1] * (1 - t) + right_color[1] * t).astype(np.uint8)
    b_ch = (left_color[2] * (1 - t) + right_color[2] * t).astype(np.uint8)
    a_ch = np.full(fill_w, 220, dtype=np.uint8)

    # Stack into RGBA (fill_w, 4), then broadcast to (bar_h, fill_w, 4)
    row  = np.stack([r_ch, g_ch, b_ch, a_ch], axis=1)             # (fill_w, 4)
    grad_arr = np.broadcast_to(row, (bar_h, fill_w, 4)).copy()    # (bar_h, fill_w, 4)
    grad = Image.fromarray(grad_arr)

    # Rounded left/right mask — cairo-antialiased pill, right end cropped flat
    # when score < 99 so the cut-off aligns cleanly with the track edge.
    if score >= 99:
        mask_img = _cairo_pill_mask(fill_w, bar_h, radius)
    else:
        mask_w   = fill_w + radius       # extend right so the right cap is hidden by crop
        full_msk = _cairo_pill_mask(mask_w, bar_h, radius)
        mask_img = full_msk.crop((0, 0, fill_w, bar_h))

    fill_layer = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    fill_layer.paste(grad, (0, 0), mask_img)
    image.alpha_composite(fill_layer, dest=(x0, y0))

    # ── Highlight sliver ─────────────────────────────────────────────────
    # Drawn in bar-local coordinates; the strip is composited at (x0, y0).
    hl = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).line(
        [(radius, 1), (fill_w - 1, 1)],
        fill=(255, 255, 255, 60),
        width=1,
    )
    image.alpha_composite(hl, dest=(x0, y0))

    # ── Glow ─────────────────────────────────────────────────────────────
    if score >= glow_threshold:
        expand = glow_blur * 2
        # The glow is a thin strip at the bottom of the poster.  Render + blur it
        # on just its (padded) bounding box rather than a full-poster-size layer —
        # GaussianBlur cost scales with area, so this is ~50× less work for a
        # pixel-identical result.  pad gives the blur kernel room so its soft tail
        # isn't clipped; clamping to the canvas mirrors the old full-layer bounds.
        rx0, ry0 = x0 - expand,          y0 - expand
        rx1, ry1 = x0 + fill_w + expand, y1 + expand
        pad = glow_blur * 3 + 2
        cx0, cy0 = max(0, rx0 - pad), max(0, ry0 - pad)
        cx1, cy1 = min(W, rx1 + pad), min(H, ry1 + pad)
        # Glow colour: "match" blends the bar's own gradient ends for a cohesive
        # coloured halo; a tuple is a custom colour; anything else stays white.
        if glow_color == "match":
            gc = tuple((left_color[i] + right_color[i]) // 2 for i in range(3))
        elif isinstance(glow_color, (tuple, list)) and len(glow_color) == 3:
            gc = tuple(int(c) for c in glow_color)
        else:
            gc = (255, 255, 255)
        glow = Image.new("RGBA", (cx1 - cx0, cy1 - cy0), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            [(rx0 - cx0, ry0 - cy0), (rx1 - cx0, ry1 - cy0)],
            radius=radius + expand,
            fill=(*gc, glow_alpha),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(glow_blur))
        image.alpha_composite(glow, dest=(cx0, cy0))


# ---------------------------------------------------------------------------
# Score bar  (vertical pip)
# ---------------------------------------------------------------------------

def _draw_solid_pip(
    image: Image.Image,
    *,
    x: float,
    y_center: int,
    width: int,
    height: int,
    color: tuple[int, int, int],
) -> None:
    """Draw a single solid-colour cairo-antialiased pill pip onto *image*.

    Shared primitive used by score-driven pips (where the caller computes
    the colour from the score palette).
    """
    y0     = int(y_center - height / 2)
    radius = max(1, width // 2)

    pip_mask  = _cairo_pill_mask(width, height, radius)
    pip_strip = Image.new("RGBA", (width, height), (*color, 0))
    pip_strip.putalpha(pip_mask)
    # Offset composite rather than a full-canvas transparent layer.  y0 can fall
    # outside the canvas for a pip near the edge; alpha_composite clips exactly
    # as paste did (verified pixel-identical across negative and overflowing
    # offsets), so the edge cases behave the same.
    image.alpha_composite(pip_strip, dest=(int(x), y0))


def draw_score_bar_vertical(
    image: Image.Image,
    score: int | str,
    *,
    x: float,
    y_center: int,
    height: int = 36,
    width: int = 4,
    color_mode: int = 0,
    custom_palette: CustomScorePalette | None = None,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return

    score = max(0, min(int(score), 100))
    left_color, _right_color = score_color_for_mode(score, color_mode, custom_palette)
    _draw_solid_pip(image, x=x, y_center=y_center, width=width, height=height, color=left_color)


# ---------------------------------------------------------------------------
# Frosted bar (rating_display_mode == 4)
# ---------------------------------------------------------------------------

def draw_frosted_bar(
    image: Image.Image,
    left_text: str,
    center_text: str,
    right_text: str,
    bar_height_ratio: float = 0.090,
    font_size_ratio: float = 0.40,
    frost_opacity: float = 0.75,
    frost_saturation: float = 1.2,
    frost_reference: bool = False,
    bottom_inset: float = 0.0,
    style: str = "frosted",
    score: int | str | None = None,
    fill_color: tuple[int, int, int] | None = None,
    tint_rgb: tuple[float, float, float] | None = None,
    text_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Full-width frosted glass or dark-body strip near the bottom of the poster.

    style="frosted"        — plain frosted glass body, dark text.
    style="silver"         — dark body, solid silver accent stripe, silver text.
    style="gold"           — dark body, solid gold accent stripe, silver text.
    style="rating_black"   — dark body, rating progress bar (fill_color drives colour).
    style="rating_frosted" — frosted body, dark semi-transparent rating bar for contrast.
    fill_color pre-resolved accent colour for rating_black (ignored for rating_frosted).
    tint_rgb overrides the sampled dominant colour for frosted styles so the bar
    and the info-sash notch can share one tint (sampling the glass texture still
    comes from the actual poster region — only the colour cast is forced).
    """
    import os, colorsys as _cs

    width, height = image.size
    bar_h = max(24, int(height * bar_height_ratio))
    bar_y = height - bar_h - int(height * bottom_inset)

    # ── Font ─────────────────────────────────────────────────────────────────
    font_size = max(10, int(bar_h * font_size_ratio))
    font_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"
    )
    try:
        font = ImageFont.truetype(font_path, font_size)
    except IOError:
        font = ImageFont.load_default()

    _REF   = "Agypq0★·"
    _ref_b = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), _REF, font=font)
    # Pure optical centering — no base nudge; the stripe branches add their own
    # downward compensation to account for the accent bar stealing top space.
    text_y = (bar_h - (_ref_b[3] - _ref_b[1])) // 2 - _ref_b[1]

    _SILVER = (210, 210, 218)
    _GOLD   = (212, 175, 55)
    # Solid accent styles use a thin stripe; rating bar modes use a larger one.
    _accent_stripe = max(2, int(bar_h * 0.06))
    _rating_stripe = max(3, int(bar_h * 0.10))
    stripe = _accent_stripe  # overridden per branch below
    _lift          = max(1, int(bar_h * 0.025))   # small upward correction for non-plain styles
    _stripe_nudge  = max(1, int(bar_h * 0.05)) + _rating_stripe // 2 - _lift

    def _score_pct() -> int:
        try:    return max(0, min(int(score), 100))   # type: ignore[arg-type]
        except: return 0

    def _build_frosted_base() -> tuple[Image.Image, float, float, float]:
        """Returns (bar_img, raw_h, raw_s, raw_v) — HSV before lightening."""
        blur_r = max(6, int(bar_h * 0.45))
        cy = max(0, bar_y); ch = min(bar_h, height - cy)
        reg = image.crop((0, cy, width, cy + ch))
        blr = reg.filter(ImageFilter.GaussianBlur(radius=blur_r))
        # Colour comes from tint_rgb (a whole-poster sample the caller takes from
        # the un-graded art); the blurred texture still comes from the image.
        if tint_rgb is not None:
            dr, dg, db = tint_rgb
        else:
            dr, dg, db = dominant_frost_rgb(image)
        _h2, _s2, _v2 = _cs.rgb_to_hsv(dr/255, dg/255, db/255)
        r, g, b = _frosted_tint(dr, dg, db, frost_saturation, frost_reference)
        base  = blr.resize((width, bar_h), Image.Resampling.LANCZOS).convert("RGBA")
        frost = Image.new("RGBA", (width, bar_h), (r, g, b, int(frost_opacity*255)))
        return Image.alpha_composite(base, frost), _h2, _s2, _v2

    def _frosted_ink() -> tuple[int, int, int]:
        """Label colour for a frosted body — dark on a light bar, light on a dark
        one.  The bar shares the notch's frosted colour, and one matched to a
        tinted vignette can be genuinely dark where every other frost is light."""
        dr, dg, db = tint_rgb if tint_rgb is not None else dominant_frost_rgb(image)
        return _frost_ink(*_frosted_tint(dr, dg, db, frost_saturation, frost_reference))

    if style == "pure_black":
        ink = (*_SILVER, 248)
        arr = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        bar_img = Image.fromarray(arr)
        # No accent stripe, so no stripe compensation — centre like plain frosted.
        text_y += max(1, int(bar_h * 0.03))

    elif style in ("silver", "gold"):
        stripe = _accent_stripe
        accent = _GOLD if style == "gold" else _SILVER
        ink    = (*_SILVER, 248)
        arr    = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        arr[:stripe, :, 0] = accent[0]; arr[:stripe, :, 1] = accent[1]
        arr[:stripe, :, 2] = accent[2]; arr[:stripe, :, 3] = 240
        bar_img = Image.fromarray(arr)
        text_y += max(1, int(bar_h * 0.05)) + stripe // 2 - _lift

    elif style == "rating_black":
        stripe = _rating_stripe
        fc  = fill_color or _SILVER
        dim = tuple(max(0, int(c * 0.20)) for c in fc)
        ink = (*_SILVER, 248)
        arr = np.zeros((bar_h, width, 4), dtype=np.uint8)
        arr[:, :, :3] = 12;  arr[:, :, 3] = int(frost_opacity * 255)
        # Unfilled
        arr[:stripe, :, 0] = dim[0]; arr[:stripe, :, 1] = dim[1]
        arr[:stripe, :, 2] = dim[2]; arr[:stripe, :, 3] = 240
        # Filled
        fw = int(width * _score_pct() / 100)
        if fw > 0:
            arr[:stripe, :fw, 0] = fc[0]; arr[:stripe, :fw, 1] = fc[1]
            arr[:stripe, :fw, 2] = fc[2]; arr[:stripe, :fw, 3] = 240
        bar_img = Image.fromarray(arr)
        text_y += _stripe_nudge

    elif style == "rating_frosted":
        stripe = _rating_stripe
        ink = (*_frosted_ink(), 248)
        bar_img, _, _, _ = _build_frosted_base()
        if fill_color is not None:
            # Explicit colour chosen — use it directly.
            fill_col = fill_color
            dim_col  = tuple(max(0, int(c * 0.12)) for c in fill_col)
        else:
            # Colour Sample: derive a contrasting fill from the bar's own tint.
            # The frosted tint's effective value ≈ _v2*0.4+0.60; if the bar is
            # light go darker, if dark go brighter — always staying hue-matched.
            bar_img2, _h2, _s2, _v2 = _build_frosted_base()
            bar_img = bar_img2  # rebuild with HSV data
            _tint_v = _v2 * 0.4 + 0.60
            if _tint_v > 0.70:  # light bar → dark fill
                _fv = max(0.15, _v2 * 0.30)
            else:                # dark bar → bright fill
                _fv = min(1.0, _v2 * 0.40 + 0.70)
            fr2, fg2, fb2 = _cs.hsv_to_rgb(_h2, min(1.0, _s2 * 1.6), _fv)
            fill_col = (int(fr2 * 255), int(fg2 * 255), int(fb2 * 255))
            dim_col  = tuple(max(0, int(c * 0.12)) for c in fill_col)
        fw = int(width * _score_pct() / 100)
        sa = np.zeros((stripe, width, 4), dtype=np.uint8)
        sa[:, :, 0] = dim_col[0]; sa[:, :, 1] = dim_col[1]
        sa[:, :, 2] = dim_col[2]; sa[:, :, 3] = 90
        if fw > 0:
            sa[:, :fw, 0] = fill_col[0]; sa[:, :fw, 1] = fill_col[1]
            sa[:, :fw, 2] = fill_col[2]; sa[:, :fw, 3] = 230
        bar_img.alpha_composite(Image.fromarray(sa), (0, 0))
        text_y += _stripe_nudge

    else:  # plain frosted — small nudge down, no stripe compensation needed
        ink = (*_frosted_ink(), 248)
        bar_img, _, _, _ = _build_frosted_base()
        text_y += max(1, int(bar_h * 0.03))

    if text_color is not None:
        ink = (*text_color, 248)

    txt_layer = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    td        = ImageDraw.Draw(txt_layer)
    h_pad     = max(20, int(width * 0.055))

    if center_text:
        cw = int(td.textlength(center_text, font=font))
        td.text(((width - cw) // 2, text_y), center_text, font=font, fill=ink)
    if left_text:
        td.text((h_pad, text_y), left_text, font=font, fill=ink)
    if right_text:
        rw = int(td.textlength(right_text, font=font))
        td.text((width - h_pad - rw, text_y), right_text, font=font, fill=ink)

    bar_final = Image.alpha_composite(bar_img, txt_layer)
    result    = image.copy()
    result.alpha_composite(bar_final, (0, bar_y))
    return result


# Weighted score
# ---------------------------------------------------------------------------

def is_anime_rated(ratings: dict) -> bool:
    """True when *ratings* carries a score from an anime source.

    This is the whole test for whether a title scores with the anime weights:
    a MyAnimeList, AniList or Kitsu rating is present, or it isn't. The
    sources are only ever populated for anime, so no genre or keyword
    guesswork is needed on top.
    """
    return any(source in ratings for source in ANIME_RATING_SOURCES)


def calculate_weighted_score(
    ratings: dict,
    weights: dict,
    *,
    fallback_to_imdb: bool = False,
    fallback_source: str | None = None,
) -> int | str:
    """Blend the available ratings using *weights*, renormalised over the
    sources actually present.

    *fallback_source* names a source to fall back on when no weighted source is
    present at all. Used by the anime path: the provider's score is the only
    rating such a title has, and existing weights strings name none of the anime
    sources, so without this the score would always be N/A on exactly the titles
    that path exists for. Checked before *fallback_to_imdb* because an
    anime-native request has no IMDb rating to fall back to.
    """

    total_weight = 0.0
    weighted_sum = 0.0

    for source, value in ratings.items():
        if source not in weights:
            continue

        weight = weights[source]

        if weight == 0:
            continue

        normaliser = SCORE_NORMALISERS.get(source)
        if not normaliser:
            logger.warning(f"No normaliser for source '{source}' — skipping")
            continue

        weighted_sum += normaliser(value) * weight
        total_weight += weight

    if total_weight == 0:
        if fallback_source:
            fallback_value = ratings.get(fallback_source)
            fallback_normaliser = SCORE_NORMALISERS.get(fallback_source)
            if fallback_value is not None and fallback_normaliser:
                return round(fallback_normaliser(fallback_value))
        imdb_value = ratings.get("imdb")
        imdb_normaliser = SCORE_NORMALISERS.get("imdb")
        if fallback_to_imdb and imdb_value is not None and imdb_normaliser:
            return round(imdb_normaliser(imdb_value))
        return "N/A"

    return round(weighted_sum / total_weight)
