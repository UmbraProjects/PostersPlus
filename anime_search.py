"""Anime entries for the configurator's search that TMDB's can't offer.

TMDB lists an anime as one show, so a search there finds no later season or
cour to preview, and nothing at all for a title TMDB doesn't have.  Kitsu
lists each season as its own entry, needs no key and isn't tightly
rate-limited, so /search asks it alongside TMDB (or Cinemeta) and this
module keeps only what the TMDB results lack, by the id mapping:

- the start of a show the mapping ties to a TMDB id: dropped when TMDB's
  results have that show, else kept in its place (a misspelling TMDB didn't
  match, "solo levelling");
- a later season, cour or special (anime_ids.season_place): kept, listed
  straight under its show when that is among the results;
- an entry with no TMDB id: kept, after the TMDB results, specials last.

A row that doesn't sit under one of TMDB's results must also carry every
word of the query in one of its titles, as Kitsu's text search is loose.

An anime row is shaped like a TMDB one, plus ``anime_id`` ("kitsu:45857"),
which the configurator's preview sends as stremio_id, as Nuvio does, and
``anime_label`` ("Kitsu · Season", "Kitsu · Special", "Kitsu") for the
result list.  ``anime_nested`` marks a season listed under its show; one
whose show isn't among the results (a misspelling TMDB didn't match) stands
on its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from difflib import SequenceMatcher

import httpx

import anime
import anime_ids
from cache import get_cached_tvdb_json, set_cached_tvdb_json

logger = logging.getLogger(__name__)

_VERSION = "v1"
_TTL = 6 * 3600
_LIMIT = 12
# Kept: Kitsu's subtypes of things a poster is asked for ("music" and the
# like are not).
_SUBTYPES = {"TV", "ONA", "OVA", "movie", "special"}
# Rows at most: TMDB's own, then the seasons of its shows, then the titles
# TMDB lacks.
_TMDB_ROWS = 10
_SEASON_ROWS = 8
_UNLINKED_ROWS = 4
# How long /search waits on Kitsu before answering with TMDB's rows alone.
# The lookup carries on and is cached, so the next search has it.
SEARCH_WAIT = 2.5

_background: set = set()


async def kitsu_entries(task: "asyncio.Task") -> list[dict]:
    """*task*'s (kitsu_search's) entries if they come within SEARCH_WAIT,
    else [] while it finishes in the background."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), SEARCH_WAIT)
    except asyncio.TimeoutError:
        logger.info("Kitsu search slow: answering without it")
        _background.add(task)
        task.add_done_callback(_background.discard)
        return []


def enabled() -> bool:
    """Only with anime rendering on and the mapping loaded: without the
    mapping every season 1 would repeat a TMDB row."""
    import config
    return bool(config.ANIME_SOURCES_ENABLED) and anime_ids.is_ready()


async def kitsu_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Kitsu's anime entries for *query* (its JSON:API data), or [] when it
    can't be reached.  Cached per query."""
    query = " ".join((query or "").split()).casefold()
    if not query:
        return []
    key = f"kitsusearch:{_VERSION}:{hashlib.sha1(query.encode()).hexdigest()[:20]}"
    cached = get_cached_tvdb_json(key)
    if cached is not None:
        return cached
    try:
        async with anime._get_semaphore("kitsu"):
            logger.info(f"External API Call: Kitsu search for {query!r}")
            resp = await client.get(
                f"{anime.KITSU_API_BASE}/anime",
                params={
                    "filter[text]": query, "page[limit]": str(_LIMIT),
                    "fields[anime]": "canonicalTitle,titles,subtype,startDate,posterImage",
                },
                headers={"Accept": "application/vnd.api+json"},
                timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True,
            )
        if resp.status_code != 200:
            logger.warning(f"Kitsu search {resp.status_code} for {query!r}")
            return []
        data = resp.json().get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"Kitsu search failed for {query!r}: {type(exc).__name__}")
        return []
    data = [d for d in data if isinstance(d, dict)]
    set_cached_tvdb_json(key, data, _TTL)
    return data


def _label(place: anime_ids.SeasonPlace | None) -> str:
    # Not TMDB's season number: where TMDB runs a show as one season, a
    # third season is its "S1 from episode 48", while Kitsu's title already
    # says which season it is.
    if place is None:
        return "Kitsu"
    return "Kitsu · Special" if place.season == 0 else "Kitsu · Season"


