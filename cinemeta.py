#cinemeta.py
"""Cinemeta (Stremio's catalogue addon) as a key-less, IMDb-keyed art source.

Design notes
------------
- Cinemeta is what Stremio itself reads for every ordinary title: one JSON
  document per IMDb id carrying the name, year, genres, runtime, status, cast,
  director, and the poster / background / logo urls on the Metahub CDN. No key
  is needed for any of it, which makes it the only spine PostersPlus can render
  from when the operator has no TMDB key and the client has no ``tmdb_id``.
- It is a *secondary* spine, like ``anime.py``: it produces the same 8-tuple
  ``tmdb.fetch_poster_metadata`` returns, so main.py renders through the normal
  pipeline with nothing special-cased downstream. It engages only when the TMDB
  path can't — no key, or TMDB has no record for the IMDb id — and as an extra
  no-art rescue tier when TMDB has a record but no artwork. When a TMDB key is
  present and TMDB knows the title, nothing here runs.
- Metahub's ``poster`` is the official one-sheet with the title baked in, and
  ``background`` is a textless-by-design backdrop, so the tuple is shaped
  exactly like a TMDB title with no textless poster: ``is_textless`` False,
  ``backdrop_path`` set, and main.py's existing backdrop-to-portrait fallback
  gives the composited-logo look. Original-art mode gets the poster.
- ``moviedb_id`` on the document is TMDB's id, which is what lets an IMDb-only
  request be resolved to a TMDB id without a TMDB key at all.
- Failures never propagate into a request: any error logs and yields None, and
  main.py falls through to the genre canvas as it does for a TMDB title with no
  art. Only a definite "no such id" (404) is negative-cached — a throttle or an
  outage must not pin a title to the canvas for the whole cache window.
"""
import asyncio
import logging
import re
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

from cache import (
    get_cached_tvdb_json,
    set_cached_tvdb_json,
    delete_cached_tvdb_json,
)
from config import (
    CINEMETA_API_BASE,
    CINEMETA_METADATA_CACHE_DURATION,
    CINEMETA_NEG_CACHE_DURATION,
)

METAHUB_BASE = "https://images.metahub.space"

_SENTINEL_MISS = {"__miss__": True}

# Bumped whenever the normaliser or the genre vocabulary changes, so cached rows
# built by the old logic are re-fetched rather than served.
_METADATA_VERSION = "v3"   # v2: dvdRelease and the episode summary joined the slim row; v3: country

_IMDB_ID_RE = re.compile(r"^tt[0-9]{1,10}\Z")


class _TransientError(Exception):
    """Cinemeta was reachable but couldn't answer right now (throttled, 5xx)."""


# ---------------------------------------------------------------------------
# Vocabulary mapping
# ---------------------------------------------------------------------------

# Cinemeta carries IMDb's genre vocabulary. Mapped to TMDB numeric ids so the
# existing GENRE_MAP / GENRE_PRIORITY machinery (genre canvas, info-sash label)
# works unchanged. IMDb-only genres map to the closest TMDB parent rather than
# being dropped, because a title with no genre at all loses its genre canvas.
_GENRE_IDS: dict[str, int] = {
    "action":       28,
    "adventure":    12,
    "animation":    16,
    "comedy":       35,
    "crime":        80,
    "documentary":  99,
    "drama":        18,
    "family":       10751,
    "fantasy":      14,
    "history":      36,
    "horror":       27,
    "music":        10402,
    "musical":      10402,
    "mystery":      9648,
    "romance":      10749,
    "sci-fi":       878,
    "science fiction": 878,
    "thriller":     53,
    "war":          10752,
    "western":      37,
    # IMDb-only -> closest TMDB parent
    "biography":    36,      # History
    "film-noir":    80,      # Crime
    "sport":        18,      # Drama
    "short":        18,      # Drama
    "reality-tv":   10764,
    "reality":      10764,
    "talk-show":    10767,
    "game-show":    10764,   # Reality
    "news":         10763,
    "soap":         10766,
    "kids":         10762,
}


def _map_genres(names: "list[str]") -> list[int]:
    """Map Cinemeta genre names to deduped TMDB numeric ids."""
    out: list[int] = []
    for name in names or []:
        gid = _GENRE_IDS.get((name or "").strip().lower())
        if gid is not None and gid not in out:
            out.append(gid)
    return out


