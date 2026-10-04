"""A later anime season's own landscape art (ANIME_SEASON_ART).

Kitsu and AniList give every season, and often every cour, its own id; TMDB
lists the whole run as one show.  anime_ids.py maps a later season to that
show, which is right for the logo and the ratings but leaves every season's
landscape poster on the show's one backdrop.  For an entry the mapping places
after the show's start (season 2+, TMDB's specials, or a cour that starts
part-way into a season), or one anime_resolve found through its prequel,
this picks the season's own art instead:

1. Kitsu's cover image of the season, when a 16:9 cut of it needs little
   upscaling and carries no title (text_detect.cover_has_text): most covers
   are a wide strip of the season's key art.  A request by AniList id borrows
   the Kitsu entry's through the mapping, since AniList's banner is too thin
   a strip to cut.
2. Else TMDB's still of the season's first episode, by the mapping's season
   and episode offset.  TMDB often runs a show as one long season where the
   mapping counts several (Jujutsu Kaisen, Solo Leveling), so this 404s
   there and the show's backdrop stays.

The pick is cached per anime id: a cover once vetted isn't downloaded and
scanned again, and the vetted cut is stored under the landscape art key the
render fetches it by.
"""
from __future__ import annotations

import asyncio
import io
import logging

import httpx
from PIL import Image

import anime
import anime_ids
import config as _cfg
from cache import get_cached_tvdb_json, set_cached_tvdb_json

logger = logging.getLogger(__name__)

_VERSION = "v1"
_HIT_TTL = 30 * 86400
# Kitsu adds covers and TMDB stills as a season airs.
_MISS_TTL = 3 * 86400
_FAIL_TTL = 600
# A cover's 16:9 cut must be at least this share of the landscape canvas's
# height: Kitsu's covers of popular seasons cut to 482-1210 px tall, while
# the small old ones (300 px) would be blown up nearly twice.
_MIN_CUT_HEIGHT = 0.8

_inflight: dict[str, asyncio.Future] = {}


class _Transient(Exception):
    pass


def _cut_height(width: int, height: int) -> float:
    """The height of the largest 16:9 cut of a *width* x *height* image."""
    return min(height, width * 9 / 16)


async def _kitsu_cover(client: httpx.AsyncClient, namespace: str, anime_id: int) -> str | None:
    """The Kitsu cover image of this entry, from its (cached) metadata."""
    kitsu_id = anime_id if namespace == "kitsu" else anime_ids.kitsu_for_anilist(anime_id)
    if kitsu_id is None:
        return None
    meta = await anime.fetch_anime_metadata(client, "kitsu", kitsu_id)
    if meta is None:
        return None
    if "anime_banner" in (meta[7] or {}):
        return meta[7]["anime_banner"]
    # Cached before the metadata carried the cover: just the cover, from
    # Kitsu, rather than waiting out the row's cache life.
    async with anime._get_semaphore("kitsu"):
        try:
            resp = await client.get(
                f"{anime.KITSU_API_BASE}/anime/{kitsu_id}",
                params={"fields[anime]": "coverImage"},
                headers={"Accept": "application/vnd.api+json"}, timeout=15.0,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise _Transient(f"Kitsu {type(exc).__name__}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _Transient(f"Kitsu {resp.status_code}")
    attrs = (resp.json().get("data") or {}).get("attributes") or {}
    return (attrs.get("coverImage") or {}).get("original")


async def _vetted_cover(client: httpx.AsyncClient, tmdb_id: str, url: str) -> bool:
    """Whether the cover at *url* makes landscape art: big enough to cut to
    16:9 and free of lettering.  A cover that does is stored as the render
    will fetch it."""
    import tmdb
    try:
        content = await tmdb._get_art(client, url, follow_redirects=True)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404, 410):
            return False
        raise _Transient(f"cover {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise _Transient(f"cover {type(exc).__name__}") from exc
    except tmdb.BlankArtError:
        return False

    def check() -> bool:
        try:
            image = Image.open(io.BytesIO(content))
            image.load()
        except OSError:
            logger.info(f"Season cover isn't an image we can read: {url}")
            return False
        with image:
            if _cut_height(*image.size) < _MIN_CUT_HEIGHT * _cfg.LANDSCAPE_HEIGHT:
                logger.info(f"Season cover too small for landscape: {url} {image.size}")
                return False
            cut = tmdb.normalise_landscape(image.convert("RGBA"))
        if _cfg.TEXTLESS_TEXT_DETECTION:
            import text_detect
            if text_detect.cover_has_text(cut):
                return False
        tmdb._store_art(tmdb.landscape_image_cache_key(tmdb_id, url), cut)
        return True

    return await asyncio.to_thread(check)


async def _first_episode_still(client: httpx.AsyncClient, tmdb_id: str,
                               place: anime_ids.SeasonPlace, tmdb_key: str) -> str | None:
    """TMDB's still of the entry's first episode, or None."""
    if place.season is None or (place.season == 0 and not place.episode_offset):
        # Which special it is isn't known without an offset.
        return None
    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{place.season}",
            params={"api_key": tmdb_key}, timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise _Transient(f"TMDB season {type(exc).__name__}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _Transient(f"TMDB season {resp.status_code}")
    episodes = resp.json().get("episodes") or []
    if place.episode_offset >= len(episodes):
        return None
    return episodes[place.episode_offset].get("still_path") or None


async def _find(client: httpx.AsyncClient, namespace: str, anime_id: int, tmdb_id: str,
                place: anime_ids.SeasonPlace | None, tmdb_key: str | None) -> dict:
    cover = await _kitsu_cover(client, namespace, anime_id)
    if cover and await _vetted_cover(client, tmdb_id, cover):
        return {"path": cover, "via": "kitsu"}
    if place is not None and tmdb_key:
        still = await _first_episode_still(client, tmdb_id, place, tmdb_key)
        if still:
            return {"path": still, "via": "still"}
    return {}


async def season_art(client: httpx.AsyncClient, *, namespace: str, anime_id: int,
                     tmdb_id: str, place: anime_ids.SeasonPlace | None,
                     tmdb_key: str | None) -> str | None:
    """The landscape art path for a later season (a Kitsu cover url or a TMDB
    still path, for tmdb.fetch_landscape_image), or None to keep the show's."""
    key = f"seasonart:{_VERSION}:{namespace}:{anime_id}:{tmdb_id}"
    cached = get_cached_tvdb_json(key)
    if cached is None:
        fut = _inflight.get(key)
        if fut is not None:
            cached = await asyncio.shield(fut)
        else:
            fut = asyncio.get_running_loop().create_future()
            _inflight[key] = fut
            try:
                cached = await _find(client, namespace, anime_id, tmdb_id, place, tmdb_key)
                set_cached_tvdb_json(key, cached, _HIT_TTL if cached.get("path") else _MISS_TTL)
                if cached.get("path"):
                    logger.info(f"Season art for {namespace}:{anime_id} ({cached['via']}): {cached['path']}")
            except Exception as exc:
                # Never the render's problem: the show's backdrop stands in,
                # and the title is tried again shortly.
                logger.warning(f"No season art for {namespace}:{anime_id} yet: {exc}")
                cached = {}
                set_cached_tvdb_json(key, cached, _FAIL_TTL)
            finally:
                _inflight.pop(key, None)
                if not fut.done():
                    fut.set_result(cached)
    return (cached or {}).get("path")
