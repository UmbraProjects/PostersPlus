#imdb_dataset.py
"""
Local IMDb ratings source, sourced from IMDb's own non-commercial datasets
(https://datasets.imdbws.com/title.ratings.tsv.gz) rather than MDBList.

Why this exists: every rating PostersPlus can weight — including the one
labelled "imdb" — currently comes from a single MDBList API call per title.
That is fine for the many sources MDBList aggregates (Letterboxd, Trakt,
Rotten Tomatoes, Metacritic, ...), which have no public bulk-download route.
IMDb is the one exception: IMDb itself publishes a free, no-key-required,
daily-refreshed TSV of every title's aggregate rating and vote count. An
operator who only cares about the IMDb score does not need an MDBList key
(or its rate limits) at all to get one.

This module downloads that TSV on a schedule, loads it into a small SQLite
table keyed on the IMDb id (tconst), and answers point lookups from it. It
never makes a per-title network call — the whole dataset is pulled in one
shot on a timer, matching the pattern digital_release.py uses for its own
periodic background sync.

Enabled with IMDB_DATASET_ENABLED=true. Fully inert (no download, no table,
lookups always return None) when disabled — the default.
"""
import asyncio
import gzip
import io
import logging
import os
import sqlite3
import threading
import time
from contextlib import suppress

import httpx

from cache import claim_app_state_slot, set_app_state
from config import (
    IMDB_DATASET_ENABLED,
    IMDB_DATASET_PATH,
    IMDB_DATASET_REFRESH_HOURS,
    IMDB_DATASET_MIN_VOTES,
)

logger = logging.getLogger(__name__)

_DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
# Shared across worker processes via cache.db's app_state table, so only one
# worker per interval performs the download. See imdb_dataset_refresh_loop.
_REFRESH_CLAIM_KEY = "imdb_dataset_refresh_claimed_at"
# How long a worker with an empty table waits before looking again.
_NOT_READY_RETRY_SECS = 60

_local = threading.local()
_last_refresh_ts: float | None = None
_last_refresh_rows: int = 0
_last_refresh_error: str | None = None
# Cached COUNT(*). The table runs to ~1.7 M rows and SQLite has to walk all of
# them to count, so this is far too expensive to do per request — /server-caps
# is polled by the configurator. Recomputed at startup and after each refresh,
# which are the only two moments it changes.
_row_count: int = 0


def is_enabled() -> bool:
    return bool(IMDB_DATASET_ENABLED)


def is_ready() -> bool:
    """True once the table actually holds ratings.

    Distinct from is_enabled(): between the first-ever startup and the first
    completed download there is a window where the feature is on but every
    lookup returns None. Folded into the composite cache signature so posters
    rendered during that window aren't served from cache afterwards.
    """
    return is_enabled() and _row_count > 0


