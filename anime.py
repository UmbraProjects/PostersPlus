#anime.py
"""AniList / Kitsu as *primary* art+metadata sources for anime-native ids.

Design notes
------------
- Unlike ``tvdb.py`` (a fallback that only supplies pixels), this module is a
  primary provider: when a client passes ``anilist_id`` or ``kitsu_id`` there is
  no TMDB record involved at all, so the module must produce the full metadata
  tuple ``fetch_poster_metadata`` returns and main.py consumes.
- No id conversion happens anywhere. The caller (AIOMetadata and friends) hands
  us the anime-native id; if they can't, the feature simply doesn't engage.
  Simpler metadata providers group anime under TV series with tmdb/imdb ids and
  keep working exactly as before.
- Both providers return artwork, titles, genres, air dates, status *and* a
  community score in a single request, so the rating arrives at zero marginal
  HTTP cost and is injected into the normal weighted-score pipeline as the
  ``anilist`` / ``kitsu`` source.
- These are single-poster providers: there is exactly one cover image per title,
  it is portrait, and it essentially always has the title logotype baked into
  the art. So ``is_textless`` is always False and callers must treat the result
  the way ``use_original_art`` is treated — no logo compositing, no text
  detection, no backdrop rescue. AniList banners are 1900x400 and would be
  destroyed by the portrait crop, so no backdrop is offered either.
- Failures never propagate into a request: any error logs and yields None, and
  main.py falls through to the genre canvas exactly as it does for a TMDB title
  with no art.
"""
import asyncio
import time
import hashlib
import logging

import httpx

logger = logging.getLogger(__name__)

from cache import (
    get_cached_tvdb_json,
    set_cached_tvdb_json,
)
from config import (
    ANILIST_CONCURRENCY,
    KITSU_CONCURRENCY,
    ANIME_METADATA_CACHE_DURATION,
    ANIME_NEG_CACHE_DURATION,
    ANILIST_API_URL,
    KITSU_API_BASE,
    JIKAN_API_URL,
)

# Namespaces accepted on the wire, in the order they're probed by the request
# handler.  Kept as a tuple so the id-parsing helper and the query-param list in
# main.py can't drift apart.
NAMESPACES = ("anilist", "kitsu")
# Also parsed, though never fetched from: MAL needs auth, so a MAL id is
# translated to a provider id through anime_ids before anything is fetched.
ID_NAMESPACES = NAMESPACES + ("mal",)

_SENTINEL_MISS = {"__miss__": True}


class _TransientError(Exception):
    """Provider was reachable but couldn't answer right now (throttled, 5xx).

    Distinct from "no such id" because only the latter may be negative-cached:
    caching a throttle would pin the title to the genre canvas for the whole
    negative-cache window, which on an AniList rate-limit burst would affect
    every uncached title in the catalogue at once.
    """


# Lazily-created asyncio primitives (bind to the running loop on first use).
# One per provider: sharing a semaphore would make Kitsu queue behind AniList's
# much tighter budget for no reason.
_CONCURRENCY = {"anilist": ANILIST_CONCURRENCY, "kitsu": KITSU_CONCURRENCY, "jikan": 2}
_semaphores: "dict[str, asyncio.Semaphore]" = {}


# AniList's Retry-After, honoured across every caller: once it throttles,
# further calls until then only earn more 429s (and, in a catalog burst, keep
# the limit from resetting).  Per worker, which is where the budget is spent.
_anilist_quiet_until = 0.0


def anilist_cooling() -> bool:
    """Whether AniList asked us to wait and the wait isn't over."""
    return time.monotonic() < _anilist_quiet_until


def note_anilist_throttle(retry_after: "str | None") -> None:
    """Start the wait an AniList 429 asked for (60 s when it named none)."""
    global _anilist_quiet_until
    try:
        wait = min(max(float(retry_after), 1.0), 300.0)
    except (TypeError, ValueError):
        wait = 60.0
    _anilist_quiet_until = max(_anilist_quiet_until, time.monotonic() + wait)


