#main.py
import asyncio
import dataclasses
import hashlib
import hmac
import io
import logging
import os
import re
import time
import httpx
import numpy as np
from datetime import datetime, timedelta, timezone
import zoneinfo
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# Pull uvicorn's loggers into our root handler so all output shares the same format.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True


class _TruncateUrlFilter(logging.Filter):
    """
    Redact API keys and truncate long URL paths in log records.

    Two responsibilities:
      1. For uvicorn.access records, truncate the request path so long URLs
         don't fill the log.
      2. For ALL records, redact every common API-key query parameter pattern
         in both record.msg and record.args.  This catches keys that slip
         through when an httpx exception is logged (its __str__ includes the
         full upstream URL with our outbound api_key=) as well as anything
         else that might inadvertently include a key.
    """
    _MAX = 80
    # Match query params we hold (tmdb_key, mdblist_key, access_key) AND the
    # upstream parameter names we forward keys under (api_key, apikey).
    _KEY_RE = re.compile(
        r'((?:tmdb_key|mdblist_key|access_key|api_key|apikey)=)[^&\s\'\"]*',
        re.IGNORECASE,
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._KEY_RE.sub(r'\1***', value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: args = (client_addr, method, path, http_version, status_code, ...)
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            path = record.args[2]
            if isinstance(path, str):
                path = self._KEY_RE.sub(r'\1***', path)
                if len(path) > self._MAX:
                    path = path[: self._MAX] + "…"
                record.args = (record.args[0], record.args[1], path) + record.args[3:]

        # Generic redaction for every other record (application logs).
        # We redact in msg and args so the formatted output is safe regardless
        # of whether the record uses % substitution or pre-formatted strings.
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}

        # Tracebacks (logger.exception / exc_info=True) are formatted lazily
        # by the handler.  Pre-format and redact exc_text here so the
        # downstream formatter uses our sanitised copy rather than re-rendering.
        if record.exc_info and not record.exc_text:
            import traceback
            record.exc_text = self._redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)

        return True


# Attach to the root handler, not the root logger — propagation calls
# callHandlers() directly on parent loggers, skipping their logger-level filters.
_url_filter = _TruncateUrlFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_url_filter)

# httpx logs every outbound HTTP request at INFO level, including full URLs with
# API keys in query strings.  Raise its level to WARNING so those lines are never
# written to the log — our own try/except blocks capture errors explicitly.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request coalescing
# ---------------------------------------------------------------------------
# Maps final_cache_key -> Future[bytes] for in-flight renders.
# When multiple requests arrive simultaneously for the same uncached poster
# (common during a burst from AIOMetadata loading a library), only the first
# runs the full pipeline; the rest await its Future and get the result for free.
# This dict is per-worker-process — cross-process deduplication would require
# a shared store like Redis, but intra-process coalescing handles the common
# burst pattern well enough at this scale.
# The future carries (jpeg_bytes, provisional): a coalesced request has to know
# whether the render it is riding on was one the pipeline refused to persist, or
# it would hand out a validator for a poster the server never committed to.
_render_inflight: dict[str, "asyncio.Future[tuple[bytes, bool]]"] = {}

# Coalesces concurrent fetch_poster_metadata calls for the same (tmdb_id,
# media_type, language) tuple.  Without this, simultaneous /poster + /logo
# requests for the same cold title each fire their own TMDB API call.
_metadata_inflight: dict[str, "asyncio.Future[tuple]"] = {}


async def _coalesced_fetch_poster_metadata(
    client: "httpx.AsyncClient",
    tmdb_id: str,
    tmdb_key: str,
    media_type: str,
    lang: str,
    secondary_lang: str = "",
) -> tuple:
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    inflight_key = tmdb_metadata_cache_key(endpoint, tmdb_id, lang, secondary_lang)

    existing = _metadata_inflight.get(inflight_key)
    if existing is not None:
        logger.debug(f"Coalescing metadata fetch for {media_type}/{tmdb_id} ({lang})")
        return await existing

    fut: "asyncio.Future[tuple]" = asyncio.get_running_loop().create_future()
    fut.add_done_callback(
        lambda f: f.exception() if not f.cancelled() and f.exception() else None
    )
    _metadata_inflight[inflight_key] = fut
    try:
        result = await fetch_poster_metadata(
            client, tmdb_id, tmdb_key, media_type, lang, secondary_lang
        )
        fut.set_result(result)
        return result
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    except BaseException:
        if not fut.done():
            fut.cancel()
        raise
    finally:
        _metadata_inflight.pop(inflight_key, None)


# ---------------------------------------------------------------------------
# Background quality fetching
# ---------------------------------------------------------------------------
# Quality data (AIOStreams / scrapers) is fetched in the background so poster
# responses are never blocked by a slow scraper call.  The poster is served
# immediately without quality badges on a cache miss; the next request for the
# same title will find the quality cached and render badges normally.
#
# _quality_bg_inflight: tracks imdb_ids with an active background fetch so
#   scroll bursts don't launch duplicate fetches for the same title.
# _quality_bg_semaphore: caps concurrent AIOStreams calls so a large burst
#   doesn't hammer the scrapers with hundreds of simultaneous requests.

_quality_bg_inflight: set[str] = set()
_quality_bg_semaphore: "asyncio.Semaphore | None" = None   # created inside event loop
_quality_source_backoff_until: dict[str, float] = {}
_quality_source_fail_count: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Rating fetch deduplication
# ---------------------------------------------------------------------------
# Prevents concurrent requests for the same imdb_id (different raw_params /
# final_cache_key) from triggering duplicate MDBlist API calls.  The most
# common burst: AIOMetadata requests many posters simultaneously; several
# share an uncached title with different user-config hashes so render
# coalescing alone doesn't protect them.
#
# _rating_fetch_inflight: maps imdb_id -> asyncio.Event that fires once the
#   first fetch completes.  Subsequent requests wait, then re-read the DB.
# _rating_backoff: maps (imdb_id, API key) -> loop-time after which a new
#   attempt is allowed. Scoping by key lets a rotated or replaced key retry
#   the same title immediately. Network failures use an escalating ladder
#   (30s/2m/8m/1h); rate-limit responses use Retry-After or 1h flat.

_rating_fetch_inflight:         dict[str, asyncio.Event] = {}
_rating_backoff:                dict[tuple[str, str], float] = {}
_rating_fail_count:             dict[tuple[str, str], int]   = {}
_mdblist_semaphore:             "asyncio.Semaphore | None" = None  # caps concurrent MDBlist HTTP calls; created inside event loop
# Caps parallel burned-in-text scans. Each slot owns an independent RapidOCR
# session in a dedicated executor, so cold-cache OCR cannot occupy render workers.
# Created inside the event loop.
_detect_semaphore:              "asyncio.Semaphore | None" = None
_detect_executor:               "ThreadPoolExecutor | None" = None
# Maps immutable image/detector keys to active OCR tasks. Different poster
# configurations often render the same source image during a burst; they should
# share one scan even when their final composite cache keys differ.
_text_detection_inflight:       dict[str, "asyncio.Task[bool | None]"] = {}
_foreground_detection_count = 0
_active_poster_renders = 0
_background_detection_queue: "asyncio.Queue[_DeferredTextDetection] | None" = None
_background_detection_keys: set[str] = set()
_background_detection_task: "asyncio.Task[None] | None" = None


@dataclass(frozen=True)
class _DeferredTextDetection:
    cache_key: str
    image_cache_key: str
    title: tuple[str, ...]
    source: str
    tmdb_id: str
    media_type: str
    image_path: str
    vote_count: int | None
    source_key: str


def _get_detect_semaphore() -> "asyncio.Semaphore":
    """Lazily create the detection-admission semaphore inside the event loop."""
    global _detect_semaphore
    if _detect_semaphore is None:
        _detect_semaphore = asyncio.Semaphore(_cfg.TEXTLESS_DETECTION_CONCURRENCY)
    return _detect_semaphore


def _get_detect_executor() -> ThreadPoolExecutor:
    """Dedicated workers so OCR bursts cannot starve poster compositing."""
    global _detect_executor
    if _detect_executor is None:
        _detect_executor = ThreadPoolExecutor(
            max_workers=_cfg.TEXTLESS_DETECTION_CONCURRENCY,
            thread_name_prefix="text-detect",
        )
    return _detect_executor


def _shutdown_detect_executor() -> None:
    global _detect_executor
    if _detect_executor is not None:
        _detect_executor.shutdown(wait=True, cancel_futures=True)
        _detect_executor = None


def _reserve_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count += 1


def _release_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count = max(0, _foreground_detection_count - 1)


def _start_text_detection(
    cache_key: str,
    image: Image.Image,
    *,
    title: tuple[str, ...],
    source: str,
    tmdb_id: str,
    vote_count: int | None,
    source_key: str,
    media_type: str | None = None,
    image_path: str | None = None,
    foreground: bool = True,
    foreground_reserved: bool = False,
) -> "asyncio.Task[bool | None]":
    """Start or join one OCR scan for an immutable source image."""
    cached = get_cached_text_detection(cache_key)
    if cached is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        async def _cached_result() -> bool:
            return cached
        return asyncio.create_task(_cached_result())

    existing = _text_detection_inflight.get(cache_key)
    if existing is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        logger.info(
            f"Coalescing burned-in text scan for {tmdb_id} "
            f"(votes={vote_count}, source={source_key})"
        )
        return existing

    if foreground and not foreground_reserved:
        _reserve_foreground_detection()

    async def _scan() -> bool | None:
        from text_detect import poster_has_burned_in_text

        try:
            async with _get_detect_semaphore():
                result = await asyncio.get_running_loop().run_in_executor(
                    _get_detect_executor(),
                    lambda: poster_has_burned_in_text(
                        image,
                        conf=_cfg.PPOCR_BOX_THRESHOLD,
                        title=title,
                        source=source,
                        debug=True,
                    ),
                )
            if result is not None:
                set_cached_text_detection(cache_key, result)
            if result is True and source == "poster" and media_type and image_path:
                from textless_report import report_fake_textless_poster
                report_fake_textless_poster(
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    image_path=image_path,
                    vote_count=vote_count,
                )
            return result
        finally:
            if foreground:
                _release_foreground_detection()

    logger.info(
        f"Scanning textless poster {tmdb_id} for burned-in text "
        f"(votes={vote_count}, source={source_key}, "
        f"priority={'foreground' if foreground else 'background'})"
    )
    task = asyncio.create_task(_scan())
    _text_detection_inflight[cache_key] = task

    def _cleanup(done: "asyncio.Task[bool | None]") -> None:
        if _text_detection_inflight.get(cache_key) is done:
            _text_detection_inflight.pop(cache_key, None)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_cleanup)
    return task


def _queue_background_text_detection(item: _DeferredTextDetection) -> None:
    """Queue one vote-gated scan without retaining its decoded image."""
    if get_cached_text_detection(item.cache_key) is not None:
        return
    if item.cache_key in _background_detection_keys:
        return
    if _background_detection_queue is None:
        logger.warning(
            f"Background text-detection queue unavailable for {item.tmdb_id}; "
            "scan will retry on the next request"
        )
        return
    _background_detection_keys.add(item.cache_key)
    _background_detection_queue.put_nowait(item)
    logger.info(
        f"Queued vote-gated text scan for {item.tmdb_id} "
        f"(votes={item.vote_count}, pending={_background_detection_queue.qsize()})"
    )


def _load_detection_image(image_cache_key: str) -> Image.Image | None:
    cached_bytes = get_cached_tmdb_poster(image_cache_key)
    if not cached_bytes:
        return None
    return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")


async def _background_text_detection_worker() -> None:
    """Drain vote-gated scans only while no foreground scan is queued or running."""
    assert _background_detection_queue is not None
    while True:
        item = await _background_detection_queue.get()
        try:
            if get_cached_text_detection(item.cache_key) is not None:
                continue
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            image = await asyncio.get_running_loop().run_in_executor(
                None, _load_detection_image, item.image_cache_key
            )
            if image is None:
                logger.warning(
                    f"Deferred text scan source unavailable for {item.tmdb_id}; "
                    "scan will retry on the next request"
                )
                continue

            # A poster render may have arrived while the image was loading.
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            await asyncio.shield(_start_text_detection(
                item.cache_key,
                image,
                title=item.title,
                source=item.source,
                tmdb_id=item.tmdb_id,
                vote_count=item.vote_count,
                media_type=item.media_type,
                image_path=item.image_path,
                source_key=item.source_key,
                foreground=False,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"Deferred text scan failed for {item.tmdb_id}: {exc}"
            )
        finally:
            _background_detection_keys.discard(item.cache_key)
            _background_detection_queue.task_done()


# Per-key cooldown timestamps (event-loop time). Keyed by the API key string so
# rotation is independent — a rate-limited key stands down while the other serves.
_mdblist_key_cooldown: dict[str, float] = {}
# Index into _cfg.SERVER_MDBLIST_KEYS for the currently active server-side key.
_mdblist_active_key_idx: int = 0


def _quality_backoff_remaining(now: float | None = None) -> float:
    if now is None:
        now = asyncio.get_running_loop().time()
    return max(0.0, _quality_source_backoff_until.get(active_quality_source(), 0.0) - now)


def _record_quality_result(result) -> None:
    # QUALITY_PENDING means the source answered and is healthy — it just has no
    # value for this title yet. It is neither a success to reset the failure
    # count on nor a failure to count, so the backoff state is left untouched.
    if result is QUALITY_PENDING:
        return
    source = active_quality_source()
    if result is not FETCH_FAILED:
        _quality_source_backoff_until.pop(source, None)
        _quality_source_fail_count.pop(source, None)
        return
    now = asyncio.get_running_loop().time()
    if _quality_source_backoff_until.get(source, 0.0) > now:
        return
    failures = _quality_source_fail_count.get(source, 0) + 1
    _quality_source_fail_count[source] = failures
    delay = min(30.0 * (4 ** (failures - 1)), 1800.0)
    _quality_source_backoff_until[source] = now + delay
    logger.warning(f"Quality source {source} unavailable; backing off for {delay:.0f}s")


def _next_mdblist_server_key(current_key: str, now: float | None = None) -> str | None:
    """Select a healthy configured server key after *current_key*."""
    global _mdblist_active_key_idx
    keys = _cfg.SERVER_MDBLIST_KEYS
    if len(keys) < 2 or current_key not in keys:
        return None
    if now is None:
        now = asyncio.get_running_loop().time()
    start = keys.index(current_key)
    for offset in range(1, len(keys)):
        idx = (start + offset) % len(keys)
        candidate = keys[idx]
        if now >= _mdblist_key_cooldown.get(candidate, 0.0):
            _mdblist_active_key_idx = idx
            return candidate
    return None


def _mdblist_server_key_number(key: str | None) -> int | None:
    if not key:
        return None
    for idx, candidate in enumerate(_cfg.SERVER_MDBLIST_KEYS):
        if candidate == key:
            return idx + 1
    return None


def _mdblist_server_key_label(key: str | None) -> str:
    number = _mdblist_server_key_number(key)
    if number is None:
        return "request-supplied key"
    return f"configured key #{number}"


def _mark_mdblist_rate_limit(
    canonical_id: str, key: str, result
) -> tuple[float, str | None]:
    """Cool down a rate-limited key and select a healthy configured fallback."""
    if result.retry_after:
        backoff_secs = min(float(result.retry_after), 3600.0)
    else:
        backoff_secs = 3600.0
    now = asyncio.get_running_loop().time()
    _mdblist_key_cooldown[key] = now + backoff_secs
    _rating_backoff[_rating_retry_key(canonical_id, key)] = now + backoff_secs
    return backoff_secs, _next_mdblist_server_key(key, now)


async def _background_quality_fetch(
    quality_id: str,
    media_type: str,
    season: int,
    episode: int,
    release_date: str | None,
) -> None:
    """Fetch quality tokens from the configured quality source and cache them.  Never raises."""
    global _quality_bg_semaphore
    if _quality_bg_semaphore is None:
        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
    try:
        async with _quality_bg_semaphore:
            if _HTTP_CLIENT is None:
                return
            remaining = _quality_backoff_remaining()
            if remaining > 0:
                logger.debug(
                    f"Quality fetch skipped for {quality_id}; source cooldown has {remaining:.0f}s remaining"
                )
                return
            result = await _with_retry(
                fetch_quality,
                _HTTP_CLIENT, quality_id, media_type, season, episode, release_date,
            )
            _record_quality_result(result)
            if result is QUALITY_PENDING:
                # QualiCache is collecting in the background; the next request
                # for this title picks up the value once it lands.
                logger.info(f"Background quality fetch pending for {quality_id}")
            elif result is not FETCH_FAILED:
                logger.info(f"Background quality fetch complete for {quality_id}")
    except Exception as exc:
        _record_quality_result(FETCH_FAILED)
        logger.warning(f"Background quality fetch failed for {quality_id}: {exc}")
    finally:
        _quality_bg_inflight.discard(quality_id)

# Local imports
from age_badge import draw_quality_age_badge, draw_quality_corner_bookmark, draw_tier_bar, _score_points
from landscape import build_landscape
from awards import _dominant_cluster, _is_skin_tone, dominant_frost_rgb
from awards import FETCH_FAILED, _RateLimited, draw_award_badge, draw_award_sash, parse_mdblist_awards
from festivals import match_festival_keyword
from i18n import load_languages, translate_genre, translate_sash
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster_entry,
    set_cached_final_poster,
    delete_cached_final_poster,
    get_cached_tmdb_poster,
    get_cached_tmdb_metadata,
    get_cached_text_detection,
    set_cached_text_detection,
    init_db,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
    release_status_ttl_seconds,
    get_cache_stats,
    get_app_state,
    set_app_state,
    get_db,
)
from digital_release import digital_release_poll_loop
import imdb_dataset
from imdb_dataset import imdb_dataset_refresh_loop
import config as _cfg
from discovery import (
    ALL_PRIORITY_SLOTS,
    DiscoveryMeta,
    extract_discovery_meta,
    pick_sash,
)
from quality import (
    QUALITY_PENDING,
    QUALITY_SOURCES,
    BadgeItem,
    active_quality_source,
    fetch_quality,
    get_resized_badge,
    parse_quality,
    quality_source_configured,
    render_badges_left,
)
from ratings import (
    CustomScorePalette,
    calculate_weighted_score,
    draw_frosted_bar,
    draw_score_bar,
    fetch_rating,
    parse_custom_score_palette,
    score_color_for_mode,
    _draw_solid_pip,
    _score_color,
    _score_color_alt,
    _score_color_metal,
)
from tmdb import composite_logo, logo_centre_y, fetch_logo, image_language_order, fetch_poster_metadata, fetch_poster_image, fetch_backdrop_image, fetch_landscape_image, fetch_trending_rank, fetch_trending_candidates, fetch_popular_candidates, fetch_supplemental_candidates, fetch_catalog_candidates, fetch_release_status, fetch_recent_movie_digital_release_date, svg_logo_supported, tmdb_metadata_cache_key, _CROP_VERSION, _fetch_metahub_logo, LOGO_ABS_MAX_H, TEXT_FORWARD_PRIORITIES as _TEXT_FORWARD_LOGO_PRIORITIES

# Logo priorities that consult the secondary preferred language ("custom").
# Elsewhere the secondary language is inert and must be kept out of the image
# fetch / cache key so single-language requests keep their existing cache entry.
_SECONDARY_LANGUAGE_PRIORITIES = frozenset({
    "native_custom_text",
    "native_custom_original_text",
})
import tvdb
import anime

# ---------------------------------------------------------------------------
# Persistent HTTP client
# ---------------------------------------------------------------------------
# One client for the lifetime of the process. httpx keeps TCP connections
# alive in its connection pool, so repeated requests to the same host
# (TMDB, MDblist, AIOStreams) reuse the existing socket rather than paying
# TLS + TCP handshake overhead on every poster request.
#
# Timeouts are split:
#   connect=5s  — fail fast when a host is unreachable
#   read=12s    — allow slow responses from external APIs
#   pool=5s     — don't block forever waiting for a pool slot

_HTTP_CLIENT: httpx.AsyncClient | None = None

def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        http2=False,   # most poster APIs don't support h2; skip the negotiation
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_TMDB_ID_RE  = re.compile(r'^\d{1,10}$')
_IMDB_ID_RE  = re.compile(r'^tt\d{1,10}$')
_VALID_TYPES = frozenset({"movie", "tv", "series"})


def _check_tmdb_id(val: str) -> None:
    if not _TMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid tmdb_id")


def _check_imdb_id(val: str) -> None:
    if not _IMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid imdb_id")


def _normalise_optional_id(raw: str | None, name: str) -> str:
    """Trim an optional id param, reading an unsubstituted placeholder as absent.

    A template pasted into a metadata provider arrives with the placeholder
    still in it when that provider has no id for the title — AIOMetadata's
    optional "{name?}" form is left verbatim by older builds, and some addons
    reject the "?" syntax outright so operators write the plain form. Either way
    the value is "no id", not a malformed one, and 400ing it would take down
    every poster served through that template.

    Deliberately narrow: only this parameter's own two literals. Accepting any
    brace-wrapped value would silently swallow genuine typos.
    """
    value = (raw or "").strip()
    if value in ("{" + name + "}", "{" + name + "?}"):
        return ""
    return value


def _canonical_rating_id(imdb_id: str, anime_key: str, tmdb_id: str) -> str:
    """The immutable cache/coalescing identity for a request.

    Chosen once, before any metadata is fetched, and never revised: it keys the
    rating cache read, the rating cache write, the coalescing map and the
    back-off tables, so a value that changed mid-request would read one row and
    write another — turning every subsequent request for that title into a fresh
    MDBList call, permanently.

    An IMDb id discovered later from TMDB metadata therefore never lands here.
    See _quality_identity() for the identity that may use it.

    The "tmdb:" form can't collide with a bare TMDB id or a tt-prefixed IMDb id,
    and matches the namespacing the anime path already stores in these columns —
    so no migration is needed.
    """
    return imdb_id or anime_key or f"tmdb:{tmdb_id}"


def _merge_imdb_dataset_rating(
    ratings_dict, effective_imdb_id: str | None, rcfg: "RequestConfig"
):
    """Supply the "imdb" entry in *ratings_dict* from the local IMDb dataset.

    Three modes, per rcfg.imdb_rating_source:

      "mdblist"  (default) — no-op. The IMDb weight comes from MDBList like
                             every other source.
      "dataset"            — always override. MDBList is not consulted for
                             this one source at all, so the weight works
                             with no MDBList key configured.
      "fallback"           — backfill only. MDBList's answer wins whenever
                             it has one; the dataset fills the gap when it
                             does not.

    "fallback" is the mode that pays off for operators who do use MDBList,
    and it covers two distinct gaps with one rule. A hard gap: MDBList was
    rate-limited, timed out, or every configured key is cooling down, so
    the failure path arrives here with an empty dict. And a soft gap:
    MDBList answered fine but carried no IMDb score for this title, or
    carried one that RATING_MIN_VOTES filtered out. Both look identical
    from here — "imdb" is absent — so both are covered by testing for its
    absence rather than by inspecting how the fetch went.

    Note the deliberate asymmetry with fallback_to_imdb, which is a
    *scoring* fallback: it fires when no weighted source scored at all and
    reaches for whatever "imdb" value is present. This one fires earlier,
    at the point the ratings are assembled, and is what puts a value there
    for it to find. The two compose: dataset backfill, then weighting,
    then fallback_to_imdb if the weights still produced nothing.

    In every no-op case the existing MDBList behaviour is left exactly as
    it was — this only ever adds or replaces the single "imdb" key, and
    only when it has something to put there.
    """
    if not isinstance(ratings_dict, dict):
        return ratings_dict
    mode = rcfg.imdb_rating_source
    if mode not in ("dataset", "fallback"):
        return ratings_dict
    if mode == "fallback" and ratings_dict.get("imdb") is not None:
        return ratings_dict
    if not effective_imdb_id:
        return ratings_dict
    value = imdb_dataset.get_rating(effective_imdb_id)
    if value is None:
        return ratings_dict
    return {**ratings_dict, "imdb": value}


def _ratings_base(ratings_dict):
    """Normalise "MDBList was never asked" to an empty dict.

    An instance with no MDBList key and no cached rating row resolves its
    rating tuple from cached_ratings_dict, which is None rather than {}.
    That is precisely the configuration the dataset / direct-TMDB sources
    exist to serve, so it has to reach the merge helpers as a dict —
    otherwise they are skipped, no MDBList-free source is ever consulted,
    and the score is left as None (which the debug JSON then fails to
    int()).

    Anything that is already a dict, and any non-None sentinel such as the
    "N/A" string, is passed through untouched: those are real answers, not
    an absence of one.
    """
    return {} if ratings_dict is None else ratings_dict


def _merge_direct_tmdb_rating(ratings_dict, tmdb_data: dict, rcfg: "RequestConfig"):
    """Supply the "tmdb" entry in *ratings_dict* from TMDB's own vote_average.

    Same three modes as _merge_imdb_dataset_rating, per
    rcfg.tmdb_rating_source: "mdblist" (default, no-op), "direct" (always
    override), "fallback" (backfill only when MDBList has no "tmdb" value,
    whether because the fetch failed or because it simply carried none).
    See that function for why absence is the right thing to test.

    "fallback" is close to free here in a way it is not for the IMDb
    dataset: there is no download, no table and no readiness window, so an
    MDBList outage is covered for this source on an instance that has
    opted into nothing else.

    Unlike the IMDb dataset, this costs nothing extra: vote_average rides
    along in the same TMDB details call already made for genre, year and
    credits, so this is a pure re-use of data already in hand — no schedule,
    no local storage, no separate opt-in infrastructure. It works with no
    MDBList key configured at all, same rationale as the IMDb dataset source.

    SCORE_NORMALISERS["tmdb"] is the identity function because MDBList's own
    "tmdb" source is already expressed on a 0-100 scale; TMDB's API reports
    vote_average on a 0-10 scale, so it is rescaled here to match.

    RATING_MIN_VOTES applies here exactly as it does to every MDBList-sourced
    rating (see ratings.fetch_mdblist_data), including MDBList's own "tmdb".
    Without it this path would be the one rating source in the app with no
    vote floor, and a single 10/10 vote on an obscure title would render a
    score of 100.
    """
    if not isinstance(ratings_dict, dict):
        return ratings_dict
    mode = rcfg.tmdb_rating_source
    if mode not in ("direct", "fallback"):
        return ratings_dict
    if mode == "fallback" and ratings_dict.get("tmdb") is not None:
        return ratings_dict
    vote_average = tmdb_data.get("vote_average") if tmdb_data else None
    if vote_average is None:
        return ratings_dict
    try:
        vote_average = float(vote_average)
    except (TypeError, ValueError):
        return ratings_dict
    if vote_average <= 0:
        return ratings_dict
    vote_count = tmdb_data.get("vote_count")
    try:
        vote_count = int(vote_count) if vote_count is not None else None
    except (TypeError, ValueError):
        vote_count = None
    if vote_count is not None and vote_count < _cfg.RATING_MIN_VOTES:
        logger.info(
            f"Skipping direct TMDB rating: vote_count={vote_count} < "
            f"{_cfg.RATING_MIN_VOTES}"
        )
        return ratings_dict
    return {**ratings_dict, "tmdb": vote_average * 10}


def _quality_identity(
    imdb_id: str, anime_key: str, effective_imdb_id: str | None
) -> str | None:
    """The id sent to the configured quality source, or None to skip the lookup.

    Unlike the rating identity this is an *upstream* identity — Torrentio, Comet,
    AIOStreams and QualiCache have to recognise it — so it is resolved after
    metadata, and may use an IMDb id that only TMDB knew about.

    Precedence is load-bearing. The anime-native id outranks a TMDB-discovered
    IMDb id because it is what Stremio itself sends those addons for anime, and
    because promoting the tt id would orphan every quality row already cached
    under "kitsu:…". A title with no IMDb id at all yields None: there is no
    accepted "tmdb:<id>" stream id for the ordinary sources, so the lookup is
    skipped rather than issued in a form nothing answers.
    """
    return imdb_id or anime_key or effective_imdb_id or None


def _check_type(val: str) -> None:
    if val not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type")


def _resolve_anime_request(
    anilist_id: str, kitsu_id: str, stremio_id: str = ""
) -> "tuple[str | None, int | None]":
    """Select the anime provider for this request, or (None, None) for the
    ordinary TMDB path.

    *stremio_id* is the preferred input: it carries the raw Stremio meta id
    ("kitsu:7442", "tt0903747", "tmdb:1396"), so a client can send it with a
    plain, always-populated placeholder and never needs the optional "{name?}"
    syntax that some addons reject. Non-anime ids simply yield the TMDB path.

    The per-namespace params remain accepted so URLs generated before this
    existed keep working.

    A malformed per-namespace id raises 400 rather than falling through to the
    TMDB path, where it would surface as a confusing "Invalid tmdb_id".  AniList
    wins when both are supplied — arbitrary, but deterministic, so a client that
    sends both always lands on the same cache entry.
    """
    if not _cfg.ANIME_SOURCES_ENABLED:
        return None, None

    # A raw Stremio id is never malformed from our point of view — anything we
    # don't recognise is just a non-anime title — so it never raises.
    namespace, parsed = anime.parse_stremio_id(stremio_id)
    if namespace is not None:
        return namespace, parsed
    for namespace, raw in (("anilist", anilist_id), ("kitsu", kitsu_id)):
        raw = (raw or "").strip()
        if not raw:
            continue
        # A template pasted into a metadata provider may arrive with the
        # placeholder unsubstituted ("{kitsu_id}") when that provider has no id
        # for the title. Treat it as absent rather than malformed — otherwise a
        # single anime placeholder in the URL would 400 every live-action
        # poster served through the same template.
        if raw.startswith("{") and raw.endswith("}"):
            continue
        parsed = anime.parse_anime_id(namespace, raw)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"Invalid {namespace}_id")
        return namespace, parsed
    return None, None


# ---------------------------------------------------------------------------
# Key resolution helpers
# ---------------------------------------------------------------------------

def _resolve_tmdb_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_TMDB_KEY:
        return _cfg.SERVER_TMDB_KEY
    return None


def _resolve_mdblist_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_MDBLIST_KEYS:
        return _cfg.SERVER_MDBLIST_KEYS[_mdblist_active_key_idx % len(_cfg.SERVER_MDBLIST_KEYS)]
    return None


def _rating_retry_key(canonical_id: str, mdblist_key: str) -> tuple[str, str]:
    """Identify retry state for one title on one MDBList API key."""
    return canonical_id, mdblist_key


def _detection_vote_ok(vote_count: int | None) -> bool:
    """True when an asset should be scanned during the foreground request."""
    return vote_count is not None and vote_count <= _cfg.TEXTLESS_DETECTION_MAX_VOTES


# ---------------------------------------------------------------------------
# Per-request configuration
# ---------------------------------------------------------------------------

_CLIENT_EDGE_INSETS = {
    "stremio_tv_nuvio": (0.0, 0.0),
    "stremio_desktop_web": (0.007, 0.004),
    # Plex renders posters uncropped in its grid/details views — no edge
    # compensation needed. Used by the plex_sync.py companion script.
    "plex": (0.0, 0.0),
    # Same story for Jellyfin's web/desktop clients — posters render
    # uncropped in the library grid and detail views. Used by the
    # jellyfin_sync.py companion script.
    "jellyfin": (0.0, 0.0),
}


