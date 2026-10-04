"""TMDB ids for anime the community id mapping doesn't know yet.

anime_ids.py fills a request's missing tmdb_id/imdb_id from Fribb's mapping,
which lags new releases by weeks: a season that started airing this month,
or a show TMDB added last week, has no row.  Without a TMDB id the render has
no logo, no landscape backdrop and no IMDb/TMDB ratings, so a landscape
poster falls to the genre canvas while the configurator (which sends the
TMDB id) looks fine.

Two ways back to a TMDB id, tried in order:

1. A sequel season: follow AniList's PREQUEL links (series only, a few hops)
   to a season the mapping does know, and take its ids.  This is what the
   mapping itself does for a later season (it maps to the parent series, as
   TMDB and IMDb list one show), so the result is the same one a mapped
   sequel gets.
2. A new show: search TMDB by its English and romaji titles and take a
   result only when its name matches one of them outright, it is Animation,
   and it started within a year of the anime.  Strict on purpose: a wrong id
   would print another show's logo and ratings.

Kitsu and MAL ids are taken to AniList first (Kitsu's own mappings, AniList's
idMal).  Results are cached: a hit for _HIT_TTL, a miss for _MISS_TTL, since
TMDB and the mapping catch up within days, and a throttled or failed lookup
for _FAIL_TTL.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

import httpx

import anime
import anime_ids
from cache import get_cached_tvdb_json, set_cached_tvdb_json

logger = logging.getLogger(__name__)

_HIT_TTL = 14 * 86400
_MISS_TTL = 86400
# A throttle clears in a minute or two (AniList names the wait), so a title
# caught in one is tried again soon rather than drawn on the canvas for long.
_FAIL_TTL = 120
_MAX_HOPS = 4
_SERIES_FORMATS = {"TV", "TV_SHORT", "ONA", "OVA"}

_QUERY = """
query ($id: Int, $mal: Int) {
  Media(id: $id, idMal: $mal, type: ANIME) {
    id
    format
    title { english romaji }
    synonyms
    startDate { year }
    relations { edges { relationType node { id type format } } }
  }
}
"""

_inflight: dict[str, asyncio.Future] = {}


class _Transient(Exception):
    pass


async def _anilist(client: httpx.AsyncClient, *, anilist_id: int | None = None,
                   mal_id: int | None = None) -> dict | None:
    variables = {"id": anilist_id} if anilist_id is not None else {"mal": mal_id}
    if anime.anilist_cooling():
        raise _Transient("AniList asked us to wait")
    async with anime._get_semaphore("anilist"):
        logger.info(f"External API Call: AniList relations for {variables}")
        resp = await client.post(anime.ANILIST_API_URL, json={"query": _QUERY, "variables": variables},
                                 timeout=15.0)
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        anime.note_anilist_throttle(resp.headers.get("retry-after"))
    if resp.status_code != 200:
        raise _Transient(f"AniList {resp.status_code}")
    return ((resp.json().get("data") or {}).get("Media")) or None


async def _kitsu_to_anilist(client: httpx.AsyncClient, kitsu_id: int) -> int | None:
    async with anime._get_semaphore("kitsu"):
        resp = await client.get(
            f"{anime.KITSU_API_BASE}/anime/{kitsu_id}/mappings",
            headers={"Accept": "application/vnd.api+json"}, timeout=15.0, follow_redirects=True,
        )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _Transient(f"Kitsu {resp.status_code}")
    for item in resp.json().get("data") or []:
        attrs = item.get("attributes") or {}
        if attrs.get("externalSite") == "anilist/anime" and str(attrs.get("externalId") or "").isdigit():
            return int(attrs["externalId"])
    return None


def _norm(name: str | None) -> str:
    name = unicodedata.normalize("NFKD", name or "").casefold()
    return re.sub(r"[^0-9a-z]+", "", "".join(c for c in name if not unicodedata.combining(c)))


async def _search_tmdb(client: httpx.AsyncClient, tmdb_key: str, kind: str,
                       media: dict) -> str | None:
    """A TMDB id whose name is one of *media*'s titles outright, or None."""
    titles = media.get("title") or {}
    names = [titles.get("english"), titles.get("romaji"), *(media.get("synonyms") or [])[:3]]
    wanted = {_norm(n) for n in names if n and _norm(n)}
    year = (media.get("startDate") or {}).get("year")
    for query in [n for n in names[:2] if n]:
        resp = await client.get(
            f"https://api.themoviedb.org/3/search/{kind}",
            params={"api_key": tmdb_key, "query": query, "include_adult": "false"}, timeout=15.0,
        )
        if resp.status_code != 200:
            raise _Transient(f"TMDB search {resp.status_code}")
        for hit in (resp.json().get("results") or [])[:10]:
            if 16 not in (hit.get("genre_ids") or []):
                continue
            hit_names = {_norm(hit.get("name") or hit.get("title")),
                         _norm(hit.get("original_name") or hit.get("original_title"))}
            if not (hit_names & wanted):
                continue
            date = hit.get("first_air_date") or hit.get("release_date") or ""
            if year and date[:4].isdigit() and abs(int(date[:4]) - int(year)) > 1:
                continue
            return str(hit["id"])
    return None


