"""TMDB files TV under one "Sci-Fi & Fantasy" genre (10765).  The show's
keywords — and, on a tie, IMDb's genres via Cinemeta — split it into the film
ids 878 (Sci-Fi) or 14 (Fantasy); a show nothing decides keeps 10765."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

import cache
import tmdb
from tmdb import _scifi_or_fantasy, _split_tv_scifi_fantasy


class ClassifierTests(unittest.TestCase):
    def test_keywords_decide(self):
        self.assertEqual(_scifi_or_fantasy(["dragon", "fantasy world"]), 14)
        self.assertEqual(_scifi_or_fantasy(["android", "robot", "artificial intelligence (a.i.)"]), 878)
        self.assertEqual(_scifi_or_fantasy(["space opera", "space western"]), 878)
        self.assertEqual(_scifi_or_fantasy(["witchcraft", "dark fantasy"]), 14)

    def test_terms_match_whole_words(self):
        # "los angeles" is not an angel, "alienation" not an alien.
        self.assertIsNone(_scifi_or_fantasy(["los angeles, california"]))
        self.assertIsNone(_scifi_or_fantasy(["alienation"]))
        self.assertEqual(_scifi_or_fantasy(["dystopian future"]), 878)
        self.assertEqual(_scifi_or_fantasy(["vampires"]), 14)

    def test_imdb_genres_only_break_a_tie(self):
        self.assertEqual(_scifi_or_fantasy([], ["Drama", "Mystery", "Sci-Fi"]), 878)
        self.assertEqual(_scifi_or_fantasy(["time travel", "magic"], ["Fantasy"]), 14)
        self.assertEqual(_scifi_or_fantasy(["robot"], ["Fantasy"]), 878)
        self.assertIsNone(_scifi_or_fantasy([], ["Drama", "Fantasy", "Sci-Fi"]))
        self.assertIsNone(_scifi_or_fantasy([], ["Action", "Adventure", "Drama"]))


class SplitTests(unittest.TestCase):
    def _split(self, ids, keywords, imdb_id=None, cinemeta=None):
        async def fake_meta(client, imdb, media_type):
            return cinemeta
        with mock.patch.object(tmdb.cinemeta, "fetch_cinemeta_meta", side_effect=fake_meta) as m, \
             mock.patch.object(tmdb, "CINEMETA_ENABLED", True):
            out = asyncio.run(_split_tv_scifi_fantasy(None, ids, keywords, imdb_id))
        return out, m.call_count

    def test_replaces_in_place_and_dedupes(self):
        out, calls = self._split([10765, 18, 10759], ["fantasy world"])
        self.assertEqual(out, [14, 18, 10759])
        self.assertEqual(calls, 0)
        out, _ = self._split([878, 10765], ["alien"])
        self.assertEqual(out, [878])

    def test_untouched_without_10765(self):
        out, calls = self._split([18, 80], ["dragon"], "tt1")
        self.assertEqual((out, calls), ([18, 80], 0))

    def test_cinemeta_only_on_a_tie(self):
        out, calls = self._split([10765, 18], [], "tt11280740",
                                 {"genres": ["Drama", "Mystery", "Sci-Fi"]})
        self.assertEqual((out, calls), ([878, 18], 1))

    def test_unresolved_keeps_10765(self):
        out, _ = self._split([10765, 18], [], "tt1", {"genres": ["Drama"]})
        self.assertEqual(out, [10765, 18])
        out, _ = self._split([10765], [], "tt1", None)
        self.assertEqual(out, [10765])


class CachedRowRefreshTests(unittest.TestCase):
    """v4 rows are kept, except a TV row still carrying the merged genre."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()

    def tearDown(self):
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved
        self._dir.cleanup()

    def _store(self, key, genre_ids, version):
        cache.set_cached_tmdb_metadata(
            key, "T", "2020", genre_ids, True, "/p.jpg", [],
            original_title="T", vote_count=10, metadata_version=version,
        )

    def test_tv_rows_before_v6_refresh(self):
        self._store("tv_1_en_x", [10765, 18], 4)
        self._store("tv_2_en_x", [18], 4)
        self._store("movie_3_en_x", [10765], 4)
        self._store("tv_4_en_x", [10765], 5)
        self._store("movie_5_en_x", [18], 5)
        self._store("tv_6_en_x", [18, 27], 6)
        with mock.patch.object(cache, "invalidate_final_posters") as inv:
            # Only the v4 row with the merged genre takes its composites along.
            self.assertIsNone(cache.get_cached_tmdb_metadata("tv_1_en_x"))
            inv.assert_called_once_with("1", "tv")
            inv.reset_mock()
            for key in ("tv_2_en_x", "tv_4_en_x"):
                self.assertIsNone(cache.get_cached_tmdb_metadata(key), key)
            inv.assert_not_called()
        for key in ("movie_3_en_x", "movie_5_en_x", "tv_6_en_x"):
            self.assertIsNotNone(cache.get_cached_tmdb_metadata(key), key)


