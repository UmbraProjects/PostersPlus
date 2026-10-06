"""AniList and Kitsu scores for any anime title, not just ones requested by
that site's id: MDBList carries only MyAnimeList, so the title's TMDB / IMDb
id is mapped back to each site's entry through the anime id list.  A later
season takes its own entry's scores (MyAnimeList's through Jikan), and the
first season's where its own has none."""
import asyncio
import unittest
from unittest import mock

import anime
import main
from tests.test_anime_id_map import _TempTable
import anime_ids

SEASONS = [
    # Sword Art Online: three seasons under one TMDB show and IMDb id.
    {"type": "TV", "kitsu_id": 6589, "anilist_id": 11757, "mal_id": 11757,
     "themoviedb_id": {"tv": 45782}, "imdb_id": ["tt2250192"]},
    {"type": "TV", "kitsu_id": 8174, "anilist_id": 20594, "mal_id": 21881,
     "themoviedb_id": {"tv": 45782}, "imdb_id": ["tt2250192"]},
    {"type": "TV", "kitsu_id": 13893, "anilist_id": 100182,
     "themoviedb_id": {"tv": 45782}, "imdb_id": ["tt2250192"]},
    # Black Clover: a Jump Festa special filed under the show as TMDB's
    # season 0 has a lower id on both sites than the series itself.
    {"type": "SPECIAL", "kitsu_id": 12625, "anilist_id": 87528, "mal_id": 34420,
     "themoviedb_id": {"tv": 73223}, "imdb_id": ["tt7441658"], "season": {"tmdb": 0}},
    {"type": "TV", "kitsu_id": 13209, "anilist_id": 97940, "mal_id": 34572,
     "themoviedb_id": {"tv": 73223}, "imdb_id": ["tt7441658"], "season": {"tmdb": 1}},
    # A film only the IMDb id reaches.
    {"type": "MOVIE", "kitsu_id": 11614, "anilist_id": 21519, "imdb_id": ["tt5311514"]},
]


class ReverseLookupTests(_TempTable):
    def setUp(self):
        super().setUp()
        self._load(SEASONS)

    def test_a_show_maps_to_its_first_season_on_each_site(self):
        self.assertEqual(anime_ids.reverse_lookup("tv", "45782", None), {"kitsu": 6589, "anilist": 11757})
        self.assertEqual(anime_ids.reverse_lookup("series", None, "tt2250192"), {"kitsu": 6589, "anilist": 11757})

    def test_a_special_filed_under_the_show_is_not_its_first_season(self):
        self.assertEqual(anime_ids.reverse_lookup("tv", "73223", None), {"kitsu": 13209, "anilist": 97940})
        self.assertTrue(anime_ids.is_series_start("anilist", 97940))
        self.assertFalse(anime_ids.is_series_start("anilist", 87528))

    def test_the_kind_must_match_and_imdb_is_the_fallback(self):
        self.assertEqual(anime_ids.reverse_lookup("movie", "45782", None), {})
        self.assertEqual(anime_ids.reverse_lookup("movie", "999", "tt5311514"), {"kitsu": 11614, "anilist": 21519})

    def test_not_anime(self):
        self.assertEqual(anime_ids.reverse_lookup("movie", "550", "tt0137523"), {})
        self.assertEqual(anime_ids.reverse_lookup("tv", "kitsu:6589", None), {})