@dataclass
class RequestConfig:
    """
    Holds all user-tuneable config values for a single request.
    Defaults come from the global config module; query params override them.
    """
    show_award_sash:     bool = field(default_factory=lambda: _cfg.SHOW_AWARD_SASH)
    sash_poster_color:   bool = False   # diagonal sash colour derived from poster art
    cinema_greyscale:    bool = True    # greyscale art when release_status == "Cinema"
    cinema_greyscale_skip_if_available: bool = False  # keep colour if Web/Remux source found
    release_status_cinema_only: bool = False  # only show release status when "Cinema"
    badge_display_mode:  int  = field(default_factory=lambda: _cfg.BADGE_DISPLAY_MODE)
    rating_display_mode: int  = field(default_factory=lambda: _cfg.SHOW_RATING_DISPLAY_MODE)

    accent_bar_font_size_ratio:    float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_SIZE_RATIO)
    # Score Bar mode label suffix: 0 = Year (legacy default), 1 = Info sash, 2 = Year + Info sash
    accent_bar_append_mode:        int   = 0
    # Score Bar position knob — distance from poster bottom edge as fraction of height.
    # Default matches the legacy hardcoded 30px on a 500x750 poster.
    accent_bar_bottom_ratio:       float = 0.04
    numeric_score_font_size_ratio: float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_SIZE_RATIO)
    # Clean mode (mode 2) numeric format.  When True, the rating is divided by
    # 10 and shown to one decimal (87 → "8.7", 100 → "10.0").  Default keeps
    # the legacy 0-100 integer form.
    score_out_of_10: bool = False
    accent_bar_y_offset:           float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_Y_OFFSET)
    numeric_score_y_offset:        float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_Y_OFFSET)
    score_glow_threshold:          int   = field(default_factory=lambda: _cfg.SCORE_GLOW_THRESHOLD)
    score_glow_blur:               int   = field(default_factory=lambda: _cfg.SCORE_GLOW_BLUR)
    score_glow_alpha:              int   = field(default_factory=lambda: _cfg.SCORE_GLOW_ALPHA)
    # Glow colour: "" = white (default), "match" = the score bar's own colour, or
    # a 6-digit hex string for a custom colour.
    score_glow_color:              str   = ""
    minimalist_mode_font_size_ratio:  float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_SIZE_RATIO)
    minimalist_mode_font_x_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_X_OFFSET)
    minimalist_mode_font_y_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_Y_OFFSET)
    # What to append after the genre in Minimalist mode:
    #   0 = Year (Genre + year, rating as a colour-coded pip — the original look)
    #   1 = Rating (Genre | Score, score printed as text)
    #   2 = Year + Rating (Genre | Year | Score)
    #   3 = Split (the mode-2 group split across both margins)
    minimalist_append_mode: int = 0
    minimalist_score_out_of_10: bool = False
    # Centre the strip on the poster instead of hanging it off the right margin,
    # so it sits under the logo (which is centred).  No effect under Split,
    # whose two groups are defined by the margins they sit on.
    minimalist_center: bool = False
    # Separator glyphs.  The field separator (genre | year) is "pip" — the
    # silver bar — or "bullet"; it covers Year mode's score-coloured separator
    # too, which takes the same shape in the score's colour.  The rating
    # separator, immediately before a printed score, adds "star" and defaults
    # to it.  Both default to what the mode already drew, so nothing changes
    # for anyone who doesn't ask.
    minimalist_separator: str = "pip"
    minimalist_rating_separator: str = "star"

    # Frosted bar (rating_display_mode == 4)
    bar_height_ratio:        float = 0.080
    bar_font_size_ratio:     float = 0.55
    bar_frost_opacity:       float = 0.85
    bar_frost_saturation:    float = 1.2   # frosted colour-cast strength (0 = grey)
    bar_bottom_inset:        float = 0.0
    bar_style:               str   = "frosted"  # "frosted"|"silver"|"gold"|"rating_black"|"rating_frosted"
    bar_accent:              str   = "silver"   # "silver"|"gold"|"palette_0"|"palette_1"|"palette_2"|"palette_custom"
    bar_score_out_of_10:     bool  = False
    bar_append:              str   = "rating_year"  # "rating_year"|"rating"|"year"|"sash"

    logo_max_w_ratio:   float = field(default_factory=lambda: _cfg.LOGO_MAX_W_RATIO)
    logo_max_h_ratio:   float = field(default_factory=lambda: _cfg.LOGO_MAX_H_RATIO)
    logo_bottom_ratio:  float = field(default_factory=lambda: _cfg.LOGO_BOTTOM_RATIO)
    logo_bottom_anchor:  bool  = False
    sash_winner_star:    bool  = False

    badge_height:            int   = field(default_factory=lambda: _cfg.BADGE_HEIGHT)
    badge_gap:               int   = field(default_factory=lambda: _cfg.BADGE_GAP)
    badge_anchor_x:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_X_RATIO)
    badge_anchor_y:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_Y_RATIO)
    badge_min_score:          int  = 2
    combined_badge_stacked:   bool = False

    movie_weights: dict | None = None
    tv_weights:    dict | None = None
    fallback_to_imdb: bool = False
    # Where the "imdb" weight comes from. "mdblist" (default) is the MDBList
    # response, same as every other weighted source. "dataset" looks it up
    # locally from IMDb's own free non-commercial dataset (imdb_dataset.py),
    # bypassing MDBList for this source entirely. "fallback" keeps MDBList as
    # the source of truth and only consults the dataset when MDBList has no
    # IMDb value — an outage, an exhausted key, or simply a title it has no
    # score for. See _merge_imdb_dataset_rating.
    imdb_rating_source: str = "mdblist"
    # Same three modes for the "tmdb" weight, against TMDB's own vote_average
    # from the metadata call PostersPlus already makes for genre/year/credits:
    # "mdblist" (default), "direct" (always), "fallback" (only when MDBList
    # has no tmdb value). Zero extra requests in every mode.
    # See _merge_direct_tmdb_rating.
    tmdb_rating_source: str = "mdblist"

    logo_language: str = field(default_factory=lambda: _cfg.DEFAULT_LOGO_LANGUAGE)
    # Secondary preferred language ("custom").  Only consulted by the
    # native_custom_* priorities below; blank elsewhere (and blank there degrades
    # those modes to their non-custom equivalents).
    logo_language_secondary: str = ""
    # Logo resolution priority.  "native" = the viewer's chosen logo_language
    # (e.g. en); "custom" = logo_language_secondary; "original" = the content's
    # own original language (e.g. ja for an anime).  "text" = render the
    # translated title as text.
    #   "native_original" (default): native → original → text
    #   "original_native":           original → native → text
    #   "native_if_original_english": native if content is native, else English
    #                                 → original → text
    #   "native_text":               native → English → neutral → text
    #                                 (no original-language logo)
    #   "native_custom_text":         native → custom → English → neutral → text
    #   "native_custom_original_text": native → custom → original → English
    #                                 → neutral → text
    logo_priority: str = "native_original"
    # Fallback-poster style for titles with no art: "minimal" (procedural textured
    # backdrop) or "photoreal" (hand-made photographic art that blends with real
    # posters).  Missing photoreal art degrades to the minimal set.
    fallback_bg_style: str = "minimal"
    # Original-art mode: serve TMDB's primary poster (title/logo baked into the
    # art) as-is, skipping our own logo overlay, text detection and the textless/
    # backdrop fallbacks.  The logo is part of the art in this mode.
    use_original_art: bool = False
    # Which poster original-art mode serves:
    #   "primary"   = TMDB's designated default poster (most recognisable)
    #   "top_rated" = highest-voted poster, by logo_priority language order
    original_art_source: str = "primary"
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))
    muted: bool = False
    textless: bool = False
    top_gradient:    str = "high"   # off | low | medium | high | custom - strength of the top vignette
    bottom_gradient: str = "high"   # off | low | medium | high | custom - strength of the bottom vignette
    top_vignette_sash_only: bool = False
    # Tint a vignette from the poster art instead of painting it black.  Chosen per
    # band: the top sits under sashes, badges and the age rating while the bottom
    # sits under the logo and rating bar, so they are not one decision.  Both draw
    # the same whole-poster colour sample the frosted bar / notch / sash use, so a
    # tinted vignette always agrees with them.  (Legacy `vignette_poster_color`
    # sets both — see build_request_config.)
    vignette_poster_color_top: bool = False
    vignette_poster_color_bottom: bool = False
    # Defaults for the sliders and their two toggles are what the tuning settled
    # on; the two per-band toggles above stay off because tinting a vignette is a
    # transformative change to every poster on a shelf and belongs opted into.
    vignette_color_saturation: float = 2.5  # chroma of the tint (0 = plain black vignette)
    vignette_color_lightness: float = 1.3   # scales the tint's Value (1.0 = the tuned base)
    vignette_color_blur: float = 1.0        # 0 = follows the art, 1 = flat dominant colour
    vignette_color_ramp: bool = True        # ramp between the poster's two colours, not one flat tint
    vignette_color_local: bool = True       # weigh the band's own seam against the whole poster
    top_gradient_opacity: float | None = None
    top_gradient_height: float | None = None
    bottom_gradient_opacity: float | None = None
    bottom_gradient_height: float | None = None
    hide_genre: bool = False
    # --- Landscape (16:9) rendering -------------------------------------
    # "portrait" (default, unchanged) | "landscape".  Landscape is a separate
    # renderer, not a variant of the portrait layout — see landscape.py.
    shape: str = "portrait"
    # Which art the landscape renderer draws on:
    #   "textless" — the language-neutral backdrop, with our logo composited
    #   "original" — the highest-voted language-tagged backdrop (title treatment
    #                already baked in), served as-is with no logo of ours
    landscape_art: str = "textless"
    # Where the info badge sits: "top_left" | "top_right" | "logo".  "logo"
    # stacks it above the logo in textless mode; in original-art mode there is
    # no logo of ours to stack on, so it takes the bottom-left slot itself.
    landscape_badge_pos: str = "top_left"
    score_color_mode: int = 2
    score_custom_palette: CustomScorePalette | None = None
    sash_badge: bool = False              # legacy; superseded by sash_mode (kept for back-compat parsing)
    sash_mode: str = "sash"               # "sash" (diagonal) | "notch"
    sash_badge_style:  str   = "frosted" # "silver" | "gold" | "frosted"
    sash_badge_size_w: float = 1.05      # horizontal scale of badge
    sash_badge_size_h: float = 1.05      # vertical scale of badge
    sash_badge_inset: float = 0.0          # top-edge offset as fraction of poster height (± small)
    sash_badge_pad:   float = 1.0          # vertical padding scale (<1 tightens top/bottom space)
    sash_badge_font_ratio:   float = 0.43  # font size as fraction of badge height
    sash_badge_frost_opacity: float = 0.75 # frosted overlay opacity (0.0–1.0)
    sash_badge_frost_saturation: float = 1.2 # frosted colour-cast strength (0 = grey)
    # Take the frosted notch's colour from whatever a tinted vignette landed on,
    # instead of from its own whole-poster sample.  Ignored when neither band is
    # tinted, or when the band that is came out too near black to have a colour.
    notch_vignette_color: bool = False
    # Reference colour mode: match the frosted tint to the poster's true colour
    # (bolder, un-pastel) instead of the saturation-scaled frosted tint. Global.
    frost_reference:         bool  = False
    sash_length_ratio: float = 1.15  # diagonal sash length as fraction of poster width
    sash_height_ratio: float = 0.12  # diagonal sash height (thickness) as fraction of poster width
    wait_for_quality: bool = False  # block response until quality is fetched (for poster-warm workflows)
    greyscale_no_quality: bool = False  # greyscale art when no quality found (needs wait_for_quality)
    rating_text_color: tuple[int, int, int] | None = None
    sash_text_color:   tuple[int, int, int] | None = None


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no")


def _parse_hex_color(val: str | None) -> tuple[int, int, int] | None:
    if not val:
        return None
    v = val.strip().lstrip("#")
    if len(v) != 6:
        return None
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def _parse_weights(raw: str | None, sources: list[str]) -> dict | None:
    if not raw:
        return None
    out = {}
    try:
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, val = part.split(":", 1)
            key = key.strip().lower()
            if key in sources:
                out[key] = max(0.0, min(1.0, float(val)))
    except Exception:
        return None
    return out if out else None


# A sash_priority value that opens with this token is a *diff* against the
# default order rather than a replacement for it.  Spelling it out keeps the
# legacy form untouched: an all-exclusions value like "-cult" still means "only
# these slots", which is how the configurator says "every sash off", and
# quietly reinterpreting that as "default minus cult" would switch sashes back
# on for anyone who had turned them all off.
_SASH_DIFF_SEED = "default"


def _apply_sash_diff(tokens: list[str]) -> list[str]:
    """Default order, with "-slot" removals and "slot@N" moves applied in order.

    Written for URL length: the full order is 30 slots and ~350 characters, and
    a URL carrying it is most of the way to the 2000-character ceiling some
    metadata clients enforce.  Most people move one or two slots, which this
    says in a dozen characters.

    Unknown slots and unparseable positions are skipped rather than rejected, on
    the same principle as the legacy branch — a URL that half-parses still
    renders a poster, where a 400 renders nothing.
    """
    order = list(_cfg.SASH_PRIORITY)
    for token in tokens:
        if token == _SASH_DIFF_SEED:
            continue
        if token.startswith("-"):
            slot = token[1:]
            if slot in order:
                order.remove(slot)
            continue
        slot, _, position = token.partition("@")
        if slot not in ALL_PRIORITY_SLOTS:
            continue
        if position:
            try:
                index = int(position)
            except ValueError:
                continue
            if slot in order:
                order.remove(slot)
            order.insert(max(0, min(index, len(order))), slot)
        elif slot not in order:
            order.append(slot)
    return order


def _parse_sash_priority(raw: str | None) -> list[str]:
    if not raw:
        return list(_cfg.SASH_PRIORITY)
    tokens = [s.strip() for s in raw.split(",") if s.strip()]

    if tokens and tokens[0] == _SASH_DIFF_SEED:
        return _apply_sash_diff(tokens[1:])
    # Tokens prefixed with "-" are explicit exclusions
    excluded  = {t[1:] for t in tokens if t.startswith("-") and t[1:] in ALL_PRIORITY_SLOTS}
    active    = [t      for t in tokens if not t.startswith("-") and t in ALL_PRIORITY_SLOTS]

    # Back-compat: the legacy combined "structural" / "release_status" tokens
    # expand in place to their granular slots so older saved URLs keep working.
    if "structural" in active:
        idx = active.index("structural")
        expanded = [s for s in ["short_film", "mini_series", "binge_ready"] if s not in excluded and s not in active]
        active = active[:idx] + expanded + active[idx+1:]
        
    if "release_status" in active:
        idx = active.index("release_status")
        expanded = [s for s in ["cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"] if s not in excluded and s not in active]
        active = active[:idx] + expanded + active[idx+1:]
        
    # An empty, exclusion-free selection means no real sash config was supplied,
    # so fall back to the full default set. Otherwise the explicit selection is
    # authoritative: slots the user did not list are omitted rather than
    # force-appended, so sashes added in a newer version stay off until the user
    # opts in (re-import the old URL, then enable the new sashes).
    if not active and not excluded:
        return list(_cfg.SASH_PRIORITY)
    return active


def build_request_config(params: dict) -> RequestConfig:
    """Build a RequestConfig from raw query-param strings.

    All numeric overrides are clamped to a sensible range so a malicious or
    careless caller can't pass values that would melt a worker (e.g.
    score_glow_blur=99999 turning into a Gaussian kernel of that radius, or
    badge_height=99999 triggering a multi-GB image resize).  Bounds are
    deliberately a little more generous than the configurator sliders so
    power users can push past UI limits without bypassing safety.
    """
    cfg = RequestConfig()

    # Client profiles provide defaults only; explicit inset parameters below
    # remain authoritative for users who fine-tune either edge manually.
    _client_insets = _CLIENT_EDGE_INSETS.get(
        (params.get("primary_client") or "").strip().lower()
    )
    if _client_insets is not None:
        cfg.bar_bottom_inset, cfg.sash_badge_inset = _client_insets

    def _b(key, default): return _parse_bool(params.get(key), default)

    def _f(key, default, lo: float, hi: float):
        """Float param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, float(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    def _i(key, default, lo: int, hi: int):
        """Int param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, int(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    cfg.show_award_sash         = _b("show_award_sash",        cfg.show_award_sash)
    cfg.sash_poster_color       = _b("sash_poster_color",      cfg.sash_poster_color)
    cfg.cinema_greyscale        = _b("cinema_greyscale",       cfg.cinema_greyscale)
    cfg.cinema_greyscale_skip_if_available = _b("cinema_greyscale_skip_if_available", cfg.cinema_greyscale_skip_if_available)
    cfg.release_status_cinema_only = _b("release_status_cinema_only", cfg.release_status_cinema_only)
    cfg.muted                   = _b("muted",                  cfg.muted)
    cfg.score_out_of_10         = _b("score_out_of_10",        cfg.score_out_of_10)
    cfg.textless                = _b("textless",               cfg.textless)
    # top_gradient accepts off / low / medium / high.  Legacy boolean values
    # (true / false) from pre-v1.0.4 URLs map to high / off respectively so
    # cached configurator links keep working.
    _tg_raw = (params.get("top_gradient") or "").strip().lower()
    if _tg_raw in _TOP_GRADIENT_LEVELS:
        cfg.top_gradient = _tg_raw
    elif _tg_raw in ("true", "1", "yes"):
        cfg.top_gradient = "high"
    elif _tg_raw in ("false", "0", "no"):
        cfg.top_gradient = "off"
    elif _tg_raw == "custom":
        cfg.top_gradient = "custom"
    # else: leave RequestConfig default ("high")

    # bottom_gradient — same four-level enum as top.  Brand-new param so no
    # legacy boolean form to honour; unknown values fall through to the
    # RequestConfig default ("high") which matches the legacy behaviour.
    _bg_raw = (params.get("bottom_gradient") or "").strip().lower()
    if _bg_raw in _BOTTOM_GRADIENT_LEVELS:
        cfg.bottom_gradient = _bg_raw
    elif _bg_raw == "custom":
        cfg.bottom_gradient = "custom"

    cfg.top_vignette_sash_only = _b("top_vignette_sash_only", cfg.top_vignette_sash_only)
    # vignette_poster_color was a single toggle covering both bands before they were
    # split. Honour it as the default for each side so existing URLs and presets
    # keep rendering identically; an explicit per-band param wins over it.
    _vpc_legacy = _b("vignette_poster_color", False)
    cfg.vignette_poster_color_top    = _b("vignette_poster_color_top",    _vpc_legacy)
    cfg.vignette_poster_color_bottom = _b("vignette_poster_color_bottom", _vpc_legacy)
    cfg.vignette_color_saturation = _f("vignette_color_saturation", cfg.vignette_color_saturation, 0.0, 3.0)
    cfg.vignette_color_blur       = _f("vignette_color_blur",       cfg.vignette_color_blur,       0.0, 1.0)
    cfg.vignette_color_lightness  = _f("vignette_color_lightness",  cfg.vignette_color_lightness,
                                       _VIGNETTE_LIGHT_MIN, _VIGNETTE_LIGHT_MAX)
    cfg.vignette_color_ramp    = _b("vignette_color_ramp",    cfg.vignette_color_ramp)
    cfg.vignette_color_local   = _b("vignette_color_local",   cfg.vignette_color_local)
    val_tgo = params.get("top_gradient_opacity")
    if val_tgo is not None:
        try: cfg.top_gradient_opacity = float(val_tgo)
        except ValueError: pass
    val_tgh = params.get("top_gradient_height")
    if val_tgh is not None:
        try: cfg.top_gradient_height = float(val_tgh)
        except ValueError: pass
    val_bgo = params.get("bottom_gradient_opacity")
    if val_bgo is not None:
        try: cfg.bottom_gradient_opacity = float(val_bgo)
        except ValueError: pass
    val_bgh = params.get("bottom_gradient_height")
    if val_bgh is not None:
        try: cfg.bottom_gradient_height = float(val_bgh)
        except ValueError: pass
    cfg.hide_genre = _b("hide_genre", cfg.hide_genre)

    _shape = (params.get("shape") or "").strip().lower()
    if _shape in ("portrait", "landscape"):
        cfg.shape = _shape
    _ls_art = (params.get("landscape_art") or "").strip().lower()
    if _ls_art in ("textless", "original"):
        cfg.landscape_art = _ls_art
    _ls_badge = (params.get("badge_pos") or "").strip().lower()
    if _ls_badge in ("top_left", "top_right", "logo"):
        cfg.landscape_badge_pos = _ls_badge

    cfg.sash_badge              = _b("sash_badge",              cfg.sash_badge)
    # sash_mode supersedes the legacy sash_badge bool; fall back to it for old
    # URLs/presets (sash_badge=true → notch, false → diagonal sash).
    _sm_raw = (params.get("sash_mode") or "").strip().lower()
    if _sm_raw in ("hidden", "sash", "notch"):
        cfg.sash_mode = _sm_raw
    elif "show_award_sash" in params and not cfg.show_award_sash:
        cfg.sash_mode = "hidden"   # legacy: sashes turned off
    elif "sash_badge" in params:
        cfg.sash_mode = "notch" if cfg.sash_badge else "sash"
    cfg.sash_badge_inset         = _f("sash_badge_inset",         cfg.sash_badge_inset,         -0.02, 0.02)
    cfg.sash_badge_pad           = _f("sash_badge_pad",           cfg.sash_badge_pad,           0.5, 1.5)
    cfg.sash_badge_font_ratio    = _f("sash_badge_font_ratio",    cfg.sash_badge_font_ratio,    0.10, 1.0)
    cfg.sash_badge_frost_opacity = _f("sash_badge_frost_opacity", cfg.sash_badge_frost_opacity, 0.0, 1.0)
    cfg.sash_badge_frost_saturation = _f("sash_badge_frost_saturation", cfg.sash_badge_frost_saturation, 0.0, 2.0)
    cfg.notch_vignette_color        = _b("notch_vignette_color", cfg.notch_vignette_color)
    cfg.sash_badge_size_w       = _f("sash_badge_size_w",       cfg.sash_badge_size_w,       0.5, 2.0)
    cfg.sash_badge_size_h       = _f("sash_badge_size_h",       cfg.sash_badge_size_h,       0.5, 2.0)
    _style_raw = params.get("sash_badge_style", cfg.sash_badge_style)
    if _style_raw in ("silver", "gold", "frosted", "black"):
        cfg.sash_badge_style = _style_raw
    cfg.sash_length_ratio       = _f("sash_length_ratio",      cfg.sash_length_ratio,      0.8, 1.5)
    cfg.sash_height_ratio       = _f("sash_height_ratio",      cfg.sash_height_ratio,      0.06, 0.20)
    cfg.wait_for_quality        = _b("wait_for_quality",        cfg.wait_for_quality)
    cfg.greyscale_no_quality    = _b("greyscale_no_quality",    cfg.greyscale_no_quality)
    cfg.score_color_mode        = _i("score_color_mode",       cfg.score_color_mode,       0,   3)
    cfg.score_custom_palette    = parse_custom_score_palette(params.get("score_custom_palette"))
    cfg.badge_display_mode      = _i("badge_display_mode",     cfg.badge_display_mode,     0,   6)
    cfg.rating_display_mode     = _i("rating_display_mode",    cfg.rating_display_mode,    0,   4)

    if "show_quality_badges" in params and "badge_display_mode" not in params:
        if _parse_bool(params.get("show_quality_badges"), True):
            cfg.badge_display_mode = 1
        else:
            cfg.badge_display_mode = 0

    # Font-size ratios are multiplied by the poster width — anything above ~0.3
    # would overflow the poster; we cap at 0.5 to leave headroom for experimentation.
    cfg.accent_bar_font_size_ratio    = _f("accent_bar_font_size_ratio",    cfg.accent_bar_font_size_ratio,    0.0, 0.5)
    cfg.accent_bar_append_mode        = _i("accent_bar_append_mode",        cfg.accent_bar_append_mode,        0,   2)
    cfg.accent_bar_bottom_ratio       = _f("accent_bar_bottom_ratio",       cfg.accent_bar_bottom_ratio,       0.0, 0.5)
    cfg.numeric_score_font_size_ratio = _f("numeric_score_font_size_ratio", cfg.numeric_score_font_size_ratio, 0.0, 0.5)
    cfg.accent_bar_y_offset           = _f("accent_bar_y_offset",           cfg.accent_bar_y_offset,           0.0, 1.0)
    cfg.numeric_score_y_offset        = _f("numeric_score_y_offset",        cfg.numeric_score_y_offset,        0.0, 1.0)
    cfg.score_glow_threshold          = _i("score_glow_threshold",          cfg.score_glow_threshold,          0,   100)
    # Glow blur is a Gaussian kernel radius — cost is O(r²) per pixel, so anything
    # above ~50 starts measurably slowing the render.  Hard cap at 50.
    cfg.score_glow_blur               = _i("score_glow_blur",               cfg.score_glow_blur,               0,   50)
    cfg.score_glow_alpha              = _i("score_glow_alpha",              cfg.score_glow_alpha,              0,   255)
    _gc_raw = (params.get("score_glow_color") or "").strip().lstrip("#").lower()
    if _gc_raw == "match":
        cfg.score_glow_color = "match"
    elif len(_gc_raw) == 6 and all(ch in "0123456789abcdef" for ch in _gc_raw):
        cfg.score_glow_color = _gc_raw
    else:
        cfg.score_glow_color = ""
    cfg.minimalist_mode_font_size_ratio = _f("minimalist_mode_font_size_ratio", cfg.minimalist_mode_font_size_ratio, 0.0, 0.5)
    cfg.minimalist_mode_font_x_offset = _f("minimalist_mode_font_x_offset", cfg.minimalist_mode_font_x_offset, 0.0, 1.0)
    cfg.minimalist_mode_font_y_offset = _f("minimalist_mode_font_y_offset", cfg.minimalist_mode_font_y_offset, 0.0, 1.0)
    cfg.minimalist_append_mode = _i("minimalist_append_mode", cfg.minimalist_append_mode, 0, 3)
    cfg.minimalist_score_out_of_10 = _b("minimalist_score_out_of_10", cfg.minimalist_score_out_of_10)
    cfg.minimalist_center = _b("minimalist_center", cfg.minimalist_center)
    _msep = (params.get("minimalist_separator") or "").strip().lower()
    if _msep in ("pip", "bullet"):
        cfg.minimalist_separator = _msep
    # "star" only here: it labels the score it sits in front of, so it has
    # nothing to say between a genre and a year.
    _mrsep = (params.get("minimalist_rating_separator") or "").strip().lower()
    if _mrsep in ("pip", "bullet", "star"):
        cfg.minimalist_rating_separator = _mrsep

    cfg.bar_height_ratio        = _f("bar_height_ratio",        cfg.bar_height_ratio,        0.04, 0.20)
    cfg.bar_font_size_ratio     = _f("bar_font_size_ratio",     cfg.bar_font_size_ratio,     0.15, 0.70)
    cfg.bar_frost_opacity       = _f("bar_frost_opacity",       cfg.bar_frost_opacity,       0.0,  1.0)
    cfg.bar_frost_saturation    = _f("bar_frost_saturation",    cfg.bar_frost_saturation,    0.0,  2.0)
    cfg.frost_reference         = _b("frost_reference",         cfg.frost_reference)
    cfg.bar_bottom_inset        = _f("bar_bottom_inset",        cfg.bar_bottom_inset,        0.0,  0.10)
    _bst = (params.get("bar_style") or "").strip().lower()
    if _bst in ("frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"):
        cfg.bar_style = _bst
    _bac = (params.get("bar_accent") or "").strip().lower()
    if _bac in ("silver", "gold", "sample", "palette_0", "palette_1", "palette_2", "palette_custom"):
        cfg.bar_accent = _bac
    cfg.bar_score_out_of_10     = _b("bar_score_out_of_10",     cfg.bar_score_out_of_10)
    _bap = (params.get("bar_append") or "").strip().lower()
    if _bap in ("rating_year", "rating", "year", "sash"):
        cfg.bar_append = _bap

    cfg.logo_max_w_ratio   = _f("logo_max_w_ratio",   cfg.logo_max_w_ratio,  0.0, 1.5)
    cfg.logo_max_h_ratio   = _f("logo_max_h_ratio",   cfg.logo_max_h_ratio,  0.0, 1.0)
    cfg.logo_bottom_ratio  = _f("logo_bottom_ratio",  cfg.logo_bottom_ratio, 0.0, 1.0)
    cfg.logo_bottom_anchor = _b("logo_bottom_anchor", cfg.logo_bottom_anchor)
    cfg.sash_winner_star   = _b("sash_winner_star",   cfg.sash_winner_star)

    # badge_height in pixels — generous enough to cover any reasonable customisation
    # but well below the size that would cost real memory on resize.
    cfg.badge_height             = _i("badge_height",             cfg.badge_height,             1,   200)
    cfg.badge_gap                = _i("badge_gap",                cfg.badge_gap,                0,   100)
    cfg.badge_anchor_x           = _f("badge_anchor_x",           cfg.badge_anchor_x,           0.0, 1.0)
    cfg.badge_anchor_y           = _f("badge_anchor_y",           cfg.badge_anchor_y,           0.0, 1.0)
    cfg.badge_min_score      = _i("badge_min_score",
                                  _i("combined_badge_min_score", cfg.badge_min_score, 2, 6),
                                  2, 6)
    cfg.combined_badge_stacked   = _b("combined_badge_stacked",   cfg.combined_badge_stacked)

    all_sources = list(_cfg.MOVIE_WEIGHTS.keys())
    cfg.movie_weights = _parse_weights(params.get("movie_weights"), all_sources)

    tv_sources = list(_cfg.TV_WEIGHTS.keys())
    cfg.tv_weights = _parse_weights(params.get("tv_weights"), tv_sources)
    cfg.fallback_to_imdb = _b("fallback_to_imdb", cfg.fallback_to_imdb)
    _irs = params.get("imdb_rating_source", cfg.imdb_rating_source).strip().lower()
    cfg.imdb_rating_source = (
        _irs if _irs in ("mdblist", "dataset", "fallback") else cfg.imdb_rating_source
    )
    _trs = params.get("tmdb_rating_source", cfg.tmdb_rating_source).strip().lower()
    cfg.tmdb_rating_source = (
        _trs if _trs in ("mdblist", "direct", "fallback") else cfg.tmdb_rating_source
    )

    cfg.logo_language        = (params.get("logo_language", cfg.logo_language).strip().lower())
    cfg.logo_language_secondary = (
        params.get("logo_language_secondary", cfg.logo_language_secondary).strip().lower()
    )
    _lp = params.get("logo_priority")
    if _lp in (
        "native_original",
        "original_native",
        "native_if_original_english",
        "native_text",
        "native_custom_text",
        "native_custom_original_text",
    ):
        cfg.logo_priority = _lp
    elif "logo_native_fallback" in params:
        # Legacy param (boolean): true → native_original, false → native_text.
        cfg.logo_priority = "native_original" if _b("logo_native_fallback", True) else "native_text"
    _fbs = (params.get("fallback_bg_style") or "").strip().lower()
    if _fbs in ("minimal", "photoreal"):
        cfg.fallback_bg_style = _fbs
    cfg.use_original_art      = _b("use_original_art", cfg.use_original_art)
    _oas = (params.get("original_art_source") or "").strip().lower()
    if _oas in ("primary", "top_rated"):
        cfg.original_art_source = _oas
    cfg.sash_priority        = _parse_sash_priority(params.get("sash_priority"))
    cfg.rating_text_color    = _parse_hex_color(params.get("rating_text_color"))
    cfg.sash_text_color      = _parse_hex_color(params.get("sash_text_color"))

    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolved(value):
    return value


async def _with_retry(coro_fn, *args, **kwargs):
    """Call coro_fn(*args, **kwargs) and retry once if FETCH_FAILED is returned."""
    result = await coro_fn(*args, **kwargs)
    if result is FETCH_FAILED:
        result = await coro_fn(*args, **kwargs)
    return result


def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]
    ascent, descent = font.getmetrics()
    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust
    return x, y


# ---------------------------------------------------------------------------
# Poster composition
# ---------------------------------------------------------------------------

# Top-vignette strength.  Each entry maps a level name to
# (top_height_ratio, top_max_alpha).  None means "don't draw the gradient
# at all".  The "high" preset matches the legacy always-on behaviour so
# existing URLs / cached posters render identically when top_gradient is
# omitted.  Tweak the values here to retune any preset.
_TOP_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.20, 150),
    "medium": (0.25, 190),
    "high":   (0.40, 220),
}

# Bottom-vignette strength.  Same shape as the top gradient — (height_ratio,
# max_alpha).  Defaults to "high" which matches the legacy alpha-255 / 50%-
# height fade.  The previous auto-softening for Minimalist/Compact rating
# modes is dropped now that users can pick the level themselves; if you
# liked the softer look on those modes, set bottom_vignette=medium.
_BOTTOM_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.30, 180),
    "medium": (0.40, 210),
    "high":   (0.50, 225),
}
# Easing exponent shared across all bottom-gradient presets — controls the
# curve shape (1.0 = linear; >1 starts darker at the bottom and fades faster
# at the top).  Decoupled from strength so retuning one doesn't affect the
# other.
_BOTTOM_GRADIENT_CURVE = 1.5

# --- Poster-coloured vignette ------------------------------------------------
# HSV Value and Saturation of the tint at full slider strength.  Both are keyed
# to the *slider*, never to how saturated the source art happens to be: the art
# contributes hue, the slider contributes intensity.  Deriving intensity from the
# source instead made output wildly inconsistent across a shelf — a vivid red
# poster earned both more chroma and more Value and blew out, while a muted one
# was scaled down twice over and barely showed at the same setting.
_VIGNETTE_TINT_V = 0.38
_VIGNETTE_TINT_S = 1.00
# Slider value at which the tint reaches that full strength.  This is the top of
# the configurator's range, so the slider maps linearly onto 0 → full.
_VIGNETTE_SAT_FULL = 3.0
# The lightness slider scales _VIGNETTE_TINT_V and nothing else, so 1.0 is exactly
# the tuned value above and the two ends are a near-black band and an airy wash of
# the same hue.  It deliberately does *not* relax the chroma ceiling: that ceiling
# exists to stop a band overpowering the art, and a lighter band overpowers more,
# not less.  Raising lightness alone therefore lifts the band and lets the cap
# take the colour back out of it — ask for both and you raise saturation too.
_VIGNETTE_LIGHT_MIN = 0.4
_VIGNETTE_LIGHT_MAX = 2.5
# Noise gate on the sampled hue, in chroma (Value × Saturation): below _FLOOR the
# sample is treated as colourless and the vignette stays black, reaching full
# trust at _SOLID.  Deliberately generous — this exists only to reject greyscale
# art and near-black shadow noise, NOT to scale down honestly muted palettes,
# which is the mistake that made low-saturation posters need a high Value before
# they read at all.
_VIGNETTE_HUE_FLOOR = 0.02
_VIGNETTE_HUE_SOLID = 0.08
# --- Which hue the poster is "made of" ---------------------------------------
# The tint's hue is chosen from a chroma-weighted hue histogram of the whole
# poster rather than from its largest colour cluster.  A cluster pick answers
# "what is the biggest single colour here", which is the wrong question: it let a
# red coat covering 1.6% of an otherwise black-and-white Schindler's List beat the
# greyscale it stands in, and let a saturated 8% brown outrank the pale blue that
# is half of a hazy landscape.  Summing chroma per hue instead answers "how much
# of this poster is actually this colour", which is what a whole-band wash needs.
_VIGNETTE_HUE_BINS = 36
# Bins either side of a peak that count toward it.  Colour in real art is spread
# over neighbouring hues — a sunset is not one hue but a band of them — so support
# is measured over a family (±3 bins ≈ ±30°), not a single slice.
_VIGNETTE_HUE_SPAN = 3
# Faces are everywhere in poster art and are nobody's idea of a poster's colour,
# but they are still part of it — a sepia portrait really is warm.  Half weight
# keeps skin from *deciding* the hue while letting it corroborate one.
_VIGNETTE_SKIN_WEIGHT = 0.5
# ...and a pixel too dark to read as a colour doesn't get a vote at all, however
# chromatic it measures.  This is where invented reds come from, and why they are
# nearly always red: black in real artwork is not neutral.  The Wire's lower half
# is RGB (13, 4, 3) — the eye calls that black, but it is HSV Saturation 0.79 at
# hue 0.02, and there is enough of it to outvote the poster's actual yellow.  Film
# stock, colour grading and chroma subsampling all leave warm residue in the
# shadows; almost nothing leaves green or blue residue there, which is why one
# colour kept turning up in posters that hadn't got it.  Chroma weighting alone
# doesn't save you: each pixel counts for little, but half a poster of them adds
# up to more support than a real colour covering a tenth of it.
_VIGNETTE_DARK_FLOOR = 0.06   # below this Value a pixel's hue is discarded
_VIGNETTE_DARK_SOLID = 0.16   # ...and above this it is trusted in full
# Support (mean chroma per pixel landing in one hue family) at which the tint is
# fully trusted, and below which it fades to black.  This is the whole
# black-and-white answer: a poster whose colour is one small prop scores an order
# of magnitude below one that is genuinely graded, so it keeps the plain black
# vignette instead of announcing an accent nothing else in the art supports.
_VIGNETTE_SUPPORT_LOW  = 0.006
_VIGNETTE_SUPPORT_FULL = 0.028
# A peak is only worth taking if it actually beat the alternatives.  Where a strip
# of art is two colours in equal measure — a red jacket against a teal sky, at the
# depth where the band happens to meet both — the winner is decided by a rounding
# difference, and the band paints a colour the strip is only half made of.  Worse,
# it is unstable: the same poster re-encoded picks the other one.  So the tint
# fades out as its nearest real rival closes in, and a dead heat lands on black,
# which is the honest answer to "what colour is this" when there are two.
_VIGNETTE_RIVAL_HUE   = 0.15   # hue distance at which a rival is a *different* colour
_VIGNETTE_RIVAL_CLEAR = 0.75   # rival/peak below this is a clear win, above it fades
# Confidence a band has to reach before a frosted notch is allowed to match it.
# The colour a low-confidence band paints is mostly black however vivid the hue it
# was found from, and a notch matching that hue would be the one thing on the
# poster wearing it — the opposite of the agreement the option is asking for.
_VIGNETTE_MATCH_MIN_CONF = 0.35
# Perceived brightness of a fully saturated hue swings roughly 8x between blue
# and yellow at one HSV Value, so matching Value alone still leaves a shelf
# uneven — it only moves which posters shout.  Pull each hue part of the way
# toward a common luminance instead: 0.0 would keep raw Value, 1.0 would match
# luminance exactly and drive blue to a clipped, garish extreme (it cannot be
# bright without being vivid).  Half cuts the spread from ~8x to under 3x while
# leaving every hue inside its natural range.
_VIGNETTE_LUMA_REF     = 0.40
_VIGNETTE_LUMA_CORRECT = 0.5
# ...and of that correction, the share taken out of Saturation rather than Value
# when a hue is *brighter* than the reference.  There is no such thing as a dark
# yellow: drop a gold's Value far enough to match a blue's luminance and it stops
# reading as gold and starts reading as olive mud, which is exactly what made
# yellow and amber posters the worst outputs on a shelf.  Spending half the
# correction on chroma instead lands the same luminance as a paler, cleaner gold.
# Only bright hues are affected — a blue or red is already darker than the
# reference and keeps its full Value boost.
_VIGNETTE_LUMA_SAT_SHARE = 0.5
_LUMA_COEFFS = np.array([0.299, 0.587, 0.114], dtype=np.float32)
# Ceiling on how *colourful* the tint is allowed to be, as CIELAB C* at full
# slider — the other half of the same job the luma correction above does.  Value
# and Saturation say nothing about perceived colourfulness: at one luminance a red
# or a violet carries roughly twice the chroma of a gold or a teal, which is why a
# shelf matched for brightness still had a few bands that took the poster over
# while the rest sat under it.  Everything the eye called pleasant measured 15–19,
# everything called too much measured 29–50, so the budget is set just above the
# pleasant band and scaled by the slider, leaving the slider live across its range.
_VIGNETTE_TINT_MAX_CHROMA = 27.0
_SRGB_TO_XYZ = np.array([[0.4124, 0.3576, 0.1805],
                         [0.2126, 0.7152, 0.0722],
                         [0.0193, 0.1192, 0.9505]], dtype=np.float32)
