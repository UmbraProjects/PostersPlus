"""Composite cache keys, the stored replay query, and the smaller caches and
bookkeeping around them."""

import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

import cache
import main
from tests.test_trending_snapshot_turnover import _TempDb


class RenderSignatureTests(unittest.TestCase):
    """The composite key hashes the parsed config, not the raw query."""

    def _sig(self, **params):
        return main._render_config_signature(main.build_request_config(params))

    def test_unknown_parameters_do_not_change_the_key(self):
        # Each distinct junk value used to mint a new composite and a full render.
        self.assertEqual(self._sig(), self._sig(x="1"))
        self.assertEqual(self._sig(badge_height="40"), self._sig(badge_height="40", cb="random"))

    def test_equivalent_spellings_share_a_key(self):
        self.assertEqual(self._sig(logo_max_w_ratio="0.5"), self._sig(logo_max_w_ratio="0.50"))
        self.assertEqual(self._sig(textless="true"), self._sig(textless="1"))

    def test_real_settings_still_change_the_key(self):
        self.assertNotEqual(self._sig(), self._sig(textless="true"))
        self.assertNotEqual(self._sig(badge_height="40"), self._sig(badge_height="41"))
        self.assertNotEqual(self._sig(), self._sig(shape="landscape"))


class ReplayQueryTests(unittest.TestCase):
    def setUp(self):
        self._access_key = main._cfg.ACCESS_KEY

    def tearDown(self):
        main._cfg.ACCESS_KEY = self._access_key

    def test_access_key_is_not_stored(self):
        stored = main._sanitize_request_params(
            "tmdb_id=1&access_key=old&tmdb_key=u&mdblist_key=m&shape=landscape"
        )
        self.assertEqual(stored, "tmdb_id=1&shape=landscape")

    def test_replay_carries_the_current_key(self):
        # A row stored before keys were stripped still names the old key.
        main._cfg.ACCESS_KEY = "rotated"
        self.assertEqual(
            main._replay_query("tmdb_id=1&access_key=old"), "tmdb_id=1&access_key=rotated"
        )
        self.assertEqual(main._replay_query(""), "access_key=rotated")

    def test_open_instance_replays_without_a_key(self):
        main._cfg.ACCESS_KEY = None
        self.assertEqual(main._replay_query("tmdb_id=1&access_key=old"), "tmdb_id=1")


class DebugCanvasCacheTests(unittest.TestCase):
    def setUp(self):
        self._access_key = main._cfg.ACCESS_KEY
        main._cfg.ACCESS_KEY = None
        main._debug_canvas_cache.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        main._cfg.ACCESS_KEY = self._access_key
        main._debug_canvas_cache.clear()

    def test_a_repeat_request_is_served_from_the_cache(self):
        renders = []

        def _build(*args, **kwargs):
            renders.append(kwargs.get("fallback_title"))
            return Image.new("RGBA", (20, 30))
        with mock.patch.object(main, "build_poster", _build):
            for title in ("A", "A", "B"):
                self.assertEqual(
                    self.client.get("/debug/canvas", params={"title": title}).status_code, 200
                )
        self.assertEqual(renders, ["A", "B"])


class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_is_held_until_it_finishes(self):
        release = asyncio.Event()

        async def _job():
            await release.wait()
        task = main._spawn_background(_job())
        self.assertIn(task, main._background_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, main._background_tasks)


class CompositeCapTests(_TempDb):
    def setUp(self):
        super().setUp()
        self._cap = mock.patch.object(cache, "COMPOSITE_MAX_ENTRIES", 3)
        self._cap.start()
        cache._composite_count_estimate = None
        cache._composite_writes_since_count = 0

    def tearDown(self):
        self._cap.stop()
        cache._composite_count_estimate = None
        cache._composite_writes_since_count = 0
        super().tearDown()

    def _rows(self):
        # count(1), so the trace below only sees the cache's own COUNT(*).
        return cache.get_db().execute("SELECT count(1) FROM final_poster_cache").fetchone()[0]

    def test_cap_holds_without_counting_on_every_write(self):
        counts = []
        cache.get_db().set_trace_callback(
            lambda sql: counts.append(sql) if "COUNT(*)" in sql else None
        )
        try:
            for i in range(8):
                cache.set_cached_final_poster(f"tt{i}:{i}:movie:aaaa", b"x")
                self.assertLessEqual(self._rows(), 3)
        finally:
            cache.get_db().set_trace_callback(None)
        self.assertEqual(self._rows(), 3)
        # The first write counts, the next two ride the estimate, then each
        # write past the cap recounts to evict.
        self.assertEqual(len(counts), 6)

    def test_a_recount_happens_at_least_every_n_writes(self):
        with mock.patch.object(cache, "COMPOSITE_MAX_ENTRIES", 10_000), \
             mock.patch.object(cache, "_COMPOSITE_RECOUNT_EVERY", 4):
            counts = []
            cache.get_db().set_trace_callback(
                lambda sql: counts.append(sql) if "COUNT(*)" in sql else None
            )
            try:
                for i in range(9):
                    cache.set_cached_final_poster(f"tt{i}:{i}:movie:aaaa", b"x")
            finally:
                cache.get_db().set_trace_callback(None)
        self.assertEqual(len(counts), 3)   # writes 1, 5 and 9


if __name__ == "__main__":
    unittest.main()