def _get_semaphore(namespace: str) -> "asyncio.Semaphore":
    sem = _semaphores.get(namespace)
    if sem is None:
        sem = asyncio.Semaphore(_CONCURRENCY.get(namespace, 3))
        _semaphores[namespace] = sem
    return sem


# ---------------------------------------------------------------------------
# Id handling
# ---------------------------------------------------------------------------

def parse_anime_id(namespace: str, raw: str) -> int | None:
    """Validate a caller-supplied anime id.  Both providers use plain positive
    integers; anything else is rejected rather than passed upstream."""
    if namespace not in ID_NAMESPACES:
        return None
    raw = (raw or "").strip()
    # Tolerate the Stremio-style prefixed form ("kitsu:12345") as well as a bare
    # id, since clients differ on which they send.
    if ":" in raw:
        prefix, _, rest = raw.partition(":")
        if prefix.strip().lower() != namespace:
            return None
        raw = rest.strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    return value if 0 < value < 10_000_000 else None


def parse_stremio_id(raw: str) -> "tuple[str | None, int | None]":
    """Pull an anime namespace out of a raw Stremio meta id.

    This is the preferred way to receive an anime id, because a client can pass
    it with a plain, always-populated placeholder — AIOMetadata's ``{id}``,
    present in every version — rather than an per-namespace one that is empty
    for most titles. A placeholder that is empty for live-action titles forces
    the client into the optional ``{name?}`` syntax, which not every build or
    addon accepts.

    Stremio ids look like ``tt0903747``, ``tmdb:1396``, ``kitsu:7442`` or
    ``kitsu:7442:1:2`` (with season/episode). Only the anime namespaces we
    support are recognised — ``mal:`` included, which the caller translates to
    a provider id; everything else returns (None, None) so the caller takes the
    ordinary TMDB path.
    """
    raw = (raw or "").strip()
    if ":" not in raw:
        return None, None
    namespace, _, rest = raw.partition(":")
    namespace = namespace.strip().lower()
    if namespace not in ID_NAMESPACES:
        # tt…, tmdb:, tvdb:, and anidb:, which we don't source from.
        return None, None
    # Drop any season/episode suffix.
    parsed = parse_anime_id(namespace, rest.split(":", 1)[0])
    return (namespace, parsed) if parsed is not None else (None, None)


def namespaced_id(namespace: str, anime_id: int) -> str:
    """Canonical string id used for cache keys and scraper stream ids.

    Deliberately keeps the ``<namespace>:<id>`` shape: it can never collide with
    a bare numeric TMDB id or a ``tt``-prefixed IMDb id in the shared cache
    tables, and it is exactly the form Torrentio/Comet/AIOStreams already expect
    for anime stream lookups.
    """
    return f"{namespace}:{anime_id}"


# ---------------------------------------------------------------------------
# Vocabulary mapping
# ---------------------------------------------------------------------------

# AniList and Kitsu genre/category names -> TMDB numeric genre ids, so the
# existing GENRE_MAP / GENRE_PRIORITY machinery (genre canvas, info sash label)
# works unchanged.  Anime-only genres with no TMDB equivalent are mapped to the
# closest sensible parent rather than dropped, because a title that resolves to
# no genre at all falls back to "Unknown" and loses its genre background.
_GENRE_IDS: dict[str, int] = {
    # Direct equivalents
    "action":         28,
    "adventure":      12,
    "comedy":         35,
    "crime":          80,
    "documentary":    99,
    "drama":          18,
    "fantasy":        14,
    "horror":         27,
    "music":          10402,
    "musical":        10402,
    "mystery":        9648,
    "romance":        10749,
    "sci-fi":         878,
    "science fiction": 878,
    "thriller":       53,
    "western":        37,
    "historical":     36,
    "history":        36,
    # Anime-specific -> closest TMDB parent
    "mecha":          878,    # Sci-Fi
    "psychological":  53,     # Thriller
    "supernatural":   14,     # Fantasy
    "mahou shoujo":   14,     # Fantasy
    "magic":          14,
    "slice of life":  18,     # Drama
    "military":       10752,  # War
    "war":            10752,
    "kids":           10762,
    "family":         10751,
}