# Cinemeta's ``status`` is free text from IMDb; the lifecycle sash logic reads
# the TMDB vocabulary, so map the values seen in the wild and pass the rest
# through untouched (an unknown string simply matches no sash).
_STATUS = {
    "ended":      "Ended",
    "continuing": "Returning Series",
    "canceled":   "Canceled",
    "cancelled":  "Canceled",
}


# ---------------------------------------------------------------------------
# Metahub art urls
# ---------------------------------------------------------------------------

def poster_url(imdb_id: str, size: str = "medium") -> str:
    """Metahub poster. ``medium`` is 500x750 — the portrait canvas exactly."""
    return f"{METAHUB_BASE}/poster/{size}/{imdb_id}/img"


def background_url(imdb_id: str, size: str = "large") -> str:
    """Metahub background. ``large`` is 1920x1080, the same class of source
    the TMDB w1280 backdrop is, so the portrait crop has room to work."""
    return f"{METAHUB_BASE}/background/{size}/{imdb_id}/img"


# ---------------------------------------------------------------------------
# Normalisation to the metadata tuple
# ---------------------------------------------------------------------------

def _blank_tmdb_data() -> dict:
    """Every key main.py and the sash/discovery helpers read off ``tmdb_data``,
    with neutral empties, so no consumer has to know the spine isn't TMDB."""
    return {
        "credits":              {},
        "production_companies": [],
        "original_language":    None,
        "original_title":       None,
        "runtime":              None,
        "number_of_seasons":    None,
        "number_of_episodes":   None,
        "tmdb_status":          None,
        "vote_count":           None,
        "text_backdrop_path":   None,
        "alt_poster_path":      None,
        "original_poster_path": None,
        "poster_langs":         {},
        "imdb_id":              None,
        "tmdb_release_date":    None,
        "last_air_date":        None,
        "next_episode":         None,
        "last_episode":         None,
        "seasons":              [],
        # Cinemeta extras consumed by main.py.
        "cinemeta_source":      None,
        "cinemeta_tmdb_id":     None,
    }


def _parse_runtime(raw) -> int | None:
    """``"142 min"`` -> 142. Cinemeta occasionally ships ``"1h 30min"``."""
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw)
    hours = re.search(r"(\d+)\s*h", text)
    mins  = re.search(r"(\d+)\s*min", text)
    if hours or mins:
        return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)
    digits = re.search(r"\d+", text)
    return int(digits.group(0)) if digits else None


def _iso_date(value) -> str | None:
    """YYYY-MM-DD from Cinemeta's ISO timestamps, or None."""
    text = str(value or "")
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def summarise_episodes(videos) -> dict | None:
    """The TV structure the lifecycle sashes read, from Cinemeta's episode list.

    Cinemeta lists every episode with its season, number and air date.  TMDB
    hands the same facts over as ``number_of_seasons`` / ``number_of_episodes``,
    a ``seasons`` list and the last aired / next-to-air episodes; this builds
    those from the list so mini-series, binge-ready, new-season, returning
    and finale all work on a Cinemeta-spined title.  Specials (season 0) are
    left out, as TMDB leaves them out of its counts.
    """
    if not isinstance(videos, list) or not videos:
        return None
    from datetime import date as _date
    today = _date.today().isoformat()
    per_season: dict[int, dict] = {}
    last_ep: dict | None = None
    next_ep: dict | None = None
    for v in videos:
        if not isinstance(v, dict):
            continue
        try:
            season = int(v.get("season"))
            number = int(v.get("episode") or v.get("number"))
        except (TypeError, ValueError):
            continue
        if season <= 0:
            continue
        aired = _iso_date(v.get("released") or v.get("firstAired"))
        row = per_season.setdefault(season, {"season_number": season, "episode_count": 0, "air_date": None})
        row["episode_count"] += 1
        if aired and (row["air_date"] is None or aired < row["air_date"]):
            row["air_date"] = aired
        if not aired:
            continue
        ep = {"season_number": season, "episode_number": number, "air_date": aired}
        if aired <= today:
            if last_ep is None or (aired, season, number) > (last_ep["air_date"], last_ep["season_number"], last_ep["episode_number"]):
                last_ep = ep
        elif next_ep is None or (aired, season, number) < (next_ep["air_date"], next_ep["season_number"], next_ep["episode_number"]):
            next_ep = ep
    if not per_season:
        return None
    seasons = [per_season[k] for k in sorted(per_season)]
    return {
        "number_of_seasons":  len(seasons),
        "number_of_episodes": sum(s["episode_count"] for s in seasons),
        "seasons":            seasons,
        "last_episode":       last_ep,
        "next_episode":       next_ep,
    }