class GenreOrderSettingTests(unittest.TestCase):
    """The dashboard's drag list stores a full ranking of known ids."""

    def setUp(self):
        import settings
        self.settings = settings
        self.s = settings.REGISTRY["GENRE_PRIORITY"]

    def test_declared_as_an_order_of_every_genre(self):
        import config
        self.assertEqual(self.s.kind, "order")
        self.assertEqual({int(c) for c in self.s.choices}, set(config.GENRE_MAP))
        self.assertEqual(self.s.labels["10765"], "Sci-Fi & Fantasy (TV, not split)")

    def test_missing_ids_keep_their_default_place_at_the_end(self):
        out = self.settings.normalise(self.s, "14, 878")
        parts = out.split(",")
        self.assertEqual(parts[:2], ["14", "878"])
        self.assertEqual(sorted(parts), sorted(self.s.choices))

    def test_unknown_or_repeated_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.settings.normalise(self.s, "14,99999")
        with self.assertRaises(ValueError):
            self.settings.normalise(self.s, "14,14")

    def test_an_order_saved_before_an_entry_takes_it_beside_its_neighbour(self):
        import config
        rid = str(config.ROMCOM_GENRE_ID)
        saved = [c for c in self.s.choices if c != rid]
        # The operator had moved Comedy to the top; Rom-Com lands in front of it.
        saved.remove("35")
        saved.insert(0, "35")
        out = self.settings.normalise(self.s, ",".join(saved)).split(",")
        self.assertEqual(out[:2], [rid, "35"])
        self.assertEqual(sorted(out), sorted(self.s.choices))

    def test_environment_value_is_parsed_leniently(self):
        import config
        with mock.patch.dict(os.environ, {"X_ORDER": "14,junk,14,878"}):
            order = config._genre_order("X_ORDER", (878, 14, 18), "x", "")
        self.assertEqual(order, [14, 878, 18])


if __name__ == "__main__":
    unittest.main()


