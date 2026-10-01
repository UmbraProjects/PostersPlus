#tvdb.py
"""TheTVDB v4 as a *fallback* art source (logos, backdrops, optionally posters).

Design notes
------------
- Entirely opt-in: every entry point short-circuits when ``SERVER_TVDB_KEY`` is
  empty, so a server without a key behaves exactly as TMDB-only.
- TVDB v4 has no "api key per request" mode (unlike TMDB/MDBList). The key is
  exchanged once via ``POST /login`` for a JWT bearer token valid ~1 month; the
  token is cached (in the shared SQLite cache) and refreshed on expiry or 401,
  guarded by a single-flight lock so concurrent requests don't stampede login.
- Artwork entries only carry a numeric ``type`` id. The meaning of each id comes
  from ``GET /artwork/types``; we fetch that catalogue once (cached long) and
  classify by slug/name keyword ("clearlogo"/"poster"/"background") rather than
  hardcoding ids, so a future TVDB id reshuffle can't silently break us.
- Failures never propagate into a request: any error logs and yields None, which
  the caller treats identically to "TVDB has nothing for this title".

This module is the data layer only; wiring into the render fallback chains lives
in main.py and is added in later phases.
"""
import asyncio
import io
import logging
import random
import time

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

from cache import (
    get_cached_tvdb_json,
    set_cached_tvdb_json,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
)
from config import (
    SERVER_TVDB_KEY,
    TVDB_SUBSCRIBER_PIN,
    TVDB_CONCURRENCY,
    TVDB_ARTWORK_CACHE_DURATION,
    TVDB_NEG_CACHE_DURATION,
    TVDB_TYPES_CACHE_DURATION,
    POSTER_WIDTH,
    POSTER_HEIGHT,
)

_API_BASE      = "https://api4.thetvdb.com/v4"
_ARTWORK_BASE  = "https://artworks.thetvdb.com"

# Refresh the ~1-month token comfortably before it expires.
_TOKEN_TTL_SECONDS = 25 * 86400
_TOKEN_CACHE_KEY   = "auth:token"
_TYPES_CACHE_KEY   = "artwork:types"

# Lazily-created asyncio primitives (bind to the running loop on first use).
_token_lock: "asyncio.Lock | None" = None
_semaphore:  "asyncio.Semaphore | None" = None
# In-process token cache so the common path needs neither DB nor login.
_token_mem: str | None = None


def tvdb_enabled() -> bool:
    """True when a TVDB API key is configured."""
    return bool(SERVER_TVDB_KEY)


def tvdb_status() -> str:
    """Compact runtime status for startup logging."""
    if not SERVER_TVDB_KEY:
        return "disabled (no TVDB_API_KEY)"
    return "enabled (token acquired lazily on first use)"


def _get_lock() -> "asyncio.Lock":
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


def _get_semaphore() -> "asyncio.Semaphore":
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(TVDB_CONCURRENCY)
    return _semaphore


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# A failed login is not tried again for a while: every render that wants TVDB
# art would otherwise log in anew, one after another under the token lock,
# each with up to a 15 s timeout.  A rejected key (401) waits for a restart,
# which is when a corrected key would arrive.
_LOGIN_RETRY_SECS = 300.0
_login_failed_at: float | None = None
_login_rejected = False


async def _login(client: httpx.AsyncClient) -> str | None:
    """Exchange the API key for a bearer token. Returns None on failure."""
    global _login_failed_at, _login_rejected
    if _login_rejected:
        return None
    if _login_failed_at is not None and time.monotonic() - _login_failed_at < _LOGIN_RETRY_SECS:
        return None
    payload: dict = {"apikey": SERVER_TVDB_KEY}
    if TVDB_SUBSCRIBER_PIN:
        payload["pin"] = TVDB_SUBSCRIBER_PIN
    try:
        logger.info("External API Call: TVDB login")
        resp = await client.post(f"{_API_BASE}/login", json=payload, timeout=15.0)
        if resp.status_code == 401:
            _login_rejected = True
            logger.error("TVDB rejected the API key (401); TVDB art is off until restart")
            return None
        resp.raise_for_status()
        token = ((resp.json() or {}).get("data") or {}).get("token")
        if not token:
            _login_failed_at = time.monotonic()
            logger.warning("TVDB login returned no token")
            return None
        set_cached_tvdb_json(
            _TOKEN_CACHE_KEY, {"token": token}, _TOKEN_TTL_SECONDS
        )
        _login_failed_at = None
        return token
    except Exception as exc:
        _login_failed_at = time.monotonic()
        logger.warning(f"TVDB login failed: {exc}; not retried for {_LOGIN_RETRY_SECS:.0f}s")
        return None


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str | None:
    """Return a valid bearer token, logging in once (single-flight) as needed."""
    global _token_mem
    if not force:
        if _token_mem:
            return _token_mem
        cached = get_cached_tvdb_json(_TOKEN_CACHE_KEY)
        if cached and cached.get("token"):
            _token_mem = cached["token"]
            return _token_mem
    async with _get_lock():
        # Another coroutine may have refreshed while we waited for the lock.
        if not force:
            if _token_mem:
                return _token_mem
            cached = get_cached_tvdb_json(_TOKEN_CACHE_KEY)
            if cached and cached.get("token"):
                _token_mem = cached["token"]
                return _token_mem
        _token_mem = await _login(client)
        return _token_mem


