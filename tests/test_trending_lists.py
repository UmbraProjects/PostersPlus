"""Visitors pick their own trending lists (trending_list=, Choose Lists).

Each pick is its own stored list, shared by everyone who picked alike, and
the operator's lists stay where they always were.  Only the menu can be
picked from, so the lists built stay bounded by it.
"""

import asyncio
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import cache
import config
import main
import tmdb
import trending_lists
from tests.test_trending_catalogs_addon import TMDB_SIG, _AddonTest, _cfg_segment
from tests.test_trending_snapshot_turnover import _TempDb


class _Menu:
    """Patches the operator's trending config for one test."""

    def __init__(self, test, **values):
        defaults = {"TRENDING_SOURCE_MOVIE": "", "TRENDING_SOURCE_TV": "",
                    "TRENDING_SOURCE_ANIME": "", "TRENDING_SOURCE_ANIME_MOVIE": "",
                    "TRENDING_SOURCE_CHOICES": "", "TRENDING_HIDE_UNRELEASED": False,
                    "TRENDING_LIST_CHOICE": True, "TRENDING_CATALOGS_ENABLED": True,
                    # The public lists off unless a test turns them on.
                    "TRENDING_LIST_SOURCES": ""}
        defaults.update(values)
        for name, value in defaults.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            test.addCleanup(patcher.stop)


class ParseTests(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self)

    def test_the_operators_lists_are_no_token_and_the_bare_keys(self):
        lists = trending_lists.parse("")
        self.assertTrue(lists.is_default)
        self.assertEqual(lists.token(), "")
        self.assertEqual([lists.key(e) for e in trending_lists.ENDPOINTS],
                         ["movie", "tv", "anime", "anime_movie"])
        # Picking what the operator already uses is the same lists.
        self.assertEqual(trending_lists.parse("m:tmdb,t:tmdb,h:0").token(), "")

    def test_a_pick_keys_only_the_lists_it_changes(self):
        lists = trending_lists.parse("m:tmdb_week")
        self.assertEqual(lists.token(), "m:tmdb_week")
        self.assertEqual(lists.key("movie"), "movie@tmdb_week.h0.anilist.anilist")
        self.assertEqual(lists.key("series"), "tv")
        self.assertEqual(lists.key("anime"), "anime")
        self.assertTrue(lists.week("movie"))

    def test_home_changes_every_list(self):
        lists = trending_lists.parse("h:1")
        self.assertEqual(lists.token(), "h:1")
        self.assertEqual(lists.key("tv"), "tv@tmdb.h1.anilist.anilist")
        self.assertEqual(lists.key("anime_movie"), "anime_movie@anilist.h1")

    def test_what_the_menu_does_not_offer_is_the_operators(self):
        lists = trending_lists.parse("m:https://evil.example/list,t:nope,h:7,x:tmdb_week")
        self.assertTrue(lists.is_default)

    def test_spellings_of_one_pick_canonicalise_alike(self):
        self.assertEqual(trending_lists.parse(" H:1 , M:TMDB_WEEK ").token(),
                         trending_lists.parse("m:tmdb_week,h:1").token())

    def test_off_everyone_gets_the_operators_lists(self):
        with mock.patch.object(config, "TRENDING_LIST_CHOICE", False):
            self.assertTrue(trending_lists.parse("m:tmdb_week,h:1").is_default)

    def test_keys_read_back(self):
        for raw in ("m:tmdb_week", "h:1", "t:tmdb_week,h:1", ""):
            lists = trending_lists.parse(raw)
            for e in trending_lists.ENDPOINTS:
                endpoint, back = trending_lists.from_key(lists.key(e))
                self.assertEqual(endpoint, e)
                self.assertEqual(back.key(e), lists.key(e))

    def test_without_the_anime_split_the_movie_key_leaves_anime_out(self):
        with mock.patch.object(config, "TRENDING_CATALOGS_ENABLED", False):
            self.assertEqual(trending_lists.parse("m:tmdb_week").key("movie"), "movie@tmdb_week.h0")

    def test_the_operators_source_leads_its_menu_and_tmdb_stays_pickable(self):
        with mock.patch.object(config, "TRENDING_SOURCE_MOVIE", "https://mdblist.com/lists/a/b"):
            ids = [i for i, _n in trending_lists.menu("movie")]
            self.assertEqual(ids, ["server", "tmdb", "tmdb_week"])
            self.assertEqual(trending_lists.default().movie, "server")
            lists = trending_lists.parse("m:tmdb")
            self.assertEqual(lists.url("movie"), "")
            self.assertEqual(lists.key("movie"), "movie@tmdb.h0.anilist.anilist")

    def test_extra_choices_are_slugged_and_numbered_apart(self):
        raw = ("movie|Trakt Trending|https://mdblist.com/lists/u/m, "
               "movie|Trakt  trending!|https://mdblist.com/lists/u/m2, "
               "series|Trakt Trending|https://mdblist.com/lists/u/s, "
               "movie|TMDB|https://x.example/a, bogus|X|https://x.example, movie|No URL|ftp://x")
        with mock.patch.object(config, "TRENDING_SOURCE_CHOICES", raw):
            self.assertEqual([i for i, _n in trending_lists.menu("movie")],
                             ["tmdb", "tmdb_week", "trakt-trending", "trakt-trending-2", "tmdb-2"])
            self.assertEqual([i for i, _n in trending_lists.menu("tv")],
                             ["tmdb", "tmdb_week", "trakt-trending"])
            lists = trending_lists.parse("m:trakt-trending-2")
            self.assertEqual(lists.url("movie"), "https://mdblist.com/lists/u/m2")

    def test_caps_list_every_menu_with_its_default(self):
        caps = trending_lists.caps()
        self.assertTrue(caps["enabled"])
        self.assertFalse(caps["home_default"])
        self.assertEqual(caps["lists"]["movie"]["default"], "tmdb")
        self.assertEqual([c["id"] for c in caps["lists"]["anime"]["choices"]], ["anilist"])


