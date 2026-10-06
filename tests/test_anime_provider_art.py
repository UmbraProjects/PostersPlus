"""anime_provider_art: which art a title requested by an anime id draws."""
import unittest
from unittest import mock

import anime
import anime_ids
import main
from tests.test_anime_id_map import _TempTable


def _cfg(**params):
    return main.build_request_config(params)


class ParsingTests(unittest.TestCase):
    def test_default_and_anime_providers(self):
        self.assertEqual(_cfg().anime_provider_art, "provider")
        for value in ("kitsu", "anilist", "tmdb", "TMDB "):
            with self.subTest(value=value):
                self.assertEqual(_cfg(anime_provider_art=value).anime_provider_art,
                                 value.strip().lower())
        self.assertEqual(_cfg(anime_provider_art="cinemeta").anime_provider_art, "provider")

    def test_fanart_and_tvdb_as_the_instance_offers_them(self):
        with mock.patch.multiple(main._cfg, FANART_POSTERS=False, FANART_API_KEY="k"), \
             mock.patch.object(main.tvdb, "poster_source_enabled", lambda: False):
            self.assertEqual(_cfg(anime_provider_art="fanart").anime_provider_art, "provider")
            self.assertEqual(_cfg(anime_provider_art="tvdb").anime_provider_art, "provider")
        with mock.patch.multiple(main._cfg, FANART_POSTERS=True, FANART_API_KEY="k"), \
             mock.patch.object(main.tvdb, "poster_source_enabled", lambda: True):
            self.assertEqual(_cfg(anime_provider_art="fanart").anime_provider_art, "fanart")
            self.assertEqual(_cfg(anime_provider_art="tvdb").anime_provider_art, "tvdb")

    def test_landscape_keeps_the_provider(self):
        self.assertEqual(_cfg(anime_provider_art="tmdb", shape="landscape").anime_provider_art,
                         "provider")

    def test_default_keeps_existing_composite_keys(self):
        self.assertNotIn("anime_provider_art", main._render_config_signature(_cfg()))
        self.assertIn("anime_provider_art",
                      main._render_config_signature(_cfg(anime_provider_art="tmdb")))


class WiringTests(unittest.TestCase):
    def test_non_anime_requests_drop_it_before_the_cache_key(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        reset = src.index('rcfg.anime_provider_art = "provider"')
        self.assertLess(src.rindex("if not is_anime or (", 0, reset), reset)
        self.assertLess(reset, src.index("_render_config_signature(rcfg)"))


def _entry(kitsu, season=None, offset=None, tv=45782):
    e = {"type": "TV", "kitsu_id": kitsu, "themoviedb_id": {"tv": tv}}
    if season is not None:
        e["season"] = {"tmdb": season}
    if offset is not None:
        e["episode_offset"] = {"tmdb": offset}
    return e


class SeriesStartTests(_TempTable):
    """Only a show's first season takes the series' TMDB art: later seasons,
    cours and specials keep their own cover (Sword Art Online)."""

    def setUp(self):
        super().setUp()
        self._load([
            _entry(6589, season=1), _entry(8174, season=2), _entry(7914, season=0, offset=9),
            _entry(42213, season=4), _entry(42927, season=4, offset=12),
            # No season in the list: the lowest id on the site is the start.
            _entry(100, tv=999), _entry(200, tv=999),
            {"type": "MOVIE", "kitsu_id": 11614, "themoviedb_id": {"movie": [5]}},
        ])

    def test_seasons(self):
        for kitsu_id, start in ((6589, True), (8174, False), (7914, False),
                                (42213, False), (42927, False),
                                (100, True), (200, False), (11614, True), (1, True)):
            with self.subTest(kitsu_id=kitsu_id):
                self.assertEqual(anime_ids.is_series_start("kitsu", kitsu_id), start)

    def test_wired_before_the_swap(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        # Before the composite key, so a later season shares As Requested's.
        check = src.index("anime_ids.is_series_start(anime_namespace, anime_id)")
        self.assertLess(check, src.index("_render_config_signature(rcfg)"))


class KnownMissTests(unittest.TestCase):
    def test_only_the_negative_cache_is_a_known_miss(self):
        rows = {anime._cache_key("kitsu", 1): {"__miss__": True},
                anime._cache_key("kitsu", 2): {"title": "x"}}
        with mock.patch.object(anime, "get_cached_tvdb_json", rows.get):
            self.assertTrue(anime.known_miss("kitsu", 1))
            self.assertFalse(anime.known_miss("kitsu", 2))
            self.assertFalse(anime.known_miss("kitsu", 3))


if __name__ == "__main__":
    unittest.main()
