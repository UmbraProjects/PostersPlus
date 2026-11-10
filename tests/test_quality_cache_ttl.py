import sqlite3
import time
import unittest
from datetime import datetime, timedelta

import cache
import quality

DAY = 86400


def _date(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


class QualityCacheTtlTests(unittest.TestCase):
    """A quality answer's lifetime is set by the title's age when it was taken."""

    def setUp(self):
        self.previous_initialised = cache._initialised
        self.previous_conn = getattr(cache._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE quality_cache (
                imdb_id       TEXT PRIMARY KEY,
                tokens        TEXT,
                cached_at     INTEGER,
                release_date  TEXT,
                cache_context TEXT NOT NULL DEFAULT '',
                expires_at    INTEGER
            )
            """
        )
        cache._initialised = True
        cache._local.conn = self.conn

    def tearDown(self):
        self.conn.close()
        cache._initialised = self.previous_initialised
        if self.previous_conn is None:
            try:
                del cache._local.conn
            except AttributeError:
                pass
        else:
            cache._local.conn = self.previous_conn

    def _insert(self, tokens, cached_days_ago, release_date=None, expires_at=None):
        self.conn.execute(
            "INSERT INTO quality_cache VALUES (?, ?, ?, ?, ?, ?)",
            (
                "tt32261958",
                "|".join(tokens),
                int(time.time() - cached_days_ago * DAY),
                release_date,
                cache._quality_cache_context(),
                expires_at,
            ),
        )

    def test_answer_taken_while_new_expires_after_the_title_ages_out(self):
        # Verity: released 41 days ago, 1080P cached on day 14, read on day 41.
        # Measured at read time this was "old" and kept for 90 days.
        self._insert(["1080P"], cached_days_ago=27)
        self.assertIsNone(cache.get_cached_quality("tt32261958", _date(41)))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM quality_cache").fetchone(),
            "the expired row is dropped so the next read refetches",
        )

    def test_answer_taken_once_old_keeps_the_long_window(self):
        self._insert(["4K", "WEBDL"], cached_days_ago=30, release_date=_date(200))
        self.assertEqual(cache.get_cached_quality("tt32261958"), ["4K", "WEBDL"])

    def test_answer_taken_while_new_is_fresh_within_a_day(self):
        self._insert(["1080P"], cached_days_ago=0.5, release_date=_date(5))
        self.assertEqual(cache.get_cached_quality("tt32261958"), ["1080P"])

    def test_source_expiry_caps_the_ttl(self):
        self._insert(["4K"], cached_days_ago=3, release_date=_date(300),
                     expires_at=int(time.time() - 60))
        self.assertIsNone(cache.get_cached_quality("tt32261958"))

    def test_source_expiry_in_the_future_keeps_the_row(self):
        self._insert(["4K"], cached_days_ago=3, release_date=_date(300),
                     expires_at=int(time.time() + DAY))
        self.assertEqual(cache.get_cached_quality("tt32261958"), ["4K"])

    def test_set_cached_quality_stores_the_expiry(self):
        cache.set_cached_quality("tt32261958", ["4K", "DV"], "2026-09-30",
                                 expires_at=1794347303.9)
        self.assertEqual(
            self.conn.execute(
                "SELECT tokens, release_date, expires_at FROM quality_cache"
            ).fetchone(),
            ("4K|DV", "2026-09-30", 1794347303),
        )

    def test_ttl_without_a_timestamp_still_measures_from_now(self):
        self.assertEqual(cache._quality_ttl(_date(3)), cache.NEW_CACHE_DURATION)
        self.assertEqual(cache._quality_ttl(_date(60)), cache.QUALITY_OLD_CACHE_DURATION)
        self.assertEqual(cache._quality_ttl(None), cache.QUALITY_OLD_CACHE_DURATION)
        self.assertEqual(cache._quality_ttl("not-a-date"), cache.QUALITY_OLD_CACHE_DURATION)


class QualiCacheExpiryTests(unittest.TestCase):
    def test_next_refresh_at_is_used(self):
        self.assertEqual(quality._qualicache_expiry({"next_refresh_at": 1794347303.1}),
                         1794347303.1)

    def test_stale_answer_is_kept_for_a_day(self):
        expiry = quality._qualicache_expiry({"stale": True, "next_refresh_at": 1.0})
        self.assertAlmostEqual(expiry, time.time() + DAY, delta=5)

    def test_missing_or_bad_values_give_no_cap(self):
        for payload in ({}, {"next_refresh_at": None}, {"next_refresh_at": "soon"},
                        {"next_refresh_at": True}, {"next_refresh_at": 0}):
            with self.subTest(payload=payload):
                self.assertIsNone(quality._qualicache_expiry(payload))


if __name__ == "__main__":
    unittest.main()
