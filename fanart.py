"""fanart.tv poster source (poster_source=fanart, optionally poster_pick=random),
and landscape art source (landscape_art_source=fanart).

Picks the most-liked textless poster (lang "00", then untagged) for a title,
or in original-art mode the most-liked poster in the preferred language; either
can instead be a random one of the top five.  Movies are looked up by TMDB id,
series by TVDB id (resolved through tvdb.py, so series need a TVDB key as
well).  Responses, including misses, are cached in the TVDB JSON table under a
"fanart:" prefix.
"""

import logging
import random

import httpx

import config as _cfg
import tvdb
from cache import get_cached_tvdb_json, set_cached_tvdb_json

logger = logging.getLogger(__name__)

_API = "https://webservice.fanart.tv/v3"
_HIT_TTL = 7 * 86400
_MISS_TTL = 2 * 86400
_RANDOM_POOL = 5


def fanart_enabled() -> bool:
    return bool(_cfg.FANART_POSTERS and _cfg.FANART_API_KEY)


# fanart.tv tags Czech as "cz"; everything else we've seen is ISO 639-1.
_LANG_FIXUPS = {"cz": "cs"}


async def fanart_poster_url(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tmdb_id: str,
    imdb_id: str | None = None,
    random_top: bool = False,
    languages: list[str] | None = None,
) -> str | None:
    """Return a fanart.tv poster url, or None.

    Without *languages*: the most-liked textless poster.  With *languages*
    (original-art mode): the most-liked poster in the first of those languages
    that has one.  *random_top* picks one of the top five instead.
    """
    if not fanart_enabled() or not tmdb_id:
        return None
    is_movie = media_type == "movie"
    cache_key = f"fanart:v4:{'movie' if is_movie else 'tv'}:{tmdb_id}"
    pools = get_cached_tvdb_json(cache_key)
    if pools is None:
        pools = await _fetch_pools(client, is_movie, media_type, tmdb_id, imdb_id)
        if pools is None:
            return None
        found = pools["textless"] or pools["langs"]
        set_cached_tvdb_json(cache_key, pools, _HIT_TTL if found else _MISS_TTL)
    if languages is None:
        urls = pools.get("textless") or []
    else:
        langs = pools.get("langs") or {}
        urls = next((langs[code] for code in languages if langs.get(code)), [])
    if not urls:
        return None
    return random.choice(urls[:_RANDOM_POOL]) if random_top else urls[0]