async def _authed_get(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> dict | None:
    """GET a TVDB endpoint with the bearer token, retrying once on 401.
    Returns the parsed ``data`` payload, or None on any failure."""
    global _token_mem
    token = await _get_token(client)
    if not token:
        return None
    url = f"{_API_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if resp.status_code == 401 and attempt == 1:
                # Token expired/invalid — force one refresh and retry.
                _token_mem = None
                token = await _get_token(client, force=True)
                if not token:
                    return None
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return (resp.json() or {}).get("data")
        except Exception as exc:
            logger.warning(f"TVDB GET {path} failed: {exc}")
            return None
    return None


# ---------------------------------------------------------------------------
# Artwork-type catalogue  (id -> category, derived by slug/name keyword)
# ---------------------------------------------------------------------------

def _classify(slug: str, name: str) -> str | None:
    """Map a TVDB artwork type's slug/name to one of our categories."""
    text = f"{slug} {name}".lower()
    if "clearlogo" in text or text.strip().endswith("logo") or " logo" in text:
        return "logos"
    if "background" in text or "fanart" in text:
        return "backgrounds"
    if "poster" in text:
        return "posters"
    return None


async def _type_map(client: httpx.AsyncClient) -> dict[str, dict[int, str]]:
    """Return ``{record_type: {type_id: category}}`` for movie/series artworks.

    ``category`` is one of 'logos' | 'backgrounds' | 'posters'. Cached long since
    the catalogue is effectively static; classification is keyword-based so a TVDB
    id renumbering can't break us as long as the slug/name still describes the art.
    """
    cached = get_cached_tvdb_json(_TYPES_CACHE_KEY)
    if cached:
        # JSON keys are strings — restore the inner id keys to ints.
        return {
            rt: {int(k): v for k, v in inner.items()}
            for rt, inner in cached.items()
        }
    data = await _authed_get(client, "/artwork/types")
    out: dict[str, dict[int, str]] = {"movie": {}, "series": {}}
    if isinstance(data, list):
        for t in data:
            rt   = (t.get("recordType") or "").lower()
            cat  = _classify(t.get("slug") or "", t.get("name") or "")
            tid  = t.get("id")
            if rt in out and cat and isinstance(tid, int):
                out[rt][tid] = cat
    if out["movie"] or out["series"]:
        set_cached_tvdb_json(
            _TYPES_CACHE_KEY,
            {rt: {str(k): v for k, v in inner.items()} for rt, inner in out.items()},
            TVDB_TYPES_CACHE_DURATION * 86400,
        )
    return out


def _record_type(media_type: str) -> str:
    return "series" if media_type in ("tv", "series") else "movie"


# TVDB tags artwork with ISO 639-2/B (3-letter) codes; the rest of the app uses
# ISO 639-1 (2-letter).  Map the common ones so language-preferred selection
# works; unknown codes pass through unchanged (still matches the neutral/eng/best
# fallbacks in _select_by_language).
_LANG_2_TO_3 = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita",
    "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho", "ru": "rus",
    "nl": "nld", "pl": "pol", "sv": "swe", "da": "dan", "no": "nor",
    "fi": "fin", "tr": "tur", "ar": "ara", "hi": "hin", "cs": "ces",
    "hu": "hun", "el": "ell", "he": "heb", "th": "tha", "uk": "ukr",
    "ro": "ron",
}


