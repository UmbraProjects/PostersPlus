"""COMPOSITE_MAX_GB / COMPOSITE_MAX_DISK_PCT (the trigger-kept size total and
eviction by size), CACHE_MIN_FREE_GB (source images not stored, composites
only in space the database already has, and disk relief on a nearly full
volume) and handing free pages back to the volume."""

import unittest
from collections import namedtuple
from unittest import mock

import cache
from tests.test_trending_snapshot_turnover import _TempDb

_Usage = namedtuple("_Usage", "total used free")
_GB = 1024 ** 3


class _Base(_TempDb):
    def setUp(self):
        super().setUp()
        # Neither cap nor floor unless a test sets one.
        self._patches = [
            mock.patch.object(cache, "COMPOSITE_MAX_ENTRIES", 0),
            mock.patch.object(cache, "COMPOSITE_MAX_GB", 0),
            mock.patch.object(cache, "COMPOSITE_MAX_DISK_PCT", 0),
            mock.patch.object(cache, "CACHE_MIN_FREE_GB", 0),
            # disk_low's background relief, tested directly below.
            mock.patch.object(cache, "_start_disk_relief"),
        ]
        for p in self._patches:
            p.start()
        cache._disk_checked_at = None
        cache._disk_is_low = False
        cache._volume_total = None

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cache._disk_checked_at = None
        cache._disk_is_low = False
        cache._volume_total = None
        super().tearDown()

    def _summed(self):
        return cache.get_db().execute(
            "SELECT COALESCE(SUM(LENGTH(jpeg_bytes)), 0) FROM final_poster_cache").fetchone()[0]

    def _keys(self):
        return {r[0] for r in cache.get_db().execute("SELECT cache_key FROM final_poster_cache")}


class CompositeTotalTests(_Base):
    def test_total_follows_inserts_replaces_and_every_kind_of_delete(self):
        cache.set_cached_final_poster("tt1:1:movie:a", b"x" * 100)
        cache.set_cached_final_poster("tt2:2:movie:a", b"x" * 50)
        self.assertEqual(cache.composite_total_bytes(), 150)
        # INSERT OR REPLACE of the same key counts the new image only.
        cache.set_cached_final_poster("tt1:1:movie:a", b"x" * 30)
        self.assertEqual(cache.composite_total_bytes(), 80)
        cache.delete_cached_final_poster("tt2:2:movie:a")
        self.assertEqual(cache.composite_total_bytes(), 30)
        cache.get_db().execute("DELETE FROM final_poster_cache")
        cache.get_db().commit()
        self.assertEqual(cache.composite_total_bytes(), 0)

    def test_rows_from_before_the_size_column_count_by_their_length(self):
        # A row without size, as every composite written before it has.
        db = cache.get_db()
        db.execute("INSERT INTO final_poster_cache (cache_key, jpeg_bytes, cached_at) "
                   "VALUES ('old', ?, 1)", (b"y" * 70,))
        db.commit()
        self.assertEqual(cache.composite_total_bytes(), 70)
        cache.set_cached_final_poster("old", b"y" * 10)
        self.assertEqual(cache.composite_total_bytes(), 10)
        cache.delete_cached_final_poster("old")
        self.assertEqual(cache.composite_total_bytes(), 0)

    def test_an_existing_cache_is_summed_once_when_the_total_is_created(self):
        db = cache.get_db()
        db.execute("INSERT INTO final_poster_cache (cache_key, jpeg_bytes, cached_at) "
                   "VALUES ('a', ?, 1), ('b', ?, 2)", (b"z" * 40, b"z" * 60))
        db.execute("DELETE FROM final_poster_totals")
        db.commit()
        cache.init_db()
        self.assertEqual(cache.composite_total_bytes(), 100)
        self.assertEqual(cache.get_cache_stats()["composite_bytes"], 100)

    def test_a_restart_does_not_sum_the_table_again(self):
        seen = []
        cache.get_db().set_trace_callback(seen.append)
        try:
            cache.init_db()
        finally:
            cache.get_db().set_trace_callback(None)
        self.assertFalse([sql for sql in seen if "SUM(" in sql])