# Every title from these providers is animation by definition.  Appending 16
# guarantees a genre is always resolvable — GENRE_PRIORITY ranks Animation below
# the specific genres, so "Animation" only wins when nothing more specific
# matched, and a genre background always exists.
_ANIMATION_GENRE_ID = 16

# Provider release status -> the TMDB status strings main.py's lifecycle sash
# logic already understands.
_ANILIST_STATUS = {
    "FINISHED":         "Ended",
    "RELEASING":        "Returning Series",
    "NOT_YET_RELEASED": "Planned",
    "CANCELLED":        "Canceled",
    "HIATUS":           "Returning Series",
}
_KITSU_STATUS = {
    "finished":   "Ended",
    "current":    "Returning Series",
    "tba":        "Planned",
    "unreleased": "Planned",
    "upcoming":   "Planned",
}

# Kitsu ageRating -> the numeric age the badge renderer expects (it stringifies
# whatever it is given, so these must be the displayed numerals).  AniList only
# exposes a boolean isAdult, which is handled separately.
_KITSU_AGE_RATING = {
    "G":   0,
    "PG":  12,
    "R":   17,
    "R18": 18,
}

# Formats that should render as a movie rather than a series.  Everything else
# (TV, TV_SHORT, ONA, OVA, SPECIAL, MUSIC) behaves as a series for rating
# weights and scraper stream ids.
_MOVIE_FORMATS = {"MOVIE", "movie"}