class RomComTests(unittest.TestCase):
    def test_derived_only_from_both(self):
        import config
        rid = config.ROMCOM_GENRE_ID
        self.assertIn(rid, config.with_derived_genres([35, 10749, 18]))
        self.assertNotIn(rid, config.with_derived_genres([35, 18]))
        self.assertNotIn(rid, config.with_derived_genres([10749]))
        self.assertEqual(config.with_derived_genres([35, 10749, rid]).count(rid), 1)

    def test_label_follows_the_order(self):
        import config
        rid = config.ROMCOM_GENRE_ID
        d = config.with_derived_genres
        self.assertEqual(config.genre_label(d([10749, 35]), [rid, 35, 10749]), "Rom-Com")
        self.assertEqual(config.genre_label(d([35]), [rid, 35, 10749]), "Comedy")
        # Ranked below the two, it never wins.
        self.assertEqual(config.genre_label(d([10749, 35]), [35, 10749, rid]), "Comedy")
        # A stronger genre still comes first.
        self.assertEqual(config.genre_label(d([14, 35, 10749]), [14, rid, 35, 10749]), "Fantasy")
        self.assertEqual(config.genre_label([], [rid]), "Unknown")

    def test_label_does_not_derive_on_its_own(self):
        # IMDb/TVDB genres reach genre_label underived: Friends is Comedy there.
        import config
        rid = config.ROMCOM_GENRE_ID
        self.assertEqual(config.genre_label([35, 10749], [rid, 35, 10749]), "Comedy")

    def test_derived_only_on_tmdb_and_anime_genres(self):
        import inspect
        import main
        src = inspect.getsource(main.get_poster)
        cm = src.index(") = _cm_meta")
        self.assertIn("_derive_romcom = False", src[cm:cm + 80])
        self.assertIn("if _derive_romcom:\n            genre_ids = _cfg.with_derived_genres(genre_ids)", src)

    def test_default_puts_it_in_front_of_comedy(self):
        import config
        order = list(config._DEFAULT_GENRE_PRIORITY)
        self.assertEqual(order.index(config.ROMCOM_GENRE_ID) + 1, order.index(35))
        anime = list(config._DEFAULT_ANIME_GENRE_PRIORITY)
        self.assertLess(anime.index(config.ROMCOM_GENRE_ID), anime.index(10749))


class TvHorrorTests(unittest.TestCase):
    def _run(self, keywords, imdb_id="tt1", tvdb_id=None, tvdb_on=False,
             tvdb_record=None, cinemeta=None, cinemeta_down=False):
        async def fake_meta(client, imdb, media_type):
            return cinemeta

        async def fake_record(client, tid, want):
            return tvdb_record
        # fetch_cinemeta_meta negatively caches a real miss; an outage leaves
        # the key empty.
        miss = None if cinemeta_down else {"__miss__": True}
        with mock.patch.object(tmdb.cinemeta, "fetch_cinemeta_meta", side_effect=fake_meta) as cm, \
             mock.patch.object(tmdb.tvdb, "_fetch_record", side_effect=fake_record) as tv, \
             mock.patch.object(tmdb.tvdb, "tvdb_enabled", return_value=tvdb_on), \
             mock.patch.object(tmdb, "get_cached_tvdb_json", return_value=miss), \
             mock.patch.object(tmdb, "CINEMETA_ENABLED", True):
            out = asyncio.run(tmdb._tv_is_horror(None, keywords, imdb_id, tvdb_id))
        return out, tv.call_count, cm.call_count

    def test_keywords_decide_when_present(self):
        self.assertEqual(self._run(["haunted house", "supernatural horror"]), (True, 0, 0))
        self.assertEqual(self._run(["horror anthology"]), (True, 0, 0))
        # Whole words: "horrorcore" is music.
        self.assertEqual(self._run(["haunted house", "horrorcore"]), (False, 0, 0))

    def test_no_keywords_asks_cinemeta_first(self):
        cm = {"genres": ["Drama", "Horror", "Sci-Fi"]}
        self.assertEqual(self._run([], tvdb_id=250487, tvdb_on=True, cinemeta=cm), (True, 0, 1))
        self.assertEqual(self._run([], cinemeta={"genres": ["Drama"]}), (False, 0, 1))

    def test_tvdb_only_when_cinemeta_cannot_answer(self):
        rec = {"genres": [{"name": "Horror"}, {"name": "Drama"}]}
        # Cinemeta has no entry.
        self.assertEqual(self._run([], tvdb_id=250487, tvdb_on=True, tvdb_record=rec), (True, 1, 1))
        # No IMDb id to ask Cinemeta with.
        self.assertEqual(self._run([], imdb_id=None, tvdb_id=1, tvdb_on=True,
                                   tvdb_record={"genres": [{"name": "Drama"}]}), (False, 1, 0))
        # Without a TVDB key there is nothing further to ask.
        self.assertEqual(self._run([], tvdb_id=250487, tvdb_record=rec), (False, 0, 1))
        self.assertEqual(self._run([], imdb_id=None), (False, 0, 0))

    def test_cinemeta_outage_is_unsettled_not_no(self):
        self.assertEqual(self._run([], cinemeta_down=True), (None, 0, 1))
        # TVDB answering settles it after all.
        rec = {"genres": [{"name": "Horror"}]}
        self.assertEqual(self._run([], tvdb_id=1, tvdb_on=True, tvdb_record=rec,
                                   cinemeta_down=True), (True, 1, 1))