_D65_WHITE   = np.array([0.95047, 1.0, 1.08883], dtype=np.float32)


def _lab_chroma(rgb: np.ndarray) -> np.ndarray:
    """CIELAB C* of an sRGB array shaped (..., 3) on 0–255.

    Perceptual chroma, not HSV Saturation: the point is to compare how colourful
    two different hues look, which HSV cannot answer — a fully saturated navy and
    a fully saturated gold are both S=1 and nowhere near each other on the eye.
    """
    c = np.clip(rgb, 0.0, 255.0) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _SRGB_TO_XYZ.T) / _D65_WHITE
    f   = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    return np.hypot(500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2]))


def _vignette_hue_profile(
    poster: Image.Image, rows: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(rgb, hue, weight, support) for a poster, as flat 64x64 fields plus a
    per-hue-family support curve.

    ``rows`` optionally weights each row of the region before anything else —
    used to fade out the part of a seam the band will hide (see
    _vignette_band_colour).  It is resampled to the working height, so it can be
    given at the region's own resolution.

    ``weight`` is each pixel's chroma — max minus min channel — so neutrals
    contribute nothing at any brightness, unlike HSV Saturation which explodes on
    near-black (pure black plus a little warm noise reads S=0.22 and used to hand
    black-and-white art an invented olive tint).  It is then faded out over the
    shadows, where a hue is measurable but not visible, and halved over skin; see
    _VIGNETTE_DARK_FLOOR and _VIGNETTE_SKIN_WEIGHT.

    ``support[i]`` is the mean weight per pixel falling in bin ``i``'s hue family,
    i.e. how much of the poster is that colour.  It is both how the hue is chosen
    (the peak) and how far the tint is trusted (the peak's height).
    """
    a = np.asarray(poster.convert("RGB").resize((64, 64), Image.Resampling.BOX),
                   dtype=np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    maxc = a.max(axis=-1)
    chroma = maxc - a.min(axis=-1)
    safe = np.maximum(chroma, 1e-6)
    hue = np.where(maxc == r, ((g - b) / safe) % 6.0,
          np.where(maxc == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0)) / 6.0
    hue = np.where(chroma <= 1e-6, 0.0, hue)
    sat = np.where(maxc > 0, chroma / np.maximum(maxc, 1e-6), 0.0)
    # Vectorised _is_skin_tone (awards.py) — same warm R>G>B band, same limits.
    skin = ((r > g) & (g > b) & (hue >= 0.015) & (hue <= 0.11)
            & (sat >= 0.20) & (sat <= 0.68) & (maxc >= 0.35))
    weight = chroma * np.where(skin, _VIGNETTE_SKIN_WEIGHT, 1.0)
    weight *= np.clip((maxc - _VIGNETTE_DARK_FLOOR)
                      / (_VIGNETTE_DARK_SOLID - _VIGNETTE_DARK_FLOOR), 0.0, 1.0)
    if rows is not None and len(rows):
        n = weight.shape[0]
        weight = weight * np.interp(
            np.linspace(0.0, 1.0, n), np.linspace(0.0, 1.0, len(rows)), rows
        ).astype(np.float32)[:, None]
    idx = np.minimum((hue * _VIGNETTE_HUE_BINS).astype(np.int32), _VIGNETTE_HUE_BINS - 1)
    hist = np.bincount(idx.ravel(), weights=weight.ravel(),
                       minlength=_VIGNETTE_HUE_BINS) / weight.size
    support = np.zeros(_VIGNETTE_HUE_BINS, dtype=np.float64)
    for offset in range(-_VIGNETTE_HUE_SPAN, _VIGNETTE_HUE_SPAN + 1):
        # Triangular window, wrapped: hue is circular, so bin 35 neighbours bin 0.
        support += (1.0 - abs(offset) / (_VIGNETTE_HUE_SPAN + 1)) * np.roll(hist, -offset)
    return a, hue, weight, support


def _vignette_family_rgb(
    a: np.ndarray, hue: np.ndarray, weight: np.ndarray, centre: float
) -> tuple[float, float, float] | None:
    """The art's own colour at hue ``centre``: the weighted mean of the pixels in
    that family, pulled back onto the family's hue.

    Averaging alone drifts the hue toward whatever else is nearby, which is how a
    family's own colour comes back as a slightly different one.  Saturation and
    Value are kept from the art, so the result is still a colour the poster has.
    """
    dh = np.abs(hue - centre)
    dh = np.minimum(dh, 1.0 - dh)
    mask = (dh <= (_VIGNETTE_HUE_SPAN + 0.5) / _VIGNETTE_HUE_BINS) * weight
    total = mask.sum()
    if total <= 0:
        return None
    import colorsys
    mean = [float((a[..., i] * mask).sum() / total) for i in range(3)]
    _h, s, v = colorsys.rgb_to_hsv(*mean)
    return tuple(c * 255.0 for c in colorsys.hsv_to_rgb(centre, s, v))


def _vignette_hue_pick(
    poster: Image.Image, rows: np.ndarray | None = None,
) -> tuple[tuple[float, float, float] | None, float]:
    """(tint colour, confidence) for a region — the hue the most of it is made of.

    ``confidence`` answers "is this colour really what the art is", and it is the
    same number that chose the hue: the support behind the winning family.  A
    poster that is genuinely graded — a cold war photo, a navy Terminator, a teal
    landscape — clears _VIGNETTE_SUPPORT_FULL even when muted, because the cast
    covers it.  A black-and-white one whose only colour is a coat or a face fades
    to black instead, and both a plain grey and a warm off-white read as no colour
    at all.  The tint is never invented, only found.
    """
    a, hue, weight, support = _vignette_hue_profile(poster, rows)
    peak = int(np.argmax(support))
    return (_vignette_family_rgb(a, hue, weight, (peak + 0.5) / _VIGNETTE_HUE_BINS),
            _vignette_hue_confidence(support, peak))


def _vignette_hue_confidence(support: np.ndarray, peak: int) -> float:
    """How far a hue peak is to be trusted: how much of the art carries it, and
    how clearly it beat the best rival far enough away to be a different colour
    rather than its own family's shoulder."""
    conf = (support[peak] - _VIGNETTE_SUPPORT_LOW) / (_VIGNETTE_SUPPORT_FULL - _VIGNETTE_SUPPORT_LOW)
    bins  = np.arange(_VIGNETTE_HUE_BINS)
    apart = np.minimum(np.abs(bins - peak), _VIGNETTE_HUE_BINS - np.abs(bins - peak))
    rival = support[apart >= _VIGNETTE_RIVAL_HUE * _VIGNETTE_HUE_BINS]
    if rival.size and support[peak] > 0:
        margin = 1.0 - rival.max() / support[peak]
        conf = min(conf, margin / (1.0 - _VIGNETTE_RIVAL_CLEAR))
    return float(np.clip(conf, 0.0, 1.0))


def _vignette_dominant_rgb(
    poster: Image.Image,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
    """Whole-poster colour for the vignette tint → (strict_pick, vignette_pick,
    confidence).

    ``strict_pick`` is the ordinary cluster pick the frosted bar / notch / sash
    use, returned so the caller can share it with them rather than quantising
    twice.  The vignette's own pick comes from _vignette_hue_pick instead: a
    frosted element is a small patch that wants to match one prominent colour,
    while a vignette is a wash across the whole width and wants the colour the
    poster is mostly made of.  The two agree on ordinary art and part company
    exactly where they should — on a poster with one vivid accent in a neutral
    field, which the bar may match and the vignette must not.
    """
    rgb, _v, _s, _skin = _dominant_cluster(poster)
    strict = rgb if rgb is not None else (128.0, 128.0, 128.0)
    pick, conf = _vignette_hue_pick(poster)
    return strict, (pick if pick is not None else strict), (conf if pick is not None else 0.0)


def _vignette_secondary_rgb(
    poster: Image.Image, primary: tuple[float, float, float]
) -> tuple[float, float, float] | None:
    """Second colour for the two-tone ramp, or None if the art hasn't got one.

    The best-scoring chromatic cluster whose hue is far enough from ``primary`` to
    actually read as a different colour — a ramp between two shades of one hue is
    just a flat tint with a smudge in it, so it is better to fall back to flat.
    Scored by population, biased toward chroma, so the two ends are the poster's
    two real colours rather than its colour and an incidental highlight.

    Deliberately left on clusters rather than moved onto the hue histogram the
    primary now uses.  Support answers "how much of the poster is this colour",
    which is the right question for the wash the whole band takes but the wrong one
    for its far end: it only ever nominates a *distant* hue, and distant hues blend
    the short way round the wheel, so the ramp sweeps through violets and magentas
    the art hasn't got.  Picking the nearest real cluster instead keeps the far end
    somewhere adjacent, which is what makes the ramp read as depth in the colour
    rather than as a rainbow laid over the poster.

    Skin is excluded outright, and the coverage bar is high.  Both matter: on a
    poster that is a man against a blue sky, his face and hands are the only thing
    far enough from blue to qualify, so without these the ramp announced a second
    colour — red — that is nowhere in the art.
    """
    import colorsys
    small = poster.convert("RGB")
    if max(small.size) > 64:
        small = small.resize((48, 48), Image.Resampling.LANCZOS)
    try:
        q = small.quantize(colors=12, method=Image.Quantize.FASTOCTREE)
    except Exception:
        q = small.quantize(colors=12)
    palette, counts = q.getpalette() or [], q.getcolors() or []
    if not palette or not counts:
        return None
    p_h = colorsys.rgb_to_hsv(*(c / 255 for c in primary))[0]
    total = float(sum(c for c, _ in counts)) or 1.0
    best, best_score = None, -1.0
    for count, idx in counts:
        r, g, b = palette[idx * 3:idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < _VIGNETTE_RAMP_MIN_V or s < _VIGNETTE_RAMP_MIN_S:
            continue
        if v * s < _VIGNETTE_RAMP_MIN_CHROMA:   # a real colour, not compression noise
            continue
        if _is_skin_tone(r, g, b):          # a face is not a poster's second colour
            continue
        weight = count / total
        if weight < _VIGNETTE_RAMP_MIN_W:
            continue
        dh = abs(h - p_h)
        dh = min(dh, 1.0 - dh)                           # hue is circular
        if not _VIGNETTE_RAMP_MIN_HUE <= dh <= _VIGNETTE_RAMP_MAX_HUE:
            continue
        score = weight * (0.3 + s * v)
        if score > best_score:
            best_score, best = score, (float(r), float(g), float(b))
    return best


def _vignette_level_band(
    image: Image.Image, box: tuple[int, int, int, int], ramp: Image.Image, amount: float
) -> None:
    """Darken over-bright artwork inside one vignette band, in place.

    The tint is composited *over* the art at the vignette's alpha, so whatever the
    art does at (1 - alpha) lands in the result untouched.  That is the entire
    reason a poster whose bottom is white cloud reads pale next to one whose bottom
    is dark, at identical settings — the tint contributes the same to both.  This
    scales the bright case down so the bleed-through is comparable, weighted by the
    same alpha ramp so there is no seam, and scaled by ``amount`` — how hard the
    two sliders are asking for a wash at all — so a faint one doesn't aggressively
    regrade the poster.

    ``amount`` deliberately does *not* include the tint's confidence.  Hue
    confidence answers "is this the poster's colour", which has nothing to do with
    how much art should show through; letting it in meant the posters whose hue was
    least trusted — the near-monochrome ones — were also the only ones that kept
    their art legible under the band, which is exactly the inconsistency this
    function exists to remove.

    Only ever darkens: art already below the bleed budget is left exactly alone.
    """
    if amount <= 0:
        return
    x0, y0, x1, y1 = box
    prof = np.asarray(ramp, dtype=np.float32)
    peak = float(prof.max())
    if peak <= 0:
        return
    band = image.crop(box)
    art  = np.asarray(band.convert("RGB"), dtype=np.float32)
    # Alpha-weighted mean luminance of the art, i.e. what actually reaches the eye.
    w = prof / peak
    art_luma = float((art @ _LUMA_COEFFS * w).sum() / max(w.sum(), 1e-6))
    bleed = (1.0 - peak / 255.0) * art_luma
    if bleed <= _VIGNETTE_ART_BLEED:
        return
    k = max(_VIGNETTE_LEVEL_FLOOR, _VIGNETTE_ART_BLEED / bleed)
    k = 1.0 - (1.0 - k) * min(1.0, amount)
    mask = ramp.point(lambda a, _p=peak: min(255, int(a * 255 / _p)))
    # Scale the RGB array rather than Image.point, which on an RGBA band would
    # scale the alpha channel too.
    levelled = Image.fromarray(np.clip(art * k, 0, 255).astype(np.uint8), "RGB")
    image.paste(levelled, (x0, y0), mask=mask)


def _vignette_band_colour(
    poster: Image.Image,
    seam: tuple[tuple[int, int, int, int], np.ndarray],
    whole: tuple[tuple[float, float, float], float, tuple[float, float, float] | None,
                 np.ndarray],
    local: bool,
    want_ramp: bool,
) -> tuple[tuple[float, float, float], float, tuple[float, float, float] | None]:
    """(tint, confidence, secondary) for one vignette band.

    With ``local`` off this is just the whole-poster pick, so both bands agree and
    so does every frosted element.

    With it on there are two candidates and a rule for choosing between them.  The
    seam — the window around the band's inner edge, see _vignette_seam — is where
    the tint and the artwork are seen together, so a band that matches it reads as
    the poster's own colour deepening rather than as a different colour arriving.
    But the seam is a sliver, and a sliver can be unrepresentative: a red coat, a
    lit shoulder, one lamp.  The whole poster is representative by construction and
    can be a colour that is nowhere near the join.  Each is right where the other
    is wrong, and neither is right often enough to use alone.

    So a candidate is scored by the *weaker* of its two supports — how much of the
    join carries that hue, and how much of the poster does — and the better score
    wins.  A colour has to be earned twice.  A prop at the join fails on the
    poster; a poster colour absent from the join fails at the join; a colour that
    is genuinely both is the one the band should be.  The scores are directly
    comparable because both curves are in the same units, mean weighted chroma per
    pixel, and only the two candidates are scored — taking the best hue of the
    combined curve instead would invent a third colour that neither sample chose.

    Confidence comes from that same combined curve, so a band whose colour only
    one side supports fades out rather than committing.

    The winner brings its own ramp partner, from the sample that chose it: pairing
    a seam primary with a secondary from the far end of the poster would ramp
    toward a colour that isn't anywhere near the join.
    """
    if not local:
        return whole[:3]
    box, rows = seam
    region = poster.crop(box)
    _a, _hue, _w, seam_sup = _vignette_hue_profile(region, rows)
    both = np.minimum(seam_sup, whole[3])
    seam_peak, whole_peak = int(np.argmax(seam_sup)), int(np.argmax(whole[3]))
    peak = seam_peak if both[seam_peak] > both[whole_peak] else whole_peak
    conf = _vignette_hue_confidence(both, peak)
    if conf <= 0.0:
        return whole[:3]
    if peak == whole_peak and whole[0] is not None:
        return whole[0], conf, whole[2]
    tint = _vignette_family_rgb(_a, _hue, _w, (peak + 0.5) / _VIGNETTE_HUE_BINS)
    if tint is None:
        return whole[:3]
    return tint, conf, (_vignette_secondary_rgb(region, tint) if want_ramp else None)


def _vignette_hue_gate(field: np.ndarray) -> np.ndarray:
    """0–1 confidence that each cell of ``field`` carries a usable hue.

    Keyed on chroma — HSV Value x Saturation, which reduces to (max - min) / 255 —
    so it rejects white, grey and near-black equally, and accepts a dark but vivid
    hue.  0 means there is nothing there worth tinting from.
    """
    maxc = field.max(axis=-1)
    minc = field.min(axis=-1)
    chroma = np.where(maxc > 0, (maxc - minc) / 255.0, 0.0)
    return np.clip(
        (chroma - _VIGNETTE_HUE_FLOOR) / (_VIGNETTE_HUE_SOLID - _VIGNETTE_HUE_FLOOR), 0.0, 1.0
    )
# Horizontal resolution the band is reduced to at blur=0, before being smoothed
# back up.  High enough that the band visibly follows the art (which is the whole
# point of the low end of the slider), low enough that faces and title text stay
# a colour haze rather than a legible ghost.
_VIGNETTE_TINT_COLUMNS = 64
# Easing exponents for the blur slider.  Both are front-loaded so the flattening
# is obvious within the first half of the travel — at a linear ramp the top end
# was indistinguishable from the bottom, since even a coarse sample is already
# smooth once it has been scaled back up.
_VIGNETTE_BLUR_DETAIL_CURVE = 3.0   # how fast the sampled detail collapses
_VIGNETTE_BLUR_MIX_CURVE    = 0.7   # how fast it commits to the flat dominant colour
# Peak Gaussian radius applied to the artwork inside the band, as a fraction of
# poster width so it is resolution-independent.  Flattening the *tint* alone left
# the art underneath perfectly sharp, which reads as a plain colour cast rather
# than as blur; frosting the art is what actually sells the top of the slider.
# The ceiling is what decides whether a busy poster (a spider's legs, a cartoon
# background) reads as a deliberate wash or as colour sprayed over legible art —
# at 0.09 even the top of the slider left too much of it standing.  Returns
# diminish fast above this: a Gaussian takes away detail but not contrast, so
# doubling the radius again buys a few percent where the bleed budget below buys
# a third.  Frosting the art is what sells the top of the slider; levelling it is
# what makes the top of the slider look the same on every poster.
_VIGNETTE_BLUR_MAX_RATIO = 0.24
# How much of the artwork's own luminance the band tolerates bleeding through the
# vignette, in luma units at peak alpha.  The tint's own contribution is already
# near-constant across posters (~33); what made a bright poster look washed next
# to a dark one was purely this bleed — a poster whose bottom is white cloud sends
# ~30 luma through a "high" vignette, a dark one ~3.  Levelling only ever darkens,
# so dark art is untouched by construction and can never be made worse.
#
# This budget, not the blur radius, is what decides whether a band reads as mist
# or as art seen through a colour cast: blur removes *detail* but keeps contrast,
# and it is surviving contrast that the eye reads as "the background is still
# there".  Doubling the radius barely moves that; halving the budget does.
_VIGNETTE_ART_BLEED  = 3.0
# ...and the floor has to be low enough for the budget to be reachable on bright
# art.  At 0.30 a poster whose band is white paper or cloud hit the floor before
# it hit the budget, so the very posters that showed the most kept showing it.
_VIGNETTE_LEVEL_FLOOR = 0.12   # never darken the art below this fraction
# Columns the two-tone ramp is drawn at.  It needs its own floor because the blur
# slider collapses the sample to a single cell at the top end, which would leave
# the ramp with nowhere to ramp.
_VIGNETTE_RAMP_COLUMNS = 24
# Hue separation (0–1) the ramp's second colour has to sit in.  Below the minimum
# the two ends are shades of one hue and the ramp reads as a flat tint with a
# smudge.  Above the maximum they are too far apart to join: the blend takes the
# short way round the wheel, so a blue reaching for a red goes through violet and
# magenta, and the band ends up mostly made of colours the poster hasn't got.  A
# gold to a green is a gradient; a blue to a red is a rainbow.  Flat is the honest
# fallback for the second case, which is why the window is closed at both ends
# rather than the minimum simply being raised.
_VIGNETTE_RAMP_MIN_HUE = 0.08
_VIGNETTE_RAMP_MAX_HUE = 0.30
# A ramp endpoint must be a real presence in the art, not a passing accent — it
# claims half the band.  A 1% highlight promoted to a co-headline colour is a
# colour the poster does not actually have.
_VIGNETTE_RAMP_MIN_W = 0.06
# ...and bright enough to be a colour statement rather than a shadow: a dark brown
# that is really hair or shading passes a low bar easily and then gets announced
# as half the poster's palette.
_VIGNETTE_RAMP_MIN_V = 0.40
# How chromatic a cluster must be to be a ramp endpoint at all.  Saturation alone
# is close to meaningless on dark clusters — pure black plus a little warm
# compression noise reads S=0.22 while its chroma is 0.03 — so both are checked.
_VIGNETTE_RAMP_MIN_S      = 0.12
_VIGNETTE_RAMP_MIN_CHROMA = 0.10
# Depth of the seam window either side of a band's inner edge, as a fraction of
# the poster.  The window spans both: outside it is the art the band fades into,
# inside it is the art the band has begun to cover but not yet hidden, and the
# join the eye actually sees is made of both.  Thin on purpose — reach further and
# the sample starts answering for content nowhere near the join.
_VIGNETTE_SEAM_H = 0.08


def _vignette_seam(
    width: int, height: int, edge: int, inward: int, ramp: Image.Image
) -> tuple[tuple[int, int, int, int], np.ndarray]:
    """(crop rect, per-row weights) for the seam around a band's inner edge.

    ``edge`` is that edge's y; ``inward`` is +1 when the band lies below it (the
    bottom vignette) and -1 when it lies above (the top).

    Rows are weighted by how much of the artwork still shows at that height — one
    minus the band's own alpha — so the window fades out exactly as the art it is
    reading disappears under the wash.  A flat window would let the first rows
    inside the band vote as loudly as the untouched art beside them at a shallow
    setting and be nearly hidden at a deep one, which is what made the tint jump
    around as the vignette level was changed: the sample was moving over the art
    without any regard for how much of that art would survive.
    """
    depth = max(1, int(height * _VIGNETTE_SEAM_H))
    y0, y1 = max(0, edge - depth), min(height, edge + depth)
    if y1 - y0 < 2:                     # degenerate band: read whatever is there
        y0, y1 = max(0, min(height - 2, y0)), min(height, max(2, y1))
    alpha = np.asarray(ramp, dtype=np.float32)
    alpha = alpha.mean(axis=1) if alpha.ndim > 1 else alpha
    peak  = max(float(alpha.max()), 1.0)
    # The ramp is stored top-down over the band's own rows, so the band starts at
    # the edge going down and ends at it going up.
    start = edge if inward > 0 else edge - len(alpha)
    into  = np.arange(y0, y1) - start
    rows  = 1.0 - np.where(
        (into >= 0) & (into < len(alpha)), alpha[np.clip(into, 0, len(alpha) - 1)], 0.0
    ) / peak
    return (0, y0, width, y1), rows.astype(np.float32)


def _vignette_tint_band(
    src: Image.Image,
    box: tuple[int, int, int, int],
    dominant: tuple[float, float, float],
    confidence: float,
    saturation: float,
    blur: float,
    secondary: tuple[float, float, float] | None = None,
    lightness: float = 1.0,
) -> Image.Image:
    """Colour field to paint one vignette band with, sampled from the poster art.

    ``box`` is the band's crop rect in ``src`` — the artwork snapshot taken
    *before* any gradient darkened it, since sampling the graded image would just
    return the near-black a previous band already painted.

    ``blur`` (0–1) trades local colour for the whole-poster ``dominant``: at 0 the
    band follows the art across its width (a red left edge stays red), at 1 it is
    one flat wash of the dominant colour.  It drives both the coarseness of the
    downsample and the mix toward the dominant, each on its own front-loaded curve
    (see _VIGNETTE_BLUR_*_CURVE) so the two ends read as clearly different.  The
    same slider also frosts the art itself — see _vignette_frost_band, which is
    what makes the high end read as blur rather than as a flat colour cast.

    ``confidence`` (0–1, from _vignette_hue_pick) is how much of the art actually
    carries the chosen hue.  It is the only thing besides the slider allowed to
    affect strength, and it is judged once for the whole region.

    ``saturation`` (0–_VIGNETTE_SAT_FULL) sets the tint's strength on its own; the
    art supplies only the hue.  Two posters at the same setting therefore land on
    the same intensity however saturated their art is, differing in hue alone —
    without that, a shelf of posters is wildly uneven, since a vivid one earns
    both more chroma and more Value while a muted one is scaled down twice over.
    0 is exactly black, i.e. the untinted vignette.  Strength moves Value and
    Saturation together, so the band always darkens the poster as hard as a black
    vignette would: the slider changes how much hue shows, never how much light
    the vignette takes away.

    ``lightness`` is the one control that does move it, scaling the tint's Value
    alone — 1.0 is the tuned default, below it the band tends to black and above
    it to an airy wash of the same hue.  It cannot lighten a band that has no
    colour: Value is still multiplied by strength, so saturation 0 stays exactly
    black however light this is set, and the guarantee that a colourless vignette
    is pixel-identical to the untinted one survives.
    """
    band = src.crop(box).convert("RGB")
    bw, bh = band.size
    if bw <= 0 or bh <= 0:
        return Image.new("RGB", (max(bw, 1), max(bh, 1)), (0, 0, 0))

    # Downsample to a handful of colour cells (BOX = area average, so every pixel
    # contributes), then let the upscale do the smoothing — far cheaper than a
    # Gaussian over the full-size band and indistinguishable at this softness.
    detail = (1.0 - blur) ** _VIGNETTE_BLUR_DETAIL_CURVE
    cols   = max(1, min(bw, int(round(_VIGNETTE_TINT_COLUMNS * detail))))
    if secondary is not None:
        # The ramp needs columns to ramp across, and blur has just taken them away
        # at the top of its range — give it back a floor of its own.
        cols = max(1, min(bw, max(cols, _VIGNETTE_RAMP_COLUMNS)))
    rows   = max(1, min(bh, max(1, cols // 2)))
    field  = np.asarray(band.resize((cols, rows), Image.Resampling.BOX), dtype=np.float32)
    if secondary is None:
        dom = np.asarray(dominant, dtype=np.float32)
    else:
        # Two-tone: the poster's two real colours, ramped left to right across the
        # band, shaped (1, cols, 3) to broadcast over the rows.
        #
        # Interpolated along the *hue arc*, not through RGB. A straight RGB lerp
        # between distant hues passes through desaturated mud — blue to red goes via
        # grey, which reads as two flat zones butted together rather than a blend.
        # Walking the short way round the hue wheel keeps every intermediate fully
        # saturated, so blue to red travels through purple as a gradient should.
        import colorsys
        h1, s1, v1 = colorsys.rgb_to_hsv(*(c / 255.0 for c in dominant))
        h2, s2, v2 = colorsys.rgb_to_hsv(*(c / 255.0 for c in secondary))
        dh = (h2 - h1 + 0.5) % 1.0 - 0.5          # shortest way round the wheel
        dom = np.asarray(
            [
                [c * 255.0 for c in colorsys.hsv_to_rgb(
                    (h1 + dh * t) % 1.0, s1 + (s2 - s1) * t, v1 + (v2 - v1) * t)]
                for t in np.linspace(0.0, 1.0, cols)
            ],
            dtype=np.float32,
        )[None, :, :]
    if blur > 0:
        mix   = blur ** _VIGNETTE_BLUR_MIX_CURVE
        field = field * (1.0 - mix) + dom * mix

    # A cell with no colour of its own borrows the poster's rather than dropping to
    # black.  White and black are the two things local sampling handles worst — a
    # snowfield or a shadow has no hue to offer, and leaving those cells black made
    # whole bands of monochrome posters look untinted.  Borrowing keeps the band
    # coloured wherever the poster has any colour at all; if the poster has none,
    # `dominant` is neutral too and the gate below still takes the band to black.
    borrow = (1.0 - _vignette_hue_gate(field))[..., None]
    field  = field * (1.0 - borrow) + dom * borrow

    # Per-cell HSV, vectorised.  `full` is the cell's hue at full saturation and
    # value — i.e. hsv_to_rgb(h, 1, 1) — which lets the tint be rebuilt with the
    # identity hsv_to_rgb(h, s, v) == v * (1 - s * (1 - full)), no colorsys loop.
    minc  = field.min(axis=-1)
    chrom = np.maximum(field.max(axis=-1) - minc, 1e-6)
    full  = (field - minc[..., None]) / chrom[..., None]

    # Strength comes from the slider and the poster-level `confidence` only — never
    # from how chromatic this particular cell happens to be.  Cells vary in hue
    # across the band; they must not vary in intensity, or the band ends up
    # brighter over the colourful half of the art than the muted half.
    strength = min(1.0, max(0.0, saturation) / _VIGNETTE_SAT_FULL) * max(0.0, min(1.0, confidence))

    # Equalise across hues as well as across sources: scale each cell's Value by
    # how far its hue's own luminance sits from the reference, so a yellow band
    # and a blue band at the same setting land near the same brightness.  A hue
    # brighter than the reference gives part of that back as chroma instead of
    # Value (see _VIGNETTE_LUMA_SAT_SHARE), because a gold darkened far enough to
    # match a navy is no longer gold, it is mud.
    hue_luma  = np.maximum(full @ _LUMA_COEFFS, 1e-6)
    hue_ratio = _VIGNETTE_LUMA_REF / hue_luma
    v_scale   = hue_ratio ** _VIGNETTE_LUMA_CORRECT
    s_scale   = np.minimum(1.0, hue_ratio) ** _VIGNETTE_LUMA_SAT_SHARE
    s_eff = _VIGNETTE_TINT_S * strength * s_scale
    v_eff = np.minimum(
        1.0,
        _VIGNETTE_TINT_V * strength * v_scale
        * min(_VIGNETTE_LIGHT_MAX, max(_VIGNETTE_LIGHT_MIN, lightness)),
    )
    # Both stay per-cell: strength is poster-level, but the hue correction varies
    # with each cell's own hue.
    tint  = 255.0 * v_eff[..., None] * (1.0 - s_eff[..., None] * (1.0 - full))

    # Clip the result to a perceptual chroma budget, per cell, by pulling it toward
    # its own luminance — which holds the hue and the brightness and takes only the
    # colourfulness away.  A clip, not an equalisation: a hue already inside the
    # budget is left exactly alone, so the quiet posters stay exactly as quiet and
    # only the ones taking their poster over come down.  Two-tone is clipped end by
    # end for the same reason, since it is usually one end that goes too far.
    ceiling = _VIGNETTE_TINT_MAX_CHROMA * strength
    keep    = np.minimum(1.0, ceiling / np.maximum(_lab_chroma(tint), 1e-6))[..., None]
    grey    = (tint @ _LUMA_COEFFS)[..., None]
    tint    = grey + (tint - grey) * keep

    small = Image.fromarray(np.clip(tint, 0, 255).astype(np.uint8), mode="RGB")
    return small if (cols, rows) == (bw, bh) else small.resize((bw, bh), Image.Resampling.BICUBIC)


def _vignette_frost_band(
    image: Image.Image, box: tuple[int, int, int, int], ramp: Image.Image, blur: float
) -> None:
    """Blur the artwork inside one vignette band, in place, before it is tinted.

    ``ramp`` is the band's own alpha gradient.  Reusing it as the paste mask makes
    the blur strongest exactly where the darkening is and zero where the band
    fades out, so the frosted area has no visible seam against the sharp art below
    it.  The ramp is normalised first, so the blur reaches full strength at the
    poster's edge whatever vignette level is set — the two sliders stay
    independent rather than "low vignette" quietly capping the blur.

    Runs before the tint is composited so the tint lands on frosted art, and
    before every badge, logo, label and sash, none of which should be blurred.
    """
    if blur <= 0:
        return
    x0, y0, x1, y1 = box
    radius = (x1 - x0) * _VIGNETTE_BLUR_MAX_RATIO * blur
    if radius < 0.5:
        return
    peak = ramp.getextrema()[1]
    if not peak:
        return
    mask = ramp.point(lambda a, _p=peak: min(255, int(a * 255 / _p)))

    # Blur by reduction rather than running a wide Gaussian at full size: a box
    # downscale, a small Gaussian, then a bicubic upscale is indistinguishable at
    # these radii (max channel delta ~4/255) and roughly halves the cost at the
    # default blur.  PIL's Gaussian is a box approximation whose cost barely moves
    # with radius, so below a 3x reduction the resizes cost more than they save —
    # hence the threshold rather than always taking this path.
    band   = image.crop(box)
    shrink = max(1, int(radius / 4))
    if shrink > 2:
        small   = band.resize((max(1, band.width // shrink), max(1, band.height // shrink)),
                              Image.Resampling.BOX)
        small   = small.filter(ImageFilter.GaussianBlur(radius / shrink))
        blurred = small.resize(band.size, Image.Resampling.BICUBIC)
    else:
        blurred = band.filter(ImageFilter.GaussianBlur(radius))
    image.paste(blurred, (x0, y0), mask=mask)


# Genre-specific tint multipliers (R, G, B) for the fallback canvas.
# Applied to a dark base luminance of 10–18, so the dominant channel peaks
# around 30–55 at canvas midpoint — atmospheric rather than vivid.
# Names must match GENRE_MAP values exactly.
_GENRE_TINT: dict[str, tuple[float, float, float]] = {
    "Horror":      (3.2, 0.3, 0.3),   # deep blood red
    "Thriller":    (0.4, 2.2, 0.5),   # dark hunter green
    "Mystery":     (1.0, 0.3, 3.0),   # deep indigo
    "Sci-Fi":      (0.3, 1.2, 3.2),   # cold cyan-blue
    "Fantasy":     (1.6, 0.3, 3.0),   # purple-violet
    "Action":      (3.0, 0.8, 0.3),   # orange-red
    "Adventure":   (2.6, 1.5, 0.3),   # warm amber
    "Animation":   (0.4, 0.8, 3.2),   # electric blue
    "Comedy":      (2.6, 2.4, 0.3),   # golden yellow
    "Crime":       (2.4, 0.2, 0.2),   # dark crimson
    "Documentary": (0.3, 2.2, 2.4),   # teal
    "Drama":       (0.3, 0.3, 2.6),   # deep blue
    "Family":      (2.6, 1.2, 0.3),   # warm orange
    "History":     (2.2, 1.1, 0.3),   # sepia
    "Music":       (2.8, 0.3, 2.2),   # magenta
    "Romance":     (3.0, 0.3, 0.9),   # rose
    "War":         (0.9, 1.6, 0.3),   # olive green
    "Western":     (2.8, 1.1, 0.2),   # burnt sienna
    "Kids":        (0.3, 1.1, 3.0),   # bright blue
    "Reality":     (2.4, 0.8, 0.3),   # orange
    "Soap":        (2.6, 0.3, 0.9),   # rose-pink
    "Talk":        (0.3, 1.6, 2.4),   # teal-blue
    "News":        (0.3, 0.5, 2.6),   # steel blue
}
_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.4)   # neutral cool blue

# Display-only label shortenings.  Some genre names are too wide for the poster
# label strip; shortening them reads better than shrinking the font.  These map
# the genre name to its *printed* form only — the original genre key is still
# used for font / colour / background lookups.
_GENRE_LABEL_OVERRIDES: dict[str, str] = {
    "Documentary": "Doc",
}


def _make_landscape_canvas(genre_ids: list[int] | None = None) -> Image.Image:
    """The no-art canvas at 16:9.  Same genre tint and gradient as the portrait
    one — only the canvas it is painted on differs."""
    return _make_fallback_canvas(genre_ids,
                                 size=(_cfg.LANDSCAPE_WIDTH, _cfg.LANDSCAPE_HEIGHT))


def _make_fallback_canvas(genre_ids: list[int] | None = None,
                          size: tuple[int, int] | None = None) -> Image.Image:
    """
    Dark gradient canvas served when a title has no poster art on TMDB.

    Applies a genre-derived colour tint so the canvas feels atmospheric rather
    than generically dark.  The base luminance is 10–18 (very dark) so even the
    dominant channel stays below ~55 — readable against white text overlays.
    """
    # Resolve genre → tint by walking GENRE_PRIORITY so higher-priority genres
    # win when a title belongs to multiple genres (same order as the score label).
    tint = _FALLBACK_DEFAULT_TINT
    if genre_ids:
        gid_set = set(genre_ids)
        for gid in _cfg.GENRE_PRIORITY:
            if gid in gid_set:
                name = _cfg.GENRE_MAP.get(gid)
                if name and name in _GENRE_TINT:
                    tint = _GENRE_TINT[name]
                    break

    r_mult, g_mult, b_mult = tint
    W, H = size or (_cfg.POSTER_WIDTH, _cfg.POSTER_HEIGHT)
    t    = np.linspace(0, np.pi, H, dtype=np.float32)
    # sin curve: peaks at midheight (~18), dark at top/bottom (~10)
    v    = (10 + 8 * np.sin(t)).astype(np.float32)
    arr  = np.zeros((H, W, 4), dtype=np.uint8)
    # Clamp BEFORE casting to uint8 — casting first would wrap mod-256 on
    # any value above 255, silently inverting colour for high-multiplier tints.
    arr[:, :, 0] = np.minimum(255, v * r_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 1] = np.minimum(255, v * g_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 2] = np.minimum(255, v * b_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 3] = 255
    return Image.fromarray(arr, "RGBA")


def _draw_combined_text_badge(
    image: Image.Image,
    tokens: list[str],
    *,
    x: int,
    y: int,
    font_size: int,
    min_score: int = 2,
    stacked: bool = False,
) -> None:
    """Minimalist quality badge: Resolution [sep] Visual Tag

    Horizontal layout: "4K  |  HDR"  — vertical pip coloured by source.
    Stacked layout:    "4K / HDR"    — horizontal rule coloured by source,
                       stacked like a division formula (for tight notch space).

    The separator colour encodes the source — gold for Remux, silver for Web.
    Nothing is drawn if resolution or source tokens are absent, or if the
    combined quality score is below *min_score*.
    """
    token_set = set(tokens)

    if tokens and _score_points(tokens) < min_score:
        return

    if "4K" in token_set:
        res = "4K"
    elif "1080P" in token_set:
        res = "HD"
    else:
        return

    if "REMUX" in token_set:
        sep_color = (255, 210,  60)   # gold
    elif "WEBDL" in token_set:
        sep_color = (192, 192, 200)   # silver
    else:
        return

    if "DV" in token_set:
        fmt = "DV"
    elif "HDR10+" in token_set:
        fmt = "HDR+"
    elif "HDR10" in token_set:
        fmt = "HDR"
    else:
        fmt = "SDR"

    try:
        font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
    except IOError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(image)
    ink  = (235, 235, 235, 255)

    if stacked:
        # Use textbbox so spacing is based on actual rendered glyph bounds,
        # not the full em-square (which includes invisible descender space and
        # would pin the line visually against the resolution text).
        b_res   = draw.textbbox((0, 0), res, font=font)
        b_fmt   = draw.textbbox((0, 0), fmt, font=font)
        w_res   = b_res[2] - b_res[0]
        w_fmt   = b_fmt[2] - b_fmt[0]
        h_res   = b_res[3] - b_res[1]   # actual glyph height, no dead space
        total_w = max(w_res, w_fmt)
        line_h  = max(2, font_size // 12)
        v_gap   = max(4, font_size // 5)

        # Resolution — draw so its visual top sits at y
        res_x = x + (total_w - w_res) // 2 - b_res[0]
        res_y = y - b_res[1]
        draw.text((res_x, res_y), res, font=font, fill=ink)

        # Horizontal rule — v_gap below the actual glyph bottom
        ly = y + h_res + v_gap
        draw.rounded_rectangle(
            [x, ly, x + total_w, ly + line_h],
            radius=line_h // 2,
            fill=sep_color,
        )

        # Visual tag — v_gap below the rule, aligned to its own visual top
        fmt_x = x + (total_w - w_fmt) // 2 - b_fmt[0]
        fmt_y = ly + line_h + v_gap - b_fmt[1]
        draw.text((fmt_x, fmt_y), fmt, font=font, fill=ink)

    else:
        pip_gap = int(font_size * 0.55)
        pip_w   = max(3, int(font_size * 0.15))
        pip_h   = int(font_size * 1.3)
        pip_cy  = y + round(font_size * 0.60)

        cx = x
        draw.text((cx, y), res, font=font, fill=ink)
        cx += round(draw.textlength(res, font=font)) + pip_gap
        _draw_solid_pip(image, x=cx, y_center=pip_cy, width=pip_w, height=pip_h, color=sep_color)
        cx += pip_w + pip_gap
        draw.text((cx, y), fmt, font=font, fill=ink)


def build_poster(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg: RequestConfig,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta: DiscoveryMeta | None = None,
    quality_tokens: list[str] | None = None,
    release_year: str | None = None,
    age_rating: int | None = None,
    no_poster: bool = False,
    has_burned_in_text: bool = False,
) -> Image.Image:

    width, height = image.size

    # Greyscale the base art to flag "not available".  Overlays drawn afterwards
    # (sashes, badges, ratings, logo) stay in colour.  Two independent triggers:
    #   - cinema_greyscale: title still in cinemas / production (release_status,
    #     so implicitly gated on the release-status sash being enabled).
    #   - greyscale_no_quality: no stream quality was found.  Only meaningful
    #     when wait_for_quality is on (otherwise tokens may just not be fetched
    #     yet), so it's gated on it.
    _cinema_grey = (cfg.cinema_greyscale and discovery_meta is not None
                    and discovery_meta.release_status in ("Cinema", "Production"))
    # Override: if a real digital source (Web / Remux) was found, the title is
    # actually available — keep it in colour despite the cinema/production status.
    if (_cinema_grey and cfg.cinema_greyscale_skip_if_available and quality_tokens
            and any(t in ("WEBDL", "REMUX") for t in quality_tokens)):
        _cinema_grey = False
    _noquality_grey = (cfg.greyscale_no_quality and cfg.wait_for_quality and not quality_tokens)
    if _cinema_grey or _noquality_grey:
        image = ImageOps.grayscale(image).convert("RGBA")

    draw = ImageDraw.Draw(image)

    # Printed form of the genre.  Translate the canonical English name when a
    # translation exists for the request language; otherwise keep the English
    # path including the space-saving override (e.g. "Documentary" → "Doc").
    _genre_tr = translate_genre(genre, cfg.logo_language)
    if _genre_tr != genre:
        genre_label = _genre_tr
    else:
        genre_label = _GENRE_LABEL_OVERRIDES.get(genre, genre)

    if cfg.hide_genre:
        genre_label = ""
        
    # Resolve the info-sash pick once, regardless of whether the diagonal sash
    # itself is rendered independently.
    #
    # When greyscale is active on an unreleased title (Cinema / Production),
    # force the release-status slot to the front so its badge always wins — that
    # tells the user the poster is greyscale because it's unavailable, rather
    # than a title whose art happens to be black & white.
    _sash_priority = cfg.sash_priority
    if (cfg.cinema_greyscale and discovery_meta is not None
            and discovery_meta.release_status in ("Cinema", "Production")):
        _status = discovery_meta.release_status.lower()
        if _status in _sash_priority or "release_status" in _sash_priority:
            _sash_priority = [s for s in _sash_priority if s in ("release_status", _status)] + [s for s in _sash_priority if s not in ("release_status", _status)]
    sash_result = (
        pick_sash(discovery_meta, _sash_priority)
        if discovery_meta is not None
        else None
    )
    # Resolved here rather than at the draw site because the top vignette needs it
    # too — see the top gradient below.
    _sash_shown = cfg.sash_mode != "hidden" and sash_result is not None

    # Snapshot the artwork *before* the vignette gradients darken it. The frosted
    # bar/notch/sash sample their tint colour from this, not the graded image —
    # otherwise the near-black top/bottom the gradients paint on drags the sampled
    # colour to grey (e.g. a blue sky reads as white behind the notch).
    _frost_color_src = image.copy()

    # Whole-poster colour sample.  The strict pick is shared with every frosted
    # element further down so they agree without re-quantising; the vignette's own
    # pick differs only on art where the strict one found no colour at all.
    _strict_tint: tuple[float, float, float] | None = None
    _poster_tint: tuple[float, float, float] | None = None
    _poster_conf = 0.0
    _poster_tint2: tuple[float, float, float] | None = None
    _poster_support = np.zeros(_VIGNETTE_HUE_BINS)
    # Skipped entirely on burned-in-text posters: neither band will be tinted
    # (see _top_enabled below), so the hue histogram would be thrown away.
    if ((cfg.vignette_poster_color_top or cfg.vignette_poster_color_bottom)
            and not has_burned_in_text):
        _strict_tint, _poster_tint, _poster_conf = _vignette_dominant_rgb(_frost_color_src)
        if cfg.vignette_color_ramp and _poster_conf > 0:
            _poster_tint2 = _vignette_secondary_rgb(_frost_color_src, _poster_tint)
        # Kept whole so a band can weigh its own seam's colour against how much of
        # the poster carries it — see _vignette_band_colour.
        _poster_support = _vignette_hue_profile(_frost_color_src)[3]
    # Whole-poster result, used directly unless a band finds something its seam and
    # the poster agree on better.
    _whole_colour = (_poster_tint, _poster_conf, _poster_tint2, _poster_support)
    _slider_amount = min(1.0, max(0.0, cfg.vignette_color_saturation) / _VIGNETTE_SAT_FULL)
    # How hard the band is being asked to wash the art out, for the levelling pass.
    # Both sliders ask for it — colour lays a tint over the art, blur melts it — so
    # the stronger of the two drives it, and a poster is levelled the same whether
    # the wash it is under is a vivid tint or a heavy frost.
    _level_amount  = max(_slider_amount, min(1.0, max(0.0, cfg.vignette_color_blur)))

    # A sash or notch sits on top of the top vignette, and tinting that band lifts
    # it toward the sash's own colour — which is sampled from the same art, so the
    # two converge and the label stops reading. The top band therefore stays plain
    # black whenever one is shown; the bottom band is unaffected. Same reasoning as
    # the existing "Vignette Only On Sash" option, which also lets the sash decide
    # what the top of the poster does.
    #
    # Burned-in text is the other case that has to opt out. The tinted vignette
    # frosts the art it sits on (see _vignette_frost_band), and it knows to leave
    # OUR logo alone because we composite that afterwards — but a poster whose
    # title is baked into the artwork has no separate layer to protect, so the
    # blur lands squarely on the title and smears it into an unreadable mush.
    # Text detection has already told us which posters those are, so when it
    # confirms burned-in text both bands fall back to plain black. Only a
    # confirmed detection counts: has_burned_in_text is False both for a clean
    # poster and for one that was never scanned, and an unscanned poster should
    # keep the tint it has always had rather than be penalised for the gap.
    _top_enabled    = (cfg.vignette_poster_color_top and not _sash_shown
                       and not has_burned_in_text)
    _bottom_enabled = cfg.vignette_poster_color_bottom and not has_burned_in_text
    # What colour a tinted band actually *paints*, kept for the frosted notch to
    # match if it is asked to (see _frost_tint below).  Read off the band's own
    # tint field at its deepest row rather than taken from the sample the hue was
    # picked from: those two share a hue and nothing else.  The sample is the art's
    # own colour — a bright sky blue at V 0.88 — while the band lays down that hue
    # at the tint's Value and Saturation, which at any setting is a dark shade of
    # it, and lower saturation makes it darker still.  Matching the sample gave a
    # notch far brighter and more colourful than the band beside it.  The top
    # band's is preferred when both are tinted, since that is the one a notch sits
    # on.
    _vignette_shown: tuple[float, float, float] | None = None

    def _band_paint(field: Image.Image, deepest_row: int) -> tuple[float, float, float]:
        """Mean colour along a tint field's peak-alpha edge — what the eye sees
        where the band is strongest, before the art bleeding through lightens it."""
        row = np.asarray(field.convert("RGB"), dtype=np.float32)[deepest_row]
        return tuple(float(c) for c in row.mean(axis=0))

    # --- TOP GRADIENT (vectorised) ---
    # Darkens the top of the poster so the age-rating numeral and quality
    # badges stay legible over bright art.  Strength is one of four presets
    # (off / low / medium / high) — see _TOP_GRADIENT_LEVELS for the
    # (height_ratio, max_alpha) tuple each level uses.  Unknown level is
    # treated as "high" rather than skipped so a typo in a URL doesn't
    # silently disable the vignette.
    _tg_preset: tuple[float, int] | None
    if cfg.top_gradient == "custom" and cfg.top_gradient_opacity is not None and cfg.top_gradient_height is not None:
        _tg_preset = (cfg.top_gradient_height, int(cfg.top_gradient_opacity * 255 if cfg.top_gradient_opacity <= 1.0 else cfg.top_gradient_opacity))
    else:
        _tg_preset = _TOP_GRADIENT_LEVELS.get(cfg.top_gradient, _TOP_GRADIENT_LEVELS["high"])
        
    if _tg_preset is not None and (not cfg.top_vignette_sash_only or sash_result is not None):
        top_height_ratio, top_max_alpha = _tg_preset
        top_height = max(1, int(height * top_height_ratio))
        t_top = np.linspace(0, 1, top_height, dtype=np.float32)
        eased_top = ((1 - t_top) * top_max_alpha).astype(np.uint8)
        top_array = np.broadcast_to(eased_top[:, np.newaxis], (top_height, width)).copy()
        top_overlay = Image.fromarray(top_array, mode="L")
        # Black by default; a poster-coloured vignette swaps in a tint field
        # sampled from the art under this band.  Only the RGB changes — the alpha
        # ramp, and so how hard the band darkens, is identical either way.
        if _top_enabled and _poster_tint is not None:
            # The top band fades out downward, so its seam sits at top_height.
            _t_tint, _t_conf, _t_second = _vignette_band_colour(
                _frost_color_src,
                _vignette_seam(width, height, top_height, -1, top_overlay),
                _whole_colour, cfg.vignette_color_local, cfg.vignette_color_ramp,
            )
            _vignette_frost_band(
                image, (0, 0, width, top_height), top_overlay, cfg.vignette_color_blur,
            )
            _vignette_level_band(
                image, (0, 0, width, top_height), top_overlay, _level_amount
            )
            top_tinted = _vignette_tint_band(
                _frost_color_src, (0, 0, width, top_height), _t_tint, _t_conf,
                cfg.vignette_color_saturation, cfg.vignette_color_blur, _t_second,
                cfg.vignette_color_lightness,
            ).convert("RGBA")
            if _t_conf >= _VIGNETTE_MATCH_MIN_CONF and _slider_amount > 0:
                # Top band: strongest at the poster's edge, so row 0.
                _vignette_shown = _band_paint(top_tinted, 0)
        else:
            top_tinted = Image.new("RGBA", (width, top_height), (0, 0, 0, 0))
        top_tinted.putalpha(top_overlay)
        image.paste(top_tinted, (0, 0), mask=top_tinted)

    # --- BOTTOM GRADIENT (vectorised) ---
    # Strength is one of four presets (off / low / medium / high) — see
    # _BOTTOM_GRADIENT_LEVELS for the (height_ratio, max_alpha) tuple each
    # level uses.  The previous auto-softening for Minimalist / Compact modes
    # is dropped now that the user can pick the level themselves; if you'd
    # like the lighter fade those modes used to get for free, pick "medium".
    # Unknown level falls back to "high" so a typo can't accidentally turn
    # the fade off entirely (which would break label legibility).
    if cfg.bottom_gradient == "custom" and cfg.bottom_gradient_opacity is not None and cfg.bottom_gradient_height is not None:
        _bg_preset = (cfg.bottom_gradient_height, int(cfg.bottom_gradient_opacity * 255 if cfg.bottom_gradient_opacity <= 1.0 else cfg.bottom_gradient_opacity))
    else:
        _bg_preset = _BOTTOM_GRADIENT_LEVELS.get(cfg.bottom_gradient, _BOTTOM_GRADIENT_LEVELS["high"])
        
    if _bg_preset is not None:
        bottom_height_ratio, bottom_max_alpha = _bg_preset
        bottom_height = max(1, int(height * bottom_height_ratio))
        bottom_start  = height - bottom_height
        t_bot         = np.linspace(0, 1, bottom_height, dtype=np.float32)
        eased_bot     = ((1 - (1 - t_bot) ** _BOTTOM_GRADIENT_CURVE) * bottom_max_alpha).astype(np.uint8)
        bottom_array  = np.broadcast_to(eased_bot[:, np.newaxis], (bottom_height, width)).copy()
        bottom_overlay = Image.fromarray(bottom_array, mode="L")
        if _bottom_enabled and _poster_tint is not None:
            # ...and the bottom band fades out upward, so its seam sits at
            # bottom_start.
            _b_tint, _b_conf, _b_second = _vignette_band_colour(
                _frost_color_src,
                _vignette_seam(width, height, bottom_start, +1, bottom_overlay),
                _whole_colour, cfg.vignette_color_local, cfg.vignette_color_ramp,
            )
            _vignette_frost_band(
                image, (0, bottom_start, width, height), bottom_overlay, cfg.vignette_color_blur,
            )
            _vignette_level_band(
                image, (0, bottom_start, width, height), bottom_overlay, _level_amount
            )
            bottom_tinted = _vignette_tint_band(
                _frost_color_src, (0, bottom_start, width, height), _b_tint, _b_conf,
                cfg.vignette_color_saturation, cfg.vignette_color_blur, _b_second,
                cfg.vignette_color_lightness,
            ).convert("RGBA")
            if _vignette_shown is None and _b_conf >= _VIGNETTE_MATCH_MIN_CONF and _slider_amount > 0:
                # Bottom band: strongest at the poster's edge, so the last row.
                _vignette_shown = _band_paint(bottom_tinted, -1)
        else:
            bottom_tinted = Image.new("RGBA", (width, bottom_height), (0, 0, 0, 0))
        bottom_tinted.putalpha(bottom_overlay)
        image.paste(bottom_tinted, (0, bottom_start), mask=bottom_tinted)

    # --- Badge / quality overlay ---
    mode   = cfg.badge_display_mode
    tokens = quality_tokens or []

    if mode == 1:
        # If quality is below the threshold, strip the quality tokens so the
        # badge renders silver/default rather than a misleadingly coloured tier.
        _tokens_1 = (
            tokens
            if (not tokens or _score_points(tokens) >= cfg.badge_min_score)
            else []
        )
        draw_quality_age_badge(
            image,
            age_rating,
            _tokens_1,
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
        )

    elif mode == 3:
        # Age rating only — always silver, no quality dependency
        draw_quality_age_badge(
            image,
            age_rating,
            [],
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
            always_silver=True,
        )

    elif mode == 4:
        # Accent bar — small vertical pill in tier colour, no text
        if not tokens or _score_points(tokens) >= cfg.badge_min_score:
            draw_tier_bar(
                image,
                tokens,
                anchor_x_ratio=cfg.badge_anchor_x,
                anchor_y_ratio=cfg.badge_anchor_y,
                bar_height=cfg.badge_height,
            )

    elif mode == 6:
        # Corner bookmark — fixed to the poster top-left and coloured by tier.
        if not tokens or _score_points(tokens) >= cfg.badge_min_score:
            draw_quality_corner_bookmark(
                image,
                tokens,
                bookmark_size=cfg.badge_height,
            )

    elif mode == 2:
        allowed_tokens  = {"4K", "1080P", "REMUX", "WEBDL", "DV", "HDR10+", "HDR10"}
        filtered_tokens = [t for t in tokens if t in allowed_tokens]

        if filtered_tokens and _score_points(tokens) >= cfg.badge_min_score:
            bx = int(width  * cfg.badge_anchor_x)
            by = int(height * cfg.badge_anchor_y)

            badge_items: list[BadgeItem] = [
                (get_resized_badge(token, cfg.badge_height), _cfg.QUALITY_LABELS.get(token, token))
                for token in filtered_tokens
            ]

            render_badges_left(
                image, badge_items,
                x_start=bx, y_top=by,
                badge_height=cfg.badge_height,
                badge_gap=cfg.badge_gap,
            )

    elif mode == 5:
        _draw_combined_text_badge(
            image, tokens,
            x=int(width  * cfg.badge_anchor_x),
            y=int(height * cfg.badge_anchor_y),
            font_size=cfg.badge_height,
            min_score=cfg.badge_min_score,
            stacked=cfg.combined_badge_stacked,
        )

    # --- Logo / fallback title ---
    if logo:
        composite_logo(
            image, logo,
            max_w_ratio=cfg.logo_max_w_ratio,
            max_h_ratio=cfg.logo_max_h_ratio,
            bottom_ratio=cfg.logo_bottom_ratio,
            bottom_anchor=cfg.logo_bottom_anchor,
        )
    elif fallback_title:
        # ── Genre-aware font selection ────────────────────────────────────────
        # Titles are bucketed by genre and rendered in a thematically matching
        # font so different content categories feel distinct.
        #
        # Bucket → font mapping:
        #   Horror / Thriller / Mystery  → Creepster  (gothic, unsettling)
        #   Action / Sci-Fi / Adventure  → Bebas Neue (bold, cinematic)
        #   Comedy / Animation / Family  → Pacifico   (friendly, rounded)
        #   Drama / Romance / History    → Playfair   (elegant, literary)
        #   Crime / War / Documentary    → Oswald     (authoritative, strong)
        #   Default                      → NotoSerif  (neutral, readable)
        _GENRE_FONTS: dict[str, str] = {
            "Horror":           "Creepster-Regular.ttf",
            "Thriller":         "Creepster-Regular.ttf",
            "Mystery":          "Creepster-Regular.ttf",
            "Action":           "BebasNeue-Bold.ttf",
            "Sci-Fi":           "BebasNeue-Bold.ttf",
            "Adventure":        "BebasNeue-Bold.ttf",
            "Fantasy":          "BebasNeue-Bold.ttf",
            "Western":          "BebasNeue-Bold.ttf",
            "Comedy":           "Pacifico-Regular.ttf",
            "Animation":        "Pacifico-Regular.ttf",
            "Family":           "Pacifico-Regular.ttf",
            "Drama":            "PlayfairDisplay-Bold.ttf",
            "Romance":          "PlayfairDisplay-Bold.ttf",
            "History":          "PlayfairDisplay-Bold.ttf",
            "Music":            "PlayfairDisplay-Bold.ttf",
            "Crime":            "Oswald-Bold.ttf",
            "War":              "Oswald-Bold.ttf",
            "Documentary":      "Oswald-Bold.ttf",
        }
        _font_file = _GENRE_FONTS.get(genre, "NotoSerif-Bold.ttf")

        # Fallback-title rendering, sized to fill the SAME envelope a logo fills
        # (cfg.logo_max_w_ratio width × logo_max_h_ratio height) so a text title
        # looks as substantial as a logo would — short titles like "SELF-HELP"
        # grow to fill the width instead of being pinned tiny by a char-count
        # heuristic.  The logo size ratios therefore tune the fallback text too.
        max_w          = max(1, int(width * cfg.logo_max_w_ratio))
        max_h          = max(1, min(int(height * cfg.logo_max_h_ratio), LOGO_ABS_MAX_H))
        MIN_FONT_SIZE  = 22
        MAX_LINES      = 2
        FONT_PATH      = os.path.join(_FONTS_DIR, _font_file)

        def _line_width(text: str, current_font) -> int:
            bbox = draw.textbbox((0, 0), text, font=current_font)
            return int(bbox[2] - bbox[0])

        def _wrap_lines(text: str, current_font) -> list[str]:
            """Greedy word-wrap: each line packs as many words as fit within max_w."""
            words = text.split()
            if not words:
                return []
            lines: list[str] = []
            current: list[str] = []
            for word in words:
                candidate = " ".join(current + [word])
                if _line_width(candidate, current_font) <= max_w or not current:
                    current.append(word)
                else:
                    lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
            return lines

        def _measure_block(lines_to_measure: list[str], current_font, line_gap: int) -> tuple[int, int, list[tuple[str, tuple[int, int, int, int]]]]:
            line_boxes = [
                (line, draw.textbbox((0, 0), line, font=current_font))
                for line in lines_to_measure
            ]
            if not line_boxes:
                return 0, 0, []
            widths = [bbox[2] - bbox[0] for _, bbox in line_boxes]
            heights = [bbox[3] - bbox[1] for _, bbox in line_boxes]
            block_w = max(widths)
            block_h = sum(heights) + line_gap * (len(line_boxes) - 1)
            return block_w, block_h, line_boxes

        # Pick the largest font whose wrapped block fits the logo envelope: scan
        # high to low and take the first fit. Text fallbacks use the same hard
        # width/height envelope as image logos, including the absolute height cap
        # and bottom-anchor baseline semantics.
        _sizes = list(range(int(height * 0.26), 7, -2))

        def _widest_word(current_font) -> int:
            """Width of the widest single word at this size (0 for empty input)."""
            return max(
                (_line_width(word, current_font) for word in fallback_title.split()),
                default=0,
            )

        def _first_viable_index() -> int:
            """Index into _sizes of the largest size not ruled out on width alone.

            _wrap_lines places a word on a line even when it alone exceeds max_w
            (the `or not current` branch), so any size whose widest word overflows
            is guaranteed to produce a block wider than max_w and fail the test
            below.  Unlike the full fit test — which is *not* monotone, because a
            larger font can push a word onto line two and make the block narrower
            — single-word width rises monotonically with size, so the first
            viable size can be found by bisection.  Skipping straight to it avoids
            measuring dozens of oversized candidates that cannot possibly fit,
            which used to dominate the cost of rendering a text fallback.
            """
            lo, hi, first = 0, len(_sizes) - 1, 0
            while lo <= hi:
                mid = (lo + hi) // 2
                _fs = _sizes[mid]
                if _widest_word(_load_font(FONT_PATH, _fs)) + max(2, int(_fs * 0.04)) <= max_w:
                    first, hi = mid, mid - 1
                else:
                    lo = mid + 1
            return first

        try:
            font_size = MIN_FONT_SIZE
            font      = _load_font(FONT_PATH, font_size)
            lines     = _wrap_lines(fallback_title, font)
            shadow_offset = max(2, int(font_size * 0.04))
            block_w, block_h, line_boxes = _measure_block(
                lines, font, max(1, int(font_size * 0.12))
            )
            for _fs in _sizes[_first_viable_index():]:
                _f  = _load_font(FONT_PATH, _fs)
                _ls = _wrap_lines(fallback_title, _f)
                if len(_ls) > MAX_LINES:
                    continue
                _gap = max(1, int(_fs * 0.12))
                _shadow = max(2, int(_fs * 0.04))
                _block_w, _block_h, _line_boxes = _measure_block(_ls, _f, _gap)
                if _block_w + _shadow <= max_w and _block_h + _shadow <= max_h:
                    font, font_size, lines = _f, _fs, _ls
                    shadow_offset = _shadow
                    block_w, block_h, line_boxes = _block_w, _block_h, _line_boxes
                    break
        except OSError:
            font      = ImageFont.load_default()
            font_size = MIN_FONT_SIZE
            lines     = [fallback_title]
            shadow_offset = max(2, int(font_size * 0.04))
            block_w, block_h, line_boxes = _measure_block(lines, font, max(1, int(font_size * 0.12)))

        if lines and line_boxes:
            layer_w = max(1, int(np.ceil(block_w + shadow_offset)))
            layer_h = max(1, int(np.ceil(block_h + shadow_offset)))
            text_layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
            layer_draw = ImageDraw.Draw(text_layer)

            cursor_y = 0
            line_gap = max(1, int(font_size * 0.12))
            for line, bbox in line_boxes:
                line_w = bbox[2] - bbox[0]
                line_h = bbox[3] - bbox[1]
                tx = (block_w - line_w) / 2 - bbox[0]
                ty = cursor_y - bbox[1]
                layer_draw.text((tx + shadow_offset, ty + shadow_offset), line, font=font, fill=(0, 0, 0, 180))
                layer_draw.text((tx, ty),                                  line, font=font, fill=(255, 255, 255, 255))
                cursor_y += line_h + line_gap

            scale = min(max_w / text_layer.width, max_h / text_layer.height, 1.0)
            if scale < 1.0:
                text_layer = text_layer.resize(
                    (max(1, int(text_layer.width * scale)), max(1, int(text_layer.height * scale))),
                    Image.Resampling.LANCZOS,
                )

            logo_x = round((width - text_layer.width) / 2)
            if cfg.logo_bottom_anchor:
                baseline = height - int(height * cfg.logo_bottom_ratio)
                logo_y = baseline - text_layer.height
            else:
                centre_y = logo_centre_y(height, cfg.logo_bottom_ratio)
                logo_y = int(centre_y - text_layer.height / 2)
            image.paste(text_layer, (logo_x, logo_y), text_layer)


    # --- Frosted tint colour -------------------------------------------------
    # Every frosted element (rating bar, notch badge, poster-coloured sash) tints
    # from ONE whole-poster colour sample, taken from the un-graded artwork. Since
    # they all draw from the same sample they always match automatically — so the
    # bar simply adopts the notch's colour (and, below, its saturation) whenever a
    # frosted notch is shown, with no separate "match" toggle needed. A tinted
    # vignette drew from the same sample above; reuse it rather than re-quantising.
    _notch_frosted = _sash_shown and cfg.sash_mode == "notch" and cfg.sash_badge_style == "frosted"
    _sash_poster   = _sash_shown and cfg.sash_mode == "sash" and cfg.sash_poster_color
    _bar_frosted   = cfg.rating_display_mode == 4 and cfg.bar_style in ("frosted", "rating_frosted")
    _frost_tint: tuple[float, float, float] | None = (
        (_strict_tint if _strict_tint is not None else dominant_frost_rgb(_frost_color_src))
        if (_bar_frosted or _notch_frosted or _sash_poster) else None
    )
    # A tinted vignette and a frosted notch sample the same artwork but answer
    # different questions — the vignette asks what the band's own stretch of art is
    # made of, the notch what colour the poster is — so they can land some way
    # apart, which reads as two elements disagreeing.  This option settles it in
    # the vignette's favour.  It can only ever adopt a colour the vignette is
    # actually wearing: a band that came out black has no colour to match, and the
    # notch keeps its own logic rather than tinting from a hue nothing else on the
    # poster shows.  The frosted bar follows, as it already follows the notch.
    _frost_matched = (
        cfg.notch_vignette_color and _notch_frosted and _vignette_shown is not None
    )
    if _frost_matched:
        _frost_tint = _vignette_shown
    # Matching gets its own mode rather than the saturation slider or plain
    # reference.  The slider turns a poster colour into a pastel that is not that
    # colour any more; reference keeps the saturation but lifts the Value to make
    # the panel light, and since chroma is S x V that alone hands a dark muted band
    # back as a bright one.  "match" holds chroma where the band had it.  The two
    # controls are mutually exclusive in the configurator for the same reason.
    _frost_ref: bool | str = "match" if _frost_matched else cfg.frost_reference
    # One saturation for every frosted element: a frosted notch owns it (its slider
    # lives in the sash panel); otherwise the rating bar's slider drives it. Sharing
    # it keeps the bar and any sash/notch identical.
    _frost_sat = cfg.sash_badge_frost_saturation if _notch_frosted else cfg.bar_frost_saturation

    # --- Rating / genre label ---
    if cfg.rating_display_mode != 0:

        if cfg.rating_display_mode == 1:
            font_size = int(width * cfg.accent_bar_font_size_ratio)
            # Label suffix is configurable: append year, append sash text, or
            # append both joined by " · ".  Missing data degrades gracefully —
            # if "sash" is requested but no sash triggered, we just show the
            # genre; if "both" but only one is present, we show whichever did.
            #
            # The separator immediately before the sash text becomes "★" when
            # the sash is a winner (sash_type == "win") rather than "·".  Same
            # disambiguation trick used by Compact mode — festival wins and
            # nominees can share their label text, so without this they'd be
            # indistinguishable here.
            _append_year = cfg.accent_bar_append_mode in (0, 2)
            _append_sash = cfg.accent_bar_append_mode in (1, 2)
            _sash_text_for_label, _sash_type_for_label = (
                sash_result if (_append_sash and sash_result) else (None, None)
            )

            _pre_sash = [genre_label] if genre_label else []
            if _append_year and release_year:
                _pre_sash.append(str(release_year))
            _label_main = " · ".join(_pre_sash)

            if _sash_text_for_label:
                label = _label_main + " · " + translate_sash(_sash_text_for_label, cfg.logo_language) if _label_main else translate_sash(_sash_text_for_label, cfg.logo_language)
            else:
                label = _label_main
            rating_cy = height * cfg.accent_bar_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(*cfg.rating_text_color, 255) if cfg.rating_text_color else (200, 200, 200, 255),
            )
            if cfg.score_glow_color == "match":
                _glow_color = "match"
            elif len(cfg.score_glow_color) == 6:
                _glow_color = tuple(int(cfg.score_glow_color[i:i+2], 16) for i in (0, 2, 4))
            else:
                _glow_color = None
            draw_score_bar(
                image, score,
                bottom_margin=int(height * cfg.accent_bar_bottom_ratio),
                glow_threshold=cfg.score_glow_threshold,
                glow_blur=cfg.score_glow_blur,
                glow_alpha=cfg.score_glow_alpha,
                glow_color=_glow_color,
                color_mode=cfg.score_color_mode,
                custom_palette=cfg.score_custom_palette,
            )

        elif cfg.rating_display_mode == 2:
            font_size = int(width * cfg.numeric_score_font_size_ratio)
            # Score formatting:
            #   out of 100 (default): "87", "100", "N/A"
            #   out of 10:            "8.7", "8.0" (always one decimal), "10"
            #                         (no decimal — already two glyphs wide)
            # Non-numeric scores ("N/A") pass through unchanged in either mode.
            if cfg.score_out_of_10 and isinstance(score, (int, float)):
                _score_text = "10" if score >= 100 else f"{score / 10:.1f}"
            else:
                _score_text = str(score)
            label = f"{genre_label} ★ {_score_text}" if genre_label else f"★ {_score_text}"
            rating_cy = height * cfg.numeric_score_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(*cfg.rating_text_color, 255) if cfg.rating_text_color else (200, 200, 200, 255),
            )

        elif cfg.rating_display_mode == 3:
            font_size = int(width * cfg.minimalist_mode_font_size_ratio)

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            y = round(height * cfg.minimalist_mode_font_y_offset)
            right_edge = width - int(width * cfg.minimalist_mode_font_x_offset)
            _ink = (*cfg.rating_text_color, 255) if cfg.rating_text_color else (235, 235, 235, 255)

            # Segments, each tagged with the ROLE of the separator that precedes
            # it.  The role says what the separator divides; the configured
            # style says what it is drawn as.  Keeping those apart is what lets
            # every mode's separator be restyled without the layout knowing:
            #   "field"  — between two plain fields (genre | year).  Text colour.
            #   "rfield" — the same slot in Year mode, where the separator IS
            #              the rating: it takes the score's colour, because
            #              nothing else in that layout shows the score at all.
            #   "rating" — immediately before a printed score.  Defaults to the
            #              ★, which labels the number rather than dividing it
            #              off, but can be a plain separator instead.
            # Mode 0 ("Year"):   genre [rfield] year
            # Mode 1 ("Rating"): genre [rating] score
            # Mode 2 ("Both"):   genre [field] year [rating] score
            # Mode 3 ("Split"):  genre [field] year .................... score
            #   The same left-hand group as Both with the score moved to the
            #   opposite margin, where it needs nothing to say what it is — a
            #   separator earns its place between things that would otherwise
            #   run together, and nothing runs together across a poster's width.
            _has_score = score not in ("N/A", None)
            # Score formatting matches the other modes: out of 100 by default,
            # one decimal out of 10 ("8.7"), with a bare "10" at the top.
            if _has_score and cfg.minimalist_score_out_of_10:
                _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
            else:
                _score_str = str(score)
            parts = [(genre_label, None)] if genre_label else []
            left_parts: list[tuple[str, str | None]] = []
            if cfg.minimalist_append_mode == 0:
                if release_year:
                    parts.append((str(release_year), "rfield" if parts else None))
            elif cfg.minimalist_append_mode == 1:
                if _has_score:
                    parts.append((_score_str, "rating" if parts else None))
            elif cfg.minimalist_append_mode == 3:   # Split
                if release_year:
                    parts.append((str(release_year), "field" if parts else None))
                left_parts, parts = parts, ([(_score_str, None)] if _has_score else [])
            else:  # 2 — Both
                if release_year:
                    parts.append((str(release_year), "field" if parts else None))
                if _has_score:
                    parts.append((_score_str, "rating" if parts else None))

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)
            pip_cy  = round(y + font_size * 0.60)

            # Style resolution.  The two field roles share one setting because
            # they are the same slot in different layouts; the rating role has
            # its own, since the ★ only makes sense in front of a number and
            # would be nonsense between a genre and a year.  A glyph reserves
            # its own width where the bar has a fixed one, so the choice has to
            # reach the layout below and not just the drawing.
            _SEP_GLYPH = {"pip": None, "bullet": "•", "star": "★"}

            def _sep_style(role: str) -> str:
                return (cfg.minimalist_rating_separator if role == "rating"
                        else cfg.minimalist_separator)

            def _sep_glyph(role: str) -> str | None:
                # Unknown styles fall back to the bar rather than raising: this
                # runs per poster, and a bad value is a config problem, not a
                # reason to fail the render.
                return _SEP_GLYPH.get(_sep_style(role))

            def _sep_width(role: str) -> float:
                glyph = _sep_glyph(role)
                return pip_w if glyph is None else draw.textlength(glyph, font=font_meta)

            def _score_int(value) -> "int | None":
                try:
                    return max(0, min(int(value), 100))
                except (TypeError, ValueError):
                    return None

            # Lay out right-to-left: each segment, with its separator to its left.
            ops    = []   # (kind, x[, text]); kind in text|field|rfield|rating
            cursor = right_edge
            for i in range(len(parts) - 1, -1, -1):
                seg, sep = parts[i]
                seg_x = int(cursor - draw.textlength(seg, font=font_meta))
                ops.append(("text", seg_x, seg))
                cursor = seg_x
                if sep:
                    cursor -= pip_gap
                    sep_w  = _sep_width(sep)
                    sep_x  = cursor - sep_w
                    ops.append((sep, sep_x))
                    cursor = sep_x - pip_gap

            # Optional centre anchor.  The logo above is centred on the poster,
            # so the metadata line can be too; the x offset then stops being a
            # right margin and simply stops applying.  The loop above leaves
            # `cursor` on the group's left edge, so its exact drawn extent is
            # known here and the whole thing can just be slid into the middle —
            # measured after layout rather than predicted before it, because the
            # per-segment rounding above would otherwise push the result a pixel
            # or two off centre.  Split is excluded: its two groups are DEFINED
            # by the opposite margins they hang off, so there is no single group
            # left to centre, and the option is hidden in the configurator.
            if cfg.minimalist_center and cfg.minimalist_append_mode != 3 and ops:
                _shift = round(width / 2 - (cursor + right_edge) / 2)
                ops = [(op[0], op[1] + _shift, *op[2:]) for op in ops]

            # ...and the split mode's left-hand group the same way but forwards,
            # off the opposite margin, so the two groups sit symmetrically.
            cursor = width - right_edge
            for seg, sep in left_parts:
                if sep:
                    cursor += pip_gap
                    ops.append((sep, int(cursor)))
                    cursor += _sep_width(sep) + pip_gap
                ops.append(("text", int(cursor), seg))
                cursor += draw.textlength(seg, font=font_meta)

            for op in ops:
                kind, ox = op[0], op[1]
                if kind == "text":
                    draw.text((ox, y), op[2], font=font_meta, fill=_ink)
                    continue

                glyph = _sep_glyph(kind)
                if kind == "rfield":
                    # The rating shown as a colour.  Both shapes take the same
                    # score lookup, and both are skipped when there is no score
                    # to colour them with — a neutral mark in this slot would
                    # read as a rating rather than as the absence of one.
                    _sc = _score_int(score)
                    if _sc is None:
                        continue
                    _fill = score_color_for_mode(
                        _sc, cfg.score_color_mode, cfg.score_custom_palette)[0]
                else:
                    # Unless this separator is carrying the rating in Year
                    # mode, it belongs to the metadata line and follows that
                    # line's configured (or default) text colour.
                    _fill = _ink[:3]

                if glyph is None:
                    _draw_solid_pip(image, x=ox, y_center=pip_cy,
                                    width=pip_w, height=pip_h, color=_fill)
                else:
                    draw.text((ox, y), glyph, font=font_meta, fill=(*_fill, 255))

        elif cfg.rating_display_mode == 4:
            # Frosted bar — centred dot-separated label at the bottom.
            # Format: Year · Genre · ★ Rating  (omit any missing field)
            _has_score = score not in ("N/A", None)
            if _has_score:
                if cfg.bar_score_out_of_10:
                    _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
                else:
                    _score_str = str(score)
            else:
                _score_str = ""
            _year_str  = str(release_year) if release_year else ""
            _bar_sash, _ = sash_result if sash_result else (None, None)
            if cfg.bar_append == "rating_year":
                _parts = [_year_str, genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "rating":
                _parts = [genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "year":
                _parts = [_year_str, genre_label or ""]
            else:  # "sash"
                _parts = [genre_label or "", translate_sash(_bar_sash, cfg.logo_language) if _bar_sash else ""]
            _parts = [p for p in _parts if p]
            _sep = "  ·  " if len(_parts) <= 2 else " · "
            image = draw_frosted_bar(
                image,
                left_text   = "",
                center_text = _sep.join(_parts),
                right_text  = "",
                bar_height_ratio = cfg.bar_height_ratio,
                font_size_ratio  = cfg.bar_font_size_ratio,
                frost_opacity    = cfg.bar_frost_opacity,
                frost_saturation = _frost_sat,
                frost_reference  = _frost_ref,
                bottom_inset     = cfg.bar_bottom_inset,
                style            = cfg.bar_style,
                score            = score if score not in ("N/A", None) else None,
                fill_color       = (
                    None  # "sample" → let draw_frosted_bar derive from bar tint
                    if cfg.bar_accent == "sample" else
                    {"silver": (210, 210, 218), "gold": (212, 175, 55)}.get(cfg.bar_accent)
                    or (
                        score_color_for_mode(
                            int(score),
                            3 if cfg.bar_accent == "palette_custom" else int(cfg.bar_accent[-1]),
                            cfg.score_custom_palette,
                        )[0]
                        if score not in ("N/A", None) else (210, 210, 218)
                    )
                ) if cfg.bar_style in ("rating_black", "rating_frosted") else None,
                tint_rgb         = _frost_tint,
                text_color       = cfg.rating_text_color,
            )

    # --- Discovery sash / badge ---
    if cfg.sash_mode != "hidden" and sash_result is not None:
        label, sash_type = sash_result
        _is_star  = cfg.sash_winner_star and sash_type == "win"
        _label_tr = translate_sash(label, cfg.logo_language)
        if cfg.sash_mode == "notch":
            image = draw_award_badge(image, _label_tr, sash_type=sash_type,
                                     size_ratio_w=cfg.sash_badge_size_w,
                                     size_ratio_h=cfg.sash_badge_size_h,
                                     notch_style=cfg.sash_badge_style,
                                     notch_inset=cfg.sash_badge_inset,
                                     notch_pad_ratio=cfg.sash_badge_pad,
                                     font_size_ratio=cfg.sash_badge_font_ratio,
                                     frost_opacity=cfg.sash_badge_frost_opacity,
                                     frost_saturation=cfg.sash_badge_frost_saturation,
                                     frost_reference=_frost_ref,
                                     tint_rgb=_frost_tint,
                                     star=_is_star,
                                     text_color=cfg.sash_text_color)
        else:  # "sash" — diagonal
            _poster_color = _frost_tint if cfg.sash_poster_color else None
            image = draw_award_sash(image, _label_tr, sash_type=sash_type, muted=cfg.muted,
                                    length_ratio=cfg.sash_length_ratio,
                                    height_ratio=cfg.sash_height_ratio,
                                    poster_color=_poster_color,
                                    frost_saturation=_frost_sat,
                                    frost_reference=_frost_ref,
                                    star=_is_star,
                                    text_color=cfg.sash_text_color)

    return image


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def prune_rating_state(now: float) -> tuple[int, int]:
    """Drop rating back-off entries that have expired, and the failure counters
    left behind by ones that already went.  Returns how many of each.

    Both dicts are keyed by (imdb_id, API key) and are written and cleared
    together everywhere a request touches them: a failure sets the counter and
    the back-off, an expiry on access deletes both, a success clears both.  Only
    this sweep could separate them, by deleting an expired back-off and leaving
    its counter — which is why the counters are swept by *absence* of a back-off
    rather than against the list just expired.  That also collects anything
    stranded before this function existed, without which a long-lived instance
    keeps a counter for every title that ever failed a rating fetch and was never
    asked for again.
    """
    expired = [k for k, v in _rating_backoff.items() if v <= now]
    for k in expired:
        del _rating_backoff[k]
    orphans = [k for k in _rating_fail_count if k not in _rating_backoff]
    for k in orphans:
        del _rating_fail_count[k]
    return len(expired), len(orphans)


async def _cache_prune_loop() -> None:
    """Periodically prune expired rows from all cache tables."""
    # Wait a few minutes after startup before the first run so the service
    # is fully warmed before taking the SQLite write lock.
    await asyncio.sleep(300)
    while True:
        logger.info("Running scheduled cache prune")
        await asyncio.get_running_loop().run_in_executor(None, prune_caches)

        expired, orphans = prune_rating_state(asyncio.get_running_loop().time())
        if expired or orphans:
            logger.debug(
                f"Pruned {expired} expired rating backoff entries "
                f"and {orphans} stranded failure counters"
            )

        await asyncio.sleep(6 * 3600)   # every 6 hours


async def _run_cache_warm_cycle(client: httpx.AsyncClient) -> None:
    """
    Pre-populate the TMDB metadata, poster/backdrop image, and logo caches,
    plus the MDBList rating/award cache, for currently-trending titles — so
    the first real requests for them don't all hit upstream APIs/CDNs at
    once. The poster/backdrop + logo downloads are the slowest, most
    bandwidth-heavy part of a real request and the most likely to cause a
    burst-traffic pile-up against a stale cache, so every processed
    candidate gets the same default art fetched as a real view would.

    If CACHE_WARM_CATALOG_URLS is set, the catalogs exposed by those addon
    manifests are fetched first (the same way a Stremio client would when a
    user opens that catalog) and warmed ahead of trending/popular/
    supplemental, capped at CACHE_WARM_CATALOG_MAX_ITEMS items per catalog.

    Single pass over a ranked, deduped candidate list (catalog, then
    trending, then popular, then supplemental — top rated / now playing /
    on the air): walks until the TMDB metadata budget is spent or the
    candidate list is exhausted. MDBList lookups are interleaved for as long
    as the MDBList budget allows, then the loop continues warming TMDB-only
    for the remainder. Both budgets only count actual cache-miss
    metadata/rating API calls — entries already warm (including images and
    logos) cost nothing.

    If CACHE_WARM_QUALITY_ENABLED is set, also pre-fetches quality badge
    data (resolution/source/HDR tokens) for every processed candidate via
    the configured quality source (series default to S01E01). This is off
    by default — see the config comment for why.
    """
    global _mdblist_semaphore, _mdblist_active_key_idx, _quality_bg_semaphore

    if not _cfg.SERVER_TMDB_KEY:
        logger.info("Cache warm: skipped — no server TMDB key configured")
        return

    tmdb_budget    = max(0, _cfg.CACHE_WARM_TMDB_BUDGET)
    mdblist_budget = max(0, _cfg.CACHE_WARM_MDBLIST_BUDGET)
    if tmdb_budget == 0:
        logger.info("Cache warm: skipped — CACHE_WARM_TMDB_BUDGET is 0")
        return

    effective_mdblist_key = _resolve_mdblist_key("") if _cfg.SERVER_MDBLIST_KEYS else None
    if not effective_mdblist_key:
        mdblist_budget = 0

    # Mix three sources: trending (volatile, day/week hot list), popular
    # (broad, slow-moving catalogue staples), and supplemental (top rated /
    # now playing / on the air — acclaimed and currently-airing titles that
    # trending and popular tend to miss). Split the target list across the
    # three so warming covers "what's hot right now", "what people steadily
    # watch", and "what's airing/acclaimed". Each source is independently
    # ranked/deduped; the combined list is deduped again here so a title
    # appearing in multiple sources only costs one slot.
    target_total  = max(tmdb_budget, mdblist_budget) or tmdb_budget
    trending_target    = (target_total * 4 + 9) // 10  # ~40%
    popular_target     = (target_total * 3 + 9) // 10  # ~30%
    supplemental_target = target_total - trending_target - popular_target  # ~30%

    catalog_candidates, trending_candidates, popular_candidates, supplemental_candidates = await asyncio.gather(
        fetch_catalog_candidates(
            client, _cfg.CACHE_WARM_CATALOG_URLS, _cfg.SERVER_TMDB_KEY,
            max_items_per_catalog=_cfg.CACHE_WARM_CATALOG_MAX_ITEMS,
        ),
        fetch_trending_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=trending_target),
        fetch_popular_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=popular_target),
        fetch_supplemental_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=supplemental_target),
    )

    # Catalog candidates come first so a user-requested catalog is warmed
    # ahead of generic trending/popular/supplemental within the shared budgets.
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for item in catalog_candidates + trending_candidates + popular_candidates + supplemental_candidates:
        key = (item["media_type"], item["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)

    logger.info(
        f"Cache warm: starting cycle — {len(candidates)} candidates "
        f"({len(catalog_candidates)} catalog, {len(trending_candidates)} trending, "
        f"{len(popular_candidates)} popular, {len(supplemental_candidates)} supplemental, "
        f"{len(catalog_candidates) + len(trending_candidates) + len(popular_candidates) + len(supplemental_candidates) - len(candidates)} overlap), "
        f"tmdb_budget={tmdb_budget}, mdblist_budget={mdblist_budget}"
    )

    if _mdblist_semaphore is None:
        _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

    tmdb_calls      = 0
    mdblist_calls   = 0
    quality_calls   = 0
    detection_calls = 0
    titles_seen     = 0

    # Text-detection scans (~400ms each) are pipelined: queue a scan and keep
    # processing later candidates' metadata/image/rating work while it runs,
    # only blocking once _DETECTION_PIPELINE_DEPTH scans are in flight. The
    # existing TEXTLESS_DETECTION_CONCURRENCY semaphore still caps how many
    # actually run at once — this just stops the loop from idling while they do.
    _pending_detections: list[asyncio.Task] = []
    _detection_pipeline_depth = _cfg.TEXTLESS_DETECTION_CONCURRENCY + 1

    for candidate in candidates:
        if tmdb_calls >= tmdb_budget:
            break

        tmdb_id    = candidate["tmdb_id"]
        media_type = candidate["media_type"]
        endpoint   = "tv" if media_type in ("tv", "series") else "movie"

        metadata_cache_key = tmdb_metadata_cache_key(endpoint, tmdb_id, "en")
        cached_meta = get_cached_tmdb_metadata(metadata_cache_key)

        if cached_meta is None:
            try:
                genre_ids, is_textless, logos, release_year, _title, poster_path, backdrop_path, tmdb_data = (
                    await _coalesced_fetch_poster_metadata(client, tmdb_id, _cfg.SERVER_TMDB_KEY, media_type, "en")
                )
            except Exception as exc:
                logger.warning(f"Cache warm: TMDB metadata fetch failed for {media_type}/{tmdb_id}: {exc}")
                continue
            tmdb_calls += 1
            imdb_id           = tmdb_data.get("imdb_id")
            original_language = tmdb_data.get("original_language")
            original_title    = tmdb_data.get("original_title")
            vote_count        = tmdb_data.get("vote_count")
            title             = _title
        else:
            genre_ids         = cached_meta.get("genre_ids", [])
            imdb_id           = cached_meta.get("imdb_id")
            is_textless       = cached_meta.get("is_textless", False)
            logos             = cached_meta.get("logos", [])
            poster_path       = cached_meta.get("poster_path")
            backdrop_path     = cached_meta.get("backdrop_path")
            original_language = cached_meta.get("original_language")
            release_year      = cached_meta.get("release_year")
            original_title    = cached_meta.get("original_title")
            vote_count        = cached_meta.get("vote_count")
            title             = cached_meta.get("title")

        titles_seen += 1

        # Pre-fetch the poster/backdrop image and (when applicable) the logo
        # this title would render with by default — the slow, bandwidth-heavy
        # part of a real request. fetch_poster_image/fetch_backdrop_image/
        # fetch_logo each check their own disk cache first and skip the
        # download when already warm, so this is cheap in steady state.
        # Mirrors the default poster-endpoint art selection (textless poster,
        # else backdrop fallback, else text-bearing poster) but skips the
        # CPU-heavy text-detection rescue path and original-art mode, which
        # are per-request preferences rather than the common default.
        _use_backdrop = bool(backdrop_path) and (poster_path is None or not is_textless)
        try:
            if _use_backdrop:
                await fetch_backdrop_image(client, tmdb_id, backdrop_path, avoid_text=False)
                _logo_textless = True
            elif poster_path:
                await fetch_poster_image(client, tmdb_id, media_type, poster_path)
                _logo_textless = is_textless
            else:
                _logo_textless = False

            if _logo_textless and logos:
                await fetch_logo(
                    client, logos, "en",
                    imdb_id=imdb_id,
                    original_language=original_language,
                    logo_priority="native_original",
                )
        except Exception as exc:
            logger.warning(f"Cache warm: image/logo fetch failed for {media_type}/{tmdb_id}: {exc}")

        # Pre-run burned-in-text detection on the textless art selected above —
        # the same scan a real /poster request would trigger on first view, and
        # by far the slowest per-request step (~400ms cold). Mirrors the
        # /poster cache-key scheme exactly (source tag + crop version + conf +
        # detector signature) so a warmed result is a hit on the real request.
        # Unlike /poster, no vote-count gate: warming happens off the request
        # path, so every textless title gets resolved up front.
        if _cfg.TEXTLESS_TEXT_DETECTION and is_textless and (_use_backdrop or poster_path):
            try:
                from text_detect import DETECT_RES_SIG

                if _use_backdrop:
                    _det_src = f"bd:{backdrop_path}:{_CROP_VERSION}:plain"
                    _image_cache_key = f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
                    _det_source = "backdrop"
                else:
                    _det_src = f"ps:{poster_path}"
                    _image_cache_key = f"{media_type}_{tmdb_id}_{poster_path.strip('/')}"
                    _det_source = "poster"

                _det_key = f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                if get_cached_text_detection(_det_key) is None:
                    _det_image = await asyncio.get_running_loop().run_in_executor(
                        None, _load_detection_image, _image_cache_key
                    )
                    if _det_image is not None:
                        _text_titles = tuple(dict.fromkeys(
                            value for value in (title, original_title) if value
                        ))
                        _pending_detections.append(_start_text_detection(
                            _det_key,
                            _det_image,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            vote_count=vote_count,
                            source_key=_det_src,
                            media_type=media_type,
                            image_path=poster_path,
                            foreground=False,
                        ))
                        detection_calls += 1
                        if len(_pending_detections) >= _detection_pipeline_depth:
                            _done, _pending = await asyncio.wait(
                                _pending_detections, return_when=asyncio.FIRST_COMPLETED
                            )
                            _pending_detections = list(_pending)
            except Exception as exc:
                logger.warning(f"Cache warm: text detection failed for {media_type}/{tmdb_id}: {exc}")

        # Optionally pre-fetch quality badge data (resolution/source/HDR) via
        # the configured quality source. Series default to S01E01 — the warm
        # cycle has no concept of "which episode", so this is a best-effort
        # warm of the most commonly requested entry point. Off by default;
        # see CACHE_WARM_QUALITY_ENABLED for why.
        if _cfg.CACHE_WARM_QUALITY_ENABLED and imdb_id:
            if quality_source_configured() and _quality_backoff_remaining() <= 0:
                if get_cached_quality(imdb_id, release_year) is None:
                    if _quality_bg_semaphore is None:
                        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
                    try:
                        async with _quality_bg_semaphore:
                            q_result = await _with_retry(
                                fetch_quality,
                                client, imdb_id, media_type, 1, 1, release_year,
                            )
                        quality_calls += 1
                        _record_quality_result(q_result)
                        if q_result is QUALITY_PENDING:
                            # Still counts as a warm: the lookup registered the
                            # title with QualiCache, which now queues it.
                            logger.debug(f"Cache warm: quality pending for {imdb_id}")
                        elif q_result is FETCH_FAILED:
                            logger.warning(f"Cache warm: quality fetch failed for {imdb_id}")
                    except Exception as exc:
                        quality_calls += 1
                        _record_quality_result(FETCH_FAILED)
                        logger.warning(f"Cache warm: quality fetch failed for {imdb_id}: {exc}")

        if mdblist_calls >= mdblist_budget:
            continue

        # Warm under exactly the identity /poster reads, or the row is written
        # where nothing looks for it. A title TMDB has no IMDb link for is warmed
        # through the TMDB route rather than skipped.
        warm_canonical_id = _canonical_rating_id(imdb_id or "", "", tmdb_id)
        warm_provider     = "imdb" if imdb_id else "tmdb"
        warm_media_id     = imdb_id or tmdb_id

        if get_cached_rating(warm_canonical_id) is not None:
            continue  # rating already fresh — nothing to do

        await asyncio.sleep(0.25)

        async def _fetch_rating_warm(_key: str):
            async with _mdblist_semaphore:
                return await fetch_rating(
                    client, _key, genre_ids, media_type,
                    media_id=warm_media_id, provider=warm_provider,
                )

        result = await _fetch_rating_warm(effective_mdblist_key)
        mdblist_calls += 1

        if isinstance(result, _RateLimited):
            backoff_secs, replacement = _mark_mdblist_rate_limit(warm_canonical_id, effective_mdblist_key, result)
            logger.warning(
                f"Cache warm: MDBList rate-limited on {warm_canonical_id}; "
                f"key cooling down for {backoff_secs:.0f}s"
            )
            if replacement:
                effective_mdblist_key = replacement
            else:
                logger.info("Cache warm: no healthy MDBList key remains — stopping MDBList warming for this cycle")
                mdblist_budget = mdblist_calls  # stop further MDBList attempts
            continue

        if result is FETCH_FAILED:
            logger.warning(f"Cache warm: MDBList fetch failed for {warm_canonical_id} — stopping MDBList warming for this cycle")
            mdblist_budget = mdblist_calls  # stop further MDBList attempts
            continue

        ratings_dict, genre, rel, keywords, age_rating = result
        award_wins, award_noms = parse_mdblist_awards(keywords, tmdb_id=tmdb_id)
        kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
        festival_keyword = match_festival_keyword(kw_names)
        is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
        is_true_story = "based-on-true-story" in kw_names
        is_metacritic = "metacritic-must-see" in kw_names

        set_cached_rating(
            warm_canonical_id,
            ratings_dict if isinstance(ratings_dict, dict) else {},
            genre or "Unknown",
            rel,
            award_wins,
            award_noms,
            awards_fetched=True,
            festival_keyword=festival_keyword,
            age_rating=age_rating,
            is_cult=is_cult,
            is_true_story=is_true_story,
            is_metacritic=is_metacritic,
        )

    if _pending_detections:
        await asyncio.gather(*_pending_detections, return_exceptions=True)

    logger.info(
        f"Cache warm: cycle complete — {titles_seen} titles processed, "
        f"{tmdb_calls} TMDB calls, {mdblist_calls} MDBList calls, "
        f"{quality_calls} quality calls, {detection_calls} text-detection scans"
    )


_CACHE_WARM_LAST_RUN_KEY = "cache_warm_last_run"
# Wait this long after startup before the very first-ever cycle, so it
# doesn't compete with other startup warm-up work (text detection model,
# genre backgrounds, etc.).
_CACHE_WARM_STARTUP_GRACE_SECS = 60
# When a restart finds the cycle already overdue, still wait this long before
# running — avoids hammering startup with warming work on a crash-loop.
_CACHE_WARM_MIN_WAIT_SECS = 60


def _format_local(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _seconds_until_next_hour(target_hour: float, now: float | None = None) -> float:
    """Seconds from `now` until the next occurrence of `target_hour` (0-24,
    local time, may be fractional e.g. 4.5 for 4:30am). Always returns a
    positive value — if `target_hour` is the current hour, rolls to tomorrow.
    """
    if now is None:
        now = time.time()
    local = time.localtime(now)
    midnight = now - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
    target = midnight + target_hour * 3600
    if target <= now:
        target += 86400
    return target - now

# Third-party credentials must never be persisted to the poster cache. They are
# stripped before storage; the background regeneration cycle re-supplies the
# server-side keys. access_key is intentionally kept — the /poster replay needs
# it to pass the instance access gate, and it is the instance's own key.
_UNCACHEABLE_PARAMS = {"tmdb_key", "mdblist_key"}


def _sanitize_request_params(query: str) -> str:
    """Drop user API keys from a stored query string, preserving order."""
    if not query:
        return query
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k not in _UNCACHEABLE_PARAMS]
    return urlencode(kept)


async def _run_trending_fetch_cycle(client: httpx.AsyncClient) -> None:
    logger.info("Starting scheduled trending fetch cycle")
    if not _cfg.SERVER_TMDB_KEY:
        logger.info("Trending fetch: skipped - no server TMDB key configured")
        return

    try:
        trending = await fetch_trending_candidates(
            client, _cfg.SERVER_TMDB_KEY, max_items=max(_cfg.TRENDING_FETCH_COUNT, _cfg.TRENDING_BROAD_FETCH_COUNT)
        )
    except Exception as exc:
        logger.error(f"Trending fetch: failed to fetch candidates: {exc}")
        return

    # Build the set of trending (tmdb_id, type) pairs. TV titles are cached under
    # both "tv" and "series" (Stremio uses "series"), so include both variants.
    # Keeping the media type prevents a movie and a TV show that share a numeric
    # TMDB id from cross-triggering each other's regeneration.
    trending_pairs: set[tuple[str, str]] = set()
    for item in trending:
        tid = item.get("tmdb_id")
        if tid is None:
            continue
        tid = str(tid)
        mt = item.get("media_type")
        if mt in ("tv", "series"):
            trending_pairs.add((tid, "tv"))
            trending_pairs.add((tid, "series"))
        elif mt:
            trending_pairs.add((tid, mt))
    if not trending_pairs:
        return

    db = get_db()
    try:
        rows = db.execute("SELECT cache_key, request_params FROM final_poster_cache WHERE request_params IS NOT NULL").fetchall()
    except Exception as exc:
        logger.error(f"Trending fetch: failed to query cache: {exc}")
        return

    # Use a test client to regenerate posters through the API
    regenerated_count = 0
    
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as local_client:
        for cache_key, req_params_str in rows:
            parts = cache_key.split(":")
            if len(parts) < 4:
                continue
            if (parts[1], parts[2]) not in trending_pairs:
                continue
            if not req_params_str:
                continue
            logger.info(f"Trending fetch: regenerating poster for {cache_key}")
            try:
                # Delete first so the replay misses the cache and re-renders with
                # the fresh trending rank instead of serving the stale composite.
                delete_cached_final_poster(cache_key)
                resp = await local_client.get(f"/poster?{req_params_str}")
                if resp.status_code >= 400:
                    logger.warning(f"Trending fetch: regenerate for {cache_key} returned HTTP {resp.status_code}")
                else:
                    regenerated_count += 1
            except Exception as exc:
                logger.error(f"Trending fetch: failed to regenerate poster {cache_key}: {exc}")
                    
    logger.info(f"Trending fetch cycle completed. Regenerated {regenerated_count} posters.")


async def _trending_fetch_loop() -> None:
    """Periodically fetch trending items and regenerate cached posters."""
    # First run immediately on startup
    await asyncio.sleep(10)
    try:
        if _HTTP_CLIENT is not None:
            await _run_trending_fetch_cycle(_HTTP_CLIENT)
    except Exception as exc:
        logger.error(f"Trending fetch: startup cycle failed: {exc}")

    while True:
        if not _cfg.TRENDING_FETCH_TIME:
            wait = 86400.0
        else:
            try:
                tz = zoneinfo.ZoneInfo(_cfg.TRENDING_FETCH_TIMEZONE)
            except Exception as exc:
                logger.error(f"Trending fetch: invalid timezone {_cfg.TRENDING_FETCH_TIMEZONE}: {exc}, using UTC")
                tz = zoneinfo.ZoneInfo("UTC")

            try:
                h, m = map(int, _cfg.TRENDING_FETCH_TIME.split(':'))
            except ValueError:
                h, m = 0, 0
                
            now_dt = datetime.now(tz)
            target_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if target_dt <= now_dt:
                target_dt += timedelta(days=1)
            wait = (target_dt - now_dt).total_seconds()
            
        logger.info(f"Trending fetch: next cycle scheduled in {wait / 3600:.1f} hours")
        await asyncio.sleep(wait)
        try:
            if _HTTP_CLIENT is not None:
                await _run_trending_fetch_cycle(_HTTP_CLIENT)
        except Exception as exc:
            logger.error(f"Trending fetch: cycle failed: {exc}")


async def _cache_warm_loop(digital_release_ready: asyncio.Event | None = None) -> None:
    """
    Periodically warm the TMDB metadata/image and MDBList rating caches for
    trending + popular titles.

    The last completed cycle's timestamp is persisted (app_state table) so a
    container restart within CACHE_WARM_INTERVAL_HOURS of the last run
    doesn't immediately re-run the whole cycle — it instead waits out the
    remainder of the interval. The very first run ever uses a short startup
    grace period instead.

    If CACHE_WARM_AT_HOUR is set, steady-state cycles (after the first) are
    instead scheduled for the next occurrence of that local hour-of-day,
    rather than exactly CACHE_WARM_INTERVAL_HOURS after the previous run.

    On the very first cycle, also wait (briefly) for the digital-release
    (movieleaks) sync to finish first, so the two startup background jobs
    don't both hammer external APIs at the same time.
    """
    if not _cfg.CACHE_WARM_ENABLED:
        return

    interval_secs = max(1.0, _cfg.CACHE_WARM_INTERVAL_HOURS) * 3600

    last_run_raw = get_app_state(_CACHE_WARM_LAST_RUN_KEY)
    if last_run_raw is not None:
        try:
            last_run = float(last_run_raw)
        except ValueError:
            last_run = None
    else:
        last_run = None

    if last_run is None:
        wait = float(_CACHE_WARM_STARTUP_GRACE_SECS)
    elif _cfg.CACHE_WARM_AT_HOUR is not None:
        wait = max(_CACHE_WARM_MIN_WAIT_SECS, _seconds_until_next_hour(_cfg.CACHE_WARM_AT_HOUR))
    else:
        wait = max(_CACHE_WARM_MIN_WAIT_SECS, (last_run + interval_secs) - time.time())

    first_cycle = True
    while True:
        logger.info(
            f"Cache warm: next cycle scheduled for {_format_local(time.time() + wait)} "
            f"(in {wait / 60:.1f} min)"
        )
        await asyncio.sleep(wait)
        if first_cycle and digital_release_ready is not None and not digital_release_ready.is_set():
            try:
                await asyncio.wait_for(digital_release_ready.wait(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning(
                    "Cache warm: digital release sync didn't finish within 120s — proceeding anyway"
                )
        first_cycle = False
        try:
            if _HTTP_CLIENT is not None:
                await _run_cache_warm_cycle(_HTTP_CLIENT)
                set_app_state(_CACHE_WARM_LAST_RUN_KEY, str(time.time()))
            else:
                logger.warning("Cache warm: HTTP client not ready — skipping this cycle")
        except Exception as exc:
            logger.error(f"Cache warm: cycle failed: {exc}")
        if _cfg.CACHE_WARM_AT_HOUR is not None:
            wait = max(_CACHE_WARM_MIN_WAIT_SECS, _seconds_until_next_hour(_cfg.CACHE_WARM_AT_HOUR))
        else:
            wait = interval_secs


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _HTTP_CLIENT, _configurator_html, _render_assets_signature
    global _background_detection_queue, _background_detection_task
    init_db()
    logger.info(f"Cache initialised (composite TTL {_cfg.COMPOSITE_CACHE_TTL}s / "
                f"{_cfg.COMPOSITE_CACHE_TTL / 86400:.1f}d)")
    imdb_dataset.init_db()
    if imdb_dataset.is_enabled():
        logger.info(
            f"IMDb local dataset enabled (refresh every {_cfg.IMDB_DATASET_REFRESH_HOURS}h, "
            f"{imdb_dataset.row_count()} titles currently loaded)"
        )
    _HTTP_CLIENT = _make_http_client()
    logger.info("HTTP client initialised")
    # Warn on quality source misconfiguration
    _quality_source = active_quality_source()
    if _cfg.QUALITY_SOURCE not in QUALITY_SOURCES:
        logger.warning(
            f"Unknown QUALITY_SOURCE={_cfg.QUALITY_SOURCE!r} — expected one of "
            f"{', '.join(QUALITY_SOURCES)}; defaulting to aiostreams behaviour."
        )
    elif _quality_source != "aiostreams" and (bool(_cfg.AIOSTREAMS_URL) or bool(_cfg.AIOSTREAMS_AUTH)):
        logger.warning(
            f"QUALITY_SOURCE={_quality_source} but AIOSTREAMS_URL/AIOSTREAMS_AUTH are also set — "
            f"{_quality_source} will be used; AIOSTREAMS settings are ignored. "
            "Unset AIOSTREAMS_URL and AIOSTREAMS_AUTH to silence this warning."
        )
    if _quality_source == "scraper" and not _cfg.SCRAPER_URL:
        logger.warning("QUALITY_SOURCE=scraper but SCRAPER_URL is not set — quality fetching is disabled.")
    if _quality_source == "qualicache" and not _cfg.QUALICACHE_URL:
        logger.warning("QUALITY_SOURCE=qualicache but QUALICACHE_URL is not set — quality fetching is disabled.")
    if _quality_source == "qualicache" and _cfg.QUALICACHE_URL:
        logger.info(f"Quality source: QualiCache at {_cfg.QUALICACHE_URL}")
    _configurator_html = _load_configurator_html()
    load_languages()   # poster-output translations (English fallback if absent)
    _render_assets_signature = _compute_render_assets_signature()
    # Count the genre fallback backgrounds without decoding them.  These are only
    # used when a title has no usable art at all, so warming the whole set into
    # memory cost ~172 MB resident for a path most requests never touch; they now
    # load on demand into a bounded LRU (see _load_genre_background).  The count
    # still logs, because "no art found" is worth telling the operator about.
    try:
        _available = 0
        for _style in _GENRE_BG_STYLES:
            _sdir = os.path.join(_GENRE_BG_DIR, _style)
            if not os.path.isdir(_sdir):
                continue
            _available += sum(
                1 for _fn in os.listdir(_sdir) if _fn.lower().endswith(".png")
            )
        if _available:
            logger.info(
                f"Genre backgrounds available: {_available} entries "
                f"(loaded on demand, cache limit {_GENRE_BG_CACHE_MAX})"
            )
        else:
            logger.info("No genre background art found — using gradient fallbacks")
    except Exception as exc:
        logger.warning(f"Genre background scan skipped: {exc}")
    # Burned-in-text detection: fetch + load PP-OCRv5 Mobile in the background so
    # the first textless request isn't blocked by the one-time ~4.6 MB
    # download.  On by default; skipped when the operator has opted out.
    if _cfg.TEXTLESS_TEXT_DETECTION:
        _background_detection_queue = asyncio.Queue()
        _background_detection_task = asyncio.create_task(
            _background_text_detection_worker()
        )

        async def _warm_text_detector():
            try:
                from text_detect import text_detection_status, warm_model
                ok = await asyncio.get_running_loop().run_in_executor(_get_detect_executor(), warm_model)
                log = logger.info if ok else logger.warning
                log(f"Burned-in-text detection: {text_detection_status()}")
            except Exception as exc:
                logger.warning(f"PP-OCR warm-up failed: {exc}")
        asyncio.create_task(_warm_text_detector())

    try:
        from tvdb import tvdb_status
        logger.info(f"TVDB fallback art source: {tvdb_status()}")
    except Exception as exc:
        logger.warning(f"TVDB status check failed: {exc}")

    _digital_release_ready = asyncio.Event()
    prune_task   = asyncio.create_task(_cache_prune_loop())
    digital_task = asyncio.create_task(digital_release_poll_loop(_HTTP_CLIENT, _digital_release_ready))
    cache_warm_task = asyncio.create_task(_cache_warm_loop(_digital_release_ready))
    trending_task = asyncio.create_task(_trending_fetch_loop())
    imdb_dataset_task = asyncio.create_task(imdb_dataset_refresh_loop(_HTTP_CLIENT))
    yield
    prune_task.cancel()
    digital_task.cancel()
    cache_warm_task.cancel()
    trending_task.cancel()
    imdb_dataset_task.cancel()
    if _background_detection_task is not None:
        _background_detection_task.cancel()
    # Await the cancelled tasks so their finally: blocks finish unwinding
    # before we close the HTTP client they may still be using.
    with suppress(asyncio.CancelledError):
        await prune_task
    with suppress(asyncio.CancelledError):
        await digital_task
    with suppress(asyncio.CancelledError):
        await cache_warm_task
    with suppress(asyncio.CancelledError):
        await trending_task
    with suppress(asyncio.CancelledError):
        await imdb_dataset_task
    if _background_detection_task is not None:
        with suppress(asyncio.CancelledError):
            await _background_detection_task
        _background_detection_task = None
    _background_detection_queue = None
    _background_detection_keys.clear()
    _shutdown_detect_executor()
    await _HTTP_CLIENT.aclose()
    logger.info("HTTP client closed")


app = FastAPI(lifespan=lifespan)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(BASE_DIR, "fonts")


# Font objects are immutable once built and re-parsing the TTF per size adds up
# fast in the fallback-title fit loop, which probes many sizes for one title.
# Shared across render threads, matching what quality.py and age_badge.py
# already do with their own font caches.
@lru_cache(maxsize=256)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


# ── Genre fallback backgrounds ────────────────────────────────────────────
# Atmospheric 500x750 PNGs (procedurally generated by genre_backgrounds.py, or
# hand-made overrides dropped into the same folder) used as the base for no-art
# fallback posters instead of the flat gradient.  Cached in memory; a *copy* is
# returned per request because build_poster draws onto the base.
_GENRE_BG_DIR = os.path.join(BASE_DIR, "static", "genre_bg")
# Two interchangeable fallback-background sets, chosen per request via
# fallback_bg_style: "minimal" (procedural textured) or "photoreal" (hand-made
# photographic art that blends with real posters).
_GENRE_BG_STYLES = ("minimal", "photoreal")
# Bounded LRU, keyed "style/genre", holding decoded RGBA at canvas size.
#
# Both bounds matter.  Decoded, the full set is ~172 MB (the photoreal art ships
# at 1024x1536, 6 MB each as RGBA), and it used to be loaded in full at startup
# and held forever — a permanent cost for a path that only fires when a title has
# no usable art at all.  Capping the cache keeps the resident set to the handful
# of genres a given library actually hits; entries are cheap to reload (one PNG
# decode) on the rare miss.
_GENRE_BG_CACHE_MAX = 8
_genre_bg_cache: "OrderedDict[str, Image.Image | None]" = OrderedDict()


def _genre_bg_path(style: str, name: str) -> "str | None":
    """Filesystem path to a genre-background PNG, or None if it doesn't exist."""
    p = os.path.join(_GENRE_BG_DIR, style, f"{name}.png")
    return p if os.path.exists(p) else None


def _load_genre_background(genre: str, style: str = "minimal") -> "Image.Image | None":
    """Return a fresh RGBA copy of the genre fallback background for *style*, or
    None if none exists.  A missing image degrades gracefully: the style's
    default.png → the minimal set's genre/default → None (caller then renders the
    procedural gradient canvas).  So selecting a not-yet-populated style never
    breaks — it just falls back to minimal.

    The returned canvas is always POSTER_WIDTH x POSTER_HEIGHT.  build_poster
    takes its geometry from the canvas it is handed, so returning the photoreal
    art at its native 1024x1536 made those fallbacks render at a different size
    from every other poster — and paid a 4x encode for the privilege."""
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    key = f"{style}/{genre}"
    if key in _genre_bg_cache:
        _genre_bg_cache.move_to_end(key)
    else:
        path = (
            _genre_bg_path(style, genre)
            or _genre_bg_path(style, "default")
            or (_genre_bg_path("minimal", genre) if style != "minimal" else None)
            or _genre_bg_path("minimal", "default")
        )
        try:
            _genre_bg_cache[key] = (
                _normalise_fallback_canvas(Image.open(path)) if path else None
            )
        except Exception:
            _genre_bg_cache[key] = None
        while len(_genre_bg_cache) > _GENRE_BG_CACHE_MAX:
            _evicted = _genre_bg_cache.popitem(last=False)[1]
            if _evicted is not None:
                _evicted.close()
    base = _genre_bg_cache[key]
    return base.copy() if base is not None else None


def _normalise_fallback_canvas(image: Image.Image) -> Image.Image:
    """Fit-cover a fallback background to the poster canvas, as RGBA.

    Fit-cover rather than a plain resize so a background authored at some other
    aspect ratio is centre-cropped instead of squashed.  The shipped art is
    already 2:3, for which this is just the resize."""
    target_w, target_h = _cfg.POSTER_WIDTH, _cfg.POSTER_HEIGHT
    src_w, src_h = image.size
    if (src_w, src_h) != (target_w, target_h):
        scale = max(target_w / src_w, target_h / src_h)
        new_w, new_h = round(src_w * scale), round(src_h * scale)
        image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left, top = round((new_w - target_w) / 2), round((new_h - target_h) / 2)
        image = image.crop((left, top, left + target_w, top + target_h))
    return image.convert("RGBA")


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def remove_server_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["server"] = "unknown"
    return response


# ---------------------------------------------------------------------------
# Server capability endpoint
# ---------------------------------------------------------------------------

# Parameters the configurator must always send, whatever their value.  The
# first four are identity and authentication rather than render settings, and
# primary_client is the one setting that *changes other defaults* — the client
# profile picks the two edge insets, so the defaults below are only meaningful
# alongside it.
_NEVER_OMITTED_PARAMS = frozenset({
    "tmdb_id", "imdb_id", "type", "stremio_id", "anilist_id", "kitsu_id",
    "access_key", "tmdb_key", "mdblist_key", "primary_client", "quality",
    "bar_bottom_inset", "sash_badge_inset",
})


def _render_param_defaults() -> dict:
    """Every render setting's default value, keyed by its query-parameter name.

    Read straight off a freshly built RequestConfig rather than restated here,
    because the whole point is that the configurator can drop a parameter it
    knows the server would have chosen anyway — and a default duplicated in
    JavaScript is a default that drifts.  When it drifts, the configurator omits
    a parameter believing it is the default, the server picks something else,
    and the poster silently changes.

    The two client-profile insets are excluded: their default depends on
    primary_client, so there is no single answer to publish.
    """
    cfg = RequestConfig()
    defaults: dict = {}
    for spec in dataclasses.fields(cfg):
        if spec.name in _NEVER_OMITTED_PARAMS:
            continue
        value = getattr(cfg, spec.name)
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            defaults[spec.name] = value
    return defaults


@app.get("/server-caps")
async def server_caps(access_key: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    next_refresh_hours = None
    if _cfg.TRENDING_FETCH_TIME:
        import zoneinfo
        from datetime import datetime, timedelta
        try:
            tz = zoneinfo.ZoneInfo(_cfg.TRENDING_FETCH_TIMEZONE)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")
        try:
            h, m = map(int, _cfg.TRENDING_FETCH_TIME.split(':'))
            now_dt = datetime.now(tz)
            target_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if target_dt <= now_dt:
                target_dt += timedelta(days=1)
            next_refresh_hours = round((target_dt - now_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

    return {
        "tmdb_key_set":          bool(_cfg.SERVER_TMDB_KEY),
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEYS),
        "mdblist_key_count":     len(_cfg.SERVER_MDBLIST_KEYS),
        "aiostreams_configured": bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH),
        "quality_source":        active_quality_source(),
        "quality_configured":    quality_source_configured(),
        "trending_fetch_count":  _cfg.TRENDING_FETCH_COUNT,
        "trending_fetch_time":   _cfg.TRENDING_FETCH_TIME,
        "trending_fetch_timezone": _cfg.TRENDING_FETCH_TIMEZONE,
        "trending_next_refresh_hours": next_refresh_hours,
        # Lets the configurator leave out any parameter already at its default.
        # A generated URL was running ~1500 characters, most of it restating
        # defaults, against metadata clients that truncate at 2000.
        "param_defaults":        _render_param_defaults(),
        "never_omitted_params":  sorted(_NEVER_OMITTED_PARAMS),
        "sash_priority_default": list(_cfg.SASH_PRIORITY),
        "sash_priority_diff_seed": _SASH_DIFF_SEED,
        "imdb_dataset_enabled":  imdb_dataset.is_enabled(),
        "imdb_dataset_titles":   imdb_dataset.row_count(),
    }


# ---------------------------------------------------------------------------
# Configurator HTML
# ---------------------------------------------------------------------------

_configurator_html: str | None = None
# Strong ETag for the configurator HTML — short hash of its bytes so the
# browser can revalidate cheaply.  Without this, browsers heuristically
# cache the page and keep serving stale HTML after a container rebuild,
# which is what made sliders / dropdowns drift out of sync with the new
# defaults until a manual Reset.
_configurator_etag: str | None = None
# "3": photoreal genre fallback backgrounds now render at the poster canvas size
#      rather than their native 1024x1536, so previously cached oversized
#      composites must be re-rendered.
# "4": the tinted vignette no longer frosts posters with confirmed burned-in
#      text, so composites cached with a blurred-over title must be re-rendered.
# "5": mode 6 adds a tier-coloured bookmark at the poster top-left corner.
# "6": the mode 6 bookmark is redrawn with rounded tips and a curved inner edge,
#      so composites cached with the old hard-edged triangle look stale.
_RENDER_CACHE_VERSION = "6"
_render_assets_signature = "startup"


def _compute_render_assets_signature() -> str:
    digest = hashlib.sha256()
    roots = (
        os.path.join(BASE_DIR, "languages"),
        os.path.join(BASE_DIR, "static", "genre_bg"),
    )
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                digest.update(os.path.relpath(path, BASE_DIR).encode())
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    override_path = os.environ.get(
        "DISCOVERY_OVERRIDES_PATH", "/app/cache/discovery_overrides.json"
    )
    try:
        with open(override_path, "rb") as override_file:
            digest.update(override_file.read())
    except OSError:
        pass
    return digest.hexdigest()[:16]


def _server_render_signature() -> str:
    return "|".join((
        f"render={_RENDER_CACHE_VERSION}",
        f"format={_cfg.IMAGE_FORMAT}",
        # Include both quality knobs so a change to either busts the render
        # cache regardless of which format is currently active.
        f"jpeg={_cfg.JPEG_QUALITY}",
        f"webp={_cfg.WEBP_QUALITY}",
        f"contrast={int(_cfg.LOGO_CONTRAST_RESCUE)}",
        f"stretch={int(_cfg.LOGO_STRETCH_DISABLED)}:{_cfg.LOGO_STRETCH_FACTOR:g}",
        f"assets={_render_assets_signature}",
    ))


def _load_configurator_html() -> str:
    global _configurator_etag
    html_path = os.path.join(os.path.dirname(__file__), "configurator.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = content.replace("{{TRENDING_FETCH_COUNT}}", str(_cfg.TRENDING_FETCH_COUNT))
        content = content.replace("{{TRENDING_FETCH_COUNT_PLUS_ONE}}", str(_cfg.TRENDING_FETCH_COUNT + 1))
        content = content.replace("{{TRENDING_BROAD_FETCH_COUNT}}", str(_cfg.TRENDING_BROAD_FETCH_COUNT))

        _configurator_etag = '"' + hashlib.md5(content.encode("utf-8")).hexdigest()[:16] + '"'
        return content
    except FileNotFoundError:
        _configurator_etag = '"missing"'
        return "<h1>Configurator not found</h1><p>Place configurator.html alongside main.py</p>"


@app.get("/health")
async def health_check():
    """Lightweight liveness probe — no auth required, used by Docker healthcheck."""
    return {"status": "ok"}


@app.get("/stats")
async def stats(access_key: str = ""):
    """
    Operator diagnostics: cache row counts / sizes plus live runtime state
    (in-flight renders, background quality fetches, MDBList key cooldowns).
    Gated behind the access key when one is configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    now = asyncio.get_running_loop().time()
    keys = _cfg.SERVER_MDBLIST_KEYS
    mdblist_keys = []
    for i, k in enumerate(keys):
        cd = _mdblist_key_cooldown.get(k, 0.0)
        mdblist_keys.append({
            "index":         i + 1,
            "active":        i == (_mdblist_active_key_idx % len(keys)),
            "cooling_down":  now < cd,
            "cooldown_secs": max(0, round(cd - now)),
        })

    return {
        "cache":   get_cache_stats(),
        # Surfaced here rather than only on /server-caps because a failed or
        # silently stale dataset refresh is otherwise invisible outside the
        # container logs.
        "imdb_dataset": imdb_dataset.status(),
        "runtime": {
            "renders_in_flight":        len(_render_inflight),
            "quality_fetches_in_flight": len(_quality_bg_inflight),
            "quality_source_backoff_secs": round(_quality_backoff_remaining(now)),
            "rating_fetches_in_flight":  len(_rating_fetch_inflight),
            "rating_backoff_titles":     len({imdb_id for imdb_id, _ in _rating_backoff}),
            "rating_backoff_entries":    len(_rating_backoff),
            # Should track the line above: a persistent gap means counters are
            # outliving their back-off entries again.
            "rating_fail_counters":      len(_rating_fail_count),
            "mdblist_keys":              mdblist_keys,
            "composite_cache_disabled":  _cfg.DISABLE_COMPOSITE_CACHE,
            "svg_logo_support":          svg_logo_supported(),
        },
    }


# TMDB genre name → id, used only by the debug canvas preview below.
_DEBUG_GENRE_IDS = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Family": 10751, "Fantasy": 14, "History": 36,
    "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749,
    "Sci-Fi": 878, "Thriller": 53, "War": 10752, "Western": 37,
}
_DEBUG_CANVAS_TTL = 300.0
_DEBUG_CANVAS_MAX_ENTRIES = 128
_debug_canvas_cache: dict[tuple[str, str, str, str, str], tuple[float, bytes]] = {}


@app.get("/debug/canvas")
async def debug_canvas(genre: str = "Action", title: str = "Sample Title",
                       style: str = "minimal", year: str = "2024",
                       score: str = "84", access_key: str = ""):
    """
    Render a no-art fallback card exactly as a poster-less title would: the genre
    fallback background (minimal or photoreal set) with the genre-aware title and
    the usual rating label composited on top.  Lets you eyeball any genre/style
    without hunting for a title that happens to lack poster art.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Title too long")
    cache_key = (genre, title, style, year, score)
    now = asyncio.get_running_loop().time()
    cached = _debug_canvas_cache.get(cache_key)
    if cached is not None and now - cached[0] <= _DEBUG_CANVAS_TTL:
        return Response(
            content=cached[1], media_type=f"image/{_cfg.IMAGE_FORMAT}",
            headers={"Cache-Control": "private, max-age=300"},
        )
    gid = _DEBUG_GENRE_IDS.get(genre)
    canvas = _load_genre_background(genre, style)
    if canvas is None:
        canvas = _make_fallback_canvas([gid] if gid else None).convert("RGBA")
    cfg = RequestConfig()
    _score = int(score) if score.isdigit() else "—"
    img = build_poster(canvas, _score, genre, cfg, fallback_title=title,
                       release_year=(year or None), no_poster=True)
    buf = io.BytesIO()
    _quality = _cfg.WEBP_QUALITY if _cfg.IMAGE_FORMAT == "webp" else _cfg.JPEG_QUALITY
    img.convert("RGB").save(buf, format=_cfg.IMAGE_FORMAT.upper(), quality=_quality)
    data = buf.getvalue()
    if len(_debug_canvas_cache) >= _DEBUG_CANVAS_MAX_ENTRIES:
        oldest = min(_debug_canvas_cache, key=lambda key: _debug_canvas_cache[key][0])
        _debug_canvas_cache.pop(oldest, None)
    return Response(
        content=data, media_type=f"image/{_cfg.IMAGE_FORMAT}",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/debug/fallback-gallery", response_class=HTMLResponse)
async def fallback_gallery(style: str = "minimal", access_key: str = ""):
    """
    Self-contained gallery of every genre's no-art fallback card (live
    /debug/canvas renders), so an operator can review the fallback backgrounds +
    genre fonts at a glance and compare the minimal vs photoreal sets.  Gated
    behind the access key when configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    _ak = f"&access_key={access_key}" if access_key else ""

    # Every genre that has a background (covers the full genre map + any future
    # additions), derived from the minimal set so the gallery is never stale.
    try:
        _genres = sorted(
            f[:-4] for f in os.listdir(os.path.join(_GENRE_BG_DIR, "minimal"))
            if f.lower().endswith(".png") and f[:-4].lower() != "default"
        )
    except OSError:
        _genres = sorted(_DEBUG_GENRE_IDS)

    tiles = "".join(
        f'<figure><img loading="lazy" src="/debug/canvas?genre={g}'
        f'&title={g.replace(" ", "+")}&style={style}{_ak}" alt="{g}">'
        f'<figcaption>{g}</figcaption></figure>'
        for g in _genres
    )
    _tabs = "".join(
        f'<a class="{"on" if s == style else ""}" '
        f'href="/debug/fallback-gallery?style={s}{_ak}">{s.capitalize()}</a>'
        for s in _GENRE_BG_STYLES
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fallback art preview</title>
<style>
  body {{ margin:0; background:#0e0e10; color:#e8e8ea;
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:18px 20px; border-bottom:1px solid #2a2a2e;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
  h1 {{ font-size:18px; margin:0; }} p {{ color:#9a9aa0; margin:0; font-size:13px; }}
  .tabs a {{ display:inline-block; padding:5px 12px; margin-right:6px; border-radius:8px;
            font-size:13px; text-decoration:none; color:#c7c7cc; background:#1c1c20;
            border:1px solid #2a2a2e; }}
  .tabs a.on {{ background:#3a3a44; color:#fff; border-color:#4a4a56; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
           gap:16px; padding:20px; }}
  figure {{ margin:0; }}
  img {{ width:100%; border-radius:10px; display:block; background:#1a1a1d; }}
  figcaption {{ text-align:center; padding-top:8px; font-size:13px; color:#c7c7cc; }}
</style></head><body>
<header>
  <h1>Fallback art preview</h1>
  <div class="tabs">{_tabs}</div>
  <p>Live no-art render for every genre — {len(_genres)} genres, "{style}" set.</p>
</header>
<div class="grid">{tiles}</div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def get_configurator(request: Request, access_key: str = "", reload: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    # ?reload=1 re-reads configurator.html from disk — useful while iterating on
    # the UI without restarting the container.  Gated on the access key so it's
    # not a public DoS vector via disk re-reads.
    global _configurator_html
    if reload:
        _configurator_html = _load_configurator_html()
        logger.info("Configurator HTML reloaded from disk")

    if _configurator_html is None:
        _load_configurator_html()  # populates the global

    # 304 short-circuit when the browser's cached copy still matches —
    # saves the 130 KB body re-download on every navigation while still
    # forcing a fresh fetch as soon as the file's contents change.
    _cache_headers = {
        "Cache-Control": "no-cache, must-revalidate",
        "ETag":          _configurator_etag or '""',
    }
    if (
        _configurator_etag
        and request.headers.get("if-none-match") == _configurator_etag
    ):
        return Response(status_code=304, headers=_cache_headers)

    return HTMLResponse(
        content=_configurator_html or _load_configurator_html(),
        headers=_cache_headers,
    )


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@app.get("/search")
async def search_proxy(
    q: str,
    tmdb_key: str = "",
    access_key: str = "",
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long")

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(
        "https://api.themoviedb.org/3/search/multi",
        params={
            "api_key": effective_key,
            "query": q,
            "include_adult": "false",
            "page": "1",
        },
    )
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


@app.get("/resolve-imdb")
async def resolve_imdb(
    tmdb_id: str,
    type: str = "movie",
    tmdb_key: str = "",
    access_key: str = "",
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    _check_tmdb_id(tmdb_id)
    _check_type(type)

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    endpoint = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids"
        if type == "tv"
        else f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids"
    )

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(endpoint, params={"api_key": effective_key})
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Logo endpoint
# ---------------------------------------------------------------------------

@app.get("/logo")
async def get_logo(
    tmdb_id: str,
    type: str = "movie",
    lang: str = "en",
    imdb_id: str | None = None,
    access_key: str = "",
    tmdb_key: str = "",
):
    """
    Return the best available logo PNG for a title.

    Checks the local file cache first (same cache the poster endpoint uses),
    then falls through to TMDB and Metahub as needed.  No rendering is applied —
    callers receive the original PNG exactly as stored.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")

    effective_tmdb_key = _resolve_tmdb_key((tmdb_key or "").strip())
    if not effective_tmdb_key:
        raise HTTPException(status_code=503, detail="No TMDB API key configured")
    media_type = "tv" if type in ("tv", "series") else "movie"
    effective_lang = (lang or "en").strip() or "en"

    client = _HTTP_CLIENT

    _, _, logos, _, _, _, _, tmdb_data = await _coalesced_fetch_poster_metadata(
        client, tmdb_id, effective_tmdb_key, media_type, effective_lang
    )

    # Use imdb_id from metadata if not supplied — needed for Metahub fallback
    effective_imdb_id = (imdb_id or "").strip() or tmdb_data.get("imdb_id") or None
    original_language = tmdb_data.get("original_language")

    logo_image = await fetch_logo(
        client, logos, effective_lang,
        imdb_id=effective_imdb_id,
        original_language=original_language,
    )

    if logo_image is None:
        raise HTTPException(status_code=404, detail="No logo available")

    buf = io.BytesIO()
    logo_image.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=2592000"},
    )


# ---------------------------------------------------------------------------
# Poster endpoint
# ---------------------------------------------------------------------------

def _apply_poster_cache_headers(
    response: Response,
    final_cache_key: str | None,
    provisional: bool,
    expires_at: int | None = None,
) -> None:
    """Attach the validator and freshness headers a poster response has earned.

    *provisional* marks a render the pipeline itself declined to keep: quality
    is still being fetched, OCR is queued, MDBlist just failed.  Leaving those
    out of the composite cache only helps if the *client* comes back for the
    finished render — and with an ETag attached it doesn't.  The composite key
    is built from ids and render params alone, so the badge-less bytes and the
    finished poster validate against the same value: a client that cached the
    provisional one revalidates, gets a 304 off the now-complete composite, and
    keeps the badge-less image indefinitely.  That is why quality appeared never
    to arrive until a setting change rewrote the URL and minted a new key.

    A provisional render therefore ships no validator and asks not to be stored.

    *expires_at* is the composite's own deadline, which caps the advertised
    max-age so a cached copy never outlives the facts baked into it.
    """
    if provisional:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return

    if final_cache_key is not None:
        response.headers["ETag"] = f'"{final_cache_key}"'
    if _cfg.DISABLE_COMPOSITE_CACHE:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return

    remaining = None
    if expires_at is not None:
        remaining = max(0, int(expires_at) - int(time.time()))

    if _cfg.CDN_CACHE_TTL_AUTO:
        max_age = remaining
    elif _cfg.CDN_CACHE_TTL > 0:
        max_age = _cfg.CDN_CACHE_TTL if remaining is None else min(_cfg.CDN_CACHE_TTL, remaining)
    else:
        max_age = None

    if max_age:
        response.headers["Cache-Control"] = f"public, max-age={max_age}"
    elif max_age == 0:
        response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"


@app.get("/poster")
async def get_poster(
    request: Request,
    tmdb_id: str = "",
    imdb_id: str = "",
    anilist_id: str = "",
    kitsu_id: str = "",
    stremio_id: str = "",
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    access_key: str = "",
    mdblist_key: str = "",
    tmdb_key: str = "",
    show_award_sash: str | None = None,
    badge_display_mode: str | None = None,
    show_quality_badges: str | None = None,
    rating_display_mode: str | None = None,
    accent_bar_font_size_ratio: str | None = None,
    numeric_score_font_size_ratio: str | None = None,
    accent_bar_y_offset: str | None = None,
    numeric_score_y_offset: str | None = None,
    minimalist_mode_font_size_ratio: str | None = None,
    minimalist_mode_font_x_offset: str | None = None,
    minimalist_mode_font_y_offset: str | None = None,
    score_glow_threshold: str | None = None,
    score_glow_blur: str | None = None,
    score_glow_alpha: str | None = None,
    logo_max_w_ratio: str | None = None,
    logo_max_h_ratio: str | None = None,
    logo_bottom_ratio: str | None = None,
    badge_height: str | None = None,
    badge_gap: str | None = None,
    badge_anchor_x: str | None = None,
    badge_anchor_y: str | None = None,
    movie_weights: str | None = None,
    tv_weights: str | None = None,
    logo_language: str | None = None,
    sash_priority: str | None = None,
    muted: str | None = None,
    textless: str | None = None,
    score_color_mode: str | None = None,
    shape: str | None = None,
    landscape_art: str | None = None,
    badge_pos: str | None = None,
    debug: str | None = None,
    nocache: str | None = None,
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized, your access key is not valid for this instance.")

    _check_type(type)

    # Done before anything reads imdb_id — the composite cache key is built from
    # it further down, and a literal "{imdb_id?}" baked into cache keys would
    # fragment the cache per client build.
    imdb_id = _normalise_optional_id(imdb_id, "imdb_id")

    # AIOMetadata's "{id}" is the raw Stremio meta id, and for an ordinary title
    # that IS the IMDb id ("tt0903747"). Generated templates no longer send
    # imdb_id — a required placeholder with no value nulls the whole url — so
    # when a template carries {id} and nothing else identifies the title to
    # IMDb, take it from there. Request-supplied and available before any
    # metadata fetch, so unlike an id discovered from TMDB later it is safe to
    # key the cache on: it makes the row shared with every other client that
    # sends an IMDb id, rather than a second row under the tmdb: form.
    #
    # Parsed here rather than in _resolve_anime_request because that returns
    # early when anime sources are disabled, and this is not an anime concern.
    if not imdb_id and stremio_id:
        _stremio_hint = stremio_id.strip()
        if _IMDB_ID_RE.match(_stremio_hint):
            imdb_id = _stremio_hint

    # -----------------------------------------------------------------------
    # Anime-native ids (AniList / Kitsu).
    #
    # A client that can supply one — AIOMetadata and similar advanced metadata
    # providers — gets art, titles, genres and a community score straight from
    # the anime provider, with no id conversion anywhere. Clients that only
    # speak imdb/tmdb/tvdb pass neither param and take the unchanged TMDB path.
    #
    # The anime id governs the ART and the metadata spine only. AIOMetadata
    # sends tmdb_id and imdb_id alongside it whenever it has them, and those are
    # worth keeping: MDBList ratings, awards, keywords, age rating and digital
    # release are all IMDb-keyed, and trending and movie release status are
    # TMDB-keyed. Discarding them leaves the info sash with nothing to say
    # beyond the foreign-language label. So enrichment still runs on whatever
    # ids the client supplied; only the art comes from the anime provider.
    #
    # `canonical_id` is the identity for the rating cache table: the IMDb id
    # when there is one (so the row is shared with the ordinary path), else the
    # "<namespace>:<id>" anime form, which can't collide with a bare TMDB id or
    # a tt-prefixed IMDb id. See _canonical_rating_id(); the stream id sent to
    # quality sources is resolved separately, after metadata.
    # -----------------------------------------------------------------------
    anime_namespace, anime_id = _resolve_anime_request(anilist_id, kitsu_id, stremio_id)
    is_anime = anime_namespace is not None
    anime_key = anime.namespaced_id(anime_namespace, anime_id) if is_anime else ""

    if is_anime:
        # Both are optional on this path, but must still be well-formed if sent.
        if imdb_id:
            _check_imdb_id(imdb_id)
        if tmdb_id:
            _check_tmdb_id(tmdb_id)
        has_tmdb_id = bool(tmdb_id)
        # Downstream art fetching, log lines and detection keys are written in
        # terms of tmdb_id. Keep the real one when supplied (better cache keys,
        # and it re-enables the TMDB-only lookups); otherwise stand in the
        # anime id so those paths keep working without a second id threaded
        # through them.
        if not tmdb_id:
            tmdb_id = anime_key
    else:
        # tmdb_id is the one required identity: it selects the artwork and the
        # metadata spine, and nothing renders without it. imdb_id is optional
        # enrichment.
        #
        # It used to be required too, which meant a title TMDB has no IMDb link
        # for — TMDB returns imdb_id: null for these — could not be rendered at
        # all, and the 400 named a parameter the caller had no way to supply.
        # Handing the empty string to the format checks was worse still: it
        # reported a missing id as malformed, and named whichever param happened
        # to be checked first.
        if not tmdb_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Missing required parameter: tmdb_id. /poster needs tmdb_id — "
                    "it selects the artwork and metadata. imdb_id is optional; "
                    "supplying it adds the IMDb-keyed enrichment (Metahub logo "
                    "fallback, digital-release detection, stream-quality badges)."
                ),
            )
        _check_tmdb_id(tmdb_id)
        if imdb_id:
            _check_imdb_id(imdb_id)
        has_tmdb_id = True

    canonical_id = _canonical_rating_id(imdb_id, anime_key, tmdb_id)

    # The MDBList lookup route — what we ask upstream, as opposed to what we key
    # the cache on. MDBList serves the same record under /imdb/… and /tmdb/…, so
    # a title with no IMDb id still has ratings, awards, keywords and an age
    # rating available; it just has to be asked for by its TMDB id.
    #
    # Anime is deliberately excluded from the TMDB route: those titles take their
    # score, genre and age rating from the anime provider, and routing them to
    # MDBList as well would start putting awards and festival sashes on a whole
    # catalogue that has never had them. That is a rendering change to make on
    # purpose, not a side effect of this one.
    if imdb_id:
        rating_provider, rating_media_id = "imdb", imdb_id
    elif has_tmdb_id and not is_anime:
        rating_provider, rating_media_id = "tmdb", tmdb_id
    else:
        rating_provider, rating_media_id = None, None

    # -----------------------------------------------------------------------
    # Single-user mode: check for a cached final poster first.
    # The cache key includes imdb_id and type; quality is intentionally
    # excluded because in single-user mode the quality tokens come from
    # AIOStreams (not from query params) and are themselves cached per-title.
    # If the caller passes an explicit quality= override this bypass is
    # skipped so they always get the exact poster they asked for.
    # -----------------------------------------------------------------------
    effective_tmdb_key    = _resolve_tmdb_key(tmdb_key)
    effective_mdblist_key = _resolve_mdblist_key(mdblist_key)

    # Only null the key when the request has no MDBList-resolvable identity at
    # all — that makes every downstream MDBList gate (cooldown, back-off,
    # coalescing, the fetch itself) skip naturally rather than needing a branch
    # at each one.
    #
    # This used to trigger on a missing IMDb id, which quietly made the lookup
    # unreachable for TMDB-only titles no matter how the fetch itself was
    # routed. It is now the absence of a route: an anime request that carries
    # neither an IMDb id nor a TMDB id. When AIOMetadata does send an IMDb id
    # alongside the anime id, the normal fetch runs and the provider score is
    # merged into its result, so the sash gets awards, keywords and an age
    # rating too.
    if rating_media_id is None:
        effective_mdblist_key = None

    # An anime request gets its art and metadata from the provider, so a TMDB
    # key is optional there even when a tmdb_id is supplied — it only unlocks
    # the extra TMDB-keyed lookups (trending, movie release status).
    if not effective_tmdb_key and not is_anime:
        raise HTTPException(
            status_code=400,
            detail=(
                "No TMDB API key available. Either provide tmdb_key= as a query parameter "
                "or configure the TMDB_API_KEY environment variable on the server."
            ),
        )

    raw_params = {
        k: v for k, v in request.query_params.items()
        if k not in (
            "tmdb_id", "imdb_id", "anilist_id", "kitsu_id", "stremio_id",
            # Not art sources here (MAL needs auth, AniDB's API is heavily
            # restricted), but AIOMetadata templates carry the full placeholder
            # set. Excluded so their presence — substituted or not — can't
            # fragment the composite cache key across otherwise identical
            # requests.
            "mal_id", "anidb_id",
            "mdblist_key", "tmdb_key", "type",
            "quality", "season", "episode", "access_key", "debug", "nocache",
        )
    }
    rcfg = build_request_config(raw_params)

    # Anime is essentially always Japanese, so the foreign-language slot says
    # nothing here — but ranked highly (a reasonable choice for live-action,
    # where it is a real signal) it would mask every sash below it on every
    # anime title. Demote it to last rather than dropping it, so a title with
    # nothing else to say still gets a label; a user who removed the slot
    # entirely keeps it removed.
    #
    # Applied to rcfg itself rather than at the pick_sash call sites because
    # build_poster derives its own ordering from cfg.sash_priority, and that is
    # the one that ends up on the rendered poster.
    if is_anime and "foreign" in rcfg.sash_priority:
        rcfg.sash_priority = (
            [s for s in rcfg.sash_priority if s != "foreign"] + ["foreign"]
        )

    # Operator force-refresh: ?nocache=1 skips the composite cache READ so a fresh
    # render is produced (and re-cached), letting an operator invalidate a single
    # title without flushing the whole cache.  Only honoured when an ACCESS_KEY is
    # configured (and therefore already validated above) so open instances can't
    # be made to burn CPU on forced re-renders.
    _force_refresh = bool(
        nocache and nocache.strip().lower() in ("1", "true", "yes") and _cfg.ACCESS_KEY
    )

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if not quality and not _cfg.DISABLE_COMPOSITE_CACHE:
        # Server-side detection settings affect the rendered output but aren't URL
        # params, so fold a signature into the hash.  Toggling detection or
        # changing its thresholds then auto-busts stale composites (and leaves
        # cache keys unchanged when the feature is off — backward compatible).
        if _cfg.TEXTLESS_TEXT_DETECTION:
            from text_detect import DETECT_RES_SIG
            _detect_sig = (
                f"|td={_cfg.PPOCR_BOX_THRESHOLD}:{_cfg.TEXTLESS_DETECTION_MAX_VOTES}:{DETECT_RES_SIG}"
            )
        else:
            _detect_sig = ""
        _poster_selection_sig = (
            f"|ps={_cfg.TMDB_POSTER_MIN_VOTES}:"
            f"{_cfg.TMDB_POSTER_MAX_SCORE_DROP:g}"
        )
        _rating_policy_sig = (
            f"|rp={_cfg.RATING_MIN_VOTES}:"
            f"{int(rcfg.fallback_to_imdb)}"
        )
        # The IMDb dataset changes the rendered score exactly the way
        # RATING_MIN_VOTES does, so flipping IMDB_DATASET_ENABLED has to bust
        # composites the same way. Appended only when the feature is on, so
        # instances that never enable it keep every existing cache entry.
        #
        # is_ready() rather than is_enabled(): on the first-ever start the
        # feature is on but the table is empty until the download lands, and
        # posters rendered in that window would otherwise be cached at "N/A"
        # for the full composite TTL.
        _dataset_sig = (
            f"|imdbds={int(imdb_dataset.is_ready())}:{_cfg.IMDB_DATASET_MIN_VOTES}"
            if imdb_dataset.is_enabled()
            else ""
        )
        _server_sig = "|server=" + _server_render_signature()
        _params_hash = hashlib.sha256(
            (
                "&".join(f"{k}={v}" for k, v in sorted(raw_params.items()))
                + _detect_sig
                + _poster_selection_sig
                + _rating_policy_sig
                + _dataset_sig
                + _server_sig
            ).encode()
        ).hexdigest()[:16]
        # The anime key has to be part of this: the same imdb/tmdb pair renders
        # different art depending on whether an anime id came with it, so the
        # two must not share a composite cache entry.
        # Non-anime uses canonical_id rather than the raw imdb_id so a TMDB-only
        # title gets "tmdb:1698026:…" instead of a leading empty segment. For a
        # title that has an IMDb id the two are the same string, so existing
        # cache entries stay valid.
        final_cache_key = (
            f"{anime_key}:{imdb_id}:{tmdb_id}:{type}:{_params_hash}"
            if is_anime
            else f"{canonical_id}:{tmdb_id}:{type}:{_params_hash}"
        )
        _cached_entry = None if _force_refresh else get_cached_final_poster_entry(final_cache_key)
        if _force_refresh:
            logger.info(f"Force refresh (nocache) for {final_cache_key} — bypassing cache read")
        if _cached_entry is not None:
            cached_jpeg, _cached_expires_at = _cached_entry
            logger.info(f"Final poster cache hit for {final_cache_key}")
            etag = f'"{final_cache_key}"'
            if request.headers.get("if-none-match") == etag:
                _304 = Response(status_code=304)
                _apply_poster_cache_headers(_304, final_cache_key, False, _cached_expires_at)
                return _304
            _hit_resp = Response(content=cached_jpeg, media_type=f"image/{_cfg.IMAGE_FORMAT}")
            # Only a finished render is ever written to the composite cache, so
            # a cache hit is never provisional.
            _apply_poster_cache_headers(_hit_resp, final_cache_key, False, _cached_expires_at)
            return _hit_resp
    else:
        final_cache_key = None

    # ------------------------------------------------------------------
    # Request coalescing: if another request in this worker is already
    # rendering the same poster, await its result instead of duplicating
    # the pipeline.  Quality-override requests (final_cache_key=None) are
    # always rendered independently.
    # ------------------------------------------------------------------
    _render_fut: "asyncio.Future[tuple[bytes, bool, int | None]] | None" = None
    if final_cache_key is not None:
        _existing_fut = _render_inflight.get(final_cache_key)
        if _existing_fut is not None:
            logger.info(f"Coalescing request for {final_cache_key}")
            try:
                # The render we rode on decides our headers too: riding on a
                # provisional one and then stamping an ETag would cache exactly
                # the poster it was withheld to avoid.
                _coal_bytes, _coal_provisional, _coal_expires_at = await _existing_fut
                _coal_resp = Response(content=_coal_bytes, media_type=f"image/{_cfg.IMAGE_FORMAT}")
                _apply_poster_cache_headers(
                    _coal_resp, final_cache_key, _coal_provisional, _coal_expires_at
                )
                return _coal_resp
            except Exception:
                # The in-flight render failed; fall through and try ourselves.
                pass
        _render_fut = asyncio.get_running_loop().create_future()
        # Suppress asyncio's "Future exception was never retrieved" warning when
        # the render fails and no other request is coalesced onto this future.
        _render_fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() and f.exception() else None
        )
        _render_inflight[final_cache_key] = _render_fut

    # Declare globals that are both read and written in this function so Python
    # doesn't complain about use-before-global-declaration.
    global _mdblist_active_key_idx

    cached_rating = get_cached_rating(canonical_id)

    if cached_rating is not None:
        (
            cached_ratings_dict,
            cached_genre,
            cached_release_date,
            cached_award_wins,
            cached_award_noms,
            cached_awards_fetched,
            cached_festival_keyword,
            cached_age_rating,
            cached_is_cult,
            cached_is_true_story,
            cached_is_metacritic,
        ) = cached_rating
    else:
        cached_ratings_dict     = None
        cached_genre            = None
        cached_release_date     = None
        cached_award_wins       = []
        cached_award_noms       = []
        cached_awards_fetched   = False
        cached_festival_keyword = None
        cached_age_rating       = None
        cached_is_cult          = False
        cached_is_true_story    = False
        cached_is_metacritic    = False

    release_date_for_quality_ttl = cached_release_date
    rating_already_cached        = cached_rating is not None

    # ------------------------------------------------------------------
    # Rating fetch coalescing + back-off
    #
    # Goal: ensure at most one MDBList call per canonical_id per worker at a
    # time, and suppress repeated failures with key-scoped cooldowns.
    #
    # Back-off check: if a recent fetch failed, skip that title-key pair
    # until its escalating retry delay expires.
    #
    # Coalescing: if another coroutine in this worker is already fetching
    # the same canonical_id, wait for its asyncio.Event, then re-read the DB.
    # If it succeeded we get the cached data for free; if it failed we
    # re-check the back-off (now set by the other coroutine) before
    # deciding whether to attempt our own call.
    # ------------------------------------------------------------------
    _rating_event_to_set: asyncio.Event | None = None
    _rating_backoff_active = False  # set when backoff nullifies the key; used to suppress final-poster caching
    _mdblist_unavailable_reason = "no API key configured"

    if not rating_already_cached and effective_mdblist_key:
        _loop_now = asyncio.get_running_loop().time()

        # Per-key cooldown: configured server keys may rotate; request-supplied
        # keys remain isolated and simply wait for their own cooldown to expire.
        if effective_mdblist_key and _loop_now < _mdblist_key_cooldown.get(effective_mdblist_key, 0.0):
            _cooling_key = effective_mdblist_key
            _replacement = _next_mdblist_server_key(_cooling_key, _loop_now)
            if _replacement is not None:
                effective_mdblist_key = _replacement
                logger.info(
                    f"MDBList key rotated to key #{_mdblist_active_key_idx + 1} for {canonical_id}"
                )
            else:
                _remaining = _mdblist_key_cooldown.get(_cooling_key, 0.0) - _loop_now
                logger.debug(
                    f"Rating fetch for {canonical_id} skipped "
                    f"(selected MDBList key cooling down; {_remaining:.0f}s remaining)"
                )
                effective_mdblist_key = None
                _rating_backoff_active = True
                _mdblist_unavailable_reason = "selected key is cooling down"

        # Per-title and key backoff (network failures, or this title-key pair's last 429).
        if effective_mdblist_key:
            _retry_key = _rating_retry_key(canonical_id, effective_mdblist_key)
            _backoff_until = _rating_backoff.get(_retry_key)
            if _backoff_until is not None:
                if _loop_now < _backoff_until:
                    logger.debug(f"Rating fetch for {canonical_id} skipped (MDBList back-off active for selected key)")
                    effective_mdblist_key = None
                    _rating_backoff_active = True
                    _mdblist_unavailable_reason = "selected key is in back-off for this title"
                else:
                    del _rating_backoff[_retry_key]       # expired — allow a fresh attempt
                    _rating_fail_count.pop(_retry_key, None)  # reset escalation for clean slate

    if not rating_already_cached and effective_mdblist_key:
        _inflight_event = _rating_fetch_inflight.get(canonical_id)
        if _inflight_event is not None:
            # Another coroutine is mid-fetch — wait and piggyback on its result.
            logger.info(f"Rating fetch coalesced for {canonical_id} — awaiting in-flight fetch")
            await _inflight_event.wait()
            _refreshed = get_cached_rating(canonical_id)
            if _refreshed is not None:
                (
                    cached_ratings_dict,
                    cached_genre,
                    cached_release_date,
                    cached_award_wins,
                    cached_award_noms,
                    cached_awards_fetched,
                    cached_festival_keyword,
                    cached_age_rating,
                    cached_is_cult,
                    cached_is_true_story,
                    cached_is_metacritic,
                ) = _refreshed
                rating_already_cached        = True
                release_date_for_quality_ttl = cached_release_date
                logger.info(f"Rating coalesce succeeded for {canonical_id} — using cached result")
            else:
                # The owner already made the MDBList attempt for this title.
                # If it did not produce a cache row, do not launch a second
                # same-content request from a waiter in the same burst.
                logger.debug(
                    f"Rating fetch for {canonical_id} suppressed after coalescence "
                    "(owner did not cache rating)"
                )
                effective_mdblist_key = None
                _rating_backoff_active = True
                _mdblist_unavailable_reason = "coalesced fetch did not cache rating"
        else:
            # First request for this canonical_id — claim the fetch slot.
            _rating_event_to_set              = asyncio.Event()
            _rating_fetch_inflight[canonical_id] = _rating_event_to_set

    # A request with no MDBList route (anime carrying neither an IMDb nor a TMDB
    # id) nulls the key deliberately, so this would be noise rather than a warning.
    if not rating_already_cached and not effective_mdblist_key and rating_media_id:
        logger.warning(
            f"MDBList unavailable for {canonical_id}: {_mdblist_unavailable_reason} — "
            "poster will be served without rating/award data."
        )

    effective_movie_weights = rcfg.movie_weights or _cfg.MOVIE_WEIGHTS
    effective_tv_weights    = rcfg.tv_weights    or _cfg.TV_WEIGHTS

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    # Secondary preferred language, only when the chosen priority actually uses it.
    _effective_secondary = (
        rcfg.logo_language_secondary
        if rcfg.logo_priority in _SECONDARY_LANGUAGE_PRIORITIES
        else ""
    )

    global _active_poster_renders
    _active_poster_renders += 1
    try:
        # True only while we are actually rendering the anime provider's cover
        # art, which is what the art-specific rules key off. Distinct from
        # is_anime, which stays true for a request that fell back to TMDB art.
        using_anime_art = False
        _anime_art_missing = False
        if is_anime:
            # Neither provider ships title logos, so when a tmdb_id came with
            # the request pull TMDB's metadata alongside — purely for its logo
            # list, which is language-aware and has good anime coverage. The
            # art, title, genres, dates and score still come from the anime
            # provider. This is cached for a week and coalesced, so the
            # amortised cost is one extra call per title per week.
            _anime_meta, _logo_meta = await asyncio.gather(
                anime.fetch_anime_metadata(client, anime_namespace, anime_id),
                _coalesced_fetch_poster_metadata(
                    client, tmdb_id, effective_tmdb_key, type,
                    rcfg.logo_language, _effective_secondary,
                ) if (has_tmdb_id and effective_tmdb_key) else _resolved(None),
                return_exceptions=True,
            )
            if isinstance(_anime_meta, BaseException):
                logger.warning(f"Anime metadata fetch failed for {anime_key}: {_anime_meta}")
                _anime_meta = None
            # A failed logo lookup is never fatal — it just means no logo.
            if isinstance(_logo_meta, BaseException):
                logger.warning(f"TMDB logo lookup failed for {tmdb_id}: {_logo_meta}")
                _logo_meta = None

            using_anime_art = _anime_meta is not None
            if _anime_meta is None and _logo_meta is not None:
                # The provider has no entry, or was throttled or unreachable.
                # TMDB's metadata is already in hand (fetched for the logo), so
                # serve its art rather than dropping to a genre canvas — a
                # strictly better poster, and it degrades gracefully if the
                # provider has an outage. Rendering reverts to the normal TMDB
                # rules; only the anime-specific ART behaviour is skipped.
                logger.info(
                    f"No {anime_namespace} entry for {anime_key} — falling back to TMDB art"
                )
                _anime_meta = _logo_meta
                _anime_art_missing = True
            elif _anime_meta is None:
                # Nothing from either source — same genre canvas path a TMDB
                # title with no art takes.
                _anime_meta = anime.empty_metadata(anime_namespace)
                _anime_art_missing = True
            else:
                _anime_art_missing = False

            (
                genre_ids, is_textless, logos, release_year, title,
                poster_path, backdrop_path, tmdb_data,
            ) = _anime_meta
            if using_anime_art and _logo_meta is not None:
                logos = _logo_meta[2]
        else:
            genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data = (
                await _coalesced_fetch_poster_metadata(
                    client, tmdb_id, effective_tmdb_key, type, rcfg.logo_language,
                    _effective_secondary,
                )
            )
        # Canonical IMDb id for downstream lookups (e.g. TVDB remoteid resolution):
        # the request param if supplied, else the one TMDB returned in external_ids.
        # Optional: TMDB returns imdb_id: null for titles it has no IMDb link for.
        effective_imdb_id = (imdb_id or "").strip() or tmdb_data.get("imdb_id") or None

        # ------------------------------------------------------------------
        # Quality tokens — cache checked exactly once here; fetch fn only writes.
        #
        # This runs *after* metadata on purpose. The id a quality source will
        # recognise is not always one the caller sent: for an ordinary title
        # whose URL carries no imdb_id, the IMDb id TMDB just returned is the
        # only thing Torrentio/Comet/AIOStreams/QualiCache can be asked about.
        # Resolving quality before metadata would have silently dropped the
        # badges from every normally-linked title the moment generated templates
        # stopped sending imdb_id.
        #
        # quality_id is None when nothing upstream would recognise the title —
        # then the lookup is skipped rather than issued in a form that can only
        # 404. An explicit quality= override needs no lookup and is unaffected.
        # ------------------------------------------------------------------
        quality_id = _quality_identity(imdb_id, anime_key, effective_imdb_id)

        if quality:
            quality_tokens = parse_quality(quality)
            cached_tokens  = None
        elif quality_id is None:
            cached_tokens  = None
            quality_tokens = []
        else:
            cached_tokens  = get_cached_quality(quality_id, release_date_for_quality_ttl)
            quality_tokens = cached_tokens or []

        # A quality source is available when the backend QUALITY_SOURCE selects has
        # the settings it needs — AIOStreams URL + auth, SCRAPER_URL, or QUALICACHE_URL.
        _has_quality_source = quality_source_configured()
        _quality_cooldown_active = _has_quality_source and _quality_backoff_remaining() > 0

        # The landscape renderer has no quality badges — build_landscape drops the
        # tokens — so fetching them buys nothing and costs plenty: wait_for_quality
        # would block every landscape request on a provider whose answer is thrown
        # away, and a pending fetch would keep the composite out of the cache, so a
        # slow source turned every request into a fresh render.
        _is_landscape = rcfg.shape == "landscape"

        quality_needs_fetch = (
            rcfg.badge_display_mode in (1, 2, 4, 5, 6)
            and not quality
            and quality_id is not None
            and cached_tokens is None
            and _has_quality_source
            and not _quality_cooldown_active
            and not _is_landscape
        )

        quality_pending = bool(
            _quality_cooldown_active
            and quality_id is not None
            and cached_tokens is None
            and not _is_landscape
        )
        if quality_needs_fetch and not rcfg.wait_for_quality:
            # Fire-and-forget background fetch — poster is served immediately
            # without badges; the cache will be warm on the next request.
            # Torrentio, Comet and AIOStreams all accept an anime-native stream id
            # ("kitsu:12345:1:1") because that is exactly what Stremio sends them
            # for Kitsu-catalogue items, so that form passes straight through and
            # the quality badge keeps working without an IMDb id.
            if quality_id not in _quality_bg_inflight:
                _quality_bg_inflight.add(quality_id)
                asyncio.create_task(
                    _background_quality_fetch(
                        quality_id, type, season, episode,
                        release_date_for_quality_ttl,
                    )
                )
                logger.info(f"Quality fetch deferred to background for {quality_id}")
            else:
                logger.info(f"Quality background fetch already in progress for {quality_id}")
            quality_needs_fetch = False
            quality_pending = True
        _text_titles = tuple(dict.fromkeys(
            value for value in (title, tmdb_data.get("original_title")) if value
        ))

        # Resolve genre string from TMDB genre_ids immediately — this is always
        # available regardless of MDBlist status, so we can use it as a reliable
        # fallback if the rating fetch fails or is skipped entirely.
        _gid_set = set(genre_ids)
        _tmdb_genre = "Unknown"
        _genre_priority = (
            _cfg.ANIME_GENRE_PRIORITY if is_anime else _cfg.GENRE_PRIORITY
        )
        for _gid in _genre_priority:
            if _gid in _gid_set:
                _candidate = _cfg.GENRE_MAP.get(_gid, "")
                if _candidate:
                    _tmdb_genre = _candidate
                    break

        # Backdrop fallback: when no null-language textless poster exists, use
        # the landscape backdrop cropped to portrait.  Backdrops are almost always
        # textless by design and TMDB coverage is near-universal, so this recovers
        # the vast majority of titles that would otherwise fall back to a textual
        # poster — OR, when no poster art exists at all, a genre-tinted canvas.
        #   poster missing entirely  → prefer backdrop over the canvas
        #   poster exists with text  → prefer backdrop over the text-burned poster
        _use_backdrop = bool(backdrop_path) and (poster_path is None or not is_textless)
        if _use_backdrop:
            logger.info(f"No textless poster for {tmdb_id} — using backdrop crop as portrait fallback")
            is_textless = True          # backdrop is textless; enable logo compositing

        # Original-art mode: serve a TMDB poster (title baked into the art) as-is.
        # Override the textless/backdrop selection, force is_textless=False so the
        # existing gates skip our logo, text detection and the backdrop rescue.
        # Poster language reuses logo_priority (there's no text fallback here).
        # "native" is the REQUEST's logo_language (selected from poster_langs at
        # render time, so it isn't baked to whatever language first cached this
        # title).  Both fall back to the primary poster; off if none exist.
        _plangs    = tmdb_data.get("poster_langs") or {}
        _p_default = tmdb_data.get("original_poster_path")
        _original_lang = tmdb_data.get("original_language") or ""
        _poster_language_order = image_language_order(
            rcfg.logo_language, _original_lang, rcfg.logo_priority, _effective_secondary
        )
        _priority_lang = _poster_language_order[0] if _poster_language_order else ""
        _ranked_posters = [
            _plangs[language]
            for language in _poster_language_order
            if _plangs.get(language)
        ]
        # art_source only matters when the priority-first language is English —
        # the two TMDB English poster candidates (editorial primary vs
        # community top-rated) can differ meaningfully.  For non-English
        # priority languages TMDB has no separate "primary" concept so we
        # always use the vote-ranked poster regardless of art_source.
        _use_primary = (
            _priority_lang == "en"
            and rcfg.original_art_source == "primary"
        )
        if _use_primary:
            _orig_art = _p_default or next(iter(_ranked_posters), None)
        else:
            _orig_art = next(iter(_ranked_posters), None) or _p_default
        _use_original_art = rcfg.use_original_art and bool(_orig_art)
        if _use_original_art:
            poster_path   = _orig_art
            is_textless   = False
            _use_backdrop = False
            logger.info(f"Original-art mode for {tmdb_id} — poster {poster_path} "
                        f"(priority={rcfg.logo_priority})")

        # Anime providers ship exactly one cover image per title and it
        # essentially always has the title logotype baked into the art, so it is
        # served as-is under the same rules as original-art mode: no logo
        # composited over it, no burned-in-text scan (it would reject nearly
        # every one, at the cost of an OCR pass), and no backdrop rescue —
        # AniList banners are 1900x400 and the portrait crop would destroy them.
        #
        # ANIME_COMPOSITE_LOGO (default on) overrides the logo half of that.
        # In practice anime cover art either carries no logotype at all or a
        # small block of Japanese text in a corner that most viewers can't read,
        # so a composited title logo is an improvement often enough to be worth
        # doing unconditionally. Text detection stays off either way: it would
        # flag that Japanese corner text on most titles and suppress the logo
        # inconsistently, which reads worse than always printing it.
        # Gated on using_anime_art, not is_anime: when the provider missed and we
        # fell back to TMDB art, that art follows the ordinary TMDB rules
        # (textless selection, backdrop rescue, text detection) as it would for
        # any other title.
        if using_anime_art and poster_path:
            _use_original_art = True
            _use_backdrop     = False
            is_textless       = bool(_cfg.ANIME_COMPOSITE_LOGO)

        if is_anime and not rating_already_cached and not effective_mdblist_key:
            # No IMDb id (or no key), so MDBList can't be asked. Supply what the
            # provider gave us instead of nothing. With an IMDb id this branch is
            # skipped and the normal MDBList fetch runs; the provider score is
            # merged into its result below either way.
            rating_coro = _resolved((
                None,
                (
                    {},
                    _tmdb_genre,
                    tmdb_data.get("tmdb_release_date"),
                    [],
                    tmdb_data.get("anime_age_rating"),
                ),
            ))
        elif rating_already_cached or not effective_mdblist_key:
            rating_coro = _resolved((
                None,
                (cached_ratings_dict, cached_genre, cached_release_date, [], cached_age_rating),
            ))
        else:
            global _mdblist_semaphore
            if _mdblist_semaphore is None:
                _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

            async def _fetch_rating_gated(
                _key: str, _client=client, _media_id=rating_media_id,
                _provider=rating_provider, _gids=genre_ids, _type=type,
                _mw=effective_movie_weights, _tw=effective_tv_weights,
            ):
                nonlocal _rating_backoff_active, _mdblist_unavailable_reason
                async with _mdblist_semaphore:
                    _fetch_key = _key
                    _fetch_now = asyncio.get_running_loop().time()
                    if (
                        _fetch_key in _cfg.SERVER_MDBLIST_KEYS
                        and _fetch_now < _mdblist_key_cooldown.get(_fetch_key, 0.0)
                    ):
                        _replacement_key = _next_mdblist_server_key(_fetch_key, _fetch_now)
                        if _replacement_key is None:
                            _remaining = _mdblist_key_cooldown.get(_fetch_key, 0.0) - _fetch_now
                            logger.debug(
                                f"Rating fetch for {canonical_id} skipped "
                                f"({_mdblist_server_key_label(_fetch_key)} cooling down; "
                                f"{_remaining:.0f}s remaining)"
                            )
                            _rating_backoff_active = True
                            _mdblist_unavailable_reason = "selected key is cooling down"
                            return None, (
                                cached_ratings_dict, cached_genre,
                                cached_release_date, [], cached_age_rating,
                            )
                        logger.info(
                            f"MDBList key rotated from {_mdblist_server_key_label(_fetch_key)} "
                            f"to {_mdblist_server_key_label(_replacement_key)} for {canonical_id} "
                            "before outbound fetch"
                        )
                        _fetch_key = _replacement_key
                    return _fetch_key, await fetch_rating(
                        _client, _fetch_key, _gids, _type,
                        media_id=_media_id, provider=_provider,
                        movie_weights=_mw, tv_weights=_tw,
                    )

            rating_coro = _fetch_rating_gated(effective_mdblist_key)

        # Quality is normally fetched in the background (not in this gather).
        # The one exception — wait_for_quality — is handled inline after the
        # gather completes so it never blocks rating coalescing.
        _backdrop_rescued = False
        _detection_deferred = False
        _vc = tmdb_data.get("vote_count")
        _vote_detection_ok = _detection_vote_ok(_vc)

        async def _tvdb_is_clean(cand_image, art_id, *, source="backdrop", kind="bd") -> bool:
            """Inline burned-in-text vet for a TVDB candidate (background or poster),
            mirroring the TMDB text-backdrop rescue.  Returns True only when detection
            is available, vote-gated, and reports no text.  Memoised per (tvdb id,
            kind, crop, detector)."""
            if not (_cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok):
                return False
            try:
                from text_detect import DETECT_RES_SIG
                _src = f"tvdb_{kind}:{art_id}:{_CROP_VERSION}:ta"
                _key = f"{_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                _res = get_cached_text_detection(_key)
                if _res is None:
                    _res = await asyncio.shield(_start_text_detection(
                        _key, cand_image, title=_text_titles, source=source,
                        tmdb_id=tmdb_id, vote_count=_vc, source_key=_src))
                return _res is False
            except Exception as exc:
                logger.warning(f"TVDB {kind} vet failed for {tmdb_id}: {exc}")
                return False

        is_no_poster = poster_path is None and not _use_backdrop

        # ------------------------------------------------------------------
        # Landscape short-circuit.
        #
        # The whole portrait art chain above — textless poster selection,
        # backdrop-to-portrait rescue, TVDB fallbacks — exists to manufacture a
        # 2:3 image.  Landscape wants the backdrop as shot, so none of it
        # applies: pick a backdrop, fit it, done.  Falls back to the portrait
        # decisions only for the genre canvas, which has no aspect of its own.
        #
        #   textless — the language-neutral backdrop; our logo goes on top
        #   original — the highest-voted language-tagged one, title treatment
        #              already in the art, so is_textless stays False and every
        #              existing gate skips our logo for us
        #
        # Both modes fall back, and the fallback flips that: a title with no
        # text-bearing backdrop lands on the neutral one (or the genre canvas),
        # which carries no title, so is_textless goes True and our logo IS
        # wanted.  is_textless — not the requested mode — is what the renderer
        # must key off; deciding it from cfg.landscape_art downstream is how
        # these fallbacks ended up rendering with no title at all.
        # ------------------------------------------------------------------
        if _is_landscape:
            _ls_text_bd = tmdb_data.get("text_backdrop_path")
            if rcfg.landscape_art == "original":
                _ls_path = _ls_text_bd or backdrop_path
                # Only the text-bearing pick carries its own title; if the title
                # had none and we fell back to the neutral backdrop, our logo is
                # wanted after all.
                is_textless = _ls_text_bd is None and _ls_path is not None
            else:
                _ls_path = backdrop_path or _ls_text_bd
                # Falling through to a text-bearing backdrop means the title is
                # already in the art; don't double it with our logo.
                is_textless = bool(backdrop_path)
            _use_backdrop = False
            is_no_poster  = _ls_path is None
            if _ls_path is None:
                logger.info(f"No backdrop for {tmdb_id} — landscape falls back to genre canvas")
                _image_coro = _resolved(_make_landscape_canvas(genre_ids))
                is_textless = True
            else:
                _image_coro = fetch_landscape_image(client, tmdb_id, _ls_path)
        elif _use_backdrop:
            # Text-aware backdrop cropping also invokes PP-OCR, so apply the
            # same foreground vote gate used by the final burned-in-text scan.
            _backdrop_avoid_text = (
                _cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok
            )
            _image_coro = fetch_backdrop_image(
                client, tmdb_id, backdrop_path, avoid_text=_backdrop_avoid_text)
        elif is_no_poster:
            # No poster art at all.  Before settling for the genre canvas, try a
            # TVDB background (curated fanart — usually textless).  Strictly an
            # upgrade over a flat canvas.  Vet for burned-in text where possible;
            # composite our logo only on a clean one, otherwise show it as-is.
            _tvdb_bg = None
            _tvdb_bg_id = None
            if _cfg.TVDB_USE_BACKDROPS and tvdb.tvdb_enabled():
                _bd_avoid = _cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok
                _tvdb_bg, _tvdb_bg_id = await tvdb.tvdb_backdrop(
                    client, media_type=type, imdb_id=effective_imdb_id,
                    tmdb_id=tmdb_id, avoid_text=_bd_avoid,
                )
            # Opt-in TVDB poster as a further no-art rescue (TVDB_USE_POSTERS).
            # A real poster — even one carrying its own title — beats a genre
            # canvas; we composite our logo only when it vets clean.
            _tvdb_ps = None
            _tvdb_ps_id = None
            if (_tvdb_bg is None and _cfg.TVDB_USE_POSTERS and tvdb.tvdb_enabled()):
                _tvdb_ps, _tvdb_ps_id = await tvdb.tvdb_poster(
                    client, media_type=type, language=rcfg.logo_language,
                    imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                )
            if _tvdb_bg is not None:
                if await _tvdb_is_clean(_tvdb_bg, _tvdb_bg_id):
                    is_textless = True           # clean art → composite our logo
                    logger.info(f"TVDB background for {tmdb_id} clean — using with logo")
                else:
                    logger.info(f"TVDB background for {tmdb_id} unvetted/texted — using as-is")
                is_no_poster = False
                _backdrop_rescued = True          # pre-vetted → skip the scan block
                _image_coro = _resolved(_tvdb_bg)
            elif _tvdb_ps is not None:
                if await _tvdb_is_clean(_tvdb_ps, _tvdb_ps_id, source="poster", kind="ps"):
                    is_textless = True
                    logger.info(f"TVDB poster for {tmdb_id} clean — using with logo")
                else:
                    logger.info(f"TVDB poster for {tmdb_id} unvetted/texted — using as-is")
                is_no_poster = False
                _backdrop_rescued = True
                _image_coro = _resolved(_tvdb_ps)
            else:
                # Prefer the atmospheric genre background (minimal or photoreal set,
                # per the request); fall back to the flat genre-tinted gradient if no
                # background art exists for this genre in either set.
                _bg = _load_genre_background(_tmdb_genre, rcfg.fallback_bg_style)
                _image_coro = _resolved(_bg if _bg is not None else _make_fallback_canvas(genre_ids))
        else:
            # Option A: the title has only text-bearing art (no textless poster
            # or backdrop).  Before settling for the busy official poster, try a
            # text-aware crop of a text-bearing backdrop; if it comes out clean
            # we get a nicer image plus our own logo.  Gated to low-vote titles.
            _rescued = None
            _tbp = tmdb_data.get("text_backdrop_path")
            if (_cfg.TEXTLESS_TEXT_DETECTION and not is_textless and _tbp
                    and not _use_original_art
                    and _detection_vote_ok(tmdb_data.get("vote_count"))):
                try:
                    _cand = await fetch_backdrop_image(client, tmdb_id, _tbp, avoid_text=True)
                    # Memoise per (candidate backdrop and detector settings)
                    # — same rationale as the suppress path: config-independent.
                    from text_detect import DETECT_RES_SIG
                    _resc_src = f"bd:{_tbp}:{_CROP_VERSION}:ta"
                    _resc_key = f"{_resc_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                    _still_text = get_cached_text_detection(_resc_key)
                    if _still_text is None:
                        _still_text = await asyncio.shield(_start_text_detection(
                            _resc_key,
                            _cand,
                            title=_text_titles,
                            source="backdrop",
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_resc_src,
                        ))
                    if _still_text is False:
                        _rescued = _cand
                        logger.info(f"Text-aware backdrop crop clean for {tmdb_id} — using it with logo")
                    else:
                        logger.info(f"Text-aware backdrop crop still has text for {tmdb_id} — keeping official poster")
                except Exception as exc:
                    logger.warning(f"Backdrop rescue failed for {tmdb_id}: {exc}")
            if _rescued is not None:
                is_textless = True            # we now have textless art → composite logo
                _backdrop_rescued = True
                _image_coro = _resolved(_rescued)
            else:
                # Second rescue tier: a TVDB background, vetted the same way.  Only
                # for text-bearing posters (not a clean TMDB textless one), gated to
                # low-vote titles like the TMDB rescue above.  Falls through to the
                # official poster when TVDB has nothing clean.
                _tvdb_bg = None
                _tvdb_bg_id = None
                if (_cfg.TVDB_USE_BACKDROPS and tvdb.tvdb_enabled()
                        and not is_textless and not _use_original_art
                        and _detection_vote_ok(_vc)):
                    _tvdb_bg, _tvdb_bg_id = await tvdb.tvdb_backdrop(
                        client, media_type=type, imdb_id=effective_imdb_id,
                        tmdb_id=tmdb_id, avoid_text=True,
                    )
                # Third rescue tier (opt-in, TVDB_USE_POSTERS): a TVDB poster.
                # These usually have title text baked in, so it's only used when
                # text detection confirms it's clean — otherwise we keep the
                # official poster.  Same low-vote gate.
                _tvdb_ps = None
                _tvdb_ps_id = None
                if (_tvdb_bg is None and _cfg.TVDB_USE_POSTERS and tvdb.tvdb_enabled()
                        and not is_textless and not _use_original_art
                        and _detection_vote_ok(_vc)):
                    _tvdb_ps, _tvdb_ps_id = await tvdb.tvdb_poster(
                        client, media_type=type, language=rcfg.logo_language,
                        imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                    )
                if _tvdb_bg is not None and await _tvdb_is_clean(_tvdb_bg, _tvdb_bg_id):
                    is_textless = True
                    _backdrop_rescued = True
                    _image_coro = _resolved(_tvdb_bg)
                    logger.info(f"TVDB background rescue clean for {tmdb_id} — using with logo")
                elif _tvdb_ps is not None and await _tvdb_is_clean(
                        _tvdb_ps, _tvdb_ps_id, source="poster", kind="ps"):
                    is_textless = True
                    _backdrop_rescued = True
                    _image_coro = _resolved(_tvdb_ps)
                    logger.info(f"TVDB poster rescue clean for {tmdb_id} — using with logo")
                else:
                    _image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        # Start eligible foreground OCR as soon as the image arrives. Higher-vote
        # assets are recorded as deferred work instead: the request keeps waiting
        # for logo/rating/info, but never waits for their textless scan.
        _detection_task: "asyncio.Task[bool | None] | None" = None
        _detection_result: bool | None = False
        _det_src: str | None = None
        _det_key: str | None = None
        _scan_selected_image = (
            _cfg.TEXTLESS_TEXT_DETECTION
            and is_textless
            and not is_no_poster
            and not _backdrop_rescued
            # Anime art is deliberately composited with a logo regardless of any
            # Japanese corner text, so the scan would only burn an OCR pass to
            # produce an inconsistent result. See the is_textless assignment.
            # TMDB fallback art is scanned normally.
            and not using_anime_art
            # Landscape picks its art by TMDB's own language tag rather than by
            # scanning it, so there is nothing here for OCR to decide.
            and not _is_landscape
        )
        if _scan_selected_image:
            from text_detect import DETECT_RES_SIG

            if _use_backdrop:
                _crop_variant = "ta" if _backdrop_avoid_text else "plain"
                _det_src = f"bd:{backdrop_path}:{_CROP_VERSION}:{_crop_variant}"
                _image_cache_key = (
                    f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
                    + ("_ta" if _backdrop_avoid_text else "")
                )
                _det_source = "backdrop"
            else:
                _det_src = f"ps:{poster_path}"
                _image_cache_key = f"{type}_{tmdb_id}_{poster_path.strip('/')}"
                _det_source = "poster"

            _det_key = (
                f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
            )
            _detection_result = get_cached_text_detection(_det_key)
            if _detection_result is None:
                _base_image_coro = _image_coro
                if _vote_detection_ok:
                    _reserve_foreground_detection()

                async def _fetch_image_and_schedule_detection():
                    nonlocal _detection_task, _detection_deferred
                    try:
                        fetched_image = await _base_image_coro
                    except BaseException:
                        if _vote_detection_ok:
                            _release_foreground_detection()
                        raise
                    if _vote_detection_ok:
                        _detection_task = _start_text_detection(
                            _det_key,
                            fetched_image,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_det_src,
                            media_type=type,
                            image_path=poster_path,
                            foreground_reserved=True,
                        )
                    else:
                        _detection_deferred = True
                        _queue_background_text_detection(_DeferredTextDetection(
                            cache_key=_det_key,
                            image_cache_key=_image_cache_key,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            media_type=type,
                            image_path=poster_path,
                            vote_count=_vc,
                            source_key=_det_src,
                        ))
                    return fetched_image

                _image_coro = _fetch_image_and_schedule_detection()

        # Logo resolution across TMDB, the Metahub CDN, and (optionally) TVDB.
        # TVDB's position in the chain is set by TVDB_LOGO_PRIORITY:
        #   1 = TVDB first, 2 = after TMDB but before Metahub, 3 = last resort.
        # Priority 3 (default) and a missing TVDB key both reduce to the original
        # TMDB -> Metahub -> (TVDB) behaviour, so existing output is unchanged.
        _tvdb_logo_pri = _cfg.TVDB_LOGO_PRIORITY if tvdb.tvdb_enabled() else 3

        async def _resolve_logo():
            async def _tmdb(use_metahub):
                return await fetch_logo(
                    client, logos, rcfg.logo_language,
                    imdb_id=effective_imdb_id,
                    original_language=tmdb_data.get("original_language"),
                    logo_priority=rcfg.logo_priority,
                    use_metahub=use_metahub,
                    secondary_language=_effective_secondary,
                )

            async def _tvdb():
                return await tvdb.tvdb_logo(
                    client, media_type=type, logo_language=rcfg.logo_language,
                    original_language=tmdb_data.get("original_language"),
                    logo_priority=rcfg.logo_priority,
                    secondary_language=_effective_secondary,
                    imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                )

            async def _metahub():
                return (await _fetch_metahub_logo(client, effective_imdb_id)
                        if effective_imdb_id else None)

            # These modes have their own explicit order: TMDB language buckets
            # (native, then custom/original for the native_custom_* variants) ->
            # TMDB English -> Metahub -> TMDB neutral -> rendered text.
            if rcfg.logo_priority in _TEXT_FORWARD_LOGO_PRIORITIES:
                return await _tmdb(use_metahub=True)

            if _tvdb_logo_pri == 1:
                return (await _tvdb()) or (await _tmdb(use_metahub=True))
            if _tvdb_logo_pri == 2:
                return (await _tmdb(use_metahub=False)) or (await _tvdb()) or (await _metahub())
            # priority 3 — TMDB -> Metahub -> TVDB
            return (await _tmdb(use_metahub=True)) or (await _tvdb())

        (
            image,
            logo,
            rating_fetch_result,
            trending_rank,
        ) = await asyncio.gather(
            _image_coro,
            _resolve_logo() if (is_textless and not is_no_poster) else _resolved(None),
            rating_coro,
            # Trending rank is a TMDB list lookup, so it needs a real tmdb_id —
            # which AIOMetadata does send alongside the anime id when it has one.
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type)
            if has_tmdb_id and effective_tmdb_key else _resolved(None),
        )

        rating_key_used, rating_result = rating_fetch_result
        if rating_key_used is not None:
            effective_mdblist_key = rating_key_used
        elif _rating_backoff_active:
            effective_mdblist_key = None

        # A rate-limited server key gets one same-request rescue attempt on the
        # next healthy configured key. Query-supplied keys remain isolated.
        #
        # This rescue is hand-rolled rather than routed through _with_retry on
        # purpose: _with_retry re-calls blindly on FETCH_FAILED, so wrapping the
        # rating fetch would fire a second request at the key that just returned
        # 429 — before the cooldown below can register it — doubling load on a
        # key that explicitly asked us to back off. Rotate first, then retry.
        if isinstance(rating_result, _RateLimited) and effective_mdblist_key:
            _failed_key = effective_mdblist_key
            _backoff_secs, _rescue_key = _mark_mdblist_rate_limit(
                canonical_id, _failed_key, rating_result
            )
            logger.warning(
                f"MDBList {_mdblist_server_key_label(_failed_key)} rate-limited "
                f"for {canonical_id}; cooling down for {_backoff_secs:.0f}s"
            )
            if _rescue_key is not None:
                effective_mdblist_key = _rescue_key
                logger.warning(
                    f"Retrying MDBList for {canonical_id} with "
                    f"{_mdblist_server_key_label(_rescue_key)}"
                )
                _rescue_used_key, rating_result = await _fetch_rating_gated(_rescue_key)
                if _rescue_used_key is not None:
                    effective_mdblist_key = _rescue_used_key
                elif _rating_backoff_active:
                    effective_mdblist_key = None

        # Inline quality wait — runs after gather so rating coalescing is never
        # blocked.  Used for poster-warm workflows where latency doesn't matter.
        if quality_needs_fetch and rcfg.wait_for_quality:
            try:
                fetched = await asyncio.wait_for(
                    _with_retry(
                        fetch_quality,
                        client, quality_id, type, season, episode, release_date_for_quality_ttl,
                    ),
                    timeout=_cfg.QUALITY_WAIT_TIMEOUT,
                )
                _record_quality_result(fetched)
                if fetched is QUALITY_PENDING:
                    # QualiCache has queued this title but has no value yet.
                    # Waiting longer wouldn't help — it collects out of band.
                    logger.info(
                        f"Inline quality fetch pending for {quality_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
                elif fetched is not FETCH_FAILED:
                    quality_tokens = fetched
                    logger.info(f"Inline quality fetch complete for {quality_id}: {quality_tokens}")
                else:
                    # The quality source returned a transient error — don't cache
                    # the composite poster without quality so the next request retries.
                    logger.warning(
                        f"Inline quality fetch failed for {quality_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
            except asyncio.TimeoutError:
                _record_quality_result(FETCH_FAILED)
                logger.warning(
                    f"Quality wait timed out for {quality_id} "
                    f"after {_cfg.QUALITY_WAIT_TIMEOUT:.0f}s — serving without quality, "
                    "composite not cached so next request retries"
                )
                quality_pending = True
            quality_needs_fetch = False

        # ------------------------------------------------------------------
        # Unpack results
        # ------------------------------------------------------------------
        rate_limited  = isinstance(rating_result, _RateLimited)
        rating_failed = (
            not rating_already_cached
            and effective_mdblist_key
            and (rating_result is FETCH_FAILED or rate_limited)
        )

        if rating_failed:
            if rate_limited:
                _retry_key = _rating_retry_key(canonical_id, effective_mdblist_key)
                if _retry_key not in _rating_backoff:
                    backoff_secs, _ = _mark_mdblist_rate_limit(
                        canonical_id, effective_mdblist_key, rating_result
                    )
                    logger.warning(
                        f"MDBList rate-limited {canonical_id}; key cooling down for "
                        f"{backoff_secs:.0f}s"
                    )
            else:
                # Network / timeout failure — escalating back-off so a transient
                # hiccup retries quickly while a sustained outage backs off further.
                # Ladder: 30 s → 2 min → 8 min → 1 h (cap), using 4× multiplier.
                _failed_retry_key = _rating_retry_key(canonical_id, effective_mdblist_key)
                fail_n = _rating_fail_count.get(_failed_retry_key, 0) + 1
                _rating_fail_count[_failed_retry_key] = fail_n
                backoff_secs = min(30 * (4 ** (fail_n - 1)), 3600.0)
                logger.warning(
                    f"Rating fetch failed for {canonical_id} (attempt {fail_n}) "
                    f"— back-off {backoff_secs:.0f}s"
                )
            if not rate_limited:
                _failed_retry_key = _rating_retry_key(canonical_id, effective_mdblist_key)
                _rating_backoff[_failed_retry_key] = asyncio.get_running_loop().time() + backoff_secs
            ratings_dict     = {}
            genre            = cached_genre or _tmdb_genre
            rel              = cached_release_date
            # MDBList failed (or was never reachable), but the IMDb dataset and
            # TMDB's own vote average are independent, MDBList-free sources - try
            # them before giving up on a score entirely. calculate_weighted_score
            # returns "N/A" itself when ratings_dict has nothing usable, so this
            # is safe even when neither source has anything to offer.
            ratings_dict     = _merge_imdb_dataset_rating(ratings_dict, effective_imdb_id, rcfg)
            ratings_dict     = _merge_direct_tmdb_rating(ratings_dict, tmdb_data, rcfg)
            score            = calculate_weighted_score(
                ratings_dict,
                effective_tv_weights if type in ("tv", "series") else effective_movie_weights,
                fallback_to_imdb=rcfg.fallback_to_imdb,
                fallback_source=anime_namespace if is_anime else None,
            )
            keywords         = []
            award_wins       = cached_award_wins
            award_noms       = cached_award_noms
            festival_keyword = cached_festival_keyword
            age_rating       = cached_age_rating
            is_cult          = cached_is_cult
            is_true_story    = cached_is_true_story
            is_metacritic    = cached_is_metacritic
        else:
            ratings_dict, genre, rel, keywords, age_rating = rating_result
            # genre from MDBlist/cache may be None when the key is absent and
            # nothing is cached yet — fall back to the TMDB-derived genre.
            #
            # On the anime path the genre is always derived here from the
            # provider's own genre list rather than read back from the cached
            # rating row. The row's genre column exists to carry MDBList's
            # answer, which anime titles never have; trusting it would pin a
            # title to whatever ANIME_GENRE_PRIORITY said when it was first
            # cached, so a reordering wouldn't take effect until the TTL expired.
            genre = _tmdb_genre if is_anime else (genre or _tmdb_genre)

            # The provider's score rides along in the art response, so merge it
            # into whatever MDBList returned — or into an empty dict when there
            # was no IMDb id to ask about. Done here rather than at the fetch so
            # it also covers the cached path, where the row may have been first
            # written by a request that carried no anime id.
            if is_anime and isinstance(ratings_dict, dict):
                _provider_score = tmdb_data.get("anime_score")
                if _provider_score is not None:
                    ratings_dict = {**ratings_dict, anime_namespace: _provider_score}
            # Likewise the age rating: MDBList supplies one for titles it knows,
            # but Kitsu's ageRating covers those it doesn't.
            if is_anime and age_rating is None:
                age_rating = tmdb_data.get("anime_age_rating")

            # Fresh successful fetch — clear any escalation state so future
            # failures start back at the shortest interval.
            if (
                not rating_already_cached
                and not _rating_backoff_active
                and effective_mdblist_key
            ):
                _rating_fail_count.pop(
                    _rating_retry_key(canonical_id, effective_mdblist_key), None
                )

            ratings_dict = _ratings_base(ratings_dict)
            if isinstance(ratings_dict, dict):
                ratings_dict = _merge_imdb_dataset_rating(ratings_dict, effective_imdb_id, rcfg)
                ratings_dict = _merge_direct_tmdb_rating(ratings_dict, tmdb_data, rcfg)
                weights = (
                    effective_tv_weights
                    if type in ("tv", "series")
                    else effective_movie_weights
                )
                score = calculate_weighted_score(
                    ratings_dict,
                    weights,
                    fallback_to_imdb=rcfg.fallback_to_imdb,
                    # The provider's score is the only rating an anime-native
                    # title has, and existing weights strings name none of the
                    # anime sources, so fall back to it rather than showing N/A.
                    # Giving anilist/kitsu a real weight overrides this.
                    fallback_source=anime_namespace if is_anime else None,
                )
            else:
                score = ratings_dict

            if rating_already_cached:
                award_wins       = cached_award_wins
                award_noms       = cached_award_noms
                festival_keyword = cached_festival_keyword
                age_rating       = cached_age_rating
                is_cult          = cached_is_cult
                is_true_story    = cached_is_true_story
                is_metacritic    = cached_is_metacritic
            else:
                award_wins, award_noms = parse_mdblist_awards(
                    keywords,
                    tmdb_id=tmdb_id,
                )
                kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
                festival_keyword = match_festival_keyword(kw_names)
                is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
                is_true_story = "based-on-true-story" in kw_names
                is_metacritic = "metacritic-must-see" in kw_names
                logger.info(f"Awards for {canonical_id}: wins={award_wins} noms={award_noms} "
                            f"festival={festival_keyword} age_rating={age_rating} "
                            f"cult={is_cult} true_story={is_true_story} metacritic={is_metacritic}")

        # ------------------------------------------------------------------
        # Write rating + awards to cache (only on a fresh fetch).
        # ------------------------------------------------------------------
        # Anime ratings come from the provider rather than MDBList, so they are
        # cached on the same terms but gated on is_anime instead of a key.
        if not rating_failed and not rating_already_cached and (
            effective_mdblist_key or is_anime
        ):
            set_cached_rating(
                canonical_id,
                ratings_dict if isinstance(ratings_dict, dict) else {},
                genre,
                rel,
                award_wins,
                award_noms,
                awards_fetched=True,
                festival_keyword=festival_keyword,
                age_rating=age_rating,
                is_cult=is_cult,
                is_true_story=is_true_story,
                is_metacritic=is_metacritic,
            )
            logger.info(f"Rating cached for {canonical_id}: score={score} genre={genre} "
                        f"wins={award_wins} noms={award_noms} festival={festival_keyword} "
                        f"age_rating={age_rating}")

        # Publish completion only after success is cached or failure backoff is
        # established. Otherwise a waiter can wake, miss the row, and duplicate
        # the same MDBList request.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(canonical_id, None)
            _rating_event_to_set = None

        logger.info(
            f"Quality for {canonical_id}: tokens={quality_tokens} year={release_year} "
            f"(quality_id={quality_id})"
        )

        # ------------------------------------------------------------------
        # Release status / freshness facts. TV status is mapped from already
        # fetched metadata. Movie digital freshness uses the cached TMDB
        # /release_dates helper only when the Just Added sash is enabled.
        # ------------------------------------------------------------------
        _release_status: str | None = None
        _recent_digital_release_date: str | None = None
        _rs_slots = {"release_status", "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"}
        if any(s in rcfg.sash_priority for s in _rs_slots):
            # Resolved for every title regardless of age.  There used to be an
            # age gate here that skipped the lookup for anything older than a
            # configurable limit, but it silently blanked the status on older
            # titles — which read as a bug rather than a setting.  Results are
            # cached, and stale "Cinema" on an old film is handled properly by
            # CINEMA_MAX_AGE_YEARS, which downgrades it to "Streaming".
            # For series this is a pure mapping of the status field already in
            # hand — no API call — so anime series get their lifecycle sashes
            # from the provider's status. The movie branch needs TMDB's
            # /release_dates, so it runs only when a real tmdb_id and key came
            # with the request; otherwise the slot simply doesn't fire.
            if (type in ("tv", "series")
                    or (has_tmdb_id and effective_tmdb_key)):
                _release_status = await fetch_release_status(
                    client, tmdb_id, effective_tmdb_key, type,
                    tmdb_data.get("tmdb_status"),
                )
            # r/movieleaks confirmation overrides TMDB's theatrical/production
            # status — if the film is in the digital-release cache it's already
            # streaming regardless of what the official release dates say.
            if (_release_status in ("Cinema", "Production")
                    and effective_imdb_id
                    and is_digital_release(effective_imdb_id)):
                _release_status = "Streaming"
            # Cinema-only mode: keep the badge purely as an "unavailable" marker —
            # show only Cinema / Production and drop the rest so the slot is
            # skipped (and lower-priority sashes can surface) for released titles.
            if rcfg.release_status_cinema_only and _release_status not in ("Cinema", "Production"):
                _release_status = None

        if (type not in ("tv", "series") and "just_added" in rcfg.sash_priority
                and has_tmdb_id and effective_tmdb_key):
            _recent_digital_release_date = await fetch_recent_movie_digital_release_date(
                client, tmdb_id, effective_tmdb_key,
                tmdb_data.get("tmdb_status"),
            )

        # ------------------------------------------------------------------
        # Build DiscoveryMeta
        # ------------------------------------------------------------------
        discovery_meta = extract_discovery_meta(
            tmdb_data=tmdb_data,
            media_type=type,
            award_wins=award_wins,
            award_noms=award_noms,
            trending_rank=trending_rank,
            tmdb_id=tmdb_id,
            release_date=rel,
            keywords=keywords if not rating_already_cached else [],
            festival_keyword=festival_keyword,
            is_cult_override=is_cult,
            is_true_story_override=is_true_story,
            is_metacritic_override=is_metacritic,
            is_digital_release_override=bool(
                effective_imdb_id and is_digital_release(effective_imdb_id)
            ),
            release_status_override=_release_status,
            recent_digital_release_date=_recent_digital_release_date,
        )

        _sash_priority = rcfg.sash_priority

        # ------------------------------------------------------------------
        # Debug mode: return diagnostic JSON instead of rendering the poster.
        # Useful for troubleshooting wrong sashes, missing ratings, etc.
        # Activate with ?debug=1 (never cached, never stored).
        # ------------------------------------------------------------------
        if debug and debug.strip() in ("1", "true"):
            _sash_result = pick_sash(discovery_meta, _sash_priority)
            return JSONResponse({
                "imdb_id":           imdb_id or None,
                "effective_imdb_id": effective_imdb_id,
                "canonical_id":      canonical_id,
                "rating_provider":   rating_provider,
                "rating_media_id":   rating_media_id,
                "imdb_rating_source": rcfg.imdb_rating_source,
                "tmdb_rating_source": rcfg.tmdb_rating_source,
                "quality_id":        quality_id,
                "tmdb_id":           tmdb_id,
                "type":              type,
                "score":             score if isinstance(score, str) else int(score),
                "genre":             genre,
                "release_year":      release_year,
                "release_date":      rel,
                "quality_tokens":    quality_tokens,
                "age_rating":        age_rating,
                "award_wins":        award_wins,
                "award_noms":        award_noms,
                "festival_keyword":  festival_keyword,
                "festival_label":    discovery_meta.festival_label,
                "sash":              {"label": _sash_result[0], "type": _sash_result[1]} if _sash_result else None,
                "is_cult":           discovery_meta.is_cult,
                "is_true_story":     discovery_meta.is_true_story,
                "is_metacritic":     discovery_meta.is_metacritic_must_see,
                "is_new_release":    discovery_meta.is_new_release,
                "is_digital_release":discovery_meta.is_digital_release,
                "recent_digital_release_date": _recent_digital_release_date,
                "is_premiere":       discovery_meta.is_premiere,
                "is_just_added":     discovery_meta.is_just_added,
                "is_new_season":     discovery_meta.is_new_season,
                "is_returning":      discovery_meta.is_returning,
                "is_season_finale":  discovery_meta.is_season_finale,
                "trending_rank":     discovery_meta.trending_rank,
                "original_language": discovery_meta.original_language,
                "matched_studios":   discovery_meta.matched_studios,
                "matched_directors": discovery_meta.matched_directors,
                "matched_cast":      discovery_meta.matched_cast,
                "release_status":    discovery_meta.release_status,
                "sash_priority":     _sash_priority,
                "badge_display_mode":rcfg.badge_display_mode,
                "rating_display_mode":rcfg.rating_display_mode,
            })

        # ------------------------------------------------------------------
        # Burned-in-text detection. When a poster TMDB
        # tagged "textless" actually has the title burned in, compositing our
        # own logo/title would double it — so detect that and skip our overlay.
        # Cached results are always used. Uncached assets above the vote gate
        # are deferred until foreground poster rendering is idle.
        # ------------------------------------------------------------------
        _suppress_overlay = False
        if _scan_selected_image:
            _suppress_overlay = _detection_result
            if _suppress_overlay is None and _detection_task is not None:
                _suppress_overlay = await asyncio.shield(_detection_task)

            if _detection_deferred:
                logger.info(
                    f"Foreground text detection skipped for {tmdb_id}: "
                    f"vote_count={_vc!r} is outside foreground limit "
                    f"{_cfg.TEXTLESS_DETECTION_MAX_VOTES}; background scan queued"
                )
                _suppress_overlay = False
            elif _suppress_overlay is True:
                if not _use_backdrop and poster_path:
                    from textless_report import report_fake_textless_poster
                    report_fake_textless_poster(
                        media_type=type,
                        tmdb_id=tmdb_id,
                        image_path=poster_path,
                        vote_count=_vc,
                    )
                logger.info(
                    f"Burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc}); skipping logo/title overlay"
                )
            elif _suppress_overlay is False:
                logger.info(
                    f"No burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc})"
                )
            else:
                from text_detect import text_detection_status
                logger.warning(
                    f"Burned-in text scan unavailable for {tmdb_id}; "
                    f"result was not cached ({text_detection_status()})"
                )
                _suppress_overlay = False

        # Offload CPU-bound PIL compositing + JPEG encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if (is_textless and not is_no_poster and not rcfg.textless
                          and not _suppress_overlay) else None,
            fallback_title=(
                title if is_no_poster
                else (title if is_textless and not logo and not rcfg.textless
                      and not _suppress_overlay else None)
            ),
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
            no_poster=is_no_poster,
            # Only a confirmed True suppresses the tinted vignette. _suppress_overlay
            # is None when the scan was skipped or unavailable, which must not be
            # read as "clean" or as "has text" — it means we do not know, and an
            # unknown poster keeps its existing appearance.
            has_burned_in_text=(_suppress_overlay is True),
        )

        def _composite_and_encode() -> bytes:
            _render = build_landscape if _is_landscape else build_poster
            result = _render(image, score, genre, rcfg, **_bp_args)
            buf = io.BytesIO()
            _quality = _cfg.WEBP_QUALITY if _cfg.IMAGE_FORMAT == "webp" else _cfg.JPEG_QUALITY
            result.convert("RGB").save(buf, format=_cfg.IMAGE_FORMAT.upper(), quality=_quality)
            return buf.getvalue()

        img_bytes = await asyncio.get_running_loop().run_in_executor(
            None, _composite_and_encode
        )

        # Persist the finished poster so future requests skip the pipeline.
        # Skipped when:
        #   quality_pending      — badges would be missing; next request caches properly
        #   _detection_deferred — vote-gated OCR is queued in the background
        #   rating_failed        — MDBlist returned a hard failure; don't lock in N/A score
        #   _rating_backoff_active — a previous failure is still in its cool-down window;
        #                            backoff nullifies effective_mdblist_key so rating_failed
        #                            would evaluate False without this separate flag
        #   _anime_art_missing     — the provider had nothing this time, usually a
        #                            throttle or a blip rather than a real absence.
        #                            Caching the fallback would pin TMDB art for
        #                            the whole composite TTL, so let it re-render.
        #
        # The same flag decides what the *client* is told: a render we won't
        # keep must not be handed an ETag either (see _apply_poster_cache_headers).
        _render_provisional = bool(
            quality_pending or _detection_deferred or rating_failed
            or _rating_backoff_active or _anime_art_missing
        )
        _composite_expires_at: int | None = None
        if final_cache_key is not None and not _render_provisional:
            # A composite must not outlive the facts baked into it.  Trending
            # rank turns over daily; release status has its own tier (Cinema and
            # Production re-check every day, Physical every 90).  Without this
            # the render kept a "Cinema" sash — and the greyscale treatment that
            # keys off the same field — for the flat 7-day composite TTL, long
            # after the status row it came from had moved on.
            _ttl_override = None
            if discovery_meta is not None:
                _sash_result = pick_sash(discovery_meta, _sash_priority)
                if _sash_result and _sash_result[1] in ("trending", "trending_broad"):
                    _ttl_override = 86400
            if _release_status:
                _status_ttl = release_status_ttl_seconds(_release_status)
                _ttl_override = (
                    _status_ttl if _ttl_override is None
                    else min(_ttl_override, _status_ttl)
                )

            _composite_expires_at = set_cached_final_poster(
                final_cache_key,
                img_bytes,
                request_params=_sanitize_request_params(request.url.query),
                ttl_override=_ttl_override
            )
            logger.info(f"Final poster cached for {final_cache_key}")

        if _render_fut is not None:
            _render_fut.set_result((img_bytes, _render_provisional, _composite_expires_at))

        response = Response(content=img_bytes, media_type=f"image/{_cfg.IMAGE_FORMAT}")
        _apply_poster_cache_headers(
            response, final_cache_key, _render_provisional, _composite_expires_at
        )
        return response

    except ValueError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"No poster available for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.TimeoutException as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"Upstream timeout for tmdb_id={tmdb_id}: {exc.__class__.__name__}")
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except httpx.HTTPStatusError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        status = exc.response.status_code
        if status == 404:
            # TMDB returned metadata with a poster/image path that no longer exists.
            # Invalidate the (per-language) metadata cache so the next request
            # re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(tmdb_metadata_cache_key(
                _endpoint, tmdb_id, rcfg.logo_language
            ))
            logger.warning(
                f"TMDB image 404 for tmdb_id={tmdb_id} — metadata cache invalidated, "
                f"will self-heal on next request"
            )
            raise HTTPException(status_code=404, detail="Poster image not found on TMDB")
        logger.error(f"Upstream HTTP {status} for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Upstream error {status}")
    except Exception as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.exception(f"Error building poster for tmdb_id={tmdb_id}")
        raise HTTPException(status_code=500, detail="Failed to build poster")
    finally:
        _active_poster_renders = max(0, _active_poster_renders - 1)
        # Fire the rating event so any coalesced waiters unblock. Under normal
        # operation this was set after cache persistence; this is the safety
        # net for error paths that exit before reaching that point.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(canonical_id, None)
        if final_cache_key is not None:
            _render_inflight.pop(final_cache_key, None)