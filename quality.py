import logging
import os
import re
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFont

import config as _cfg
from awards import FETCH_FAILED, _FetchFailed
from cache import set_cached_quality
from config import (
    AIOSTREAMS_AUTH,
    AIOSTREAMS_URL,
    BADGE_DIR,
    BADGE_FILES,
    BADGE_HEIGHT,
    QUALITY_LABELS,
)


# ---------------------------------------------------------------------------
# Sentinel — "not an answer yet"
# ---------------------------------------------------------------------------
# Distinct from FETCH_FAILED: the quality source is healthy and reachable, it
# just doesn't have a result for *this* title yet.  Only QualiCache can return
# this — it answers from cache and collects in the background, so a first
# request for an uncrawled title is normal rather than an error.
#
# Callers must treat it as "serve without badges, don't cache the composite,
# don't count it against the source's failure budget".  Backing off the whole
# source because one cold title is still warming would stall every other title.

class _QualityPending:
    """Singleton sentinel returned when a source has no result for a title yet."""
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def __repr__(self):
        return "QUALITY_PENDING"

QUALITY_PENDING = _QualityPending()

# Union of everything a fetch function may return.
QualityResult = "list[str] | _FetchFailed | _QualityPending"


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def _extract_tokens_from_parsed_file(parsed: dict) -> set[str]:
    tokens: set[str] = set()

    res = parsed.get("resolution", "")
    if res == "2160p":
        tokens.add("4K")
    elif res == "1080p":
        tokens.add("1080P")

    visual_tags = {t.upper() for t in parsed.get("visualTags", [])}
    if "DV" in visual_tags or "DOLBY VISION" in visual_tags or "DOVI" in visual_tags:
        tokens.add("DV")
    if "HDR10+" in visual_tags:
        tokens.add("HDR10+")
    if "HDR10" in visual_tags or "HDR" in visual_tags:
        tokens.add("HDR10")

    quality = parsed.get("quality", "").upper()
    if "REMUX" in quality:
        tokens.add("REMUX")
    elif "WEB-DL" in quality or "WEBDL" in quality:
        tokens.add("WEBDL")

    audio_tags = {t.upper() for t in parsed.get("audioTags", [])}
    if any("ATMOS" in t for t in audio_tags):
        tokens.add("ATMOS")
    if "DTS:X" in audio_tags or "DTSX" in audio_tags or "DTS-X" in audio_tags:
        tokens.add("DTSX")

    return tokens


def _ordered_tokens(seen: set[str]) -> list[str]:
    """Reduce a set of detected tokens to one token per category, badge order.

    At most one resolution, one source, one visual tag and one audio tag are
    kept, each in descending preference, so the badge row is deterministic.
    """
    tokens: list[str] = []
    for group in (
        ("4K", "1080P"),
        ("REMUX", "WEBDL"),
        ("DV", "HDR10+", "HDR10"),
        ("ATMOS", "DTSX"),
    ):
        for token in group:
            if token in seen:
                tokens.append(token)
                break
    return tokens


