#anime_ids.py
"""
Kitsu / AniList -> TMDB / IMDb id mapping, from Fribb's anime-lists
(https://github.com/Fribb/anime-lists) — the same community mapping AIOMetadata
resolves its placeholders from.

Why this exists: the anime providers supply the art, titles, genres and score,
but not a logo or a backdrop, and every enrichment source (MDBList ratings,
awards, age rating, trending, release status) is keyed by IMDb or TMDB id.
A client that goes through AIOMetadata sends tmdb_id and imdb_id alongside the
anime id, so all of that works. A client that resolves its own pattern from the
catalogue item's meta id cannot: Nuvio's resolver turns "kitsu:7442" into a
kitsu_id and nothing else, so its anime landscape posters had no backdrop to
draw on and fell through to the genre canvas. This fills in the ids such a
request is missing, so it renders the same poster the AIOMetadata request does.

Same shape as imdb_dataset.py: one worker downloads the list on a timer and
swaps it into a small SQLite table; every worker answers point lookups from
that table. Lookups never touch the network.

MyAnimeList ids ride on the same list: MAL's API needs auth, so it is never an
art source, but every MAL id the list knows is translated to the Kitsu (else
AniList) id of the same entry, and the request renders as if it had sent that.

When disabled, or before the first download lands, lookups return None and
requests render exactly as before.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import suppress
from typing import Callable, NamedTuple

import httpx

from cache import claim_app_state_slot, set_app_state
from config import (
    ANIME_ID_MAP_ENABLED,
    ANIME_ID_MAP_PATH,
    ANIME_ID_MAP_REFRESH_HOURS,
    ANIME_ID_MAP_URL,
)

logger = logging.getLogger(__name__)

# Shared across worker processes via cache.db's app_state table, so only one
# worker per interval performs the download (see imdb_dataset_refresh_loop).
# Renamed when the tables gain one, so the first worker after an upgrade
# rebuilds at once instead of waiting out the previous day's claim.
_REFRESH_CLAIM_KEY = "anime_id_map_refresh_claimed_at:mal+films"   # films' TMDB ids, read from lists
_NOT_READY_RETRY_SECS = 60
# The namespaces anime.py sources art from, and the list's field for each.
_NAMESPACE_FIELDS = {"kitsu": "kitsu_id", "anilist": "anilist_id"}

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS {table} (
        namespace   TEXT    NOT NULL,
        anime_id    INTEGER NOT NULL,
        tmdb_tv     INTEGER,
        tmdb_movie  INTEGER,
        imdb_id     TEXT,
        PRIMARY KEY (namespace, anime_id)
    )
"""

# MAL id -> the provider ids of the same entry (see mal_to_provider).
_MAL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS {table} (
        mal_id      INTEGER PRIMARY KEY,
        kitsu_id    INTEGER,
        anilist_id  INTEGER
    )
