"""TRENDING_HIDE_KDRAMAS and anime_foreign_sash."""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

import main
import tmdb
from param_aliases import expand


class HideKdramasTests(unittest.TestCase):
    def _filter(self, endpoint, ids, details, on=True, metadata=None):
        async def fetch(client, tmdb_id, key, kind, *a, **k):
            if metadata is None or tmdb_id not in metadata:
                raise RuntimeError("no metadata")
            genres, lang = metadata[tmdb_id]
            return (genres,) + (None,) * 6 + ({"original_language": lang},)
        with mock.patch.object(tmdb, "TRENDING_HIDE_KDRAMAS", on), \
                mock.patch.object(tmdb, "TRENDING_HIDE_GENRES", []), \
                mock.patch.object(tmdb, "fetch_poster_metadata", fetch):
            return asyncio.run(tmdb._without_hidden_genres(None, "k", endpoint, ids, details))

    def test_korean_drama_and_soap_leave_the_tv_list(self):
        details = {"1": {"genres": [18], "lang": "ko"}, "2": {"genres": [10766], "lang": "ko"},
                   "3": {"genres": [10764], "lang": "ko"}, "4": {"genres": [18], "lang": "en"},
                   "5": {"genres": [16, 18], "lang": "ja"}, "6": {"genres": [16, 18], "lang": "ko"}}
        self.assertEqual(self._filter("tv", list(details), details), ["3", "4", "5", "6"])

    def test_off_or_other_lists_keep_them(self):
        details = {"1": {"genres": [18], "lang": "ko"}}
        self.assertEqual(self._filter("tv", ["1"], dict(details), on=False), ["1"])
        self.assertEqual(self._filter("movie", ["1"], dict(details)), ["1"])

    def test_rows_without_language_are_looked_up(self):
        details = {"1": {"genres": [18]}, "2": {}}
        meta = {"1": ([18], "ko"), "2": ([18], "en")}
        self.assertEqual(self._filter("tv", ["1", "2"], details, metadata=meta), ["2"])
        self.assertEqual(details["1"]["lang"], "ko")

    def test_failed_lookup_keeps_the_row(self):
        self.assertEqual(self._filter("tv", ["9"], {}), ["9"])

    def test_signature_changes(self):
        with mock.patch.object(tmdb, "TRENDING_HIDE_KDRAMAS", False):
            off = tmdb.trending_source_signature("tv")
        with mock.patch.object(tmdb, "TRENDING_HIDE_KDRAMAS", True):
            self.assertNotEqual(tmdb.trending_source_signature("tv"), off)
            movie = tmdb.trending_source_signature("movie")
        with mock.patch.object(tmdb, "TRENDING_HIDE_KDRAMAS", False):
            self.assertEqual(tmdb.trending_source_signature("movie"), movie)


class AnimeForeignSashTests(unittest.TestCase):
    def test_param(self):
        self.assertTrue(main.RequestConfig().anime_foreign_sash)
        self.assertFalse(main.build_request_config({"anime_foreign_sash": "false"}).anime_foreign_sash)
        self.assertEqual(expand([("afs", "false")]), [("anime_foreign_sash", "false")])

    def test_default_leaves_the_signature_alone(self):
        self.assertEqual(main._render_config_signature(main.build_request_config({})),
                         main._render_config_signature(main.build_request_config(
                             {"anime_foreign_sash": "true"})))
        self.assertNotEqual(main._render_config_signature(main.build_request_config({})),
                            main._render_config_signature(main.build_request_config(
                                {"anime_foreign_sash": "false"})))

    def test_get_poster_drops_foreign_on_anime(self):
        src = Path("main.py").read_text(encoding="utf-8")
        cut = src.index("if _is_anime_title and not rcfg.anime_foreign_sash:")
        # Before the order the debug output and the cache expiry read is taken.
        self.assertLess(cut, src.index("_sash_priority = rcfg.sash_priority\n", cut - 2000))


if __name__ == "__main__":
    unittest.main()