def parse_quality(quality_param: str) -> list[str]:
    """Parse a comma-separated quality string into validated tokens."""
    if not quality_param:
        return []
    tokens = []
    for token in quality_param.split(","):
        token = token.strip()
        if token in QUALITY_LABELS:
            tokens.append(token)
        else:
            logger.warning(f"Unknown quality token ignored: {token!r}")
    return tokens


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_quality_from_aiostreams(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed":
    """
    Returns a list of quality tokens on success (may be empty if the title
    has no streams), or ``FETCH_FAILED`` on a network / API error.

    NOTE: The caller (main.py) is responsible for checking the quality cache
    *before* calling this function and should only call it on a cache miss.
    This function no longer performs a redundant cache read; it only writes
    to the cache after a successful fetch.
    """
    if not AIOSTREAMS_URL or not AIOSTREAMS_AUTH:
        logger.info("AIOStreams URL or auth not configured — skipping quality fetch")
        return []

    if media_type in ("tv", "series"):
        aio_id   = f"{imdb_id}:{season}:{episode}"
        aio_type = "series"
    else:
        aio_id   = imdb_id
        aio_type = "movie"

    try:
        logger.info(f"External API Call: AIOStreams Quality Fetch For {imdb_id}")
        resp = await client.get(
            f"{AIOSTREAMS_URL.rstrip('/')}/api/v1/search",
            params={"type": aio_type, "id": aio_id},
            headers={"Authorization": f"Basic {AIOSTREAMS_AUTH}"},
        )

        if resp.status_code != 200:
            logger.warning(f"AIOStreams error {resp.status_code} for {imdb_id}")
            return FETCH_FAILED

        payload = resp.json()
        if not payload.get("success"):
            err = (payload.get("error") or {}).get("message", "unknown error")
            logger.warning(f"AIOStreams returned failure for {imdb_id}: {err}")
            return FETCH_FAILED

        data    = payload.get("data") or {}
        results = data.get("results", [])
        errors  = data.get("errors") or {}

        if not results:
            if errors:
                logger.warning(
                    f"AIOStreams returned no results for {imdb_id} "
                    f"with scraper errors present: {errors}"
                )
                return FETCH_FAILED

            logger.info(f"AIOStreams returned authoritative empty result for {imdb_id}")
            tokens: list[str] = []
            set_cached_quality(imdb_id, tokens, release_date)
            return tokens

        seen: set[str] = set()
        for result in results[:5]:
            seen |= _extract_tokens_from_parsed_file(result.get("parsedFile") or {})

        tokens = _ordered_tokens(seen)

        logger.info(f"AIOStreams quality for {imdb_id}: {tokens}")
        set_cached_quality(imdb_id, tokens, release_date)
        return tokens

    except Exception as exc:
        logger.error(f"AIOStreams fetch error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED


# ---------------------------------------------------------------------------
# Stremio addon scraper (simplified quality source)
# ---------------------------------------------------------------------------
# Users who find AIOStreams complex can point PostersPlus directly at any
# Stremio addon that supports the stream endpoint — Torrentio, Comet, etc.
# They paste the manifest URL (or install link) from their configured addon
# page; PostersPlus derives the stream base URL and calls it like a regular
# Stremio addon client.


def _normalize_scraper_url(url: str) -> str:
    """Normalise a user-pasted Stremio addon URL to a bare base URL."""
    url = url.strip()
    # stremio:// install links → https://
    if url.startswith("stremio://"):
        url = "https://" + url[10:]
    # Strip /manifest.json suffix
    if url.endswith("/manifest.json"):
        url = url[: -len("/manifest.json")]
    return url.rstrip("/")


def _scraper_host(url: str) -> str:
    """Scheme and host of a scraper URL, for logs.  Torrentio, Comet and
    friends carry their whole config — debrid API key included — in the path,
    so the query-stripping sanitiser used for other sources is not enough."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or '?'}"


def _tokens_from_stremio_stream(
    name: str,
    title: str,
    behavior_hints: dict | None = None,
) -> set[str]:
    """
    Extract quality tokens from a single Stremio stream's name + title fields,
    plus optional behaviorHints (Torrentio-style).

    Stremio addons embed quality in either field; we scan both to be safe.
    The name field typically looks like "Torrentio\\n4K DV" or "Comet\\n1080p".
    The title field is usually a filename like "Movie.2023.2160p.WEB-DL.Atmos.mkv".

    Torrentio also provides richer structured data in behaviorHints:
      - bingeGroup: "torrentio|4k|BluRay REMUX|HDR"  (pipe-separated quality tokens)
      - filename:   "Movie.2023.BDREMUX.2160p.HDR.mkv" (clean release filename)
    Including these fields improves detection accuracy for Torrentio responses.
    """
    binge_group = ""
    filename = ""
    if behavior_hints:
        binge_group = behavior_hints.get("bingeGroup") or ""
        filename    = behavior_hints.get("filename") or ""
    text = f"{name}\n{title}\n{binge_group}\n{filename}".upper()
    tokens: set[str] = set()

    # Resolution
    if re.search(r'\b(2160P|4K|UHD)\b', text):
        tokens.add("4K")
    elif "1080P" in text:
        tokens.add("1080P")

    # HDR — order matters: check DV before HDR10+ before HDR10
    if re.search(r'\bDV\b|DOLBY.?VISION|\bDOVI\b', text):
        tokens.add("DV")
    if "HDR10+" in text:
        tokens.add("HDR10+")
    elif re.search(r'\bHDR10\b|\bHDR\b', text):
        tokens.add("HDR10")

    # Source
    if "REMUX" in text:
        tokens.add("REMUX")
    elif re.search(r'WEB.?DL|WEBDL', text):
        tokens.add("WEBDL")

    # Audio
    if "ATMOS" in text:
        tokens.add("ATMOS")
    if re.search(r'DTS.?X\b', text):
        tokens.add("DTSX")

    return tokens


async def fetch_quality_from_scraper(
    client: httpx.AsyncClient,
    scraper_url: str,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed":
    """
    Fetch quality tokens from a user-configured Stremio addon.

    ``scraper_url`` should be the addon's manifest URL or base URL — e.g.
    ``https://torrentio.stremio.ru/{config}/manifest.json`` or the bare
    base.  Both forms are normalised before use.

    Returns a list of quality tokens on success, or ``FETCH_FAILED`` on a
    network / API error.  The caller is responsible for checking the quality
    cache before calling this function; this function only writes on success.
    """
    base = _normalize_scraper_url(scraper_url)
    if not base:
        return []

    is_series = media_type in ("tv", "series")
    if is_series:
        stream_type = "series"
        stream_id   = f"{imdb_id}:{season}:{episode}"
    else:
        stream_type = "movie"
        stream_id   = imdb_id

    url = f"{base}/stream/{stream_type}/{stream_id}.json"

    try:
        logger.info(f"External API Call: Stremio scraper quality fetch for {stream_id} → {_scraper_host(url)}")
        resp = await client.get(url, timeout=20.0, follow_redirects=True)

        if resp.status_code != 200:
            logger.warning(
                f"Scraper returned {resp.status_code} for {stream_id} "
                f"({_scraper_host(url)})"
            )
            # For series, fall back to a show-level lookup (no season/episode).
            # Some addons support this and it avoids failures when a specific
            # episode isn't indexed yet.
            if is_series:
                fallback_url = f"{base}/stream/series/{imdb_id}.json"
                logger.info(
                    f"Trying show-level series fallback for {imdb_id} → {_scraper_host(fallback_url)}"
                )
                resp = await client.get(fallback_url, timeout=20.0, follow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(
                        f"Scraper series fallback also returned {resp.status_code} "
                        f"for {imdb_id}"
                    )
                    return FETCH_FAILED
            else:
                return FETCH_FAILED

        streams = resp.json().get("streams") or []

        if not streams:
            logger.info(f"Scraper returned no streams for {imdb_id} — caching empty result")
            tokens: list[str] = []
            set_cached_quality(imdb_id, tokens, release_date)
            return tokens

        # Aggregate tokens across the top 5 streams (same logic as AIOStreams).
        seen: set[str] = set()
        for stream in streams[:5]:
            seen |= _tokens_from_stremio_stream(
                stream.get("name") or "",
                stream.get("title") or stream.get("description") or "",
                stream.get("behaviorHints"),
            )

        tokens = _ordered_tokens(seen)

        logger.info(f"Scraper quality for {imdb_id}: {tokens}")
        set_cached_quality(imdb_id, tokens, release_date)
        return tokens

    except Exception as exc:
        logger.error(f"Scraper fetch error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED


# ---------------------------------------------------------------------------
# QualiCache (cached quality API)
# ---------------------------------------------------------------------------
# QualiCache <https://github.com/UmbraProjects/QualiCache> sits in front of the
# same Stremio addons the scraper path talks to, but inverts the timing: it
# crawls catalogues on its own schedule, picks one best trusted release per
# title, and serves only what is already in its SQLite cache.  A read never
# waits on an addon, so a slow Torrentio/Comet can't slow down poster rendering.
#
# The trade-off is that a title nobody has crawled yet answers "pending".  That
# is not an error and must not trip the per-source backoff — see QUALITY_PENDING.
#
# QualiCache also ships an AIOStreams-shaped compatibility endpoint, so it can
# be used with QUALITY_SOURCE=aiostreams and no code change.  This native client
# is preferred: it reads QualiCache's own tokens directly instead of round-
# tripping them through the AIOStreams parsedFile shape, and it can tell
# "pending" apart from "failed", which the compat endpoint cannot express.


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# QualiCache names more sources than PostersPlus has badges for. The badge set
# only ever promised "1080p web-quality or better": a WEBRip and a BluRay
# *encode* both meet that bar, so they fold into the silver Web badge, and only
# a true remux keeps the gold one. Sub-1080p resolutions (720P, SD) and HDTV
# are still dropped — nothing below that bar was meant to display.
_QUALICACHE_TOKEN_FOLD = {"WEBRIP": "WEBDL", "BLURAY": "WEBDL"}


def _normalize_qualicache_url(url: str) -> str:
    """Normalise a QualiCache origin and discard pasted endpoint parameters."""
    # QUALICACHE_URL is an origin/path prefix, not a request URL. If a user
    # pastes ``?min_trust=low`` here and we append the endpoint naively, the
    # endpoint becomes part of that query value and the server receives `/`.
    # Minimum trust has its own setting and is added to ``params`` below.
    url = url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    for suffix in ("/v1/quality", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


async def fetch_quality_from_qualicache(
    client: httpx.AsyncClient,
    qualicache_url: str,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed | _QualityPending":
    """
    Read cached quality tokens from a QualiCache instance.

    Returns a list of quality tokens on success (possibly empty when QualiCache
    has decided no trusted release exists), ``QUALITY_PENDING`` when QualiCache
    is still collecting for this title, or ``FETCH_FAILED`` when QualiCache
    itself is unreachable or misconfigured.

    A per-title ``error`` status is reported as pending, not as a failure:
    QualiCache is healthy and running its own exponential retry for that title,
    so backing off every other title would be counterproductive.

    The caller is responsible for checking the quality cache before calling
    this function; only a resolved result is written back.
    """
    base = _normalize_qualicache_url(qualicache_url)
    if not base:
        logger.info("QUALICACHE_URL not configured — skipping quality fetch")
        return []

    qc_type = "series" if media_type in ("tv", "series") else "movie"
    params: dict[str, str | int] = {
        "min_trust": _cfg.QUALICACHE_MIN_TRUST,
    }
    if qc_type == "series":
        params["season"] = season
        params["episode"] = episode
    # QualiCache uses the release date to pick a refresh policy; passing ours
    # spares it a TMDB lookup when it has no TMDB key configured.  It wants a
    # full ISO date and rejects anything else with a 422 — callers here supply
    # either YYYY-MM-DD or a bare YYYY (the cache-warm path), so send only what
    # QualiCache can parse and let it resolve the rest itself.
    if release_date and _ISO_DATE.fullmatch(release_date):
        params["release_date"] = release_date

    api_key = _cfg.QUALICACHE_API_KEY
    headers = {"X-API-Key": api_key} if api_key else {}
    url = f"{base}/v1/quality/{qc_type}/{imdb_id}"

    try:
        logger.info(f"External API Call: QualiCache quality fetch for {imdb_id}")
        resp = await client.get(url, params=params, headers=headers, timeout=20.0)

        if resp.status_code == 401:
            logger.error(
                "QualiCache rejected our credentials (401) — check that "
                "QUALICACHE_API_KEY matches QualiCache's ACCESS_KEY"
            )
            return FETCH_FAILED
        if resp.status_code != 200:
            logger.warning(f"QualiCache error {resp.status_code} for {imdb_id}")
            return FETCH_FAILED

        payload = resp.json()
        status = payload.get("status") or ""

        if status == "pending":
            logger.info(f"QualiCache still collecting quality for {imdb_id}")
            return QUALITY_PENDING

        if status == "error":
            # QualiCache reached its addons and they failed. It owns the retry.
            logger.info(
                f"QualiCache has no result for {imdb_id} yet "
                f"(last error: {payload.get('last_error') or 'unknown'})"
            )
            return QUALITY_PENDING

        if status == "empty":
            logger.info(f"QualiCache found no trusted release for {imdb_id}")
            tokens: list[str] = []
            set_cached_quality(imdb_id, tokens, release_date)
            return tokens

        if status != "ready":
            logger.warning(f"QualiCache returned unknown status {status!r} for {imdb_id}")
            return FETCH_FAILED

        # QualiCache emits the same token vocabulary PostersPlus uses, plus a
        # few we have no badge for (8K, 1440P, 720P, SD, BLURAY, WEBRIP, HDTV).
        # Sources with a close-enough badge fold into it (see
        # _QUALICACHE_TOKEN_FOLD); the rest are dropped rather than guessed.
        raw_tokens = payload.get("tokens") or []
        seen = {
            _QUALICACHE_TOKEN_FOLD.get(token, token)
            for token in (str(t).upper() for t in raw_tokens)
        }
        unsupported = sorted(seen - set(QUALITY_LABELS))
        if unsupported:
            logger.debug(
                f"QualiCache tokens without a PostersPlus badge for {imdb_id}: "
                f"{', '.join(unsupported)}"
            )

        tokens = _ordered_tokens(seen)

        logger.info(f"QualiCache quality for {imdb_id}: {tokens}")
        set_cached_quality(imdb_id, tokens, release_date)
        return tokens

    except Exception as exc:
        logger.error(f"QualiCache fetch error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------

QUALITY_SOURCES = ("aiostreams", "scraper", "qualicache")

# These helpers read the config module by attribute rather than binding values
# at import time, so a redeployed instance (or a test) can flip the backend
# without a reimport.


def active_quality_source() -> str:
    """Name of the quality backend in use. Unknown values fall back to aiostreams."""
    source = _cfg.QUALITY_SOURCE
    return source if source in QUALITY_SOURCES else "aiostreams"


def quality_source_configured() -> bool:
    """True when the selected backend has everything it needs to answer."""
    source = active_quality_source()
    if source == "scraper":
        return bool(_cfg.SCRAPER_URL)
    if source == "qualicache":
        return bool(_cfg.QUALICACHE_URL)
    return bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH)


async def fetch_quality(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str = "movie",
    season: int = 1,
    episode: int = 1,
    release_date: str | None = None,
) -> "list[str] | _FetchFailed | _QualityPending":
    """
    Fetch quality tokens from whichever backend QUALITY_SOURCE selects.

    Single entry point for every call site so the backends stay interchangeable
    and adding one doesn't mean touching each caller.  The caller checks the
    quality cache first; these functions only write on a resolved result.
    """
    source = active_quality_source()
    if source == "scraper":
        return await fetch_quality_from_scraper(
            client, _cfg.SCRAPER_URL, imdb_id, media_type, season, episode, release_date,
        )
    if source == "qualicache":
        return await fetch_quality_from_qualicache(
            client, _cfg.QUALICACHE_URL, imdb_id, media_type, season, episode, release_date,
        )
    return await fetch_quality_from_aiostreams(
        client, imdb_id, media_type, season, episode, release_date,
    )


# ---------------------------------------------------------------------------
# Badge image cache
# ---------------------------------------------------------------------------
# The top-of-poster gradient ensures the background is always dark, so we
# always use the "light" variant.  The dark variant and luminosity sampling
# are therefore removed.
#
# Badges are cached in memory as pre-resized RGBA Images, keyed by
# (token, height).  The default height is pre-warmed at import time so the
# very first request never pays the resize cost.

BadgeItem = tuple[Image.Image | None, str]

# Raw (un-resized) badge images, loaded once from disk.
_RAW_BADGES: dict[str, Image.Image] = {}

# Resized badge cache: (token, height) -> Image
_BADGE_CACHE: dict[tuple[str, int], Image.Image] = {}


def _load_raw_badge(token: str) -> Image.Image | None:
    """Load and tightly crop the raw badge PNG for *token* (light variant only)."""
    stem = BADGE_FILES.get(token)
    if not stem:
        return None

    path = os.path.join(BADGE_DIR, f"{stem}_light.png")
    if not os.path.exists(path):
        logger.warning(f"Badge file not found: {path}")
        return None

    try:
        img = Image.open(path).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        return img
    except Exception as exc:
        logger.error(f"Badge load failed ({path}): {exc}")
        return None


def _warm_badge_cache(height: int) -> None:
    """Pre-resize all known badges at *height* and store in _BADGE_CACHE."""
    for token in BADGE_FILES:
        raw = _RAW_BADGES.get(token)
        if raw is None:
            continue
        w, h = raw.size
        new_w = max(1, round(w * height / h))
        _BADGE_CACHE[(token, height)] = raw.resize((new_w, height), Image.Resampling.LANCZOS)


def _init_badge_cache() -> None:
    """Load all raw badges and pre-warm the cache at the default badge height."""
    for token in BADGE_FILES:
        img = _load_raw_badge(token)
        if img is not None:
            _RAW_BADGES[token] = img

    _warm_badge_cache(BADGE_HEIGHT)
    logger.info(f"Badge cache warmed: {len(_BADGE_CACHE)} entries at {BADGE_HEIGHT}px")


# Run at import time (cheap — just disk reads + one resize pass per badge).
_init_badge_cache()


def get_resized_badge(token: str, height: int) -> Image.Image | None:
    """
    Return a cached resized badge for *token* at *height* pixels tall.
    Resizes and caches on first miss for a new height.
    """
    key = (token, height)
    cached = _BADGE_CACHE.get(key)
    if cached is not None:
        return cached

    raw = _RAW_BADGES.get(token)
    if raw is None:
        return None

    w, h = raw.size
    new_w = max(1, round(w * height / h))
    resized = raw.resize((new_w, height), Image.Resampling.LANCZOS)
    _BADGE_CACHE[key] = resized
    return resized


# ---------------------------------------------------------------------------
# Alpha-correct resize helper
# ---------------------------------------------------------------------------

def _resize_premultiplied(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize an RGBA image with premultiplied-alpha compositing and edge sharpening.

    Pillow's LANCZOS filter averages pixels without accounting for alpha, so
    fully-transparent edge pixels bleed their RGB values into the result,
    producing coloured fringes on anti-aliased edges.  Premultiplying before
    the resize and un-premultiplying after eliminates this artefact.

    A mild unsharp mask is applied after resize to recover crispness lost
    during heavy downscaling (the combined badges go from ~112 → 40-60 px).
    The sharpening is applied only to the visible (non-transparent) region so
    it doesn't introduce ringing at the alpha boundary.
    """
    import numpy as np
    from PIL import ImageFilter
    arr = np.array(img, dtype=np.float32)          # H×W×4, values 0–255
    alpha = arr[..., 3:4] / 255.0                  # normalised alpha, H×W×1
    arr[..., :3] *= alpha                           # premultiply RGB
    pre = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    pre = pre.resize(size, Image.Resampling.LANCZOS)
    arr2 = np.array(pre, dtype=np.float32)
    alpha2 = arr2[..., 3:4] / 255.0
    nonzero = alpha2[..., 0] > 0
    arr2[nonzero, :3] /= alpha2[nonzero]           # un-premultiply where visible
    result = Image.fromarray(np.clip(arr2, 0, 255).astype(np.uint8))

    # Sharpen only the RGB channels; leave alpha intact to avoid edge ringing.
    r, g, b, a = result.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=0.6, percent=120, threshold=2))
    sr, sg, sb = rgb.split()
    return Image.merge("RGBA", (sr, sg, sb, a))