"""

# For reverse_lookup: which anime entries a TMDB or IMDb id belongs to.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS anime_id_map_tmdb_tv    ON anime_id_map (tmdb_tv)",
    "CREATE INDEX IF NOT EXISTS anime_id_map_tmdb_movie ON anime_id_map (tmdb_movie)",
    "CREATE INDEX IF NOT EXISTS anime_id_map_imdb       ON anime_id_map (imdb_id)",
    # For the anime trending ranks: a Kitsu entry's AniList id, and back.
    "CREATE INDEX IF NOT EXISTS anime_mal_map_kitsu     ON anime_mal_map (kitsu_id)",
    "CREATE INDEX IF NOT EXISTS anime_mal_map_anilist   ON anime_mal_map (anilist_id)",
)

_local = threading.local()
_last_refresh_ts: float | None = None
_last_refresh_error: str | None = None
_row_count: int = 0


class MappedIds(NamedTuple):
    tmdb_id: str | None
    imdb_id: str | None


def is_enabled() -> bool:
    return bool(ANIME_ID_MAP_ENABLED)


def is_ready() -> bool:
    return is_enabled() and _row_count > 0


def _get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(ANIME_ID_MAP_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(ANIME_ID_MAP_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA.format(table="anime_id_map"))
        conn.execute(_MAL_SCHEMA.format(table="anime_mal_map"))
        for ddl in _INDEXES:
            conn.execute(ddl)
        conn.commit()
        _local.conn = conn
    return conn


def _count_rows() -> int:
    if not is_enabled():
        return 0
    try:
        return _get_db().execute("SELECT COUNT(*) FROM anime_id_map").fetchone()[0]
    except Exception:
        return 0


def init_db() -> None:
    """Idempotent; does nothing at all when the feature is disabled."""
    global _row_count
    _row_count = _count_rows() if is_enabled() else 0


def status() -> dict:
    """Operator diagnostics, surfaced on /stats."""
    return {
        "enabled": is_enabled(),
        "ready": is_ready(),
        "source": ANIME_ID_MAP_URL if is_enabled() else None,
        "last_refresh_unix": _last_refresh_ts,
        "last_refresh_error": _last_refresh_error,
        "row_count": _row_count if is_enabled() else 0,
    }


def lookup(namespace: str, anime_id: int, media_type: str) -> MappedIds | None:
    """The TMDB and IMDb ids the list gives *namespace*:*anime_id*, or None.

    The TMDB id is only returned for the kind the request is rendering: a TMDB
    id is meaningless without knowing whether it names a movie or a series, and
    fetching a movie's id as a series (or the reverse) would render a different
    title. The IMDb id carries its own kind, so it is returned either way.

    A sequel season maps to its parent series — TMDB and IMDb list anime
    seasons under one show — which is what AIOMetadata sends too, so the logo
    and backdrop are the show's and the art stays the season's own cover.

    Synchronous: one indexed SQLite lookup, safe inline in the request path.
    """
    if not is_enabled() or namespace not in _NAMESPACE_FIELDS:
        return None
    try:
        row = _get_db().execute(
            "SELECT tmdb_tv, tmdb_movie, imdb_id FROM anime_id_map "
            "WHERE namespace = ? AND anime_id = ?",
            (namespace, int(anime_id)),
        ).fetchone()
    except Exception as exc:
        logger.warning(f"Anime id mapping lookup failed for {namespace}:{anime_id}: {exc}")
        return None
    if row is None:
        return None
    tmdb_tv, tmdb_movie, imdb_id = row
    tmdb = tmdb_movie if media_type == "movie" else tmdb_tv
    if tmdb is None and not imdb_id:
        return None
    return MappedIds(str(tmdb) if tmdb is not None else None, imdb_id or None)


def mal_to_provider(mal_id: int) -> "tuple[str, int] | None":
    """The provider id to render a MyAnimeList id as: ("kitsu", 7442), or
    ("anilist", 16498) when the entry has no Kitsu id, or None when the list
    doesn't know it. Kitsu first because its covers are about twice
    AniList's resolution.  One indexed SQLite lookup, like lookup()."""
    if not is_enabled():
        return None
    try:
        row = _get_db().execute(
            "SELECT kitsu_id, anilist_id FROM anime_mal_map WHERE mal_id = ?",
            (int(mal_id),),
        ).fetchone()
    except Exception as exc:
        logger.warning(f"Anime id mapping lookup failed for mal:{mal_id}: {exc}")
        return None
    if row is None:
        return None
    kitsu_id, anilist_id = row
    if kitsu_id is not None:
        return "kitsu", int(kitsu_id)
    if anilist_id is not None:
        return "anilist", int(anilist_id)
    return None


def reverse_lookup(media_type: str, tmdb_id: str | None, imdb_id: str | None) -> dict[str, int]:
    """The AniList and Kitsu ids the list gives a TMDB or IMDb title, by
    namespace ({"anilist": 21, "kitsu": 11}); empty when it isn't anime.

    The TMDB id is matched as the kind being rendered (see lookup) and wins
    over the IMDb id when it matches anything.  A series' later seasons map
    to the same TMDB and IMDb ids as its first, so a show matches several
    entries per namespace; the lowest id is taken, which on both sites is
    normally the first season — the same entry MDBList's MyAnimeList score
    comes from.
    """
    if not is_enabled():
        return {}
    col = "tmdb_movie" if media_type == "movie" else "tmdb_tv"
    tries = []
    if tmdb_id and str(tmdb_id).isascii() and str(tmdb_id).isdigit():
        tries.append((col, int(tmdb_id)))
    if imdb_id and str(imdb_id).startswith("tt"):
        tries.append(("imdb_id", imdb_id))
    for column, value in tries:
        try:
            rows = _get_db().execute(
                f"SELECT namespace, MIN(anime_id) FROM anime_id_map WHERE {column} = ? GROUP BY namespace",
                (value,),
            ).fetchall()
        except Exception as exc:
            logger.warning(f"Anime id reverse lookup failed for {column}={value}: {exc}")
            return {}
        if rows:
            return {ns: int(aid) for ns, aid in rows if ns in _NAMESPACE_FIELDS}
    return {}


def _query(sql: str, args: tuple) -> list:
    if not is_enabled():
        return []
    try:
        return _get_db().execute(sql, args).fetchall()
    except Exception as exc:
        logger.warning(f"Anime id mapping query failed: {exc}")
        return []