def _get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(IMDB_DATASET_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(IMDB_DATASET_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS imdb_ratings (
                tconst  TEXT PRIMARY KEY,
                rating  REAL NOT NULL,
                votes   INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS imdb_ratings_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        _local.conn = conn
    return conn


def init_db() -> None:
    """Idempotent — safe to call once at startup. Does nothing at all when the
    feature is disabled: no connection, no file, no table. Enabling it later
    needs no migration step because _get_db() creates the schema on first use.
    """
    global _row_count
    if not is_enabled():
        _row_count = 0
        return
    _get_db()
    _row_count = _count_rows()


def status() -> dict:
    """Operator diagnostics, surfaced on /stats.

    last_refresh_error is the reason this exists: a download or parse failure
    is otherwise only visible in the container logs, and a silently stale
    dataset looks exactly like a working one from the outside.
    """
    return {
        "enabled": is_enabled(),
        "ready": is_ready(),
        "path": IMDB_DATASET_PATH if is_enabled() else None,
        "refresh_hours": IMDB_DATASET_REFRESH_HOURS,
        "min_votes": IMDB_DATASET_MIN_VOTES,
        "last_refresh_unix": _last_refresh_ts,
        "last_refresh_rows": _last_refresh_rows,
        "last_refresh_error": _last_refresh_error,
        "row_count": row_count(),
    }


def _count_rows() -> int:
    """Actual COUNT(*) — a full table walk. Only ever called at startup and
    after a refresh; everything else reads the cached _row_count."""
    if not is_enabled():
        return 0
    try:
        conn = _get_db()
        return conn.execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
    except Exception:
        return 0


def row_count() -> int:
    return _row_count if is_enabled() else 0


def get_rating(imdb_id: str | None) -> float | None:
    """Return the 0-10 average rating for *imdb_id*, or None if unknown /
    below IMDB_DATASET_MIN_VOTES / the feature is disabled.

    Synchronous — this is a single indexed SQLite lookup (sub-millisecond),
    not a network call, so it is safe to call inline from the request path.
    """
    if not is_enabled() or not imdb_id:
        return None
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT rating, votes FROM imdb_ratings WHERE tconst = ?",
            (imdb_id,),
        ).fetchone()
    except Exception as exc:
        logger.warning(f"IMDb dataset lookup failed for {imdb_id}: {exc}")
        return None
    if row is None:
        return None
    rating, votes = row
    if votes < IMDB_DATASET_MIN_VOTES:
        return None
    return float(rating)


async def refresh_dataset(client: httpx.AsyncClient) -> int:
    """Download and reload the full dataset. Returns the row count loaded.

    Runs the download+parse+bulk-insert off the event loop (an ~8.6 MB gzip
    that expands to ~30 MB over ~1.7 M rows) so it never blocks request
    handling; only the final connection handoff happens on this thread.
    """
    global _last_refresh_ts, _last_refresh_rows, _last_refresh_error, _row_count

    if not is_enabled():
        return 0

    try:
        # identity: the file is itself a .gz, parsed as one below.  A
        # negotiated Content-Encoding on top would be decoded by httpx and
        # hand the parser plain text.
        resp = await client.get(
            _DATASET_URL, timeout=120.0, headers={"Accept-Encoding": "identity"}
        )
        resp.raise_for_status()
        raw = resp.content
    except Exception as exc:
        _last_refresh_error = f"download failed: {exc}"
        logger.error(f"IMDb dataset download failed: {exc}")
        return 0

    def _parse_and_load() -> int:
        # Streamed rather than accumulated into a list: materialising all
        # ~1.7 M rows costs ~300 MB of RSS, and this runs in a container that
        # is simultaneously holding decoded poster bitmaps. Feeding the
        # generator straight to executemany measures at ~22 MB peak for the
        # same wall-clock time (~5 s), so the list buys nothing.
        def _rows():
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                text = io.TextIOWrapper(gz, encoding="utf-8")
                text.readline()  # header: tconst  averageRating  numVotes
                for line in text:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 3:
                        continue
                    tconst, rating_str, votes_str = parts
                    try:
                        rating = float(rating_str)
                        votes = int(votes_str)
                    except ValueError:
                        continue
                    yield (tconst, rating, votes)

        conn = sqlite3.connect(IMDB_DATASET_PATH, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS imdb_ratings (
                    tconst  TEXT PRIMARY KEY,
                    rating  REAL NOT NULL,
                    votes   INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS imdb_ratings_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # Load into a fresh table and swap it in, so concurrent readers on
            # other connections never see a half-populated table mid-refresh.
            conn.execute("DROP TABLE IF EXISTS imdb_ratings_new")
            conn.execute("""
                CREATE TABLE imdb_ratings_new (
                    tconst  TEXT PRIMARY KEY,
                    rating  REAL NOT NULL,
                    votes   INTEGER NOT NULL
                )
            """)
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO imdb_ratings_new (tconst, rating, votes) VALUES (?, ?, ?)",
                _rows(),
            )
            conn.execute("DROP TABLE imdb_ratings")
            conn.execute("ALTER TABLE imdb_ratings_new RENAME TO imdb_ratings")
            conn.execute(
                "INSERT OR REPLACE INTO imdb_ratings_meta (key, value) VALUES ('last_refresh', ?)",
                (str(int(time.time())),),
            )
            # Counted from the table rather than the generator: nothing is
            # holding the parsed rows any more, and this is the figure that
            # actually landed.
            loaded = conn.execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return loaded

    try:
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, _parse_and_load)
    except Exception as exc:
        _last_refresh_error = f"parse/load failed: {exc}"
        logger.error(f"IMDb dataset parse/load failed: {exc}")
        return 0

    # Any per-thread connections opened before the swap point at a now-stale
    # schema object; drop the cached handle on this thread so the next lookup
    # reconnects and sees the new table. Closed explicitly rather than left to
    # the collector so the old WAL reader is released immediately.
    _stale = getattr(_local, "conn", None)
    if _stale is not None:
        with suppress(Exception):
            _stale.close()
    _local.conn = None

    _last_refresh_ts = time.time()
    _last_refresh_rows = count
    _last_refresh_error = None
    _row_count = count
    logger.info(f"IMDb dataset refreshed: {count} titles loaded from {_DATASET_URL}")
    return count


async def imdb_dataset_refresh_loop(client: httpx.AsyncClient) -> None:
    """Background task: refresh shortly after startup, then every
    IMDB_DATASET_REFRESH_HOURS. IMDb regenerates this file once a day, so
    refreshing more often than that buys nothing.

    Every uvicorn worker runs its own copy of this loop, so with WORKERS > 1
    the download has to be claimed rather than simply performed: otherwise N
    workers each pull the file and each rewrite the same SQLite table, and the
    losers of the DROP/RENAME race take a SQLITE_BUSY and log a failure for a
    refresh that actually succeeded.

    The workers that don't win still re-count the table afterwards. _row_count
    is per-process and only the winner's refresh updates it directly, so
    without this a loser would keep whatever count it read at startup —
    is_ready() stuck False through the first-ever download, splitting the
    composite cache signature between workers and making /server-caps report
    different title counts depending on which worker answered.
    """
    if not is_enabled():
        return

    global _row_count
    interval = max(1, IMDB_DATASET_REFRESH_HOURS) * 3600
    await asyncio.sleep(30)  # let the service finish warming up first
    while True:
        try:
            if claim_app_state_slot(_REFRESH_CLAIM_KEY, time.time(), interval * 0.9):
                if await refresh_dataset(client) == 0:
                    # Download or parse failed. Release the claim rather than
                    # holding it for most of a day — otherwise one transient
                    # startup failure leaves every worker locked out until
                    # tomorrow, which is exactly when it matters least.
                    set_app_state(_REFRESH_CLAIM_KEY, "0")
            else:
                logger.info(
                    "IMDb dataset refresh claimed by another worker "
                    "— re-reading the table instead"
                )
                _local.conn = None      # the winner swaps the table underneath us
                _row_count = _count_rows()
        except Exception as exc:
            logger.error(f"IMDb dataset refresh loop error: {exc}")
        # A worker that lost the claim usually recounts before the winner has
        # finished writing, so an empty table here means "not ready yet", not
        # "nothing to load". Check back shortly instead of in a day; once rows
        # are visible, settle into the normal cadence. This also gets a failed
        # download retried in a minute rather than at the next daily tick.
        await asyncio.sleep(interval if _row_count > 0 else _NOT_READY_RETRY_SECS)