class WantedTests(unittest.TestCase):
    def cfg(self, **params):
        return main.build_request_config(params)

    def test_only_when_shown_or_weighted(self):
        self.assertEqual(main._anime_sources_wanted(self.cfg(), ({"imdb": 1},)), set())
        self.assertEqual(main._anime_sources_wanted(
            self.cfg(rating_display_mode="2", rating_badges="myanimelist,anilist"), ()),
            {"myanimelist", "anilist"})
        self.assertEqual(main._anime_sources_wanted(self.cfg(), ({"kitsu": 0.5, "anilist": 0},)), {"kitsu"})

    def test_badges_that_cannot_show_want_nothing(self):
        for params in ({"rating_display_mode": "1"}, {"rating_display_mode": "2", "hide_rating": "true"}):
            with self.subTest(**params):
                self.assertEqual(main._anime_sources_wanted(
                    self.cfg(rating_badges="anilist,kitsu", **params), ()), set())

    def test_landscape_wants_what_its_badges_show(self):
        # A landscape URL sends no rating_display_mode; its own toggle decides.
        on = {"shape": "landscape", "landscape_rating_badges": "true",
              "rating_badges": "myanimelist,anilist,kitsu"}
        self.assertEqual(main._anime_sources_wanted(self.cfg(**on), ()), {"myanimelist", "anilist", "kitsu"})
        self.assertEqual(main._anime_sources_wanted(
            self.cfg(**{**on, "landscape_rating_badges": "false"}), ()), set())
        self.assertEqual(main._anime_sources_wanted(self.cfg(**{**on, "hide_rating": "true"}), ()), set())
        # Portrait's mode no longer leaks into landscape, either way.
        self.assertEqual(main._anime_sources_wanted(
            self.cfg(**{**on, "landscape_rating_badges": "false", "rating_display_mode": "2"}), ()), set())

    def test_landscape_composites_from_before_the_fix_re_render(self):
        on = {"shape": "landscape", "landscape_rating_badges": "true", "rating_badges": "imdb,anilist"}
        applies = [r for r in main._RENDER_REVISIONS if r.rev == 19][0].applies
        self.assertTrue(applies(self.cfg(**on)))
        self.assertFalse(applies(self.cfg(**{**on, "rating_badges": "imdb,myanimelist"})))
        self.assertFalse(applies(self.cfg(**{**on, "landscape_rating_badges": "false"})))
        self.assertFalse(applies(self.cfg(rating_display_mode="2", rating_badges="anilist")))


def _meta(score):
    return ([], False, [], None, "t", None, None, {"anime_score": score})