def _anime_row(entry: dict) -> dict | None:
    """A Kitsu entry as a search row, or None when it isn't one to offer."""
    attrs = entry.get("attributes") or {}
    subtype = attrs.get("subtype")
    if subtype not in _SUBTYPES or not str(entry.get("id") or "").isdigit():
        return None
    kitsu_id = int(entry["id"])
    is_movie = subtype == "movie"
    mapped = anime_ids.lookup("kitsu", kitsu_id, "movie" if is_movie else "series")
    place = None if is_movie else anime_ids.season_place("kitsu", kitsu_id)
    # The start of a show (or a film) TMDB has: merge keeps it only when
    # TMDB's results miss that show, as they do for a misspelling.
    start = bool(mapped is not None and mapped.tmdb_id and place is None)
    titles = attrs.get("titles") or {}
    title = titles.get("en") or attrs.get("canonicalTitle") or titles.get("en_jp")
    if not title:
        return None
    poster = attrs.get("posterImage") or {}
    date = attrs.get("startDate") or ""
    return {
        "media_type": "movie" if is_movie else "tv",
        "id": int(mapped.tmdb_id) if mapped is not None and mapped.tmdb_id else None,
        "imdb_id": mapped.imdb_id if mapped is not None else None,
        ("title" if is_movie else "name"): title,
        ("release_date" if is_movie else "first_air_date"): date,
        "poster_url": poster.get("small") or poster.get("medium") or poster.get("original"),
        "anime_id": f"kitsu:{kitsu_id}",
        "anime_label": _label(place),
        # A season of something TMDB has, listed under it when it is among
        # the results (anime_nested, set by merge).
        "anime_season": place is not None,
        "anime_nested": False,
        "anime_start": start,
        # An unplaced special is often a recap or a PV, but can be a title in
        # its own right (Detective Conan: The Gold-Star Answer): kept, last.
        "_special": subtype == "special" and place is None,
        # Every title Kitsu knows it by, for merge's relevance check.
        "_names": [attrs.get("canonicalTitle"), *titles.values()],
    }


def _words(text: str | None) -> list[str]:
    """Words without case or accents, in any script (a CJK title is one)."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return [w for w in re.split(r"\W+", text) if w]


def _word_matches(wanted: str, word: str) -> bool:
    # Exact, a prefix of it (a word still being typed), or a near spelling
    # with the same first letter ("levelling" for "leveling", not "anora"
    # for "nora").
    if word == wanted or (len(wanted) >= 3 and word.startswith(wanted)):
        return True
    return (len(wanted) >= 4 and word[:1] == wanted[:1]
            and SequenceMatcher(None, wanted, word).ratio() >= 0.85)


def _relevant(query: str, names: list[str]) -> bool:
    """Whether one of *names* carries every word of *query*.  Kitsu's text
    search is loose: "anora" brings back Blade of the Immortal."""
    wanted = _words(query)
    if not wanted:
        return True
    for name in names:
        words = _words(name)
        if all(any(_word_matches(w, word) for word in words) for w in wanted):
            return True
    return False


def _same_title(a: dict, b: dict) -> bool:
    """Whether two rows name the same title: same type, a name in common
    (without case, accents or punctuation), and years within one of each
    other when both have one."""
    from anime_resolve import _norm
    if a.get("media_type") != b.get("media_type"):
        return False
    def names(row):
        return {_norm(row.get(f)) for f in ("title", "name", "original_title", "original_name")} - {""}
    if not names(a) & names(b):
        return False
    years = [(r.get("release_date") or r.get("first_air_date") or "")[:4] for r in (a, b)]
    return not all(y.isdigit() for y in years) or abs(int(years[0]) - int(years[1])) <= 1


def keys_of(row: dict) -> set:
    """The ids a row is known by: its TMDB id (with its type) and IMDb id."""
    out = set()
    if row.get("id") is not None:
        out.add((row["media_type"], str(row["id"])))
    if row.get("imdb_id"):
        out.add(row["imdb_id"])
    return out


def merge(results: list[dict], entries: list[dict], query: str = "") -> list[dict]:
    """TMDB's (or Cinemeta's) movie and show rows, with the anime rows they
    lack: each later season under its show, the rest after.  A row that
    doesn't sit under a TMDB result has to match *query* by its own titles."""
    base = [r for r in results if isinstance(r, dict)
            and r.get("media_type") in ("movie", "tv")][:_TMDB_ROWS]
    rows = [row for row in map(_anime_row, entries) if row is not None]
    base_keys = set().union(*map(keys_of, base)) if base else set()
    rows = [r for r in rows if (r["anime_season"] and keys_of(r) & base_keys)
            or _relevant(query, [n for n in r["_names"] if isinstance(n, str)])]

    keys = keys_of
    # A show's start stands in for the TMDB row the results lack, once.
    seen = set().union(*map(keys, base)) if base else set()
    starts = []
    for row in rows:
        # By name too: a key-less search's Cinemeta rows carry no TMDB id.
        if (row["anime_start"] and not keys(row) & seen
                and not any(_same_title(row, b) for b in base)):
            starts.append(row)
            seen |= keys(row)
    seasons = [r for r in rows if r["anime_season"]][:_SEASON_ROWS]
    # A title the mapping doesn't link may still be one of TMDB's results,
    # by name ("Solo Leveling: ReAwakening" is "Solo Leveling -ReAwakening-").
    unlinked = [r for r in rows if not r["anime_season"] and not r["anime_start"]
                and not any(_same_title(r, b) for b in base)]
    unlinked.sort(key=lambda r: r["_special"])
    shows = base + starts[:_UNLINKED_ROWS]

    out, placed = [], set()
    for show in shows:
        out.append(show)
        own = keys(show)
        for i, row in enumerate(seasons):
            if i not in placed and keys(row) & own:
                out.append({**row, "anime_nested": True})
                placed.add(i)
    out.extend(row for i, row in enumerate(seasons) if i not in placed)
    out.extend(unlinked[:_UNLINKED_ROWS])
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in out]