def _usable(mapped: anime_ids.MappedIds | None, media_type: str) -> bool:
    """Whether a mapping row names a TMDB id TMDB still has: a row naming a
    deleted one (a duplicate merged away) is what sent the request here."""
    from tmdb import tmdb_id_gone
    return bool(mapped is not None and mapped.tmdb_id
                and not tmdb_id_gone(mapped.tmdb_id, media_type))


async def _resolve(client: httpx.AsyncClient, namespace: str, anime_id: int,
                   media_type: str, tmdb_key: str | None) -> dict:
    kind = "movie" if media_type == "movie" else "tv"
    if namespace == "anilist":
        # The render fetches (and caches) the title's metadata anyway, and it
        # carries what this needs, so a catalog of unmapped titles costs
        # AniList one call each rather than two.  Rows cached before it did
        # are asked for directly.
        meta = await anime.fetch_anime_metadata(client, "anilist", anime_id)
        media = (meta[7] or {}).get("anime_lookup") if meta else None
        if media is None:
            if meta is None and anime.anilist_cooling():
                raise _Transient("AniList asked us to wait")
            media = await _anilist(client, anilist_id=anime_id)
    elif namespace == "mal":
        media = await _anilist(client, mal_id=anime_id)
    else:
        anilist_id = await _kitsu_to_anilist(client, anime_id)
        media = await _anilist(client, anilist_id=anilist_id) if anilist_id else None
    if media is None:
        return {}
    # The AniList id itself may be mapped where the Kitsu or MAL one isn't.
    mapped = anime_ids.lookup("anilist", int(media["id"]), media_type)
    if _usable(mapped, media_type):
        return {"tmdb_id": mapped.tmdb_id, "imdb_id": mapped.imdb_id, "via": "anilist"}
    # 1. A later season of a series the mapping knows.
    node, hops = media, 0
    if kind == "tv":
        while hops < _MAX_HOPS:
            prequel = next((e["node"] for e in ((node.get("relations") or {}).get("edges") or [])
                            if e.get("relationType") == "PREQUEL"
                            and (e.get("node") or {}).get("type") == "ANIME"
                            and (e["node"].get("format") or "") in _SERIES_FORMATS), None)
            if prequel is None:
                break
            hops += 1
            mapped = anime_ids.lookup("anilist", int(prequel["id"]), media_type)
            if _usable(mapped, media_type):
                return {"tmdb_id": mapped.tmdb_id, "imdb_id": mapped.imdb_id, "via": f"prequel:{hops}"}
            node = await _anilist(client, anilist_id=int(prequel["id"]))
            if node is None:
                break
    # 2. TMDB by name: the show itself, or for a sequel the first season's.
    if tmdb_key:
        for candidate in ([media, node] if node is not media else [media]):
            found = await _search_tmdb(client, tmdb_key, kind, candidate)
            if found:
                return {"tmdb_id": found, "imdb_id": None, "via": "search"}
    return {}


def _key(namespace: str, anime_id: int, media_type: str) -> str:
    kind = "movie" if media_type == "movie" else "tv"
    return f"animeres:v1:{namespace}:{anime_id}:{kind}"


def resolved_as_sequel(namespace: str, anime_id: int, media_type: str) -> bool:
    """Whether resolve() found this title's ids through a prequel: a later
    season the mapping hasn't caught up with (anime_season.py)."""
    cached = get_cached_tvdb_json(_key(namespace, anime_id, media_type)) or {}
    return str(cached.get("via") or "").startswith("prequel:")


async def resolve(client: httpx.AsyncClient, namespace: str, anime_id: int,
                  media_type: str, tmdb_key: str | None) -> anime_ids.MappedIds | None:
    """The TMDB (and maybe IMDb) id for an anime id the mapping lacks, or None."""
    kind = "movie" if media_type == "movie" else "tv"
    key = _key(namespace, anime_id, media_type)
    cached = get_cached_tvdb_json(key)
    if cached and cached.get("tmdb_id") and not _usable(anime_ids.MappedIds(cached["tmdb_id"], None), media_type):
        # Found before TMDB deleted it: look again.
        cached = None
    if cached is None:
        fut = _inflight.get(key)
        if fut is not None:
            cached = await asyncio.shield(fut)
        else:
            fut = asyncio.get_running_loop().create_future()
            _inflight[key] = fut
            try:
                cached = await _resolve(client, namespace, anime_id, media_type, tmdb_key)
                set_cached_tvdb_json(key, cached, _HIT_TTL if cached.get("tmdb_id") else _MISS_TTL)
                if cached.get("tmdb_id"):
                    logger.info(f"Resolved unmapped {namespace}:{anime_id} to TMDB {kind} "
                                f"{cached['tmdb_id']} ({cached.get('via')})")
            except (_Transient, httpx.HTTPError, ValueError) as exc:
                # Held briefly, so a throttled AniList isn't asked again by
                # every request for the title.
                logger.warning(f"Couldn't resolve unmapped {namespace}:{anime_id}: {exc}")
                cached = {}
                set_cached_tvdb_json(key, cached, _FAIL_TTL)
            finally:
                _inflight.pop(key, None)
                if not fut.done():
                    fut.set_result(cached)
    if not cached or not cached.get("tmdb_id"):
        return None
    return anime_ids.MappedIds(cached["tmdb_id"], cached.get("imdb_id"))