def _map_genres(names: "list[str]") -> list[int]:
    """Map provider genre names to deduped TMDB numeric ids, always including
    Animation as the guaranteed floor."""
    out: list[int] = []
    for name in names or []:
        gid = _GENRE_IDS.get((name or "").strip().lower())
        if gid is not None and gid not in out:
            out.append(gid)
    if _ANIMATION_GENRE_ID not in out:
        out.append(_ANIMATION_GENRE_ID)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    title { romaji english native }
    startDate { year month day }
    seasonYear
    genres
    averageScore
    popularity
    status
    format
    episodes
    duration
    isAdult
    coverImage { extraLarge large }
    bannerImage
    synonyms
    relations { edges { relationType node { id type format } } }
    studios(isMain: true) { nodes { name } }
  }
}
"""


async def _fetch_anilist(client: httpx.AsyncClient, anime_id: int) -> dict | None:
    """One GraphQL call returning art, titles, genres, status and score."""
    if anilist_cooling():
        raise _TransientError("AniList asked us to wait (Retry-After)")
    logger.info(f"External API Call: AniList metadata fetch for {anime_id}")
    resp = await client.post(
        ANILIST_API_URL,
        json={"query": _ANILIST_QUERY, "variables": {"id": anime_id}},
        timeout=15.0,
    )
    # AniList reports "not found" as a 404 (definitive) and throttling as a 429
    # with Retry-After (transient).
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        note_anilist_throttle(resp.headers.get("retry-after"))
        raise _TransientError(
            f"AniList rate-limited (retry-after={resp.headers.get('retry-after')})"
        )
    if resp.status_code != 200:
        raise _TransientError(f"AniList error {resp.status_code}")

    payload = resp.json()
    media = (payload.get("data") or {}).get("Media")
    if not media:
        logger.info(f"AniList has no entry for {anime_id}")
        return None
    return media


_ANILIST_TRENDING_QUERY = """
query ($page: Int, $perPage: Int, $statusNot: MediaStatus, $formats: [MediaFormat]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: TRENDING_DESC, isAdult: false, format_in: $formats,
          status_not: $statusNot) {
      id
      title { romaji english }
      seasonYear
      startDate { year }
      coverImage { extraLarge large }
      genres
    }
  }
}
"""

# Series and films are ranked apart, one list per catalog: an anime film typed
# as a series would open as one.  Ranks are numbered after this filter, so a
# rank is always the title's position in its catalog.
_ANILIST_SERIES_FORMATS = ["TV", "TV_SHORT", "ONA"]
_ANILIST_FILM_FORMATS = ["MOVIE"]
_ANILIST_TRENDING_PAGE = 50


async def fetch_anilist_trending(
    client: httpx.AsyncClient, details_out: dict | None = None, films: bool = False,
    limit: int | None = None, hide_unreleased: bool | None = None,
) -> "list[str] | None":
    """AniList's trending anime series (or with *films*, anime films) as
    ``anilist:<id>`` keys, in rank order, up to *limit* (the broad trending
    count by default), without what hasn't started airing when
    *hide_unreleased* (TRENDING_HIDE_UNRELEASED by default).
    None when AniList could not be read.

    For the trending catalogs addon: the anime catalogs and the rank printed
    on any anime poster both come from these lists.
    """
    from config import TRENDING_BROAD_FETCH_COUNT, TRENDING_FETCH_COUNT, TRENDING_HIDE_UNRELEASED
    limit = limit or max(TRENDING_FETCH_COUNT, TRENDING_BROAD_FETCH_COUNT)
    # AniList drops what hasn't started airing itself; null filters nothing.
    if hide_unreleased is None:
        hide_unreleased = TRENDING_HIDE_UNRELEASED
    status_not = "NOT_YET_RELEASED" if hide_unreleased else None
    ids: list[str] = []
    page = 1
    logger.info(f"External API Call: AniList trending anime {'films' if films else 'series'}")
    try:
        while len(ids) < limit:
            async with _get_semaphore("anilist"):
                resp = await client.post(
                    ANILIST_API_URL,
                    json={
                        "query": _ANILIST_TRENDING_QUERY,
                        "variables": {"page": page, "perPage": _ANILIST_TRENDING_PAGE,
                                      "statusNot": status_not,
                                      "formats": _ANILIST_FILM_FORMATS if films
                                      else _ANILIST_SERIES_FORMATS},
                    },
                    timeout=15.0,
                )
            if resp.status_code != 200:
                raise _TransientError(f"AniList error {resp.status_code}")
            data = ((resp.json().get("data") or {}).get("Page")) or {}
            for media in data.get("media") or []:
                key = namespaced_id("anilist", int(media["id"]))
                if key in ids:
                    continue
                ids.append(key)
                if details_out is not None:
                    title = media.get("title") or {}
                    cover = media.get("coverImage") or {}
                    # Films have no season; their start year stands in.
                    year = media.get("seasonYear") or (media.get("startDate") or {}).get("year")
                    details_out[key] = {k: v for k, v in {
                        "name": title.get("english") or title.get("romaji"),
                        "year": str(year) if year else None,
                        "poster": cover.get("extraLarge") or cover.get("large"),
                        # TMDB ids, for the catalogs' genre filter.
                        "genres": _map_genres(media.get("genres") or []),
                    }.items() if v}
                if len(ids) >= limit:
                    break
            if not (data.get("pageInfo") or {}).get("hasNextPage"):
                break
            page += 1
    except Exception as exc:
        logger.error(f"AniList trending fetch failed: {exc}")
        return None
    return ids


async def _fetch_kitsu(client: httpx.AsyncClient, anime_id: int) -> dict | None:
    """One JSON:API call.  Both vocabularies are sideloaded in the same response,
    so no second round-trip is needed.

    Kitsu exposes two of them and they are very different in quality:

    * ``genres`` — the older relationship, a short list of actual genres
      (Attack on Titan: Action, Drama, Mystery, Fantasy, Super Power, Military).
    * ``categories`` — the current one, a descriptive tag cloud of 9-21 entries
      mixing genres with themes, demographics and settings (the same title also
      carries Post Apocalypse, Violence, Angst, Shounen, Cops, Horror).

    ``categories`` is what makes the genre label read oddly: tags like Horror or
    Crime are attached to shows that are neither, and then win the priority
    order. ``genres`` is preferred for that reason, but it is not fully
    populated — some titles (e.g. Demon Slayer) have none at all — so
    ``categories`` remains the fallback rather than the primary.
    """
    logger.info(f"External API Call: Kitsu metadata fetch for {anime_id}")
    resp = await client.get(
        f"{KITSU_API_BASE}/anime/{anime_id}",
        params={"include": "genres,categories"},
        headers={"Accept": "application/vnd.api+json"},
        timeout=15.0,
        follow_redirects=True,
    )
    if resp.status_code == 404:
        logger.info(f"Kitsu has no entry for {anime_id}")
        return None
    if resp.status_code != 200:
        raise _TransientError(f"Kitsu error {resp.status_code}")

    payload = resp.json()
    data = payload.get("data")
    if not data:
        return None

    # Both vocabularies live in the sideloaded `included` array, not on the
    # record — and they use different attribute names ("name" vs "title").
    included = payload.get("included") or []
    genres = [
        (item.get("attributes") or {}).get("name")
        for item in included
        if item.get("type") == "genres"
    ]
    categories = [
        (item.get("attributes") or {}).get("title")
        for item in included
        if item.get("type") == "categories"
    ]
    genres = [g for g in genres if g]
    categories = [c for c in categories if c]
    # Prefer the clean list; fall back to the tag cloud when it is empty.
    data["_genres"] = genres or categories
    return data


# ---------------------------------------------------------------------------
# Normalisation to the metadata tuple
# ---------------------------------------------------------------------------

def _blank_tmdb_data() -> dict:
    """Every key main.py and the sash/discovery helpers read off ``tmdb_data``.

    Populated with provider values where an equivalent exists and neutral empties
    elsewhere, so no consumer has to learn that it might be looking at an anime
    title.  Studio/director/cast sashes simply find nothing and don't fire.
    """
    return {
        "credits":              {},
        "production_companies": [],
        "original_language":    "ja",
        "original_title":       None,
        "runtime":              None,
        "number_of_seasons":    None,
        "number_of_episodes":   None,
        "tmdb_status":          None,
        "vote_count":           None,
        # Art-selection keys: meaningless for a single-poster provider.
        "text_backdrop_path":   None,
        "original_poster_path": None,
        "poster_langs":         {},
        "imdb_id":              None,
        "tmdb_release_date":    None,
        "last_air_date":        None,
        "next_episode":         None,
        "last_episode":         None,
        "seasons":              [],
        # Anime-provider extras consumed by main.py.
        "anime_source":         None,
        "anime_score":          None,
        "anime_age_rating":     None,
        "anime_media_type":     None,
        # The provider's wide art (AniList's banner, Kitsu's cover image):
        # landscape's last art before a crop of the cover or the canvas.
        "anime_banner":         None,
        # AniList's MyAnimeList id for the same entry (Kitsu doesn't say).
        "anime_mal_id":         None,
        # AniList's titles, year, format and prequel, for anime_resolve.
        "anime_lookup":         None,
    }


def _normalise_anilist(media: dict) -> tuple:
    titles = media.get("title") or {}
    title = (
        titles.get("english")
        or titles.get("romaji")
        or titles.get("native")
        or "Unknown Title"
    )
    original_title = titles.get("native") or titles.get("romaji")

    start = media.get("startDate") or {}
    year = media.get("seasonYear") or start.get("year")
    release_year = str(year) if year else None
    # Full ISO date where AniList has one, for the lifecycle sashes and the
    # quality cache TTL, which both key off a release date rather than a year.
    release_date = None
    if start.get("year") and start.get("month") and start.get("day"):
        release_date = f"{start['year']:04d}-{start['month']:02d}-{start['day']:02d}"

    cover = media.get("coverImage") or {}
    # extraLarge is ~460x650 — below the 500x750 canvas, so normalise_poster
    # upscales slightly.  Still much better than `large` (~230x325).
    poster_url = cover.get("extraLarge") or cover.get("large")

    tmdb_data = _blank_tmdb_data()
    tmdb_data["original_title"]    = original_title
    tmdb_data["tmdb_release_date"] = release_date
    tmdb_data["tmdb_status"]       = _ANILIST_STATUS.get(media.get("status") or "")
    tmdb_data["number_of_episodes"] = media.get("episodes")
    tmdb_data["runtime"]           = media.get("duration")
    # AniList has no vote count; `popularity` is a list-count and is the closest
    # available proxy for the detection/vote gates that read this field.
    tmdb_data["vote_count"]        = media.get("popularity")
    tmdb_data["anime_source"]      = "anilist"
    # averageScore is already 0-100, so the score normaliser is the identity.
    tmdb_data["anime_score"]       = media.get("averageScore")
    tmdb_data["anime_age_rating"]  = 18 if media.get("isAdult") else None
    tmdb_data["anime_media_type"]  = (
        "movie" if (media.get("format") or "") in _MOVIE_FORMATS else "series"
    )
    tmdb_data["anime_banner"]      = media.get("bannerImage")
    tmdb_data["anime_mal_id"]      = media.get("idMal")
    # What anime_resolve needs to find a TMDB id the id mapping lacks, so it
    # doesn't ask AniList again: titles, start year, format and the prequel.
    tmdb_data["anime_lookup"] = {
        "id": media.get("id"),
        "format": media.get("format"),
        "title": {"english": titles.get("english"), "romaji": titles.get("romaji")},
        "synonyms": (media.get("synonyms") or [])[:5],
        "startDate": {"year": start.get("year")},
        "relations": {"edges": [
            e for e in ((media.get("relations") or {}).get("edges") or [])
            if e.get("relationType") == "PREQUEL"
        ]},
    }

    studios = ((media.get("studios") or {}).get("nodes")) or []
    tmdb_data["production_companies"] = [
        {"name": s.get("name")} for s in studios if s.get("name")
    ]

    genre_ids = _map_genres(media.get("genres") or [])
    return genre_ids, release_year, title, poster_url, tmdb_data


def _normalise_kitsu(data: dict) -> tuple:
    attrs = data.get("attributes") or {}
    titles = attrs.get("titles") or {}
    title = (
        titles.get("en")
        or attrs.get("canonicalTitle")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or "Unknown Title"
    )
    original_title = titles.get("ja_jp") or titles.get("en_jp")

    # Kitsu startDate is already an ISO "YYYY-MM-DD" string.
    start = attrs.get("startDate") or ""
    release_year = start[:4] if len(start) >= 4 else None
    release_date = start if len(start) == 10 else None

    poster = attrs.get("posterImage") or {}
    # Kitsu's `original` is typically 550x780, the only source across these
    # providers that doesn't need upscaling to the 500x750 canvas.
    poster_url = poster.get("original") or poster.get("large")

    tmdb_data = _blank_tmdb_data()
    tmdb_data["original_title"]     = original_title
    tmdb_data["tmdb_release_date"]  = release_date
    tmdb_data["tmdb_status"]        = _KITSU_STATUS.get(attrs.get("status") or "")
    tmdb_data["number_of_episodes"] = attrs.get("episodeCount")
    tmdb_data["runtime"]            = attrs.get("episodeLength")
    tmdb_data["vote_count"]         = attrs.get("userCount")
    tmdb_data["anime_source"]       = "kitsu"
    tmdb_data["anime_age_rating"]   = _KITSU_AGE_RATING.get(attrs.get("ageRating") or "")
    tmdb_data["anime_media_type"]   = (
        "movie" if (attrs.get("subtype") or "") in _MOVIE_FORMATS else "series"
    )
    tmdb_data["anime_banner"]       = (attrs.get("coverImage") or {}).get("original")

    # averageRating is a percentage delivered as a string ("82.53").
    raw_score = attrs.get("averageRating")
    if raw_score is not None:
        try:
            tmdb_data["anime_score"] = float(raw_score)
        except (TypeError, ValueError):
            pass

    genre_ids = _map_genres(data.get("_genres") or [])
    return genre_ids, release_year, title, poster_url, tmdb_data


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Bumped whenever the normalisers or the genre vocabulary change, so cached rows
# built by the old logic are re-fetched rather than served. Without this a
# mapping fix stays invisible for the whole ANIME_METADATA_CACHE_DURATION.
#   v2 = prefer Kitsu's `genres` relationship over its `categories` tag cloud
_METADATA_VERSION = "v2"


def _cache_key(namespace: str, anime_id: int) -> str:
    return f"anime:{_METADATA_VERSION}:{namespace}:{anime_id}"


def poster_cache_key(namespace: str, anime_id: int, url: str) -> str:
    """Filesystem-safe blob cache key for the fetched cover art.

    The url is hashed rather than embedded: provider CDN urls contain characters
    that don't belong in a filename, and the hash also invalidates the cached
    image automatically when a provider swaps the artwork.
    """
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"anime_{namespace}_{anime_id}_{digest}"


# How often an entry the provider hasn't scored yet is asked for its score.
_UNSCORED_RECHECK = 86400


def _score_check_key(namespace: str, anime_id: int) -> str:
    return f"anime_scorecheck:{namespace}:{anime_id}"


def known_miss(namespace: str, anime_id: int) -> bool:
    """True when the provider is known to have no entry for *anime_id* (the
    negative cache), as against a ``fetch_anime_metadata`` None that was a
    throttle or an outage and is worth asking again soon."""
    cached = get_cached_tvdb_json(_cache_key(namespace, anime_id))
    return bool(cached and cached.get("__miss__"))


def empty_metadata(namespace: str) -> tuple:
    """The "provider has nothing for this id" metadata tuple.

    Same shape as ``fetch_anime_metadata`` so the caller can render the genre
    canvas through the normal no-art path instead of special-casing a None.
    Genre is Animation, which always resolves to a genre background.
    """
    blank = _blank_tmdb_data()
    blank["anime_source"] = namespace
    return [_ANIMATION_GENRE_ID], False, [], None, "Unknown Title", None, None, blank


async def fetch_anime_metadata(
    client: httpx.AsyncClient,
    namespace: str,
    anime_id: int,
) -> tuple | None:
    """Return the same 8-tuple shape as ``tmdb.fetch_poster_metadata``:

        (genre_ids, is_textless, logos, release_year, title, poster_path,
         backdrop_path, tmdb_data)

    ``poster_path`` is an absolute CDN url rather than a TMDB path — the fetcher
    in tmdb.py detects that and downloads it directly.  ``is_textless`` is always
    False and ``logos``/``backdrop_path`` are always empty: these providers ship
    one text-bearing cover image and nothing else.

    Returns None when the provider has no usable entry, which the caller treats
    identically to "TMDB has no art" and renders the genre canvas.
    """
    key = _cache_key(namespace, anime_id)
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        if cached.get("__miss__"):
            logger.info(f"{namespace} negative cache hit for {anime_id}")
            return None
        hit = (
            cached["genre_ids"],
            False,
            [],
            cached["release_year"],
            cached["title"],
            cached["poster_path"],
            None,
            cached["tmdb_data"],
        )
        # A new show has no score for its first days (Kitsu waits for enough
        # ratings, AniList for its first episodes), and the metadata is kept
        # a week; an unscored entry is asked again once a day instead.
        score_check = _score_check_key(namespace, anime_id)
        if (cached["tmdb_data"] or {}).get("anime_score") is not None or get_cached_tvdb_json(score_check):
            logger.info(f"{namespace} metadata cache hit for {anime_id}")
            return hit
        set_cached_tvdb_json(score_check, {"checked": True}, _UNSCORED_RECHECK)
        logger.info(f"{namespace} metadata for {anime_id} has no score yet; asking again")
        return await _fetch_and_cache(client, namespace, anime_id, key) or hit

    fresh = await _fetch_and_cache(client, namespace, anime_id, key)
    if fresh is not None and fresh[7].get("anime_score") is None:
        set_cached_tvdb_json(_score_check_key(namespace, anime_id), {"checked": True}, _UNSCORED_RECHECK)
    return fresh


async def _fetch_and_cache(client: httpx.AsyncClient, namespace: str, anime_id: int,
                           key: str) -> tuple | None:
    try:
        async with _get_semaphore(namespace):
            if namespace == "anilist":
                raw = await _fetch_anilist(client, anime_id)
            else:
                raw = await _fetch_kitsu(client, anime_id)
    except _TransientError as exc:
        logger.warning(f"{namespace} unavailable for {anime_id}: {exc}")
        return None
    except Exception as exc:
        # Network blips are transient too — never negative-cached, for the same
        # reason (see _TransientError).
        logger.warning(
            f"{namespace} fetch failed for {anime_id}: {type(exc).__name__}: {exc}"
        )
        return None

    if raw is None:
        set_cached_tvdb_json(key, _SENTINEL_MISS, ANIME_NEG_CACHE_DURATION * 86400)
        return None

    if namespace == "anilist":
        genre_ids, release_year, title, poster_url, tmdb_data = _normalise_anilist(raw)
    else:
        genre_ids, release_year, title, poster_url, tmdb_data = _normalise_kitsu(raw)

    if not poster_url:
        logger.warning(
            f"No cover art on {namespace} for {anime_id} — fallback canvas will be served"
        )

    set_cached_tvdb_json(
        key,
        {
            "genre_ids":    genre_ids,
            "release_year": release_year,
            "title":        title,
            "poster_path":  poster_url,
            "tmdb_data":    tmdb_data,
        },
        ANIME_METADATA_CACHE_DURATION * 86400,
    )

    return genre_ids, False, [], release_year, title, poster_url, None, tmdb_data


# ---------------------------------------------------------------------------
# MyAnimeList scores through Jikan
# ---------------------------------------------------------------------------

_MAL_SCORE_TTL = 86400
_MAL_UNSCORED_TTL = 12 * 3600
# After Jikan fails, it isn't asked again for this long, so an instance that
# can't reach it doesn't wait out a timeout on every render.
_JIKAN_COOLDOWN = 300
_jikan_quiet_until = 0.0


def jikan_enabled() -> bool:
    return bool(JIKAN_API_URL)


def _mal_score_key(mal_id: int) -> str:
    return f"jikan_score:v1:{mal_id}"


async def fetch_mal_score(client: httpx.AsyncClient, mal_id: int) -> "tuple[float | None, bool]":
    """MyAnimeList's score (0-10) for *mal_id* as Jikan reports it.

    MDBList keeps one MyAnimeList score per IMDb title, the first season's,
    so this is what gives a later season its own.  Returns (score, answered):
    answered is False when Jikan couldn't be asked (unset, throttled, down),
    which the caller treats like any other provider blip.  A score of None
    with answered True is an entry MAL hasn't scored yet (or doesn't have).
    """
    global _jikan_quiet_until
    if not JIKAN_API_URL:
        return None, False
    key = _mal_score_key(mal_id)
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        return cached.get("score"), True
    if time.monotonic() < _jikan_quiet_until:
        return None, False
    try:
        async with _get_semaphore("jikan"):
            logger.info(f"External API Call: Jikan score for mal:{mal_id}")
            resp = await client.get(f"{JIKAN_API_URL}/anime/{int(mal_id)}", timeout=6.0,
                                    follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning(f"Jikan unavailable for mal:{mal_id}: {type(exc).__name__}: {exc}")
        _jikan_quiet_until = time.monotonic() + _JIKAN_COOLDOWN
        return None, False
    if resp.status_code == 404:
        set_cached_tvdb_json(key, {"score": None}, ANIME_NEG_CACHE_DURATION * 86400)
        return None, True
    if resp.status_code != 200:
        # Jikan answers 429 for its own per-second limit and passes MAL's
        # outages on as 5xx; a short pause covers both.
        logger.warning(f"Jikan error {resp.status_code} for mal:{mal_id}")
        _jikan_quiet_until = time.monotonic() + (10 if resp.status_code == 429 else _JIKAN_COOLDOWN)
        return None, False
    try:
        score = (resp.json().get("data") or {}).get("score")
        score = float(score) if score is not None else None
    except (ValueError, TypeError, AttributeError):
        return None, False
    set_cached_tvdb_json(key, {"score": score}, _MAL_SCORE_TTL if score is not None else _MAL_UNSCORED_TTL)
    return score, True