def _to_tvdb_lang(code: str | None) -> str | None:
    """Map an app language/locale code to TVDB's 3-letter code.

    TVDB does not tag artwork by region, so region-qualified locales collapse to
    their base language (es-mx → spa): a Mexican-Spanish request still wants
    Spanish artwork, and the alternative is matching nothing at all. The strict
    region separation TMDB gives us simply isn't available from this provider.
    """
    if not code:
        return code
    c = code.strip().lower().replace("_", "-")
    base = c.split("-", 1)[0]
    return _LANG_2_TO_3.get(base, base)


# ---------------------------------------------------------------------------
# ID resolution
# ---------------------------------------------------------------------------

async def resolve_tvdb_id(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tvdb_id_hint: int | str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> int | None:
    """Resolve a TVDB numeric id for a title.

    Prefers an explicit hint (e.g. tvdb_id surfaced by TMDB external_ids), then
    falls back to ``/search/remoteid`` by IMDb id, then by TMDB id. Both positive
    and negative results are cached so repeat misses don't re-hit the API.
    """
    if not tvdb_enabled():
        return None
    if tvdb_id_hint:
        try:
            return int(tvdb_id_hint)
        except (TypeError, ValueError):
            pass

    want = _record_type(media_type)
    cache_key = f"id:{want}:{imdb_id or ''}:{tmdb_id or ''}"
    cached = get_cached_tvdb_json(cache_key)
    if cached is not None:
        return cached.get("tvdb_id")  # may be None (negative cache)

    resolved: int | None = None
    async with _get_semaphore():
        for remote in (imdb_id, tmdb_id):
            if not remote:
                continue
            data = await _authed_get(client, f"/search/remoteid/{remote}")
            if not isinstance(data, list):
                continue
            for item in data:
                rec = item.get(want) if isinstance(item, dict) else None
                if isinstance(rec, dict) and rec.get("id"):
                    try:
                        resolved = int(rec["id"])
                    except (TypeError, ValueError):
                        resolved = None
                    break
            if resolved is not None:
                break

    if resolved:
        logger.info(f"TVDB id resolved: {want} imdb={imdb_id} tmdb={tmdb_id} -> {resolved}")
    else:
        logger.info(f"TVDB no match for {want} imdb={imdb_id} tmdb={tmdb_id}")
    set_cached_tvdb_json(
        cache_key,
        {"tvdb_id": resolved},
        (TVDB_ARTWORK_CACHE_DURATION if resolved else TVDB_NEG_CACHE_DURATION) * 86400,
    )
    return resolved


# ---------------------------------------------------------------------------
# Artwork index
# ---------------------------------------------------------------------------

async def fetch_tvdb_artworks(
    client: httpx.AsyncClient, tvdb_id: int, media_type: str
) -> dict[str, list[dict]]:
    """Return ``{'logos': [...], 'backgrounds': [...], 'posters': [...]}``.

    Each entry is ``{'url': str, 'language': str|None, 'score': float}`` sorted by
    descending score. The artwork index is language-agnostic, so a single fetch
    per TVDB id serves every requested logo language. Results (including empty)
    are cached.
    """
    if not tvdb_enabled():
        return {"logos": [], "backgrounds": [], "posters": []}

    want = _record_type(media_type)
    # v2 keeps includesText (backgrounds read it, see tvdb_poster_url).
    cache_key = f"art:v2:{want}:{tvdb_id}"
    cached = get_cached_tvdb_json(cache_key)
    if cached is not None:
        return cached

    out: dict[str, list[dict]] = {"logos": [], "backgrounds": [], "posters": []}
    async with _get_semaphore():
        type_map = await _type_map(client)
        endpoint = "series" if want == "series" else "movies"
        # short=false guarantees the artworks array is included (short=true drops it).
        data = await _authed_get(
            client, f"/{endpoint}/{tvdb_id}/extended", params={"short": "false"}
        )
    artworks = (data or {}).get("artworks") if isinstance(data, dict) else None
    if isinstance(artworks, list):
        id_to_cat = type_map.get(want, {})
        for art in artworks:
            cat = id_to_cat.get(art.get("type"))
            if not cat:
                continue
            image = art.get("image") or ""
            if not image:
                continue
            url = image if image.startswith("http") else f"{_ARTWORK_BASE}/{image.lstrip('/')}"
            thumb = art.get("thumbnail") or ""
            out[cat].append({
                "url": url,
                "language": art.get("language"),
                "score": float(art.get("score") or 0),
                "text": art.get("includesText"),
                # Only the dashboard's picker shows it; rows cached before it
                # was kept fall back to the full image.
                "thumb": thumb if thumb.startswith("http") or not thumb
                else f"{_ARTWORK_BASE}/{thumb.lstrip('/')}",
            })
        for cat in out:
            out[cat].sort(key=lambda a: a["score"], reverse=True)

    _has_any = any(out[c] for c in out)
    logger.info(
        f"TVDB artworks for tvdb_id={tvdb_id}: "
        f"logos={len(out['logos'])} backgrounds={len(out['backgrounds'])} "
        f"posters={len(out['posters'])}"
    )
    set_cached_tvdb_json(
        cache_key,
        out,
        (TVDB_ARTWORK_CACHE_DURATION if _has_any else TVDB_NEG_CACHE_DURATION) * 86400,
    )
    return out


# ---------------------------------------------------------------------------
# Metadata spine (titles neither TMDB nor Cinemeta know)
# ---------------------------------------------------------------------------

# Bumped whenever the normaliser changes, so rows built by the old logic are
# re-fetched rather than served.
_METADATA_VERSION = "v1"

# TVDB genre slugs that aren't a plain IMDb/TMDB genre name; the rest go
# through Cinemeta's name map.
_GENRE_SLUGS = {
    "science-fiction": "sci-fi",
    "children":        "kids",
    "talk-show":       "talk-show",
    "game-show":       "game-show",
    "suspense":        "thriller",
    "martial-arts":    "action",
    "mini-series":     "drama",
    "anime":           "animation",
}

_STATUS = {
    "continuing": "Returning Series",
    "ended":      "Ended",
    "upcoming":   "Planned",
}


def _pick_name(record: dict, language: str | None) -> str | None:
    """The title in the requested language, else English, else TVDB's own
    ``name`` (which is in the original language)."""
    names = {
        t.get("language"): t.get("name")
        for t in ((record.get("translations") or {}).get("nameTranslations") or [])
        if isinstance(t, dict) and t.get("name")
    }
    for lang in (_to_tvdb_lang(language), "eng"):
        if lang and names.get(lang):
            return names[lang]
    return record.get("name") or None


async def _fetch_record(client: httpx.AsyncClient, tvdb_id: int, want: str) -> dict | None:
    """The slim extended record, cached; None when TVDB has no such id."""
    cache_key = f"meta:{_METADATA_VERSION}:{want}:{tvdb_id}"
    cached = get_cached_tvdb_json(cache_key)
    if cached is not None:
        return None if cached.get("__miss__") else cached
    endpoint = "series" if want == "series" else "movies"
    async with _get_semaphore():
        data = await _authed_get(
            client, f"/{endpoint}/{tvdb_id}/extended",
            params={"short": "true", "meta": "translations"},
        )
    if not isinstance(data, dict) or not data.get("id"):
        # _authed_get folds errors into None, so only a short negative window.
        set_cached_tvdb_json(cache_key, {"__miss__": True}, TVDB_NEG_CACHE_DURATION * 86400)
        return None
    slim = {
        k: data.get(k)
        for k in ("id", "name", "image", "year", "firstAired", "first_release",
                  "originalLanguage", "genres", "remoteIds", "status",
                  "averageRuntime", "runtime", "translations")
        if data.get(k) is not None
    }
    set_cached_tvdb_json(cache_key, slim, TVDB_ARTWORK_CACHE_DURATION * 86400)
    return slim


async def fetch_tvdb_metadata(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tvdb_id_hint: int | str | None = None,
    imdb_id: str | None = None,
    language: str | None = None,
) -> tuple | None:
    """A TVDB record shaped like ``tmdb.fetch_poster_metadata``'s 8-tuple, the
    way ``cinemeta.normalise`` shapes Cinemeta's — the last metadata spine, for
    titles only TVDB lists (small documentaries, parodies, web series).

    The record's own poster is the one-sheet, title and all, so it goes in as
    a text-bearing poster; the best neutral background is the backdrop, which
    main.py's backdrop-to-portrait rule crops and puts our logo (or drawn
    title) on.  None when TVDB is off or has no such title.
    """
    if not tvdb_enabled():
        return None
    want = _record_type(media_type)
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint, imdb_id=imdb_id,
        )
        if not tvdb_id:
            return None
        record = await _fetch_record(client, tvdb_id, want)
        if record is None:
            return None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
    except Exception as exc:
        logger.warning(f"TVDB metadata fetch failed for {tvdb_id_hint or imdb_id}: {exc}")
        return None

    from cinemeta import _blank_tmdb_data, _map_genres

    title = _pick_name(record, language) or "Unknown Title"

    if want == "series":
        date = str(record.get("firstAired") or "")
    else:
        date = str((record.get("first_release") or {}).get("date") or "")
    release_date = date[:10] if len(date) >= 10 and date[4] == "-" else None
    year_src = str(record.get("year") or date)
    release_year = year_src[:4] if year_src[:4].isdigit() else None

    genre_names = []
    for g in record.get("genres") or []:
        if isinstance(g, dict):
            slug = (g.get("slug") or "").lower()
            genre_names.append(_GENRE_SLUGS.get(slug) or g.get("name") or slug)

    poster = record.get("image") or None
    if poster and not poster.startswith("http"):
        poster = f"{_ARTWORK_BASE}/{poster.lstrip('/')}"
    background = _select_by_language(artworks.get("backgrounds") or [], ["null"])

    tmdb_data = _blank_tmdb_data()
    tmdb_data["tvdb_id"]              = tvdb_id
    tmdb_data["cinemeta_source"]      = "tvdb"
    tmdb_data["original_title"]       = record.get("name")
    tmdb_data["tmdb_release_date"]    = release_date
    tmdb_data["original_poster_path"] = poster
    tmdb_data["runtime"] = record.get("averageRuntime") or record.get("runtime") or None
    lang3 = record.get("originalLanguage")
    if lang3:
        tmdb_data["original_language"] = _LANG_3_TO_2.get(lang3, lang3)
    status = ((record.get("status") or {}).get("name") or "").strip()
    if status:
        tmdb_data["tmdb_status"] = _STATUS.get(status.lower(), status)
    for remote in record.get("remoteIds") or []:
        if not isinstance(remote, dict):
            continue
        source, rid = (remote.get("sourceName") or "").lower(), str(remote.get("id") or "")
        if source == "imdb" and rid.startswith("tt"):
            tmdb_data["imdb_id"] = rid
        elif "themoviedb" in source and rid.isdigit():
            tmdb_data["cinemeta_tmdb_id"] = rid

    logger.info(f"TVDB metadata for tvdb_id={tvdb_id}: {title!r} ({release_year})")
    return (_map_genres(genre_names), False, [], release_year, title, poster,
            background["url"] if background else None, tmdb_data)