class SnapshotTests(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_CATALOGS_ENABLED=False)
        patcher = mock.patch.object(tmdb, "anime_split", lambda: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_operators_signatures_are_unchanged(self):
        self.assertEqual(tmdb.trending_source_signature("movie"), "tmdb")
        self.assertEqual(tmdb.trending_source_signature("movie", trending_lists.parse("m:tmdb_week")),
                         "tmdb-week")
        self.assertEqual(tmdb.trending_source_signature("movie", trending_lists.parse("h:1")),
                         "tmdb+released")

    def test_a_pick_is_its_own_list_beside_the_operators(self):
        self._store("movie", {"5": 1}, time.time() - 60)
        calls = []

        async def _ids(client, key, endpoint, details_out=None, *, window="day", home=None):
            calls.append((endpoint, window))
            return ["7", "5"]

        week = trending_lists.parse("m:tmdb_week")
        with mock.patch.object(tmdb, "_fetch_tmdb_trending_ids", side_effect=_ids):
            rank, _exp = asyncio.run(tmdb.fetch_trending_rank_entry(None, "5", "k", "movie", week))
            # Read again: the stored pick is shared, not fetched per request.
            rank2, _exp = asyncio.run(tmdb.fetch_trending_rank_entry(None, "5", "k", "movie", week))
            plain, _exp = asyncio.run(tmdb.fetch_trending_rank_entry(None, "5", "k", "movie"))
        self.assertEqual((rank, rank2, plain), (2, 2, 1))
        self.assertEqual(calls, [("movie", "week")])
        self.assertEqual(cache.get_cached_trending_snapshot("movie@tmdb_week.h0", "tmdb-week"), {"7": 1, "5": 2})
        self.assertEqual(cache.get_cached_trending_snapshot("movie", "tmdb"), {"5": 1})

    def test_a_chosen_source_is_read_from_its_own_url(self):
        seen = []

        async def _source(client, media_type, details_out=None, url=None):
            seen.append(url)
            return ["3"]

        with mock.patch.object(config, "TRENDING_SOURCE_CHOICES", "movie|Mine|https://x.example/m.json"), \
             mock.patch.object(tmdb, "fetch_trending_source_ids", side_effect=_source):
            entry = asyncio.run(tmdb.ensure_trending_snapshot(None, "k", "movie", trending_lists.parse("m:mine")))
        self.assertEqual(entry[0], {"3": 1})
        self.assertEqual(seen, ["https://x.example/m.json"])

    def test_a_broken_chosen_source_cools_down_on_its_own(self):
        client = mock.Mock()
        client.get = mock.AsyncMock(side_effect=RuntimeError("down"))
        asyncio.run(tmdb.fetch_trending_source_ids(client, "movie", url="https://x.example/m.json"))
        self.assertNotIn("movie", tmdb._trending_source_failed_at)
        self.assertEqual(len(tmdb._trending_source_failed_at), 1)

    def test_prune_drops_only_quiet_picks(self):
        self._store("movie", {"5": 1}, time.time() - 30 * 86400)
        self._store("movie@tmdb_week.h0", {"5": 1}, time.time() - 30 * 86400)
        self._store("tv@tmdb_week.h0", {"5": 1}, time.time() - 60)
        self.assertEqual(cache.prune_trending_choices(7 * 86400), 1)
        self.assertEqual(sorted(cache.trending_list_keys()), ["movie", "tv@tmdb_week.h0"])


class TurnoverTests(_TempDb):
    """A list's turnover drops the composites ranked on it, and no others."""

    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_CATALOGS_ENABLED=False)

    def _poster(self, key, params):
        cache.set_cached_final_poster(key, b"x", request_params=params)

    def _keys(self):
        return sorted(k for (k,) in cache.get_db().execute("SELECT cache_key FROM final_poster_cache"))

    def test_each_list_drops_only_its_own_composites(self):
        self._poster("tt1:5:movie:aaa", "tmdb_id=5&type=movie")
        self._poster("tt1:5:movie:bbb", "tmdb_id=5&type=movie&trending_list=m:tmdb_week")
        self._poster("tt1:5:movie:ccc", "tmdb_id=5&type=movie&trending_list=h:1")
        self._store("movie@tmdb_week.h0", {"9": 1}, time.time())
        cache.invalidate_trending_turnover("movie@tmdb_week.h0", {"5"})
        self.assertEqual(self._keys(), ["tt1:5:movie:aaa", "tt1:5:movie:ccc"])
        cache.invalidate_trending_turnover("movie", {"5"})
        self.assertEqual(self._keys(), ["tt1:5:movie:ccc"])

    def test_before_any_pick_is_stored_every_composite_is_the_operators(self):
        self._poster("tt1:5:movie:aaa", "tmdb_id=5&type=movie")
        self._poster("tt1:5:movie:bbb", "")
        cache.invalidate_trending_turnover("movie", {"5"})
        self.assertEqual(self._keys(), [])


