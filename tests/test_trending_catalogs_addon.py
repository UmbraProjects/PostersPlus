"""The trending catalogs addon serves the snapshots behind the Trending sashes.

A metadata addon (AIOMetadata) imports the catalogs, so a row's order and the
"#N Today" printed on its posters come from one snapshot and line up.
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import anime
import cache
import main
from tests.test_trending_snapshot_turnover import _TempDb


class _AddonTest(_TempDb):
    def setUp(self):
        super().setUp()
        self._saved_cfg = (
            main._cfg.TRENDING_CATALOGS_ENABLED, main._cfg.ACCESS_KEY,
            main._cfg.SERVER_TMDB_KEY, main._cfg.TRENDING_FETCH_COUNT,
            main._cfg.TRENDING_BROAD_FETCH_COUNT,
        )
        main._cfg.TRENDING_CATALOGS_ENABLED = True
        main._cfg.ACCESS_KEY = "sekrit"
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.TRENDING_FETCH_COUNT = 40
        main._cfg.TRENDING_BROAD_FETCH_COUNT = 100
        self.client = TestClient(main.app)

    def tearDown(self):
        (main._cfg.TRENDING_CATALOGS_ENABLED, main._cfg.ACCESS_KEY,
         main._cfg.SERVER_TMDB_KEY, main._cfg.TRENDING_FETCH_COUNT,
         main._cfg.TRENDING_BROAD_FETCH_COUNT) = self._saved_cfg
        super().tearDown()

    def _store_with_details(self, media_type, ids, details, sig):
        cache.set_cached_trending_snapshot(
            media_type, {i: n for n, i in enumerate(ids, start=1)}, sig, details,
        )


class ManifestTests(_AddonTest):
    def test_disabled_is_not_found(self):
        main._cfg.TRENDING_CATALOGS_ENABLED = False
        self.assertEqual(self.client.get("/trending/sekrit/manifest.json").status_code, 404)

    def test_access_key_rides_in_the_path(self):
        self.assertEqual(self.client.get("/trending/manifest.json").status_code, 403)
        self.assertEqual(self.client.get("/trending/wrong/manifest.json").status_code, 403)
        resp = self.client.get("/trending/sekrit/manifest.json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")
        cats = [(c["type"], c["id"]) for c in resp.json()["catalogs"]]
        self.assertEqual(cats, [
            ("movie", "pp.trending.movie"),
            ("series", "pp.trending.series"),
            ("series", "pp.trending.anime"),
        ])

    def test_no_access_key_serves_the_plain_path(self):
        main._cfg.ACCESS_KEY = ""
        self.assertEqual(self.client.get("/trending/manifest.json").status_code, 200)


class CatalogTests(_AddonTest):
    def test_movie_catalog_is_the_snapshot_in_rank_order(self):
        self._store_with_details("movie", ["11", "22", "33"], {
            "11": {"name": "First", "year": "2026", "imdb_id": "tt0000011", "poster": "/a.jpg"},
            "22": {"name": "Second", "poster": "/b.jpg"},
            "33": {"name": "Third"},
        }, "tmdb")

        async def _resolve(client, tmdb_id, media_type, key):
            return {"22": "tt0000022"}.get(tmdb_id)

        with mock.patch.object(main, "resolve_tmdb_to_imdb", side_effect=_resolve):
            resp = self.client.get("/trending/sekrit/catalog/movie/pp.trending.movie.json")
        self.assertEqual(resp.status_code, 200)
        metas = resp.json()["metas"]
        self.assertEqual([m["id"] for m in metas], ["tt0000011", "tt0000022", "tmdb:33"])
        self.assertEqual([m["name"] for m in metas], ["First", "Second", "Third"])
        self.assertEqual(metas[0]["poster"], "https://image.tmdb.org/t/p/w500/a.jpg")
        self.assertTrue(all(m["type"] == "movie" for m in metas))
        # Cached no longer than the snapshot, like the ranked posters.
        max_age = int(resp.headers["cache-control"].split("max-age=")[1])
        self.assertAlmostEqual(max_age, 86400, delta=5)

    def test_a_snapshot_without_details_is_filled_from_tmdb(self):
        # Snapshots written before details were stored carry ids only; Nuvio
        # showed those rows as blank tiles named by IMDb id.
        cache.set_cached_trending_snapshot("movie", {"11": 1}, "tmdb")
        calls = []

        async def _meta(client, tmdb_id, key, media_type, lang, secondary=""):
            calls.append((tmdb_id, media_type))
            return ([], False, [], "2026", "Heart of the Beast", "/h.jpg", None, {})

        async def _resolve(client, tmdb_id, media_type, key):
            return "tt7526136"

        with mock.patch.object(main, "_coalesced_fetch_poster_metadata", side_effect=_meta), \
             mock.patch.object(main, "resolve_tmdb_to_imdb", side_effect=_resolve):
            metas = self.client.get("/trending/sekrit/catalog/movie/pp.trending.movie.json").json()["metas"]
        self.assertEqual(calls, [("11", "movie")])
        self.assertEqual(metas[0]["name"], "Heart of the Beast")
        self.assertEqual(metas[0]["poster"], "https://image.tmdb.org/t/p/w500/h.jpg")
        self.assertEqual(metas[0]["releaseInfo"], "2026")

    def test_skip_returns_the_rest_of_the_list(self):
        self._store_with_details("tv", ["1", "2", "3"], {
            k: {"imdb_id": f"tt000000{k}"} for k in ("1", "2", "3")
        }, "tmdb")
        resp = self.client.get("/trending/sekrit/catalog/series/pp.trending.series/skip=2.json")
        self.assertEqual([m["id"] for m in resp.json()["metas"]], ["tt0000003"])
        resp = self.client.get("/trending/sekrit/catalog/series/pp.trending.series/skip=3.json")
        self.assertEqual(resp.json()["metas"], [])

    def test_list_stops_at_the_broad_trending_count(self):
        main._cfg.TRENDING_FETCH_COUNT = 1
        main._cfg.TRENDING_BROAD_FETCH_COUNT = 2
        self._store_with_details("tv", ["1", "2", "3"], {
            k: {"imdb_id": f"tt000000{k}"} for k in ("1", "2", "3")
        }, "tmdb")
        resp = self.client.get("/trending/sekrit/catalog/series/pp.trending.series.json")
        self.assertEqual(len(resp.json()["metas"]), 2)

    def test_anime_catalog_hands_out_anilist_ids(self):
        self._store_with_details("anime", ["anilist:5", "anilist:9"], {
            "anilist:5": {"name": "Five", "poster": "https://img.anili.st/5.jpg"},
        }, "anilist")
        resp = self.client.get("/trending/sekrit/catalog/series/pp.trending.anime.json")
        metas = resp.json()["metas"]
        self.assertEqual([m["id"] for m in metas], ["anilist:5", "anilist:9"])
        self.assertEqual(metas[0]["poster"], "https://img.anili.st/5.jpg")

    def test_unknown_catalog_and_wrong_type_are_not_found(self):
        self.assertEqual(self.client.get("/trending/sekrit/catalog/movie/pp.trending.series.json").status_code, 404)
        self.assertEqual(self.client.get("/trending/sekrit/catalog/movie/other.json").status_code, 404)

    def test_catalog_needs_the_key(self):
        self.assertEqual(self.client.get("/trending/catalog/movie/pp.trending.movie.json").status_code, 403)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class AniListTrendingTests(unittest.TestCase):
    def test_pages_are_read_in_order_deduped_and_capped(self):
        pages = {
            1: {"pageInfo": {"hasNextPage": True}, "media": [
                {"id": 1, "title": {"english": "One", "romaji": "Ichi"}, "seasonYear": 2026,
                 "coverImage": {"large": "l1"}},
                {"id": 2, "title": {"romaji": "Ni"}, "coverImage": {"extraLarge": "x2", "large": "l2"}},
            ]},
            2: {"pageInfo": {"hasNextPage": True}, "media": [
                {"id": 2, "title": {"romaji": "Ni"}},
                {"id": 3, "title": {"romaji": "San"}},
                {"id": 4, "title": {"romaji": "Shi"}},
            ]},
        }
        calls = []

        async def _post(url, json, timeout):
            calls.append(json["variables"]["page"])
            return _Resp({"data": {"Page": pages[json["variables"]["page"]]}})

        client = mock.Mock(post=_post)
        details = {}
        with mock.patch("config.TRENDING_FETCH_COUNT", 1), mock.patch("config.TRENDING_BROAD_FETCH_COUNT", 3):
            ids = asyncio.run(anime.fetch_anilist_trending(client, details))
        self.assertEqual(ids, ["anilist:1", "anilist:2", "anilist:3"])
        self.assertEqual(calls, [1, 2])
        self.assertEqual(details["anilist:1"], {"name": "One", "year": "2026", "poster": "l1"})
        self.assertEqual(details["anilist:2"]["poster"], "x2")

    def test_query_keeps_to_series_formats(self):
        self.assertIn("format_in: [TV, TV_SHORT, ONA]", anime._ANILIST_TRENDING_QUERY)
        self.assertIn("sort: TRENDING_DESC", anime._ANILIST_TRENDING_QUERY)
        self.assertIn("isAdult: false", anime._ANILIST_TRENDING_QUERY)

    def test_a_failed_read_is_none(self):
        async def _post(url, json, timeout):
            return _Resp({}, status=429)

        self.assertIsNone(asyncio.run(anime.fetch_anilist_trending(mock.Mock(post=_post))))


class AnimeRankTests(_AddonTest):
    def test_anime_rank_change_invalidates_anilist_composites(self):
        cache.set_cached_final_poster("anilist:5:tt1:77:series:h", b"x")
        cache.set_cached_final_poster("anilist:55:tt2:78:series:h", b"x")
        self._store_with_details("anime", ["anilist:5"], {}, "anilist")
        cache.set_cached_trending_snapshot("anime", {"anilist:9": 1}, "anilist")
        with cache._composite_l1_lock:
            self.assertNotIn("anilist:5:tt1:77:series:h", cache._composite_l1)
            self.assertIn("anilist:55:tt2:78:series:h", cache._composite_l1)

    def test_anilist_rank_comes_from_the_anime_snapshot(self):
        self._store_with_details("anime", ["anilist:5", "anilist:9"], {}, "anilist")
        rank, expires_at = asyncio.run(main.fetch_trending_rank_entry(None, "anilist:9", "k", "anime"))
        self.assertEqual(rank, 2)
        self.assertIsNotNone(expires_at)

    def test_render_path_ranks_anilist_requests_only_with_the_addon_on(self):
        src = Path("main.py").read_text(encoding="utf-8")
        self.assertIn(
            'fetch_trending_rank_entry(client, anime_key, effective_tmdb_key, "anime")\n'
            '            if anime_namespace == "anilist" and _cfg.TRENDING_CATALOGS_ENABLED',
            src,
        )

    def test_cycle_refreshes_anime_and_rerenders_by_the_leading_key(self):
        seen = {}

        async def _ensure(client, key, endpoint):
            if endpoint == "anime":
                cache.set_cached_trending_snapshot("anime", {"anilist:5": 1}, "anilist")
            return cache.get_cached_trending_snapshot_entry(endpoint)

        async def _regen(matches, *, log_prefix):
            seen["matches"] = matches
            return 0

        with mock.patch.object(main, "ensure_trending_snapshot", side_effect=_ensure), \
             mock.patch.object(main, "_regenerate_cached_posters", side_effect=_regen):
            asyncio.run(main._run_trending_fetch_cycle(None))
        matches = seen["matches"]
        self.assertTrue(matches("anilist:5:tt1:77:series:h".split(":")))
        self.assertFalse(matches("anilist:55:tt1:77:series:h".split(":")))
        self.assertFalse(matches("kitsu:5:tt1:77:series:h".split(":")))

    def test_a_failed_anilist_read_is_not_retried_per_request(self):
        import tmdb
        tmdb._trending_source_failed_at.pop("anime", None)
        calls = []

        async def _fail(client, details_out=None):
            calls.append(1)
            return None

        try:
            with mock.patch("anime.fetch_anilist_trending", side_effect=_fail):
                for _ in range(3):
                    self.assertIsNone(asyncio.run(tmdb.ensure_trending_snapshot(None, "k", "anime")))
        finally:
            tmdb._trending_source_failed_at.pop("anime", None)
        self.assertEqual(len(calls), 1)

    def test_only_ranked_titles_keep_details(self):
        cache.set_cached_trending_snapshot(
            "movie", {"1": 1}, "tmdb", {"1": {"name": "a"}, "2": {"name": "b"}},
        )
        self.assertEqual(cache.get_cached_trending_details("movie"), {"1": {"name": "a"}})

    def test_anime_is_only_kept_current_with_the_addon_on(self):
        self.assertIn("anime", main._trending_endpoints())
        main._cfg.TRENDING_CATALOGS_ENABLED = False
        self.assertNotIn("anime", main._trending_endpoints())


if __name__ == "__main__":
    unittest.main()