# A series TMDB has closed ("Ended", or a "Miniseries" of one season) that
# TVDB already lists a further season for — Cyberpunk: Edgerunners, whose
# second season TVDB had dated while TMDB still called it a finished
# miniseries.  Only the status, the next/last aired days and the official
# season numbers are kept; the record goes stale faster than artwork does,
# so it is held for days, not weeks.
_SERIES_STATUS_VERSION = "v1"
_SERIES_STATUS_TTL = 2 * 86400
# _authed_get folds a throttle or an outage into the same None as "no such
# series", so a miss is held only hours: a blip mustn't hide a revival.
_SERIES_STATUS_MISS_TTL = 6 * 3600


async def fetch_series_status(
    client: httpx.AsyncClient,
    *,
    tvdb_id_hint: int | str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> dict | None:
    """``{"status", "next_aired", "last_aired", "seasons"}`` for a series:
    TVDB's status in lower case ("continuing" | "ended" | "upcoming"), its
    next and last aired days (ISO, or None) and its official season numbers
    above 0.  None when TVDB is off, has no such series, or didn't answer."""
    if not tvdb_enabled():
        return None
    try:
        tvdb_id = await resolve_tvdb_id(client, media_type="series", tvdb_id_hint=tvdb_id_hint,
                                        imdb_id=imdb_id, tmdb_id=tmdb_id)
        if not tvdb_id:
            return None
        cache_key = f"status:{_SERIES_STATUS_VERSION}:{tvdb_id}"
        cached = get_cached_tvdb_json(cache_key)
        if cached is not None:
            return None if cached.get("__miss__") else cached
        async with _get_semaphore():
            data = await _authed_get(client, f"/series/{tvdb_id}/extended", params={"short": "true"})
    except Exception as exc:
        logger.warning(f"TVDB series status failed for {tvdb_id_hint or imdb_id or tmdb_id}: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("id"):
        set_cached_tvdb_json(cache_key, {"__miss__": True}, _SERIES_STATUS_MISS_TTL)
        return None

    def _day(value) -> str | None:
        value = str(value or "")
        return value[:10] if len(value) >= 10 and value[4] == "-" else None

    seasons = sorted({
        int(s["number"]) for s in data.get("seasons") or []
        if isinstance(s, dict) and ((s.get("type") or {}).get("type") == "official")
        and isinstance(s.get("number"), int) and s["number"] > 0
    })
    result = {
        "status": ((data.get("status") or {}).get("name") or "").strip().lower() or None,
        "next_aired": _day(data.get("nextAired")),
        "last_aired": _day(data.get("lastAired")),
        "seasons": seasons,
    }
    set_cached_tvdb_json(cache_key, result, _SERIES_STATUS_TTL)
    return result


def _select_by_language(
    items: list[dict],
    languages: list[str] | None,
    *,
    strict: bool = False,
) -> dict | None:
    """Pick the best artwork by language preference. Items are pre-sorted by
    score.  "null" in *languages* stands for language-neutral artwork.

    When ``strict`` is False (backgrounds/posters), the requested languages are
    followed by language-neutral, then English, then an unrelated foreign-
    language item as a last resort.  When ``strict`` is True (logos), the
    requested list is the whole order — it already says where neutral and
    English go — and ``None`` is returned when it runs out, so the caller's
    provider chain (TMDB/Metahub) is tried rather than serving, say, a French
    logo for an English title."""
    if not items:
        return None
    for language in (languages or ()):
        if not language:
            continue
        for it in items:
            if (it.get("language") in (None, "")) if language == "null" \
                    else it.get("language") == language:
                return it
    if strict:
        return None
    for it in items:
        if it.get("language") in (None, ""):
            return it
    for it in items:
        if it.get("language") == "eng":
            return it
    return items[0]


# ---------------------------------------------------------------------------
# Image fetchers
# ---------------------------------------------------------------------------

async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with _get_semaphore():
            resp = await client.get(url, follow_redirects=True, timeout=20.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning(f"TVDB image download failed ({url}): {exc}")
        return None


def _cache_key_for(url: str, prefix: str) -> str:
    # artworks.thetvdb.com paths are stable and unique per image.
    tail = url.split("artworks.thetvdb.com/", 1)[-1]
    return f"tvdb_{prefix}_" + tail.strip("/").replace("/", "_")


def _logo_language_order(
    logo_language: str | None,
    original_language: str | None,
    logo_priority: str,
    secondary_language: str | None = None,
) -> list[str]:
    """Ordered list of TVDB (3-letter) language codes to prefer, derived from the
    same priority TMDB walks so both sources agree on which languages count as
    a match (and, crucially, which don't).  "null" is language-neutral; Metahub
    is not a TVDB source and is left out.

    Deduplicated because region collapsing can fold two distinct TMDB entries
    onto one TVDB code — es-mx before es both become spa."""
    from tmdb import logo_language_steps
    steps = logo_language_steps(
        logo_language or "en", original_language, logo_priority, secondary_language
    )
    return list(dict.fromkeys(
        lang
        for lang in (step if step == "null" else _to_tvdb_lang(step)
                     for step in steps if step != "metahub")
        if lang
    ))


async def fetch_tvdb_logo(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    logo_language: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
    secondary_language: str | None = None,
) -> Image.Image | None:
    """Best TVDB clearlogo as an alpha-trimmed RGBA image, or None."""
    chosen = _select_by_language(
        artworks.get("logos", []),
        _logo_language_order(
            logo_language, original_language, logo_priority, secondary_language
        ),
        strict=True,
    )
    if not chosen:
        return None
    url = chosen["url"]
    cache_key = _cache_key_for(url, "logo")
    cached = get_cached_tmdb_logo(cache_key)
    if cached:
        logger.info("TVDB logo cache hit")
        return Image.open(io.BytesIO(cached)).convert("RGBA")

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB logo parse failed: {exc}")
        return None
    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)
    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(cache_key, buf.getvalue())
    return logo


async def tvdb_logo(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    logo_language: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
    secondary_language: str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
) -> Image.Image | None:
    """One-call logo rescue: resolve the TVDB id, pull the artwork index, return
    the best clearlogo. Safe to call unconditionally — yields None when TVDB is
    disabled or has nothing. All sub-steps are cached, so calling this alongside
    the backdrop/poster helpers in the same request costs at most one API burst.
    """
    from config import TVDB_USE_LOGOS
    if not tvdb_enabled() or not TVDB_USE_LOGOS:
        return None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        logo = await fetch_tvdb_logo(
            client, artworks, logo_language,
            original_language=original_language, logo_priority=logo_priority,
            secondary_language=secondary_language,
        )
        if logo is not None:
            logger.info(f"TVDB logo rescue succeeded for tvdb_id={tvdb_id}")
        else:
            logger.info(f"TVDB logo rescue found no usable logo for tvdb_id={tvdb_id}")
        return logo
    except Exception as exc:
        logger.warning(f"TVDB logo rescue failed: {exc}")
        return None


async def fetch_tvdb_backdrop(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    tvdb_id: int,
    *,
    avoid_text: bool = False,
) -> Image.Image | None:
    """Best TVDB background, cropped to a portrait poster via the same crop logic
    as TMDB backdrops (face-aware → saliency, optional text avoidance)."""
    chosen = _select_by_language(artworks.get("backgrounds", []), None)
    if not chosen:
        return None
    url = chosen["url"]
    # Reuse TMDB's crop + cache-version scheme so behaviour and invalidation match.
    from tmdb import _crop_and_normalise_backdrop, normalise_poster, _CROP_VERSION, poster_canvas, _canvas_suffix
    size = poster_canvas()
    cache_key = (
        _cache_key_for(url, "backdrop") + f"_{_CROP_VERSION}" + ("_ta" if avoid_text else "")
        + _canvas_suffix(size)
    )
    cached = get_cached_tmdb_poster(cache_key)
    if cached:
        logger.info(f"TVDB backdrop cache hit for {tvdb_id}")
        image = Image.open(io.BytesIO(cached)).convert("RGBA")
        if image.size != size:
            image = normalise_poster(image)
        return image

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB backdrop parse failed for {tvdb_id}: {exc}")
        return None
    image = await asyncio.get_running_loop().run_in_executor(
        None, _crop_and_normalise_backdrop, image, f"tvdb:{tvdb_id}", avoid_text, size
    )
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())
    return image


async def tvdb_backdrop(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
    avoid_text: bool = False,
) -> tuple[Image.Image | None, int | None]:
    """One-call backdrop rescue: resolve id, pull artwork index, crop the best
    background to portrait. Returns ``(image, tvdb_id)`` — the id is handed back
    so the caller can build a stable text-detection cache key. ``(None, id)`` when
    there's no usable background; ``(None, None)`` when TVDB is disabled/unmatched.
    """
    from config import TVDB_USE_BACKDROPS
    if not tvdb_enabled() or not TVDB_USE_BACKDROPS:
        return None, None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None, None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        image = await fetch_tvdb_backdrop(client, artworks, tvdb_id, avoid_text=avoid_text)
        return image, tvdb_id
    except Exception as exc:
        logger.warning(f"TVDB backdrop rescue failed: {exc}")
        return None, None


async def fetch_tvdb_poster(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    tvdb_id: int,
    language: str | None = None,
    *,
    textless_only: bool = False,
) -> Image.Image | None:
    """Best TVDB poster, normalised to poster dimensions.

    A language-neutral poster first: TVDB's no-language posters are textless
    in practice, whatever their includesText flag says, while language-tagged
    ones nearly all carry the title — and often in a style our text detection
    misses.  ``textless_only`` stops there; otherwise a poster in *language*
    comes next (a real poster, title and all, beats a genre canvas).  Callers
    still vet the result before compositing a logo over it."""
    posters = artworks.get("posters", [])
    if textless_only:
        chosen = _select_by_language(posters, ["null"], strict=True)
    else:
        _lang = _to_tvdb_lang(language)
        chosen = _select_by_language(posters, ["null"] + ([_lang] if _lang else []))
    if not chosen:
        return None
    url = chosen["url"]
    from tmdb import normalise_poster, poster_canvas, _canvas_suffix
    cache_key = _cache_key_for(url, "poster") + _canvas_suffix(poster_canvas())
    cached = get_cached_tmdb_poster(cache_key)
    if cached:
        logger.info(f"TVDB poster cache hit for {tvdb_id}")
        image = Image.open(io.BytesIO(cached)).convert("RGBA")
        if image.size != poster_canvas():
            image = normalise_poster(image)
        return image

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB poster parse failed for {tvdb_id}: {exc}")
        return None
    image = normalise_poster(image)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())
    return image