class RequestConfigTests(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self)

    def test_the_pick_is_canonical_and_keys_the_composite(self):
        plain = main.build_request_config({})
        same = main.build_request_config({"trending_list": "m:tmdb"})
        week = main.build_request_config({"trending_list": "M:TMDB_WEEK"})
        self.assertEqual((plain.trending_list, same.trending_list, week.trending_list), ("", "", "m:tmdb_week"))
        self.assertEqual(main._render_config_signature(plain), main._render_config_signature(same))
        self.assertNotEqual(main._render_config_signature(plain), main._render_config_signature(week))
        self.assertNotIn("trending_list", main._render_config_signature(plain))


class AddonListsTests(_AddonTest):
    def setUp(self):
        super().setUp()
        _Menu(self)
        self._store_with_details("movie", ["11"], {"11": {"name": "A", "imdb_id": "tt0000011"}}, TMDB_SIG)
        self._store_with_details("movie@tmdb_week.h0.anilist.anilist", ["22", "11"],
                                 {"22": {"name": "B", "imdb_id": "tt0000022"},
                                  "11": {"name": "A", "imdb_id": "tt0000011"}}, "tmdb-week" + tmdb._ANIME_SPLIT_MARK)

    def _metas(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["metas"]

    def test_the_lists_segment_picks_the_row(self):
        self.assertEqual([m["id"] for m in self._metas("/trending/sekrit/catalog/movie/pp.trending.movie.json")],
                         ["tt0000011"])
        self.assertEqual([m["id"] for m in self._metas(
            "/trending/sekrit/tl-m:tmdb_week/catalog/movie/pp.trending.movie.tmdb_week.json")],
            ["tt0000022", "tt0000011"])
        self.assertEqual(self.client.get("/trending/sekrit/tl-m:tmdb_week/manifest.json").status_code, 200)

    def test_posters_rank_on_the_rows_lists(self):
        seg = _cfg_segment("badge_display_mode=6&trending_list=h:1")
        metas = self._metas(f"/trending/sekrit/tl-m:tmdb_week/{seg}/catalog/movie/pp.trending.movie.tmdb_week.json")
        q = parse_qs(urlsplit(metas[0]["poster"]).query)
        self.assertEqual(q["trending_list"], ["m:tmdb_week"])
        self.assertEqual(q["badge_display_mode"], ["6"])

    def test_a_pick_among_the_settings_is_used_without_a_lists_segment(self):
        seg = _cfg_segment("trending_list=m:tmdb_week")
        metas = self._metas(f"/trending/sekrit/{seg}/catalog/movie/pp.trending.movie.tmdb_week.json")
        self.assertEqual([m["id"] for m in metas], ["tt0000022", "tt0000011"])

    def test_two_lists_segments_are_not_found(self):
        resp = self.client.get("/trending/sekrit/tl-h:1/tl-h:0/catalog/movie/pp.trending.movie.json")
        self.assertEqual(resp.status_code, 404)

    def test_the_server_caps_carry_the_menu(self):
        caps = self.client.get("/server-caps?access_key=sekrit").json()
        self.assertEqual(caps["trending_lists"]["lists"]["movie"]["default"], "tmdb")


class PublicSourceTests(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_LIST_SOURCES="imdb,simkl,trakt")

    def test_the_operator_picks_which_families_are_offered(self):
        ids = [i for i, _n in trending_lists.menu("movie")]
        self.assertEqual(ids, ["tmdb", "tmdb_week", "imdb", "trakt", "trakt_digital", "simkl", "simkl_week"])
        self.assertEqual([i for i, _n in trending_lists.menu("anime_movie")], ["anilist", "trakt"])
        self.assertEqual(trending_lists.parse("m:justwatch").token(), "")
        self.assertTrue(trending_lists.parse("m:imdb").url("movie").startswith("https://mdblist.com/lists/snoak/"))

    def test_several_picks_are_catalogs_and_the_first_ranks(self):
        lists = trending_lists.parse("m:imdb.tmdb.imdb.bogus.simkl")
        self.assertEqual(lists.catalogs("movie"), ("imdb", "tmdb", "simkl"))
        self.assertEqual(lists.source("movie"), "imdb")
        self.assertEqual(lists.token(), "m:imdb.tmdb.simkl")
        self.assertEqual(lists.rank_token(), "m:imdb")
        self.assertEqual(lists.for_catalog("movie", "simkl").token(), "m:simkl")
        # The operator's list ranking, with another catalog beside it.
        both = trending_lists.parse("m:tmdb.imdb")
        self.assertEqual((both.token(), both.rank_token()), ("m:tmdb.imdb", ""))

    def test_none_picks_no_catalog_and_ranks_on_the_operators(self):
        lists = trending_lists.parse("a:none")
        self.assertEqual(lists.catalogs("anime"), ())
        self.assertEqual(lists.source("anime"), "anilist")
        self.assertEqual((lists.token(), lists.rank_token()), ("a:none", ""))


class SimklParseTests(unittest.TestCase):
    _ROWS = [
        {"title": "B", "rank": 900, "release_date": "07/29/2026", "poster": "20/abc", "original_language": "en",
         "ids": {"tmdb": "22", "imdb": "tt2"}},
        {"title": "A", "rank": 5, "ids": {"tmdb": "11"}},
        {"title": "No id", "ids": {"simkl_id": 1}},
        {"title": "B again", "ids": {"tmdb": "22"}},
    ]

    def test_rows_rank_by_array_order_not_simkls_rank(self):
        details = {}
        self.assertEqual(tmdb._parse_trending_payload(self._ROWS, "movie", details), ["22", "11"])
        self.assertEqual(details["22"], {"name": "B", "year": "2026", "imdb_id": "tt2",
                                         "poster": "https://simkl.in/posters/20/abc_m.jpg",
                                         "lang": "en", "date": "2026-07-29"})

    def test_anime_is_keyed_by_anilist_and_split_into_series_and_films(self):
        rows = [{"title": "S", "anime_type": "tv", "ids": {"tmdb": "1", "anilist": "100"}},
                {"title": "F", "anime_type": "movie", "ids": {"tmdb": "2", "anilist": "200"}},
                {"title": "O", "anime_type": "ona", "ids": {"tmdb": "3"}}]
        self.assertEqual(tmdb._parse_trending_payload(rows, "anime"), ["anilist:100", "3"])
        self.assertEqual(tmdb._parse_trending_payload(rows, "anime_movie"), ["anilist:200"])


class MultiCatalogAddonTests(_AddonTest):
    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_LIST_SOURCES="imdb")
        self._store_with_details("movie", ["11"], {"11": {"name": "A", "imdb_id": "tt0000011"}}, TMDB_SIG)

    def test_the_manifest_has_a_catalog_per_pick(self):
        cats = self.client.get("/trending/sekrit/tl-m:imdb.tmdb,a:none/manifest.json").json()["catalogs"]
        self.assertEqual([(c["id"], c["name"]) for c in cats], [
            ("pp.trending.movie.imdb", "Trending Movies · IMDb Most Popular"),
            ("pp.trending.movie", "Trending Movies · TMDB Today"),
            ("pp.trending.series", "Trending Series"),
            ("pp.trending.anime.movie", "Trending Anime Movies"),
        ])
        plain = self.client.get("/trending/sekrit/manifest.json").json()["catalogs"]
        self.assertEqual([c["id"] for c in plain], ["pp.trending.movie", "pp.trending.series",
                                                    "pp.trending.anime", "pp.trending.anime.movie"])

    def test_catalog_ids_resolve_to_their_source(self):
        self.assertEqual(main._resolve_trending_catalog("pp.trending.movie.imdb", "movie"), ("movie", "imdb"))
        self.assertEqual(main._resolve_trending_catalog("pp.trending.anime.movie", "movie"), ("anime_movie", "anilist"))
        self.assertIsNone(main._resolve_trending_catalog("pp.trending.movie.nope", "movie"))
        self.assertIsNone(main._resolve_trending_catalog("pp.trending.movie.imdb", "series"))

    def test_each_catalogs_posters_rank_on_its_own_list(self):
        self._store_with_details(
            "movie@imdb.h0.anilist.anilist", ["33", "11"],
            {"33": {"name": "C", "imdb_id": "tt0000033"}, "11": {"name": "A", "imdb_id": "tt0000011"}},
            tmdb.trending_source_signature("movie", trending_lists.parse("m:imdb")))
        seg = _cfg_segment("badge_display_mode=6")
        metas = self._metas(f"/trending/sekrit/tl-m:tmdb.imdb/{seg}/catalog/movie/pp.trending.movie.imdb.json")
        self.assertEqual([m["id"] for m in metas], ["tt0000033", "tt0000011"])
        self.assertEqual(parse_qs(urlsplit(metas[0]["poster"]).query)["trending_list"], ["m:imdb"])
        plain = self._metas(f"/trending/sekrit/tl-m:tmdb.imdb/{seg}/catalog/movie/pp.trending.movie.json")
        self.assertNotIn("trending_list", parse_qs(urlsplit(plain[0]["poster"]).query))

    def _metas(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["metas"]

    def test_only_the_ranking_lists_key_a_render(self):
        a = main.build_request_config({"trending_list": "m:imdb.tmdb"})
        b = main.build_request_config({"trending_list": "m:imdb"})
        self.assertEqual((a.trending_list, b.trending_list), ("m:imdb", "m:imdb"))


class ReviewFixTests(_AddonTest):
    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_LIST_SOURCES="imdb,trakt")
        self._store_with_details("movie", ["11"], {"11": {"name": "A", "imdb_id": "tt0000011"}}, TMDB_SIG)

    def test_with_choice_off_no_other_catalog_is_served(self):
        with mock.patch.object(config, "TRENDING_LIST_CHOICE", False):
            self.assertIsNone(main._resolve_trending_catalog("pp.trending.movie.imdb", "movie"))
            resp = self.client.get("/trending/sekrit/catalog/movie/pp.trending.movie.imdb.json")
            self.assertEqual(resp.status_code, 404)
            cats = self.client.get("/trending/sekrit/tl-m:imdb/manifest.json").json()["catalogs"]
            self.assertEqual(cats[0]["id"], "pp.trending.movie")

    def test_a_pick_of_nothing_still_has_catalogs(self):
        cats = self.client.get("/trending/sekrit/tl-m:none,t:none,a:none,f:none/manifest.json").json()["catalogs"]
        self.assertEqual(len(cats), 4)

    def test_an_early_expired_pick_keeps_its_age(self):
        self._store("movie@imdb.h0.anilist.anilist", {"5": 1}, time.time() - 60)
        cache.expire_trending_snapshot("movie@imdb.h0.anilist.anilist")
        self.assertIsNone(cache.get_cached_trending_snapshot_entry("movie@imdb.h0.anilist.anilist"))
        self.assertEqual(cache.prune_trending_choices(7 * 86400), 0)
        self.assertIn("movie@imdb.h0.anilist.anilist", cache.trending_list_keys())


