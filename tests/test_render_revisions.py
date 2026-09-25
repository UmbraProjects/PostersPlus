"""Targeted composite invalidation (_RENDER_REVISIONS).

A drawing change that only some posters show re-renders just those posters,
instead of bumping _RENDER_CACHE_VERSION and re-rendering the whole cache.
"""

import os
import tempfile
import unittest

import cache
import main
from main import RequestConfig


class RevisionScopeTests(unittest.TestCase):
    """Revision 1: Clean's "★ N/A" and Minimalist Year's missing separator."""

    def _stale(self, cfg, cached_rev, facts):
        return main._composite_is_stale(
            main._revisions_applying(cfg), cfg, cached_rev, facts)

    def test_unrated_clean_and_minimalist_year_posters_are_stale(self):
        for cfg in (RequestConfig(rating_display_mode=2),
                    RequestConfig(rating_display_mode=3, minimalist_append_mode=0)):
            with self.subTest(cfg.rating_display_mode):
                self.assertEqual(self._stale(cfg, 0, {"score": "N/A"}), 1)

    def test_rated_posters_keep_their_entry(self):
        cfg = RequestConfig(rating_display_mode=2)
        self.assertIsNone(self._stale(cfg, 0, {"score": 87}))

    def test_composites_without_facts_are_kept(self):
        # Cached before facts were recorded: can't say whether it was unrated.
        cfg = RequestConfig(rating_display_mode=2)
        self.assertIsNone(self._stale(cfg, 0, None))

    def test_composites_already_at_the_revision_are_current(self):
        cfg = RequestConfig(rating_display_mode=2)
        self.assertIsNone(self._stale(cfg, 1, {"score": "N/A"}))

    def test_a_hidden_rating_drew_the_same_thing_before_and_after(self):
        cfg = RequestConfig(rating_display_mode=2)
        self.assertIsNone(self._stale(cfg, 0, {"score": "N/A", "rating_hidden": True}))
        self.assertEqual(main._revisions_applying(
            RequestConfig(rating_display_mode=2, hide_rating=True)), [])

    def test_other_layouts_are_not_looked_at(self):
        for cfg in (RequestConfig(rating_display_mode=1),
                    RequestConfig(rating_display_mode=3, minimalist_append_mode=1),
                    RequestConfig(rating_display_mode=4),
                    RequestConfig(rating_display_mode=2, shape="landscape")):
            with self.subTest(cfg=(cfg.rating_display_mode, cfg.minimalist_append_mode, cfg.shape)):
                self.assertEqual(main._revisions_applying(cfg), [])

    def test_revisions_are_numbered_upwards(self):
        revs = [r.rev for r in main._RENDER_REVISIONS]
        self.assertEqual(revs, sorted(set(revs)))
        self.assertEqual(main._RENDER_REVISION, revs[-1])

    def test_facts_record_what_was_drawn(self):
        facts = main._render_facts("N/A", RequestConfig(hide_rating=True))
        self.assertEqual(facts, {"score": "N/A", "rating_hidden": True})


class RenderMetaStorageTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()
        with cache._composite_l1_lock:
            cache._composite_l1.clear()

    def tearDown(self):
        with cache._composite_l1_lock:
            cache._composite_l1.clear()
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved
        self._dir.cleanup()

    def test_revision_and_facts_survive_l1_and_l2(self):
        cache.set_cached_final_poster("tt1:1:movie:h", b"x", render_rev=1,
                                      render_facts={"score": "N/A"})
        self.assertEqual(cache.get_cached_final_poster_render_meta("tt1:1:movie:h"),
                         (1, {"score": "N/A"}))
        with cache._composite_l1_lock:
            cache._composite_l1.clear()
        self.assertEqual(cache.get_cached_final_poster_render_meta("tt1:1:movie:h"),
                         (1, {"score": "N/A"}))
        # The L2 read promotes to L1 with the meta intact.
        self.assertIsNotNone(cache.get_cached_final_poster_entry("tt1:1:movie:h"))
        self.assertEqual(cache.get_cached_final_poster_render_meta_l1("tt1:1:movie:h"),
                         (1, {"score": "N/A"}))

    def test_rows_from_before_the_columns_read_as_revision_zero(self):
        with cache._db_lock:
            cache.get_db().execute(
                "INSERT INTO final_poster_cache (cache_key, jpeg_bytes, cached_at) "
                "VALUES (?, ?, strftime('%s','now'))", ("tt2:2:movie:h", b"x"))
            cache.get_db().commit()
        self.assertEqual(cache.get_cached_final_poster_render_meta("tt2:2:movie:h"), (0, None))
        self.assertIsNone(cache.get_cached_final_poster_render_meta("missing"))


if __name__ == "__main__":
    unittest.main()