async def tvdb_poster(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    language: str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
    textless_only: bool = False,
) -> tuple[Image.Image | None, int | None]:
    """One-call poster rescue: resolve id, pull artwork index, return the best
    poster normalised to poster dimensions, plus the resolved id for the caller's
    text-detection key. TVDB posters usually carry burned-in title text, so the
    caller MUST vet the result before compositing a logo. Gated by TVDB_USE_POSTERS
    (default off)."""
    from config import TVDB_USE_POSTERS
    if not tvdb_enabled() or not TVDB_USE_POSTERS:
        return None, None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None, None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        image = await fetch_tvdb_poster(
            client, artworks, tvdb_id, language, textless_only=textless_only)
        return image, tvdb_id
    except Exception as exc:
        logger.warning(f"TVDB poster rescue failed: {exc}")
        return None, None


# ---------------------------------------------------------------------------
# Poster source (poster_source=tvdb) and the dashboard's candidate list
# ---------------------------------------------------------------------------

_RANDOM_POOL = 5
_LANG_3_TO_2 = {three: two for two, three in _LANG_2_TO_3.items()}


def poster_source_enabled() -> bool:
    from config import TVDB_POSTER_SOURCE
    return bool(TVDB_POSTER_SOURCE and tvdb_enabled())


async def tvdb_poster_url(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tmdb_id: str | None = None,
    imdb_id: str | None = None,
    languages: list[str] | None = None,
    random_top: bool = False,
    kind: str = "posters",
) -> str | None:
    """A TVDB poster url for the tvdb poster source, or None.

    Without *languages*: the best language-neutral poster, which on TVDB means
    textless.  With *languages* (original-art mode): the best poster in the
    first of those languages that has one.  *random_top* picks one of the
    top five instead.  Neither mode trusts includesText.

    ``kind="backgrounds"`` asks the same of the backgrounds, for landscape
    (landscape_art_source=tvdb).  They are tagged the same way: a language
    on one that carries the title (key art, a title card), none on clean
    art.  For them includesText can be trusted where it is false (measured
    2026-10-01: 12 of 13 tagged "false" were clean, where tagged "true" ones
    carry text), so an original pick passes over those: drawn as-is, a clean
    background would leave the title off altogether."""
    if not poster_source_enabled():
        return None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None
        posters = (await fetch_tvdb_artworks(client, tvdb_id, media_type)).get(kind, [])
    except Exception as exc:
        logger.warning(f"TVDB {kind} source failed for {media_type} {tmdb_id}: {exc}")
        return None
    if languages is None:
        pool = [p for p in posters if p.get("language") in (None, "")]
    else:
        pool = []
        for code in dict.fromkeys(_to_tvdb_lang(c) for c in languages if c):
            pool = [p for p in posters if p.get("language") == code
                    and not (kind == "backgrounds" and p.get("text") is False)]
            if pool:
                break
    if not pool:
        return None
    return (random.choice(pool[:_RANDOM_POOL]) if random_top else pool[0])["url"]


async def artwork_candidates(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tmdb_id: str | None = None,
    imdb_id: str | None = None,
) -> dict[str, list[dict]]:
    """Every TVDB poster, logo and background for a title, best first, for
    the dashboard.  Languages come back as the app's 2-letter codes (None =
    neutral)."""
    out: dict[str, list[dict]] = {"posters": [], "logos": [], "backdrops": []}
    if not tvdb_enabled():
        return out
    tvdb_id = await resolve_tvdb_id(
        client, media_type=media_type, imdb_id=imdb_id, tmdb_id=tmdb_id,
    )
    if not tvdb_id:
        return out
    artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
    for kind, source in (("posters", "posters"), ("logos", "logos"), ("backdrops", "backgrounds")):
        for art in artworks.get(source, []):
            code = art.get("language") or None
            out[kind].append({
                "path": art["url"],
                "thumb": art.get("thumb") or art["url"],
                "language": _LANG_3_TO_2.get(code, code) if code else None,
                "score": art.get("score", 0),
            })
    return out