def normalise(meta: dict, imdb_id: str) -> tuple:
    """Shape a Cinemeta ``meta`` document like ``tmdb.fetch_poster_metadata``:

        (genre_ids, is_textless, logos, release_year, title, poster_path,
         backdrop_path, tmdb_data)

    ``poster_path`` / ``backdrop_path`` are absolute Metahub urls; the fetchers
    in tmdb.py detect that and download them directly. ``logos`` is empty —
    Metahub's logo is reached through the existing IMDb-keyed logo fallback.
    """
    title = meta.get("name") or "Unknown Title"

    # ``year`` is "1994" for a film, "2008–2013" or "2019–" for a series.
    raw_year = str(meta.get("year") or meta.get("releaseInfo") or "")
    year_match = re.match(r"(\d{4})", raw_year)
    release_year = year_match.group(1) if year_match else None

    released = str(meta.get("released") or "")
    release_date = released[:10] if re.match(r"\d{4}-\d{2}-\d{2}", released) else None

    # The document names art urls for every title whether or not the CDN has
    # the image, so these are only candidates: fetch_cinemeta_metadata probes
    # the CDN and drops the ones that 404. Read straight from the document,
    # the flags are True for every title Cinemeta knows.
    poster = poster_url(imdb_id) if meta.get("poster") else None
    backdrop = background_url(imdb_id) if meta.get("background") else None

    tmdb_data = _blank_tmdb_data()
    tmdb_data["imdb_id"]           = imdb_id
    tmdb_data["tmdb_release_date"] = release_date
    tmdb_data["runtime"]           = _parse_runtime(meta.get("runtime"))
    tmdb_data["cinemeta_source"]   = "cinemeta"
    # A movie's theatrical and disc dates, for the release-status sash when
    # TMDB's /release_dates is out of reach (no key).  Digital availability
    # comes from the r/movieleaks cache, which is IMDb-keyed already.
    tmdb_data["cinemeta_theatrical_date"] = release_date
    tmdb_data["cinemeta_physical_date"]   = _iso_date(meta.get("dvdRelease"))
    # TV structure, in TMDB's shape, from the episode summary the cache keeps.
    episodes = meta.get("episodes") or summarise_episodes(meta.get("videos"))
    if episodes:
        for key in ("number_of_seasons", "number_of_episodes", "seasons",
                    "last_episode", "next_episode"):
            tmdb_data[key] = episodes.get(key)
    # The one-sheet doubles as the original-art candidate so textless=false
    # renders the poster as-is, the way it does for a TMDB title.
    tmdb_data["original_poster_path"] = poster

    status = meta.get("status")
    if status:
        tmdb_data["tmdb_status"] = _STATUS.get(str(status).strip().lower(), status)

    tmdb_id = meta.get("moviedb_id")
    if tmdb_id is not None and str(tmdb_id).isascii() and str(tmdb_id).isdigit():
        tmdb_data["cinemeta_tmdb_id"] = str(tmdb_id)

    # Cast / director in TMDB's credits shape, so the studio/director/cast
    # sashes keep matching. Cinemeta's lists are plain name strings.
    cast = [
        {"name": name} for name in (meta.get("cast") or []) if isinstance(name, str) and name
    ]
    crew = [
        {"job": "Director", "name": name}
        for name in (meta.get("director") or []) if isinstance(name, str) and name
    ]
    if cast or crew:
        tmdb_data["credits"] = {"cast": cast, "crew": crew}

    genre_ids = _map_genres(meta.get("genres") or meta.get("genre") or [])
    return genre_ids, False, [], release_year, title, poster, backdrop, tmdb_data


def empty_metadata() -> tuple:
    """The "Cinemeta has nothing for this id" tuple, same shape as ``normalise``,
    so the caller renders the genre canvas through the normal no-art path."""
    return [], False, [], None, "Unknown Title", None, None, _blank_tmdb_data()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _cinemeta_type(media_type: str) -> str:
    return "series" if media_type in ("tv", "series") else "movie"


def _cache_key(imdb_id: str, media_type: str) -> str:
    return f"cinemeta:{_METADATA_VERSION}:{_cinemeta_type(media_type)}:{imdb_id}"


