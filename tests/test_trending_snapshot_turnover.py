"""Every poster that prints a trending rank turns over with its snapshot.

A report had two posters both reading "#10 Today". Each composite used to be
cached for a flat day from its own render, and clients were told the same, so
posters drawn from yesterday's snapshot sat beside today's. The scheduled
cycle also re-rendered them with the old ranks and a fresh day, because with
TMDB as the source it never refreshed the snapshot at all.
"""

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import cache
import main
import tmdb


class _TempDb(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()
        self._cfg_saved = (cache._cfg.TRENDING_FETCH_TIME, cache._cfg.TRENDING_FETCH_TIMEZONE)
        cache._cfg.TRENDING_FETCH_TIME = ""
        cache._cfg.TRENDING_FETCH_TIMEZONE = "UTC"
        with cache._composite_l1_lock:
            cache._composite_l1.clear()

    def tearDown(self):
        cache._cfg.TRENDING_FETCH_TIME, cache._cfg.TRENDING_FETCH_TIMEZONE = self._cfg_saved
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved
        self._dir.cleanup()

    def _store(self, media_type, rankings, cached_at, sig="tmdb"):
        with cache._db_lock:
            cache.get_db().execute(
                "INSERT OR REPLACE INTO trending_cache (media_type, rankings_json, cached_at, source_sig) "
                "VALUES (?, ?, ?, ?)",
                (media_type, __import__("json").dumps(rankings), int(cached_at), sig),
            )
            cache.get_db().commit()


class SnapshotExpiryTests(_TempDb):
    def test_rolling_snapshot_lasts_a_day(self):
        self.assertEqual(cache.trending_snapshot_expires_at(1_000_000), 1_000_000 + 86400)

    def test_scheduled_snapshot_expires_at_the_next_fetch_time(self):
        cache._cfg.TRENDING_FETCH_TIME = "04:00"
        cache._cfg.TRENDING_FETCH_TIMEZONE = "Europe/Bratislava"
        tz = ZoneInfo("Europe/Bratislava")
        written = datetime(2026, 9, 24, 13, 30, tzinfo=tz).timestamp()
        self.assertEqual(
            cache.trending_snapshot_expires_at(written),
            datetime(2026, 9, 25, 4, 0, tzinfo=tz).timestamp(),
        )
        # Written a moment after the fetch time: the next one is tomorrow's.
        at_fetch = datetime(2026, 9, 25, 4, 0, 1, tzinfo=tz).timestamp()
        self.assertEqual(
            cache.trending_snapshot_expires_at(at_fetch),
            datetime(2026, 9, 26, 4, 0, tzinfo=tz).timestamp(),
        )

    def test_entry_reports_expiry_and_goes_stale_at_it(self):
        now = time.time()
        self._store("movie", {"1": 1}, now - 100)
        rankings, expires_at = cache.get_cached_trending_snapshot_entry("movie", "tmdb")
        self.assertEqual(rankings, {"1": 1})
        self.assertEqual(expires_at, int(now - 100) + 86400)

        self._store("movie", {"1": 1}, now - 86400 - 5)
        self.assertIsNone(cache.get_cached_trending_snapshot_entry("movie", "tmdb"))
        self.assertIsNotNone(cache.get_cached_trending_snapshot_entry("movie", include_stale=True))

    def test_replacing_an_expired_snapshot_still_invalidates_dropouts(self):
        # The diff used to read the old snapshot through the expiry check, so
        # replacing an expired one (the normal case) compared against nothing
        # and never invalidated the titles that had left the list.
        self._store("movie", {"10": 1, "20": 2}, time.time() - 2 * 86400)
        with mock.patch.object(cache, "invalidate_final_posters") as inv:
            cache.set_cached_trending_snapshot("movie", {"10": 1}, "tmdb")
        self.assertIn(mock.call("20", "movie"), inv.call_args_list)
        self.assertNotIn(mock.call("10", "movie"), inv.call_args_list)


class AnimeKeyInvalidationTests(_TempDb):
    def test_anime_composites_are_cleared_from_memory(self):
        anime = "kitsu:123:tt0000001:456:tv:abcd"
        plain = "tt0000002:456:movie:abcd"
        other = "tt0000003:789:tv:abcd"
        for k in (anime, plain, other):
            cache.set_cached_final_poster(k, b"x")
        cache.invalidate_final_posters("456", "series")
        with cache._composite_l1_lock:
            self.assertNotIn(anime, cache._composite_l1)
            self.assertIn(plain, cache._composite_l1)
            self.assertIn(other, cache._composite_l1)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TmdbRankTests(unittest.TestCase):
    def test_a_title_on_two_pages_keeps_its_first_rank_and_ranks_have_no_gaps(self):
        pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 2}, {"id": 3}], 3: [], 4: [], 5: []}

        async def _get(url, params):
            return _Resp({"results": pages[params["page"]]})

        client = mock.Mock(get=_get)
        ids = asyncio.run(tmdb._fetch_tmdb_trending_ids(client, "k", "movie"))
        self.assertEqual(ids, ["1", "2", "3"])


