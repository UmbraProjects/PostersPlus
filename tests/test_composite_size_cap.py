"""COMPOSITE_MAX_GB (the trigger-kept size total and eviction by size) and
CACHE_MIN_FREE_GB (composites and source images not stored on a nearly full
volume)."""

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
            mock.patch.object(cache, "CACHE_MIN_FREE_GB", 0),
        ]
        for p in self._patches:
            p.start()
        cache._disk_checked_at = None
        cache._disk_is_low = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cache._disk_checked_at = None
        cache._disk_is_low = False
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

    def test_caching_resumes_once_space_is_freed(self):
        with mock.patch.object(cache, "CACHE_MIN_FREE_GB", 1):
            with self._free(0.1):
                self.assertTrue(cache.disk_low())
            cache._disk_checked_at = None    # the interval has passed
            with self._free(3):
                self.assertFalse(cache.disk_low())


if __name__ == "__main__":
    unittest.main()