async def _fetch_meta(client: httpx.AsyncClient, imdb_id: str, media_type: str) -> dict | None:
    url = f"{CINEMETA_API_BASE}/meta/{_cinemeta_type(media_type)}/{imdb_id}.json"
    logger.info(f"External API Call: Requested Cinemeta meta for {imdb_id}")
    resp = await client.get(url, timeout=15.0, follow_redirects=True)
    if resp.status_code == 404:
        return None
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _TransientError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    meta = (resp.json() or {}).get("meta")
    # Cinemeta answers an unknown id with 200 and an empty document.
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    return meta


async def fetch_cinemeta_meta(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str,
    want_country: bool = False,
) -> dict | None:
    """The raw Cinemeta ``meta`` document, cached. None when Cinemeta has no
    entry or is unavailable.

    ``country`` joined the cached document after v2 rows were written; a
    caller that reads it (*want_country*) has a row from before refetched,
    rather than every title paying for a version bump."""
    if not _IMDB_ID_RE.match(imdb_id or ""):
        return None
    key = _cache_key(imdb_id, media_type)
    cached = get_cached_tvdb_json(key)
    if cached is not None and not (want_country and not cached.get("__miss__")
                                   and "country" not in cached):
        if cached.get("__miss__"):
            logger.info(f"Cinemeta negative cache hit for {imdb_id}")
            return None
        logger.info(f"Cinemeta metadata cache hit for {imdb_id}")
        return cached

    try:
        meta = await _fetch_meta(client, imdb_id, media_type)
    except _TransientError as exc:
        logger.warning(f"Cinemeta unavailable for {imdb_id}: {exc}")
        return None
    except Exception as exc:
        logger.warning(f"Cinemeta fetch failed for {imdb_id}: {type(exc).__name__}: {exc}")
        return None

    if meta is None:
        set_cached_tvdb_json(key, _SENTINEL_MISS, CINEMETA_NEG_CACHE_DURATION * 86400)
        return None

    # Only the fields the normaliser reads — the document also carries every
    # episode of a series, which is dead weight in the cache.  What the
    # lifecycle sashes need from those episodes is summarised first.
    slim = {
        k: meta.get(k)
        for k in (
            "id", "name", "year", "releaseInfo", "released", "dvdRelease",
            "runtime", "status", "genres", "genre", "cast", "director",
            "poster", "background", "logo", "moviedb_id", "imdbRating",
        )
        if meta.get(k) is not None
    }
    # Always present, so a row without it is one from before it was kept.
    slim["country"] = meta.get("country")
    episodes = summarise_episodes(meta.get("videos"))
    if episodes:
        slim["episodes"] = episodes
    set_cached_tvdb_json(key, slim, CINEMETA_METADATA_CACHE_DURATION * 86400)
    return slim


async def fetch_cinemeta_metadata(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str,
) -> tuple | None:
    """Return the same 8-tuple shape as ``tmdb.fetch_poster_metadata``, or None
    when Cinemeta has no usable entry (the caller renders the genre canvas).

    Art the CDN doesn't actually serve is dropped here, so a title Cinemeta
    knows but has no images for takes the ordinary no-art path rather than
    failing on a 404 mid-render.
    """
    meta = await fetch_cinemeta_meta(client, imdb_id, media_type)
    if meta is None:
        return None
    genre_ids, is_textless, logos, year, title, poster, backdrop, tmdb_data = normalise(meta, imdb_id)
    if poster or backdrop:
        has_poster, has_background = await probe_art(client, imdb_id)
        if not has_poster:
            poster = None
            tmdb_data["original_poster_path"] = None
        if not has_background:
            backdrop = None
    return genre_ids, is_textless, logos, year, title, poster, backdrop, tmdb_data


# ---------------------------------------------------------------------------
# Art availability
# ---------------------------------------------------------------------------

_ART_PROBE_VERSION = "v1"


def _art_probe_key(imdb_id: str) -> str:
    return f"cinemeta:art:{_ART_PROBE_VERSION}:{imdb_id}"


async def _head_ok(client: httpx.AsyncClient, url: str) -> bool | None:
    """True/False for a definite answer from the CDN, None when it couldn't be
    reached (so the caller doesn't cache a blip as a missing image)."""
    try:
        resp = await client.head(url, timeout=10.0, follow_redirects=True)
    except Exception as exc:
        logger.warning(f"Metahub probe failed for {url}: {type(exc).__name__}: {exc}")
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    logger.warning(f"Metahub probe for {url}: HTTP {resp.status_code}")
    return None


