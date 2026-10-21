import json
import sqlite3
import time
import unittest

import cache


class RatingCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_initialised = cache._initialised
        self.previous_conn = getattr(cache._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE rating_cache (
                imdb_id TEXT PRIMARY KEY,
                ratings_json TEXT,
                genre TEXT,
                cached_at INTEGER,
                release_date TEXT,
                award_wins TEXT,
                award_noms TEXT,
                awards_fetched INTEGER,
                festival_label TEXT,
                age_rating INTEGER,
                is_cult INTEGER,
                is_true_story INTEGER,
                is_metacritic INTEGER,
                rating_min_votes INTEGER,
                festival_keyword TEXT
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

    def _insert(self, imdb_id, policy):
        self.conn.execute(
            """
            INSERT INTO rating_cache VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                imdb_id,
                json.dumps({"imdb": 75}),
                "Drama",
                int(time.time()),
                "2020-01-01",
                "",
                "",
                1,
                None,
                15,
                0,
                0,
                0,
                policy,
                None,
            ),
        )
        self.conn.commit()

    def test_legacy_policy_row_is_reused_and_backfilled(self):
        self._insert("tt0000001", None)

        result = cache.get_cached_rating("tt0000001")

        self.assertIsNotNone(result)
        stored_policy = self.conn.execute(
            "SELECT rating_min_votes FROM rating_cache WHERE imdb_id = ?",
            ("tt0000001",),
        ).fetchone()[0]
        self.assertEqual(stored_policy, cache.RATING_MIN_VOTES)

    def test_explicit_policy_change_still_invalidates(self):
        self._insert("tt0000002", cache.RATING_MIN_VOTES + 1)

        result = cache.get_cached_rating("tt0000002")

        self.assertIsNone(result)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM rating_cache WHERE imdb_id = ?",
            ("tt0000002",),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()


class WrongTypeMissCleanupTests(unittest.TestCase):
    """The one-time sweep of MDBList "not found" rows left by a wrong type."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE rating_cache (imdb_id TEXT PRIMARY KEY, "
                          "ratings_json TEXT, release_date TEXT)")
        self.conn.execute("CREATE TABLE final_poster_cache (cache_key TEXT PRIMARY KEY)")
        self.conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.executemany("INSERT INTO rating_cache VALUES (?, ?, ?)", [
            ("tt5687612", "{}", None),              # asked as a movie: cleared
            ("tt0326520", "{}", "1983-01-01"),      # known, too few votes: kept
            ("tt0903747", '{"imdb": 9.5}', None),   # rated: kept
            ("tmdb:49417", "{}", None),             # TMDB route, per type: kept
        ])
        self.conn.executemany("INSERT INTO final_poster_cache VALUES (?)", [
            ("tt5687612:67070:series:abc",), ("tt56876120:1:movie:abc",),
            ("tt0903747:1396:series:abc",),
        ])

    def tearDown(self):
        self.conn.close()

    def _ids(self, table, col):
        return {r[0] for r in self.conn.execute(f"SELECT {col} FROM {table}")}

    def test_clears_only_wrong_type_misses_and_their_posters_once(self):
        cache._clear_mdblist_wrong_type_misses(self.conn)
        self.assertEqual(self._ids("rating_cache", "imdb_id"),
                         {"tt0326520", "tt0903747", "tmdb:49417"})
        self.assertEqual(self._ids("final_poster_cache", "cache_key"),
                         {"tt56876120:1:movie:abc", "tt0903747:1396:series:abc"})
        # Once only: a later miss is a real one, kept for its TTL.
        self.conn.execute("INSERT INTO rating_cache VALUES ('tt5687612', '{}', NULL)")
        cache._clear_mdblist_wrong_type_misses(self.conn)
        self.assertIn("tt5687612", self._ids("rating_cache", "imdb_id"))


class BulkLetterboxdCleanupTests(unittest.TestCase):
    """The one-time sweep of rows that may hold a bulk Letterboxd value out of 10."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE rating_cache (imdb_id TEXT PRIMARY KEY, "
                          "ratings_json TEXT, cached_at INTEGER)")
        self.conn.execute("CREATE TABLE final_poster_cache (cache_key TEXT PRIMARY KEY)")
        self.conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
        after, before = cache._BULK_SHIPPED_AT + 60, cache._BULK_SHIPPED_AT - 60
        self.conn.executemany("INSERT INTO rating_cache VALUES (?, ?, ?)", [
            ("tt32916440", '{"imdb": 7.5, "letterboxd": 8.0}', after),   # cleared
            ("tmdb:550", '{"letterboxd": 4.4}', after),                  # cleared
            ("tt0111161", '{"letterboxd": 4.6}', before),                # older: kept
            ("tt0903747", '{"imdb": 9.5}', after),                       # no Letterboxd: kept
        ])
        self.conn.executemany("INSERT INTO final_poster_cache VALUES (?)", [
            ("tt32916440:1317288:movie:abc",), ("anilist:1:tt32916440:2:movie:abc",),
            ("tmdb:550:550:movie:abc",), ("tmdb:5500:5500:movie:abc",),
            ("tt0111161:278:movie:abc",), ("tt0903747:1396:series:abc",),
        ])

    def tearDown(self):
        self.conn.close()

    def _ids(self, table, col):
        return {r[0] for r in self.conn.execute(f"SELECT {col} FROM {table}")}

    def test_clears_letterboxd_rows_since_bulk_and_their_posters_once(self):
        cache._clear_bulk_letterboxd_rows(self.conn)
        self.assertEqual(self._ids("rating_cache", "imdb_id"), {"tt0111161", "tt0903747"})
        self.assertEqual(self._ids("final_poster_cache", "cache_key"), {
            "tmdb:5500:5500:movie:abc", "tt0111161:278:movie:abc", "tt0903747:1396:series:abc",
        })
        # Once only: rows written after the fix are right.
        self.conn.execute("INSERT INTO rating_cache VALUES ('tt32916440', '{\"letterboxd\": 4.0}', ?)",
                          (cache._BULK_SHIPPED_AT + 120,))
        cache._clear_bulk_letterboxd_rows(self.conn)
        self.assertIn("tt32916440", self._ids("rating_cache", "imdb_id"))