def anilist_for_kitsu(kitsu_id: int) -> list[int]:
    """The AniList id of the same entry as Kitsu *kitsu_id* (the list pairs
    them on its MyAnimeList rows), or [] when it doesn't know one."""
    return [int(a) for (a,) in _query(
        "SELECT DISTINCT anilist_id FROM anime_mal_map WHERE kitsu_id = ? AND anilist_id IS NOT NULL",
        (int(kitsu_id),))]


def anilist_for_title(media_type: str, tmdb_id: str | None, imdb_id: str | None) -> list[int]:
    """Every AniList entry the list maps to a TMDB or IMDb title: a show's
    seasons all map to the show, and AniList ranks each season apart, so the
    one trending now is any of them.  The TMDB id wins over the IMDb id when
    it matches anything, as in reverse_lookup."""
    col = "tmdb_movie" if media_type == "movie" else "tmdb_tv"
    tries = []
    if tmdb_id and str(tmdb_id).isascii() and str(tmdb_id).isdigit():
        tries.append((col, int(tmdb_id)))
    if imdb_id and str(imdb_id).startswith("tt"):
        tries.append(("imdb_id", imdb_id))
    for column, value in tries:
        rows = _query(f"SELECT anime_id FROM anime_id_map WHERE namespace = 'anilist' AND {column} = ?",
                      (value,))
        if rows:
            return sorted(int(a) for (a,) in rows)
    return []