class CompositeSizeCapTests(_Base):
    def test_oldest_go_until_under_the_low_water_mark(self):
        # A 1000-byte cap: ten 100-byte composites fill it, the eleventh tips
        # it over and the oldest go until the total is at or under 950.
        with mock.patch.object(cache, "COMPOSITE_MAX_GB", 1000 / _GB), \
             mock.patch("cache.time.time", side_effect=range(1000, 2000)):
            for i in range(11):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 100)
        self.assertLessEqual(cache.composite_total_bytes(), 950)
        self.assertEqual(cache.composite_total_bytes(), self._summed())
        self.assertNotIn("tt0:0:movie:a", self._keys())
        self.assertNotIn("tt1:1:movie:a", self._keys())
        self.assertIn("tt10:10:movie:a", self._keys())

    def test_no_cap_evicts_nothing(self):
        for i in range(20):
            cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 1000)
        self.assertEqual(len(self._keys()), 20)

    def test_a_write_evicts_a_bounded_batch_and_the_prune_finishes(self):
        for i in range(30):
            cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 100)
        with mock.patch.object(cache, "COMPOSITE_MAX_GB", 500 / _GB), \
             mock.patch.object(cache, "_BYTES_EVICT_PER_WRITE", 5), \
             mock.patch.object(cache, "_PRUNE_BATCH", 4):
            cache.set_cached_final_poster("tt99:99:movie:a", b"x" * 100)
            self.assertEqual(len(self._keys()), 26)   # 31 written, 5 evicted
            cache.prune_caches()
            self.assertLessEqual(cache.composite_total_bytes(), 475)
        self.assertEqual(cache.composite_total_bytes(), self._summed())


class DiskShareCapTests(_Base):
    def _volume(self, total):
        return mock.patch("cache.shutil.disk_usage", return_value=_Usage(total, 0, total))

    def test_the_share_of_the_volume_caps_the_cache(self):
        with mock.patch.object(cache, "COMPOSITE_MAX_DISK_PCT", 40), self._volume(100 * _GB):
            self.assertEqual(cache._composite_max_bytes(), 40 * _GB)

    def test_the_lower_of_the_two_caps_applies(self):
        with mock.patch.object(cache, "COMPOSITE_MAX_DISK_PCT", 40), self._volume(100 * _GB):
            with mock.patch.object(cache, "COMPOSITE_MAX_GB", 10):
                self.assertEqual(cache._composite_max_bytes(), 10 * _GB)
            with mock.patch.object(cache, "COMPOSITE_MAX_GB", 60):
                self.assertEqual(cache._composite_max_bytes(), 40 * _GB)

    def test_the_volume_is_looked_at_once_an_hour(self):
        with mock.patch.object(cache, "COMPOSITE_MAX_DISK_PCT", 40), self._volume(100 * _GB) as usage:
            for _ in range(5):
                cache._composite_max_bytes()
        self.assertEqual(usage.call_count, 1)

    def test_evicts_like_the_gb_cap(self):
        # 40% of a 2500-byte volume is a 1000-byte cap.
        with mock.patch.object(cache, "COMPOSITE_MAX_DISK_PCT", 40), self._volume(2500), \
             mock.patch("cache.time.time", side_effect=range(1000, 2000)):
            for i in range(11):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 100)
        self.assertLessEqual(cache.composite_total_bytes(), 950)
        self.assertNotIn("tt0:0:movie:a", self._keys())