class HomeFilterTests(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self)

    def test_a_series_row_without_a_date_is_judged_by_its_tmdb_air_date(self):
        meta = {"1": "2020-01-01", "2": "2999-01-01", "3": ""}

        async def _meta(client, tmdb_id, key, kind, *a, **k):
            return ([], False, [], None, "", None, None, {"tmdb_release_date": meta[tmdb_id]})

        with mock.patch.object(tmdb, "fetch_poster_metadata", side_effect=_meta):
            kept = asyncio.run(tmdb._released_only(None, "k", "tv", ["1", "2", "3"], {}))
        self.assertEqual(kept, ["1"])

    def test_an_anilist_series_is_judged_by_its_rows_date(self):
        details = {"anilist:1": {"date": "2020-01-01"}, "anilist:2": {"date": "2999-01-01"}, "anilist:3": {}}
        with mock.patch("anime_ids.tmdb_films_for_anilist", return_value={}):
            kept = asyncio.run(tmdb._released_only(None, "k", "anime", ["anilist:1", "anilist:2", "anilist:3"], details))
        self.assertEqual(kept, ["anilist:1", "anilist:3"])

    def test_genres_are_looked_up_only_as_far_as_the_list_needs(self):
        seen = []

        async def _meta(client, tmdb_id, key, kind, *a, **k):
            seen.append(tmdb_id)
            return ([18], False, [], None, "", None, None, {})

        ids = [str(i) for i in range(1, 301)]
        with mock.patch.object(tmdb, "TRENDING_HIDE_GENRES", ["Horror"]), \
             mock.patch.object(tmdb, "fetch_poster_metadata", side_effect=_meta):
            kept = asyncio.run(tmdb._without_hidden_genres(None, "k", "movie", ids, {}, limit=100))
        self.assertEqual(kept, ids[:100])
        self.assertEqual(len(seen), 100)


class SimklDetectTests(unittest.TestCase):
    def test_a_first_row_without_ids_still_reads_as_simkl(self):
        rows = [{"title": "odd", "rank": 1}, {"title": "B", "rank": 900, "ids": {"tmdb": "22"}},
                {"title": "A", "rank": 5, "ids": {"tmdb": "11"}}]
        self.assertEqual(tmdb._parse_trending_payload(rows, "movie"), ["22", "11"])