class EnsureSnapshotTests(_TempDb):
    def setUp(self):
        super().setUp()
        self._src = (tmdb.TRENDING_SOURCE_MOVIE, tmdb.TRENDING_SOURCE_TV)
        tmdb.TRENDING_SOURCE_MOVIE = tmdb.TRENDING_SOURCE_TV = ""

    def tearDown(self):
        tmdb.TRENDING_SOURCE_MOVIE, tmdb.TRENDING_SOURCE_TV = self._src
        super().tearDown()

    def test_a_current_snapshot_is_not_refetched(self):
        self._store("movie", {"5": 1}, time.time() - 60)
        with mock.patch.object(tmdb, "_fetch_tmdb_trending_ids") as fetch:
            rank, expires_at = asyncio.run(tmdb.fetch_trending_rank_entry(None, "5", "k", "movie"))
        fetch.assert_not_called()
        self.assertEqual(rank, 1)
        self.assertAlmostEqual(expires_at, time.time() - 60 + 86400, delta=2)

    def test_an_expired_snapshot_is_refetched(self):
        self._store("movie", {"5": 1}, time.time() - 2 * 86400)

        async def _ids(client, key, endpoint):
            return ["7", "5"]

        with mock.patch.object(tmdb, "_fetch_tmdb_trending_ids", side_effect=_ids):
            rank, expires_at = asyncio.run(tmdb.fetch_trending_rank_entry(None, "5", "k", "movie"))
        self.assertEqual(rank, 2)
        self.assertAlmostEqual(expires_at, time.time() + 86400, delta=2)

    def test_warming_does_not_replace_a_current_custom_snapshot(self):
        tmdb.TRENDING_SOURCE_MOVIE = "https://example.com/list.json"
        sig = tmdb.trending_source_signature("movie")
        self._store("movie", {"5": 1}, time.time() - 60, sig=sig)

        async def _source(client, media_type):
            return ["9", "5"] if media_type == "movie" else None

        async def _list(*a, **k):
            return []

        with mock.patch.object(tmdb, "fetch_trending_source_ids", side_effect=_source), \
             mock.patch.object(tmdb.httpx, "AsyncClient"):
            client = mock.Mock()
            client.get = mock.AsyncMock(return_value=_Resp({"results": []}))
            asyncio.run(tmdb.fetch_trending_candidates(client, "k", max_items=20))
        self.assertEqual(cache.get_cached_trending_snapshot("movie", sig), {"5": 1})


class TrendingCycleTests(_TempDb):
    def setUp(self):
        super().setUp()
        self._key = main._cfg.SERVER_TMDB_KEY
        main._cfg.SERVER_TMDB_KEY = "k"

    def tearDown(self):
        main._cfg.SERVER_TMDB_KEY = self._key
        super().tearDown()

    def _run_cycle(self, ensure):
        seen = {}

        async def _regen(matches, *, log_prefix):
            seen["matches"] = matches
            return 0

        with mock.patch.object(main, "ensure_trending_snapshot", side_effect=ensure), \
             mock.patch.object(main, "_regenerate_cached_posters", side_effect=_regen):
            asyncio.run(main._run_trending_fetch_cycle(None))
        return seen.get("matches")

    def test_current_snapshots_rerender_nothing(self):
        now = time.time()
        self._store("movie", {"1": 1}, now - 60)
        self._store("tv", {"2": 1}, now - 60)

        async def _ensure(client, key, endpoint):
            return cache.get_cached_trending_snapshot_entry(endpoint, "tmdb")

        self.assertIsNone(self._run_cycle(_ensure))

    def test_a_refresh_rerenders_old_and_new_titles_matched_from_the_key_tail(self):
        old = time.time() - 2 * 86400
        self._store("movie", {"1": 1, "2": 2}, old)
        self._store("tv", {"3": 1}, time.time() - 60)

        async def _ensure(client, key, endpoint):
            if endpoint == "movie":
                cache.set_cached_trending_snapshot("movie", {"2": 1, "4": 2}, "tmdb")
            return cache.get_cached_trending_snapshot_entry(endpoint, "tmdb")

        matches = self._run_cycle(_ensure)
        self.assertIsNotNone(matches)
        split = lambda k: k.split(":")
        for key in ("tt1:1:movie:h", "tt2:2:movie:h", "tmdb:4:movie:h",
                    "kitsu:9:tt9:4:movie:h"):
            self.assertTrue(matches(split(key)), key)
        # TV was still current, and types must not cross.
        self.assertFalse(matches(split("tt3:3:series:h")))
        self.assertFalse(matches(split("tt1:1:tv:h")))

    def test_loop_sleeps_until_the_earliest_snapshot_expires(self):
        now = time.time()
        self._store("movie", {"1": 1}, now - 3600)
        self._store("tv", {"1": 1}, now - 7200)
        self.assertAlmostEqual(main._seconds_until_trending_due(), 86400 - 7200 + 1, delta=3)

    def test_loop_retries_within_the_hour_after_a_failed_refresh(self):
        self._store("movie", {"1": 1}, time.time() - 2 * 86400)
        self.assertAlmostEqual(main._seconds_until_trending_due(), 3601, delta=3)


class RenderTtlTests(unittest.TestCase):
    def test_ranked_composites_expire_with_their_snapshot(self):
        src = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("(trending_rank, trending_expires_at),", src)
        self.assertIn("max(60, int(trending_expires_at - time.time()))", src)


if __name__ == "__main__":
    unittest.main()