async def _fetch_doc(client, is_movie, media_type, tmdb_id, imdb_id) -> dict | None:
    """fanart.tv's whole document for a title: {} when it has none (or the
    series has no TVDB id), None on a transient failure (not cached)."""
    if is_movie:
        remote_id = tmdb_id
    else:
        remote_id = await tvdb.resolve_tvdb_id(
            client, media_type=media_type, imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
    if not remote_id:
        return {}
    try:
        resp = await client.get(
            f"{_API}/{'movies' if is_movie else 'tv'}/{remote_id}",
            params={"api_key": _cfg.FANART_API_KEY},
        )
    except httpx.HTTPError as exc:
        logger.warning(f"fanart.tv lookup failed for {media_type} {tmdb_id}: {exc}")
        return None
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        logger.warning(f"fanart.tv {resp.status_code} for {media_type} {tmdb_id}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {}


def _build_pools(items: list, textless_langs: tuple[str, ...], lang_items: list | None = None) -> dict:
    """{"textless": [...], "langs": {code: [...]}}, most liked first.

    *items* feed the textless pool (tags in *textless_langs*, in that order)
    and, unless *lang_items* is given, the language pools too."""
    def ranked(seq):
        seq = [a for a in seq if a.get("url")]
        seq.sort(key=lambda a: -int(a.get("likes") or 0))
        return seq
    items = ranked(items)
    pools: dict = {"textless": [], "langs": {}}
    textless = [a for tag in textless_langs for a in items if (a.get("lang") or "") == tag]
    pools["textless"] = [a["url"] for a in textless[:_RANDOM_POOL]]
    for a in ranked(lang_items) if lang_items is not None else items:
        code = (a.get("lang") or "").lower()
        if code in ("", "00", "xx"):
            continue
        urls = pools["langs"].setdefault(_LANG_FIXUPS.get(code, code), [])
        if len(urls) < _RANDOM_POOL:
            urls.append(a["url"])
    return pools


async def _fetch_pools(client, is_movie, media_type, tmdb_id, imdb_id) -> dict | None:
    """Top poster urls, most liked first: {"textless": [...], "langs": {code:
    [...]}}.  None on a transient failure (not cached)."""
    data = await _fetch_doc(client, is_movie, media_type, tmdb_id, imdb_id)
    if data is None:
        return None
    # "00" is fanart's textless tag.  Untagged ("") posters are mostly textless
    # too, so they fill in after the tagged ones; the text scan catches the odd
    # one that isn't.  "xx" is left out everywhere: in practice it's mis-tagged
    # foreign-language art.
    return _build_pools(data.get("movieposter" if is_movie else "tvposter") or [], ("00", ""))


async def fanart_background_url(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tmdb_id: str,
    imdb_id: str | None = None,
    languages: list[str] | None = None,
) -> str | None:
    """Landscape art from fanart.tv (landscape_art_source=fanart), or None.

    Without *languages*: the most-liked background — fanart.tv's backgrounds
    are clean 1920x1080 art, untagged.  With *languages* (landscape original
    art): the most-liked thumb in the first of those languages that has one;
    thumbs are fanart.tv's 16:9 key art with the title on it.
    """
    if not fanart_enabled() or not tmdb_id:
        return None
    is_movie = media_type == "movie"
    cache_key = f"fanart:bg:v1:{'movie' if is_movie else 'tv'}:{tmdb_id}"
    pools = get_cached_tvdb_json(cache_key)
    if pools is None:
        data = await _fetch_doc(client, is_movie, media_type, tmdb_id, imdb_id)
        if data is None:
            return None
        pools = _build_pools(
            data.get("moviebackground" if is_movie else "showbackground") or [], ("", "00"),
            lang_items=data.get("moviethumb" if is_movie else "tvthumb") or [],
        )
        found = pools["textless"] or pools["langs"]
        set_cached_tvdb_json(cache_key, pools, _HIT_TTL if found else _MISS_TTL)
    if languages is None:
        urls = pools.get("textless") or []
    else:
        langs = pools.get("langs") or {}
        urls = next((langs[code] for code in languages if langs.get(code)), [])
    return urls[0] if urls else None


async def artwork_candidates(
    client, *, media_type: str, tmdb_id: str, imdb_id: str | None = None,
) -> dict[str, list[dict]]:
    """Every fanart.tv poster and logo for a title, most liked first, for the
    dashboard's picker.  Needs only the key: an operator can choose fanart.tv
    art for a title without offering fanart.tv as a user poster source.  Not
    cached — it runs when the operator opens a title."""
    out: dict[str, list[dict]] = {"posters": [], "logos": [], "backdrops": []}
    if not _cfg.FANART_API_KEY or not tmdb_id:
        return out
    is_movie = media_type == "movie"
    remote_id = tmdb_id if is_movie else await tvdb.resolve_tvdb_id(
        client, media_type=media_type, imdb_id=imdb_id, tmdb_id=tmdb_id,
    )
    if not remote_id:
        return out
    try:
        resp = await client.get(
            f"{_API}/{'movies' if is_movie else 'tv'}/{remote_id}",
            params={"api_key": _cfg.FANART_API_KEY},
        )
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"fanart.tv candidates failed for {media_type} {tmdb_id}: {exc}")
        return out
    kinds = {
        "posters": ("movieposter",) if is_movie else ("tvposter",),
        "logos": ("hdmovielogo", "movielogo") if is_movie else ("hdtvlogo", "clearlogo"),
        # Backgrounds are textless; thumbs are landscape art with the title on.
        "backdrops": ("moviebackground", "moviethumb") if is_movie else ("showbackground", "tvthumb"),
    }
    for kind, keys in kinds.items():
        items = [a for key in keys for a in (data.get(key) or []) if a.get("url")]
        items.sort(key=lambda a: -int(a.get("likes") or 0))
        for a in items:
            code = (a.get("lang") or "").lower()
            out[kind].append({
                "path": a["url"],
                # fanart.tv serves a small copy of every image under /preview/.
                "thumb": a["url"].replace("/fanart/", "/preview/", 1),
                "language": None if code in ("", "00") else _LANG_FIXUPS.get(code, code),
                "score": int(a.get("likes") or 0),
            })
    return out