class FillTests(_TempTable):
    def setUp(self):
        super().setUp()
        self._load(SEASONS)

    def fill(self, ratings, wanted, fetched, cached=None, mal=None, tmdb_id="45782",
             imdb_id="tt2250192", **kw):
        calls = []

        async def fetch(client, ns, aid):
            calls.append((ns, aid))
            got = fetched.get((ns, aid), fetched.get(ns))
            return got

        async def jikan(client, mal_id):
            calls.append(("mal", mal_id))
            return (mal or {}).get(mal_id, (None, True))
        with mock.patch.object(anime, "fetch_anime_metadata", fetch), \
             mock.patch.object(anime, "fetch_mal_score", jikan), \
             mock.patch.object(anime, "jikan_enabled", lambda: True), \
             mock.patch.object(anime, "get_cached_tvdb_json", lambda key: (cached or {}).get(key)):
            out = asyncio.run(main._fill_anime_scores(
                None, ratings, wanted, media_type="tv", tmdb_id=tmdb_id, imdb_id=imdb_id, **kw))
        return out, calls

    def test_fills_the_missing_ones_from_the_first_season(self):
        (ratings, pending), calls = self.fill({"myanimelist": 7.2, "kitsu": 73.6}, {"anilist", "kitsu"},
                                              {"anilist": _meta(68)})
        self.assertEqual(ratings, {"myanimelist": 7.2, "kitsu": 73.6, "anilist": 68})
        self.assertFalse(pending)
        self.assertEqual(calls, [("anilist", 11757)])   # Kitsu was already there

    def test_an_anime_id_request_takes_its_own_entrys_sibling(self):
        # SAO II by Kitsu id: AniList's score is SAO II's, not the show's first.
        (ratings, _), calls = self.fill({"kitsu": 70}, {"anilist"}, {"anilist": _meta(75)},
                                        own=("kitsu", 8174))
        self.assertEqual(ratings, {"kitsu": 70, "anilist": 75})
        self.assertEqual(calls, [("anilist", 20594)])
        (_, _), calls = self.fill({}, {"kitsu"}, {"kitsu": _meta(1)}, own=("anilist", 20594))
        self.assertEqual(calls, [("kitsu", 8174)])

    def test_a_first_season_without_a_paired_sibling_uses_the_show(self):
        # Kitsu 6589 is SAO; pretend the list has no AniList pair for it.
        with mock.patch.object(anime_ids, "anilist_for_kitsu", lambda k: []):
            (ratings, _), calls = self.fill({"kitsu": 80}, {"anilist"}, {"anilist": _meta(68)},
                                            own=("kitsu", 6589))
        self.assertEqual((ratings, calls), ({"kitsu": 80, "anilist": 68}, [("anilist", 11757)]))

    def test_a_later_season_falls_back_to_the_first_seasons_score(self):
        # An unmapped sequel (no sibling known): season 1's rather than nothing.
        (ratings, pending), calls = self.fill({}, {"anilist"}, {"anilist": _meta(68)},
                                              own=("kitsu", 99999), later_season=True)
        self.assertEqual((ratings, pending, calls), ({"anilist": 68}, False, [("anilist", 11757)]))

    def test_a_later_season_prefers_its_own_scores(self):
        # SAO II by Kitsu id, Kitsu itself unscored so far: AniList is SAO II's,
        # Kitsu falls back to season 1's.
        fetched = {("kitsu", 8174): _meta(None), ("anilist", 20594): _meta(75),
                   ("kitsu", 6589): _meta(80)}
        (ratings, pending), _ = self.fill({}, {"anilist", "kitsu"}, fetched,
                                          own=("kitsu", 8174), later_season=True)
        self.assertEqual((ratings, pending), ({"anilist": 75, "kitsu": 80}, False))

    def test_a_later_seasons_myanimelist_score_is_its_own_through_jikan(self):
        # MDBList's 8.1 is season 1's; Jikan has SAO II's.
        (ratings, _), calls = self.fill({"myanimelist": 8.1}, {"myanimelist"}, {},
                                        own=("kitsu", 8174), later_season=True, mal={21881: (7.9, True)})
        self.assertEqual(ratings, {"myanimelist": 7.9})
        self.assertEqual(calls, [("mal", 21881)])
        # Not scored on MAL yet: season 1's stays.
        (ratings, pending), _ = self.fill({"myanimelist": 8.1}, {"myanimelist"}, {},
                                          own=("kitsu", 8174), later_season=True)
        self.assertEqual((ratings, pending), ({"myanimelist": 8.1}, False))
        # Jikan unreachable: season 1's stays, and the render isn't kept.
        (ratings, pending), _ = self.fill({"myanimelist": 8.1}, {"myanimelist"}, {},
                                          own=("kitsu", 8174), later_season=True,
                                          mal={21881: (None, False)})
        self.assertEqual((ratings, pending), ({"myanimelist": 8.1}, True))

    def test_the_first_season_keeps_mdblists_myanimelist_score(self):
        (ratings, _), calls = self.fill({"myanimelist": 8.1}, {"myanimelist"}, {}, own=("kitsu", 6589))
        self.assertEqual((ratings, calls), ({"myanimelist": 8.1}, []))
        # ...and asks Jikan when MDBList had none.
        (ratings, _), calls = self.fill({}, {"myanimelist"}, {}, own=("kitsu", 6589),
                                        mal={11757: (8.0, True)})
        self.assertEqual(ratings, {"myanimelist": 8.0})

    def test_a_show_by_tmdb_id_skips_a_special_filed_before_it(self):
        (ratings, _), calls = self.fill({}, {"anilist"}, {("anilist", 97940): _meta(79),
                                                         ("anilist", 87528): _meta(63)},
                                        tmdb_id="73223", imdb_id="tt7441658")
        self.assertEqual((ratings, calls), ({"anilist": 79}, [("anilist", 97940)]))

    def test_nothing_wanted_fetches_nothing(self):
        (ratings, _), calls = self.fill({"imdb": 7.5}, set(), {})
        self.assertEqual((ratings, calls), ({"imdb": 7.5}, []))

    def test_a_blip_makes_the_render_provisional_but_a_known_miss_does_not(self):
        (ratings, pending), _ = self.fill({}, {"anilist"}, {})
        self.assertEqual(ratings, {})
        self.assertTrue(pending)
        miss = {anime._cache_key("anilist", 11757): {"__miss__": True}}
        (_, pending), _ = self.fill({}, {"anilist"}, {}, cached=miss)
        self.assertFalse(pending)

    def test_an_entry_without_a_score_adds_nothing(self):
        (ratings, pending), _ = self.fill({}, {"kitsu"}, {"kitsu": _meta(None)})
        self.assertEqual((ratings, pending), ({}, False))


class WiringTests(unittest.TestCase):
    def test_both_merge_sites_fill_before_weighting(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("= await _with_anime_scores(ratings_dict)"), 2)

    def test_a_failed_mdblist_fetch_keeps_the_providers_own_score(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        failed = src.index("        if rating_failed:\n")
        own = src.index('ratings_dict = {anime_namespace: tmdb_data["anime_score"]}', failed)
        self.assertLess(own, src.index("await _with_anime_scores(ratings_dict)", failed))

    def test_weights_follow_the_fill(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        for i in [m for m in range(len(src)) if src.startswith("await _with_anime_scores(ratings_dict)", m)]:
            nxt = src.index("_weights_for(ratings_dict)", i)
            self.assertLess(nxt - i, 200)


if __name__ == "__main__":
    unittest.main()