async def probe_art(client: httpx.AsyncClient, imdb_id: str) -> "tuple[bool, bool]":
    """(has_poster, has_background) as the Metahub CDN actually serves them.

    Cached: a definite answer for both for the metadata window, a definite
    miss for both for the negative window, and not at all when either probe
    couldn't be completed.
    """
    key = _art_probe_key(imdb_id)
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        return bool(cached.get("poster")), bool(cached.get("background"))

    logger.info(f"External API Call: Probing Metahub art for {imdb_id}")
    has_poster = await _head_ok(client, poster_url(imdb_id))
    has_background = await _head_ok(client, background_url(imdb_id))
    if has_poster is None or has_background is None:
        return bool(has_poster), bool(has_background)

    ttl_days = CINEMETA_METADATA_CACHE_DURATION if (has_poster or has_background) else CINEMETA_NEG_CACHE_DURATION
    set_cached_tvdb_json(key, {"poster": has_poster, "background": has_background}, ttl_days * 86400)
    return has_poster, has_background


def invalidate_art_probe(imdb_id: str) -> None:
    """Forget a probe result — called when a fetch of the art it vouched for
    came back 404, so the next request re-probes instead of failing again."""
    delete_cached_tvdb_json(_art_probe_key(imdb_id))


_SEARCH_LIMIT = 10


async def search(client: httpx.AsyncClient, query: str, limit: int = _SEARCH_LIMIT) -> list[dict]:
    """Title search over Cinemeta's movie and series catalogues, no key needed.

    Returns TMDB ``search/multi``-shaped rows, so the configurator's result
    list reads them as it reads TMDB's: ``media_type``, ``title`` /
    ``release_date`` for a movie, ``name`` / ``first_air_date`` for a show.
    Two fields differ, and are what a key-less caller has to work with:
    ``id`` is None — the catalogue carries no TMDB id — and ``imdb_id`` is
    set; ``poster_url`` is an absolute url in place of a TMDB ``poster_path``.
    Movies and series are interleaved in their own rank order, since the two
    catalogues cannot be ranked against each other.
    """
    query = (query or "").strip()
    if not query:
        return []

    async def _catalog(kind: str) -> list[dict]:
        url = f"{CINEMETA_API_BASE}/catalog/{kind}/top/search={quote(query)}.json"
        try:
            logger.info(f"External API Call: Cinemeta {kind} search for {query!r}")
            resp = await client.get(url, timeout=15.0, follow_redirects=True)
            if resp.status_code != 200:
                return []
            metas = (resp.json() or {}).get("metas") or []
        except Exception as exc:
            logger.warning(f"Cinemeta {kind} search failed: {type(exc).__name__}: {exc}")
            return []
        return [m for m in metas if isinstance(m, dict) and _IMDB_ID_RE.match(m.get("imdb_id") or m.get("id") or "")]

    movies, series = await asyncio.gather(_catalog("movie"), _catalog("series"))

    def _row(meta: dict, media_type: str) -> dict:
        imdb = meta.get("imdb_id") or meta.get("id")
        year = str(meta.get("releaseInfo") or meta.get("year") or "")[:4]
        date = f"{year}-01-01" if year.isascii() and year.isdigit() else ""
        row = {
            "media_type":  media_type,
            "id":          None,
            "imdb_id":     imdb,
            "poster_path": None,
            "poster_url":  meta.get("poster") or None,
        }
        if media_type == "movie":
            row.update({"title": meta.get("name") or "", "release_date": date})
        else:
            row.update({"name": meta.get("name") or "", "first_air_date": date})
        return row

    rows: list[dict] = []
    for i in range(max(len(movies), len(series))):
        if i < len(movies):
            rows.append(_row(movies[i], "movie"))
        if i < len(series):
            rows.append(_row(series[i], "tv"))
    return rows[:limit]


async def resolve_tmdb_id(
    client: httpx.AsyncClient,
    imdb_id: str,
    media_type: str,
) -> str | None:
    """TMDB id for an IMDb id, from Cinemeta's ``moviedb_id`` — no TMDB key
    needed. None when Cinemeta has no entry or carries no TMDB id for it."""
    meta = await fetch_cinemeta_meta(client, imdb_id, media_type)
    if not meta:
        return None
    tmdb_id = meta.get("moviedb_id")
    return str(tmdb_id) if tmdb_id is not None and str(tmdb_id).isascii() and str(tmdb_id).isdigit() else None