class DiskFloorTests(_Base):
    def _free(self, gb):
        return mock.patch("cache.shutil.disk_usage", return_value=_Usage(100 * _GB, 0, int(gb * _GB)))

    def test_a_nearly_full_volume_serves_without_storing(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(0.5), \
             mock.patch.object(cache, "_atomic_write") as write:
            cache.set_cached_final_poster("tt1:1:movie:a", b"x" * 10)
            cache.set_cached_tmdb_poster("p.jpg", b"img")
            cache.set_cached_tmdb_logo("l.png", b"img")
        self.assertEqual(self._keys(), set())
        write.assert_not_called()

    def test_enough_space_stores_as_before(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(5):
            cache.set_cached_final_poster("tt1:1:movie:a", b"x" * 10)
        self.assertEqual(self._keys(), {"tt1:1:movie:a"})

    def test_off_never_looks(self):
        with mock.patch("cache.shutil.disk_usage") as usage:
            cache.set_cached_final_poster("tt1:1:movie:a", b"x")
        usage.assert_not_called()

    def test_free_space_is_looked_at_once_per_interval(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(5) as usage:
            for i in range(5):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x")
        self.assertEqual(usage.call_count, 1)

    def test_a_nearly_full_volume_stores_in_older_composites_space(self):
        with mock.patch("cache.time.time", side_effect=range(1000, 2000)):
            for i in range(10):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 20000)
            pages = cache.get_db().execute("PRAGMA page_count").fetchone()[0]
            with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(0.5):
                for i in range(10, 15):
                    cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 20000)
        keys = self._keys()
        self.assertTrue({f"tt{i}:{i}:movie:a" for i in range(10, 15)} <= keys)
        self.assertNotIn("tt0:0:movie:a", keys)
        # The new ones took evicted composites' pages: the file didn't grow.
        self.assertLessEqual(cache.get_db().execute("PRAGMA page_count").fetchone()[0], pages)
        self.assertEqual(cache.composite_total_bytes(), self._summed())

    def test_an_all_but_full_volume_stores_nothing(self):
        cache.set_cached_final_poster("tt0:0:movie:a", b"x" * 20000)
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(0.01):
            cache.set_cached_final_poster("tt1:1:movie:a", b"x" * 100)
        self.assertEqual(self._keys(), {"tt0:0:movie:a"})

    def test_a_low_volume_starts_relief(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), self._free(0.5):
            cache.disk_low()
        cache._start_disk_relief.assert_called_once()

    def test_caching_resumes_once_space_is_freed(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1):
            with self._free(0.1):
                self.assertTrue(cache.disk_low())
            cache._disk_checked_at = None    # the interval has passed
            with self._free(3):
                self.assertFalse(cache.disk_low())


class DiskReliefTests(_Base):
    def _fill(self, n, size=20000):
        with mock.patch("cache.time.time", side_effect=range(1000, 2000)):
            for i in range(n):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * size)

    def test_evicts_the_oldest_until_the_shortfall_is_handed_back(self):
        self._fill(10)
        before = cache.get_db().execute("PRAGMA page_count").fetchone()[0]
        # Twice a 100 KB floor wanted, 150 KB free: a 50 KB shortfall.
        floor = 100_000 / _GB
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", floor), \
             mock.patch("cache.shutil.disk_usage", return_value=_Usage(100 * _GB, 0, 150_000)):
            evicted = cache.relieve_disk()
        self.assertGreaterEqual(evicted, 50_000)
        self.assertLess(evicted, 100_000)
        keys = self._keys()
        self.assertNotIn("tt0:0:movie:a", keys)
        self.assertIn("tt9:9:movie:a", keys)
        db = cache.get_db()
        self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
        self.assertLess(db.execute("PRAGMA page_count").fetchone()[0], before)

    def test_a_reclaim_that_stalls_evicts_no_more_than_the_shortfall(self):
        self._fill(10)
        floor = 100_000 / _GB
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", floor), \
             mock.patch.object(cache, "_PRUNE_BATCH", 1), \
             mock.patch.object(cache, "reclaim_free_pages", return_value=0), \
             mock.patch("cache.shutil.disk_usage", return_value=_Usage(100 * _GB, 0, 150_000)):
            evicted = cache.relieve_disk()
        self.assertLess(evicted, 80_000)
        self.assertGreaterEqual(len(self._keys()), 6)

    def test_enough_free_space_evicts_nothing(self):
        self._fill(3)
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), \
             mock.patch("cache.shutil.disk_usage", return_value=_Usage(100 * _GB, 0, 5 * _GB)):
            self.assertEqual(cache.relieve_disk(), 0)
        self.assertEqual(len(self._keys()), 3)

    def test_one_worker_at_a_time(self):
        import fcntl
        self._fill(3)
        with open(cache.DB_PATH + ".relief.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1), \
                 mock.patch("cache.shutil.disk_usage", return_value=_Usage(100 * _GB, 0, 0)):
                self.assertEqual(cache.relieve_disk(), 0)
        self.assertEqual(len(self._keys()), 3)


class ReclaimTests(_Base):
    def test_every_free_page_goes_back_a_chunk_at_a_time(self):
        for i in range(10):
            cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 20000)
        db = cache.get_db()
        db.execute("DELETE FROM final_poster_cache")
        db.commit()
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        self.assertGreater(free, 10)
        with mock.patch.object(cache, "_VACUUM_CHUNK_PAGES", 3):
            self.assertEqual(cache.reclaim_free_pages(), free)
        self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def test_the_prune_hands_back_what_it_evicted(self):
        for i in range(10):
            cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 20000)
        db = cache.get_db()
        db.execute("UPDATE final_poster_cache SET expires_at = 1")
        db.commit()
        cache.prune_caches()
        self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def test_a_wal_that_wont_checkpoint_stops_the_pass(self):
        for i in range(10):
            cache.set_cached_final_poster(f"tt{i}:{i}:movie:a", b"x" * 20000)
        db = cache.get_db()
        db.execute("DELETE FROM final_poster_cache")
        db.commit()
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        with mock.patch.object(cache, "_VACUUM_CHUNK_PAGES", 3), \
             mock.patch.object(cache, "_RECLAIM_WAL_WAITS", 1), \
             mock.patch.object(cache, "_wal_bytes", return_value=10 ** 12), \
             mock.patch("cache.time.sleep"):
            self.assertEqual(cache.reclaim_free_pages(), 3)
        self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], free - 3)

    def test_connections_cap_the_wal(self):
        self.assertEqual(cache.get_db().execute("PRAGMA journal_size_limit").fetchone()[0], 64 * 1024 ** 2)


if __name__ == "__main__":
    unittest.main()