def tmdb_films_for_anilist(anilist_ids: "set[int]") -> dict[int, int]:
    """{AniList id: its TMDB film} for each of *anilist_ids* the list maps
    to a film."""
    out: dict[int, int] = {}
    ids = sorted(anilist_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for a, m in _query(f"SELECT anime_id, tmdb_movie FROM anime_id_map WHERE namespace = 'anilist' "
                           f"AND anime_id IN ({marks}) AND tmdb_movie IS NOT NULL", tuple(chunk)):
            out[int(a)] = int(m)
    return out


def ids_for_anilist(anilist_ids: "set[int]") -> tuple[set[int], set[int], set[int]]:
    """(kitsu ids, TMDB series ids, TMDB movie ids) of *anilist_ids*: every id
    a poster of those entries can be requested and cached under."""
    kitsu: set[int] = set()
    tv: set[int] = set()
    movie: set[int] = set()
    ids = sorted(anilist_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for (k,) in _query(f"SELECT kitsu_id FROM anime_mal_map WHERE anilist_id IN ({marks}) "
                           "AND kitsu_id IS NOT NULL", tuple(chunk)):
            kitsu.add(int(k))
        for t, m in _query(f"SELECT tmdb_tv, tmdb_movie FROM anime_id_map "
                           f"WHERE namespace = 'anilist' AND anime_id IN ({marks})", tuple(chunk)):
            if t is not None:
                tv.add(int(t))
            if m is not None:
                movie.add(int(m))
    return kitsu, tv, movie


def _first_id(value) -> int | None:
    """A TMDB id as the list gives it: a number, or a list whose first entry
    is the one meant."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, int)), None)
    return value if isinstance(value, int) else None


def _rows_from_list(entries: list) -> "list[tuple]":
    """(namespace, anime_id, tmdb_tv, tmdb_movie, imdb_id) for every entry that
    maps a namespace we source from to anything we can use."""
    rows: dict[tuple[str, int], tuple] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tmdb = entry.get("themoviedb_id")
        tmdb_tv = tmdb_movie = None
        if isinstance(tmdb, dict):
            # A series' id is a number, a film's a list of them ("movie": [128]).
            tmdb_tv, tmdb_movie = (_first_id(tmdb.get("tv")), _first_id(tmdb.get("movie")))
        imdb = entry.get("imdb_id")
        if isinstance(imdb, list):
            imdb = next((i for i in imdb if isinstance(i, str) and i.startswith("tt")), None)
        elif not (isinstance(imdb, str) and imdb.startswith("tt")):
            imdb = None
        tmdb_tv = tmdb_tv if isinstance(tmdb_tv, int) else None
        tmdb_movie = tmdb_movie if isinstance(tmdb_movie, int) else None
        if tmdb_tv is None and tmdb_movie is None and imdb is None:
            continue
        for namespace, field in _NAMESPACE_FIELDS.items():
            anime_id = entry.get(field)
            if isinstance(anime_id, int):
                # First entry wins; the list has the odd duplicate id.
                rows.setdefault((namespace, anime_id),
                                (namespace, anime_id, tmdb_tv, tmdb_movie, imdb))
    return list(rows.values())


def _mal_rows_from_list(entries: list) -> "list[tuple]":
    """(mal_id, kitsu_id, anilist_id) for every entry with a MAL id and at
    least one provider id. Unlike _rows_from_list, an entry with no TMDB or
    IMDb id still counts: the provider id alone is enough to render."""
    rows: dict[int, tuple] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mal_id = entry.get("mal_id")
        if not isinstance(mal_id, int) or mal_id <= 0:
            continue
        kitsu_id = entry.get("kitsu_id")
        anilist_id = entry.get("anilist_id")
        kitsu_id = kitsu_id if isinstance(kitsu_id, int) else None
        anilist_id = anilist_id if isinstance(anilist_id, int) else None
        if kitsu_id is None and anilist_id is None:
            continue
        rows.setdefault(mal_id, (mal_id, kitsu_id, anilist_id))
    return list(rows.values())


async def refresh_mapping(client: httpx.AsyncClient) -> int:
    """Download the list and swap it into the table. Returns rows loaded."""
    global _last_refresh_ts, _last_refresh_error, _row_count

    if not is_enabled():
        return 0
    try:
        resp = await client.get(ANIME_ID_MAP_URL, timeout=120.0, follow_redirects=True)
        resp.raise_for_status()
        raw = resp.content
    except Exception as exc:
        _last_refresh_error = f"download failed: {exc}"
        logger.error(f"Anime id mapping download failed: {exc}")
        return 0

    def _parse_and_load() -> int:
        entries = json.loads(raw)
        rows = _rows_from_list(entries)
        mal_rows = _mal_rows_from_list(entries)
        if not rows:
            raise ValueError("the list parsed but mapped nothing — not replacing the table")
        conn = sqlite3.connect(ANIME_ID_MAP_PATH, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA.format(table="anime_id_map"))
            # Built alongside and swapped in, so readers on other connections
            # never see a half-populated table.
            conn.execute("DROP TABLE IF EXISTS anime_id_map_new")
            conn.execute(_SCHEMA.format(table="anime_id_map_new"))
            conn.execute("BEGIN")
            conn.executemany("INSERT INTO anime_id_map_new VALUES (?, ?, ?, ?, ?)", rows)
            conn.execute("DROP TABLE anime_id_map")
            conn.execute("ALTER TABLE anime_id_map_new RENAME TO anime_id_map")
            conn.execute("DROP TABLE IF EXISTS anime_mal_map_new")
            conn.execute(_MAL_SCHEMA.format(table="anime_mal_map_new"))
            conn.executemany("INSERT INTO anime_mal_map_new VALUES (?, ?, ?)", mal_rows)
            conn.execute("DROP TABLE IF EXISTS anime_mal_map")
            conn.execute("ALTER TABLE anime_mal_map_new RENAME TO anime_mal_map")
            for ddl in _INDEXES:
                conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()
        return len(rows), len(mal_rows)

    try:
        count, mal_count = await asyncio.get_running_loop().run_in_executor(None, _parse_and_load)
    except Exception as exc:
        _last_refresh_error = f"parse/load failed: {exc}"
        logger.error(f"Anime id mapping parse/load failed: {exc}")
        return 0

    _stale = getattr(_local, "conn", None)
    if _stale is not None:
        with suppress(Exception):
            _stale.close()
    _local.conn = None

    _last_refresh_ts = time.time()
    _last_refresh_error = None
    _row_count = count
    logger.info(f"Anime id mapping refreshed: {count} kitsu/anilist ids, {mal_count} MAL ids loaded")
    return count


async def anime_id_map_refresh_loop(client: httpx.AsyncClient,
                                    on_refresh: "Callable[[], None] | None" = None) -> None:
    """Background task: refresh shortly after startup, then daily. Claimed
    across workers exactly as imdb_dataset_refresh_loop is, for the same
    reasons; a worker that loses the claim re-reads the winner's table.
    *on_refresh* runs (in a thread) after each refresh this worker loads."""
    if not is_enabled():
        return

    global _row_count
    interval = max(1, ANIME_ID_MAP_REFRESH_HOURS) * 3600
    await asyncio.sleep(20)  # let the service finish warming up first
    while True:
        try:
            if claim_app_state_slot(_REFRESH_CLAIM_KEY, time.time(), interval * 0.9):
                if await refresh_mapping(client) == 0:
                    set_app_state(_REFRESH_CLAIM_KEY, "0")
                elif on_refresh is not None:
                    await asyncio.to_thread(on_refresh)
            else:
                _local.conn = None
                _row_count = _count_rows()
        except Exception as exc:
            logger.error(f"Anime id mapping refresh loop error: {exc}")
        await asyncio.sleep(interval if _row_count > 0 else _NOT_READY_RETRY_SECS)