class UnsettledMetadataTests(unittest.TestCase):
    """A show whose Horror couldn't be told is served, not cached."""

    def _fetch(self, horror):
        data = {
            "id": 7, "name": "Show", "genres": [{"id": 18, "name": "Drama"}],
            "keywords": {"results": []}, "external_ids": {"imdb_id": "tt7"},
            "images": {"posters": [], "logos": [], "backdrops": []}, "vote_count": 5,
        }

        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return data

        class Client:
            async def get(self, *a, **k): return Resp()

        async def fake_horror(*a, **k):
            return horror
        with mock.patch.object(tmdb, "get_cached_tmdb_metadata", return_value=None), \
             mock.patch.object(tmdb, "set_cached_tmdb_metadata") as store, \
             mock.patch.object(tmdb, "_tv_is_horror", side_effect=fake_horror):
            out = asyncio.run(tmdb.fetch_poster_metadata(Client(), "7", "key", "tv"))
        return out, store.call_count

    def test_unsettled_is_not_cached(self):
        (gids, *_rest, tmdb_data), stored = self._fetch(None)
        self.assertEqual((gids, stored, tmdb_data["genres_unsettled"]), ([18], 0, True))

    def test_settled_is_cached(self):
        (gids, *_rest, tmdb_data), stored = self._fetch(True)
        self.assertEqual((gids, stored, tmdb_data["genres_unsettled"]), ([18, 27], 1, False))


class MdblistTvHorrorTests(unittest.TestCase):
    def setUp(self):
        import ratings
        self.ratings = ratings
        self.store = {}
        p1 = mock.patch.object(cache, "set_cached_tvdb_json",
                               side_effect=lambda k, v, ttl: self.store.__setitem__(k, v))
        p2 = mock.patch.object(cache, "get_cached_tvdb_json", side_effect=self.store.get)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_remembers_shows_only(self):
        self.ratings.remember_mdblist_tv_horror(
            {"type": "show", "ids": {"tmdb": 1413}, "genres": [{"title": "Drama"}, {"title": "Horror"}]})
        self.ratings.remember_mdblist_tv_horror(
            {"type": "show", "ids": {"tmdb": 1668}, "genres": [{"title": "Comedy"}]})
        self.ratings.remember_mdblist_tv_horror(
            {"type": "movie", "ids": {"tmdb": 9}, "genres": [{"title": "Horror"}]})
        self.ratings.remember_mdblist_tv_horror({"type": "show", "ids": {"tmdb": 5}, "genres": []})
        self.assertIs(self.ratings.mdblist_tv_horror(1413), True)
        self.assertIs(self.ratings.mdblist_tv_horror("1668"), False)
        self.assertIsNone(self.ratings.mdblist_tv_horror(9))
        self.assertIsNone(self.ratings.mdblist_tv_horror(5))

    def test_overrides_the_cached_guess_both_ways(self):
        import main
        self.store["mdblist_tv_horror:1"] = {"horror": True}
        self.store["mdblist_tv_horror:2"] = {"horror": False}
        self.assertEqual(main._with_mdblist_tv_horror([18, 9648], "1"), [18, 9648, 27])
        self.assertEqual(main._with_mdblist_tv_horror([18, 27], "2"), [18])
        self.assertEqual(main._with_mdblist_tv_horror([18, 27], "3"), [18, 27])