# ---------------------------------------------------------------------------
# Combined badge (badges/combined/) — display mode 5
# ---------------------------------------------------------------------------

# Cache keyed by (res_key, src_key, vis_key, height).
_COMBINED_CACHE: dict[tuple[str, str, str, int], Image.Image | None] = {}


def get_combined_badge(tokens: list[str], height: int) -> Image.Image | None:
    """Return a single pre-composed badge from badges/combined/ for the given
    quality token list.

    Resolution (4K / 1080P) and source (REMUX / WEBDL) must both be present;
    if either is missing the function returns None and nothing is drawn.
    The visual tag (DV / HDR) is optional and defaults to 'sdr'.
    """
    token_set = set(tokens)

    # Resolution
    if "4K" in token_set:
        res = "4k"
    elif "1080P" in token_set:
        res = "hd"
    else:
        return None

    # Source
    if "REMUX" in token_set:
        src = "remux"
    elif "WEBDL" in token_set:
        src = "web"
    else:
        return None

    # Visual tag — absent means SDR
    if "DV" in token_set:
        vis = "dv"
    elif "HDR10+" in token_set or "HDR10" in token_set:
        vis = "hdr"
    else:
        vis = "sdr"

    cache_key = (res, src, vis, height)
    if cache_key in _COMBINED_CACHE:
        return _COMBINED_CACHE[cache_key]

    stem = os.path.join(BADGE_DIR, "combined", f"{res}_{src}_{vis}")
    img: Image.Image | None = None

    svg_path = stem + ".svg"
    png_path = stem + ".png"

    if os.path.exists(svg_path):
        # Rasterise the SVG at 2× the target height then downscale with
        # premultiplied LANCZOS.  Oversampling lets Cairo anti-alias curves
        # and thin strokes properly before the final downscale, removing the
        # jagged border edges that appear when rendering directly at small sizes.
        try:
            import cairosvg, io
            oversample = height * 2
            png_bytes = cairosvg.svg2png(url=svg_path, output_height=oversample)
            raw = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            w_raw, h_raw = raw.size
            new_w = max(1, round(w_raw * height / h_raw))
            img = _resize_premultiplied(raw, (new_w, height))
        except Exception as exc:
            logger.error(f"Combined badge SVG render failed ({svg_path}): {exc}")

    elif os.path.exists(png_path):
        # Fall back to PNG with premultiplied resize + sharpening.
        try:
            raw = Image.open(png_path).convert("RGBA")
            bbox = raw.getbbox()
            if bbox:
                raw = raw.crop(bbox)
            w, h = raw.size
            new_w = max(1, round(w * height / h))
            img = _resize_premultiplied(raw, (new_w, height))
        except Exception as exc:
            logger.error(f"Combined badge PNG load failed ({png_path}): {exc}")

    else:
        logger.warning(f"Combined badge not found: {stem}.(svg|png)")

    _COMBINED_CACHE[cache_key] = img
    return img


# ---------------------------------------------------------------------------
# Fallback font (loaded once at module level)
# ---------------------------------------------------------------------------

try:
    _FALLBACK_FONT: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"), 28)
except IOError:
    _FALLBACK_FONT = ImageFont.load_default()


# ---------------------------------------------------------------------------
# Badge rendering
# ---------------------------------------------------------------------------

def render_badges_left(
    image: Image.Image,
    items: list[BadgeItem],
    x_start: int,
    y_top: int,
    badge_height: int,
    badge_gap: int,
) -> None:
    if not items:
        return

    draw = ImageDraw.Draw(image)
    x = x_start

    for badge_img, label in items:
        if badge_img is not None:
            image.paste(badge_img, (x, y_top), badge_img)
            x += badge_img.width + badge_gap
        else:
            # Text fallback
            bb = draw.textbbox((0, 0), label, font=_FALLBACK_FONT)
            text_h = bb[3] - bb[1]
            ty = y_top + (badge_height - text_h) // 2
            draw.text((x, ty), label, font=_FALLBACK_FONT, fill=(255, 255, 255, 220))
            x += int(bb[2] - bb[0]) + badge_gap


