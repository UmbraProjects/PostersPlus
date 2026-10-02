"""Quality badges under a trending ribbon sit the same distance below it on
every poster, frosted quality chips take the sash's frost opacity, and
TRENDING_HIDE_UNRELEASED leaves titles not out at home off the trending lists."""

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import numpy as np
from PIL import Image

import graphic_badges as gb
import main
import tmdb
import trending_rank
from discovery import DiscoveryMeta

# The stored movie / TV lists' signature while anime has its own lists.
TMDB_SIG = "tmdb" + tmdb._ANIME_SPLIT_MARK


def _art(kind: str) -> Image.Image:
    if kind == "dark":
        return Image.new("RGBA", (500, 750), (14, 14, 16, 255))
    rng = np.random.default_rng(3)
    a = np.clip(rng.normal((215, 195, 80), 30, (750, 500, 3)), 0, 255).astype(np.uint8)
    return Image.fromarray(a).convert("RGBA")


class RibbonFootprintTests(unittest.TestCase):
    def test_body_covers_the_drawn_ribbon(self):
        for rank, label, corner in ((3, False, False), (24, True, False), (100, False, True)):
            clear = Image.new("RGBA", (500, 750), (0, 0, 0, 0))
            drawn = trending_rank.draw_rank_ribbon(clear, rank, right=True, label="FILM" if label else None,
                                                   corner=corner)
            solid = np.asarray(drawn.getchannel("A")) > 200
            ys, xs = np.nonzero(solid)
            body, extent = trending_rank.ribbon_footprint(500, rank, right=True, label=label, corner=corner)
            self.assertGreaterEqual(ys.max() + 1, body[3] - round(trending_rank._SHADE * 500) - 4)   # anti-aliased tips
            self.assertLessEqual(ys.max() + 1, body[3])
            self.assertGreaterEqual(xs.min(), body[0])
            self.assertLessEqual(xs.max() + 1, body[2])
            # Everything drawn, shadow included, lies inside the extent.
            ys, xs = np.nonzero(np.asarray(drawn.getchannel("A")) > 0)
            self.assertGreaterEqual(xs.min(), extent[0])
            self.assertLessEqual(xs.max() + 1, extent[2])
            self.assertLessEqual(ys.max() + 1, extent[3])

    def test_number_body_covers_the_drawn_numeral(self):
        clear = Image.new("RGBA", (500, 750), (0, 0, 0, 0))
        drawn = trending_rank.draw_rank_number(clear, 24, right=True)
        ys, xs = np.nonzero(np.asarray(drawn.getchannel("A")) > 200)
        body, _ = trending_rank.number_footprint(500, 24, right=True)
        self.assertGreaterEqual(xs.min(), body[0])
        self.assertLessEqual(xs.max() + 1, body[2])
        self.assertLessEqual(ys.max() + 1, body[3])


class BadgeUnderRibbonTests(unittest.TestCase):
    def badge_top(self, art: str, style: str, scale: str) -> int:
        cfg = main.build_request_config({
            "badge_display_mode": "7", "trending_style": "ribbon", "trending_side": "right",
            "sash_mode": "hidden", "badge_group1": "chip:2:res", "trending_ribbon_style": style,
            "trending_scale": scale, "trending_frost_opacity": "0.45"})

        def render(tokens):
            out = main.build_poster(_art(art), 70, "Drama", cfg, quality_tokens=tokens,
                                    discovery_meta=DiscoveryMeta(trending_rank=7), media_kind="movie")
            return np.asarray(out.convert("RGB")).astype(int)

        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            diff = np.abs(render(["4K"]) - render([])).sum(axis=2) > 40
            gb._mark.cache_clear()
        rows = np.flatnonzero(diff[:400, 330:].any(axis=1))
        self.assertTrue(rows.size)
        return int(rows.min())

    def test_same_gap_on_dark_and_bright_art(self):
        for style in ("charcoal", "frosted"):
            for scale in ("1.2", "1.3"):
                with self.subTest(style=style, scale=scale):
                    self.assertAlmostEqual(self.badge_top("dark", style, scale),
                                           self.badge_top("bright", style, scale), delta=1)

    def test_badge_clears_the_ribbon(self):
        body, _ = trending_rank.ribbon_footprint(500, 7, right=True, scale=1.3,
                                                 top_inset=round(750 * main.RequestConfig().sash_badge_inset))
        self.assertGreater(self.badge_top("bright", "charcoal", "1.3"), body[3])


class FrostOpacityTests(unittest.TestCase):
    def test_look_carries_the_opacity(self):
        self.assertEqual(gb.quality_look("frosted", (1, 2, 3), 0.5), "rgb:1,2,3@128")
        self.assertEqual(gb.quality_look("frosted", (1, 2, 3)), "rgb:1,2,3")
        self.assertEqual(gb.quality_look("frosted", None, 0.5), "auto")
        self.assertEqual(gb._parse_look("rgb:1,2,3@128"), ((1, 2, 3), 128))
        self.assertEqual(gb._parse_look("rgb:1,2,3"), ((1, 2, 3), round(255 * gb._CHIP_FROST)))
        self.assertEqual(gb.cinema_ink("frosted", gb.CinemaRun("Cinema"), (10, 20, 30), 1.0),
                         "disc|rgb:10,20,30@255|popcorn")

    def test_chip_glass_follows_it(self):
        art = Image.new("RGBA", (200, 100), (0, 0, 0, 255))

        def glass_level(opacity):
            chip = dict(gb.row_items(["4K"], None, None, 30, ("res",), True,
                                     quality_look=gb.quality_look("frosted", (250, 250, 250), opacity)))["res"]
            out = gb._resolve_chip(art, chip, 10, 10)
            return np.asarray(out)[2, out.width // 2 - 15, 0]   # glass, clear of the label

        self.assertLess(glass_level(0.3), glass_level(0.9))


class HideUnreleasedTests(unittest.TestCase):
    def run_filter(self, endpoint, ids, details, statuses=None):
        async def info(_client, tmdb_id, _key, _status, primary_release_date=None):
            return {"status": (statuses or {}).get(tmdb_id, "Streaming")}
        with mock.patch.object(tmdb, "fetch_movie_release_info", info):
            return asyncio.run(tmdb._released_only(None, "key", endpoint, ids, details))

    def test_films_need_to_be_out_at_home(self):
        kept = self.run_filter("movie", ["1", "2", "3", "4"], {},
                               {"1": "Cinema", "2": "Physical", "3": "Production", "4": "Streaming"})
        self.assertEqual(kept, ["2", "4"])

    def test_series_need_to_have_aired(self):
        today = date.today()
        details = {"1": {"date": (today + timedelta(days=3)).isoformat()},
                   "2": {"date": today.isoformat()},
                   "3": {"date": ""},          # TMDB row with no date yet
                   "4": {}}                    # a source that gives none: kept
        self.assertEqual(self.run_filter("tv", ["1", "2", "3", "4"], details), ["2", "4"])

    def test_anilist_films_are_judged_by_their_tmdb_film(self):
        with mock.patch("anime_ids.tmdb_films_for_anilist",
                        lambda ids: {a: m for a, m in {9: 822653, 8: 111}.items() if a in ids}):
            kept = self.run_filter("anime_movie", ["anilist:9", "anilist:8", "anilist:7"], {},
                                   {"822653": "Cinema", "111": "Streaming"})
        self.assertEqual(kept, ["anilist:8", "anilist:7"])     # unmapped: kept

    def test_a_failed_check_keeps_the_title(self):
        async def boom(*_a, **_k):
            raise RuntimeError("down")
        with mock.patch.object(tmdb, "fetch_movie_release_info", boom):
            self.assertEqual(asyncio.run(tmdb._released_only(None, "key", "movie", ["1"], {})), ["1"])

    def test_stops_once_the_sashes_are_filled(self):
        with mock.patch.object(tmdb, "TRENDING_FETCH_COUNT", 5), \
             mock.patch.object(tmdb, "TRENDING_BROAD_FETCH_COUNT", 10):
            kept = self.run_filter("movie", [str(i) for i in range(200)], {})
        self.assertEqual(kept, [str(i) for i in range(10)])

    def test_row_dates_are_kept_for_the_filter(self):
        self.assertEqual(tmdb._trending_item_details({"name": "X", "first_air_date": ""})["date"], "")
        self.assertNotIn("date", tmdb._trending_item_details({"title": "X", "year": 2020}))

    def test_signature_changes_with_the_setting(self):
        with mock.patch.object(tmdb, "TRENDING_HIDE_UNRELEASED", True), \
             mock.patch.object(tmdb, "TRENDING_SOURCE_MOVIE", ""), \
             mock.patch.object(tmdb, "anime_split", lambda: False):
            self.assertEqual(tmdb.trending_source_signature("movie"), "tmdb+released")
            self.assertEqual(tmdb.trending_source_signature("anime"), "anilist+released")  # never -anime
            self.assertEqual(tmdb.trending_source_signature("anime_movie"), "anilist-films+released-home")
        with mock.patch.object(tmdb, "TRENDING_SOURCE_MOVIE", ""), \
             mock.patch.object(tmdb, "anime_split", lambda: False):
            self.assertEqual(tmdb.trending_source_signature("movie"), "tmdb")
        # Leaving anime to its own lists rebuilds the movie and TV ones.
        with mock.patch.object(tmdb, "TRENDING_SOURCE_MOVIE", ""), \
             mock.patch.object(tmdb, "anime_split", lambda: True):
            self.assertEqual(tmdb.trending_source_signature("movie"), TMDB_SIG)
            self.assertEqual(tmdb.trending_source_signature("anime"), "anilist")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Anime ranks on its own lists, and leaves TMDB's
# ---------------------------------------------------------------------------

import anime_ids  # noqa: E402
import cache  # noqa: E402
from tests.test_anime_id_map import _TempTable  # noqa: E402
from tests.test_trending_catalogs_addon import _AddonTest  # noqa: E402

# Two seasons of one show (TMDB 100), a film, and a show mapped through
# Kitsu alone.
MAP = [
    {"type": "TV", "kitsu_id": 10, "anilist_id": 1, "mal_id": 101,
     "themoviedb_id": {"tv": 100}, "imdb_id": ["tt0000100"]},
    {"type": "TV", "kitsu_id": 20, "anilist_id": 2, "mal_id": 102,
     "themoviedb_id": {"tv": 100}, "imdb_id": ["tt0000100"]},
    {"type": "MOVIE", "kitsu_id": 30, "anilist_id": 3, "mal_id": 103,
     "themoviedb_id": {"movie": [300]}},
    {"type": "TV", "kitsu_id": 40, "themoviedb_id": {"tv": 400}},
]


class AnimeMappingTests(_TempTable):
    def setUp(self):
        super().setUp()
        self._load(MAP)

    def test_kitsu_and_titles_map_to_anilist(self):
        self.assertEqual(anime_ids.anilist_for_kitsu(20), [2])
        self.assertEqual(anime_ids.anilist_for_kitsu(40), [])
        self.assertEqual(anime_ids.anilist_for_title("series", "100", None), [1, 2])
        self.assertEqual(anime_ids.anilist_for_title("series", None, "tt0000100"), [1, 2])
        self.assertEqual(anime_ids.anilist_for_title("movie", "300", None), [3])
        self.assertEqual(anime_ids.anilist_for_title("movie", "100", None), [])

    def test_ids_for_anilist(self):
        self.assertEqual(anime_ids.ids_for_anilist({2, 3}), ({20, 30}, {100}, {300}))

    def test_keys_a_poster_is_ranked_by(self):
        keys = main._anime_trending_keys
        self.assertEqual(keys("anilist", 2, "series", "100", "", True), ["anilist:2", "100"])
        self.assertEqual(keys("kitsu", 20, "series", "", "", False), ["anilist:2"])
        # A show asked for by TMDB id: any season's rank counts.
        self.assertEqual(keys(None, None, "series", "100", "tt0000100", True),
                         ["anilist:1", "anilist:2", "100"])
        self.assertEqual(keys(None, None, "movie", "300", "", True), ["anilist:3", "300"])
        # Mapped through Kitsu alone: still anime.
        self.assertEqual(keys(None, None, "series", "400", "", True), ["400"])
        self.assertEqual(keys(None, None, "series", "555", "", True), [])

    def test_anime_leaves_the_tmdb_lists(self):
        details = {"7": {"anime": True}}
        self.assertEqual(tmdb._without_anime("tv", ["100", "5", "400", "7", "8"], details), ["5", "8"])
        self.assertEqual(tmdb._without_anime("movie", ["300", "100"], {}), ["100"])
        # Mapped, but Chinese: stays on the TMDB list, where it is ranked.
        self.assertEqual(tmdb._without_anime("tv", ["100", "400"], {"100": {"lang": "zh"}, "400": {"lang": "ja"}}),
                         ["100"])
        # Western or Chinese animation isn't flagged; Japanese is.
        row = {"genre_ids": [16], "original_language": "en", "name": "Pixar"}
        self.assertNotIn("anime", tmdb._trending_item_details(row))
        self.assertNotIn("anime", tmdb._trending_item_details({**row, "original_language": "zh"}))
        self.assertTrue(tmdb._trending_item_details({**row, "original_language": "ja"})["anime"])


class AnimeRankLookupTests(_AddonTest):
    def test_best_rank_any_key_holds(self):
        self._store_with_details("anime", ["anilist:9", "anilist:2", "anilist:1"], {}, "anilist")
        rank, exp = asyncio.run(main.fetch_anime_trending_rank_entry(None, ["anilist:1", "anilist:2", "100"], "k"))
        self.assertEqual(rank, 2)
        self.assertIsNotNone(exp)
        self.assertEqual(asyncio.run(main.fetch_anime_trending_rank_entry(None, ["anilist:5"], "k"))[0], None)

    def test_off_the_anime_list_the_tmdb_rank_shows(self):
        self._store_with_details("anime", ["anilist:9"], {}, "anilist")
        self._store_with_details("tv", ["5", "100"], {}, TMDB_SIG)
        rank = lambda keys, tid: asyncio.run(main._anime_or_tmdb_rank(None, keys, tid, "k", "series"))[0]
        self.assertEqual(rank(["anilist:9", "100"], "100"), 1)     # the anime list wins
        self.assertEqual(rank(["anilist:1", "100"], "100"), 2)     # else its TMDB rank
        self.assertIsNone(rank(["anilist:1"], None))

    def test_an_unread_anime_list_keeps_the_short_ttl(self):
        async def unread(*_a, **_k):
            return None, None

        async def tmdb_list(*_a, **_k):
            return None, 12345.0
        with mock.patch.object(main, "fetch_anime_trending_rank_entry", unread), \
             mock.patch.object(main, "fetch_trending_rank_entry", tmdb_list):
            self.assertEqual(asyncio.run(main._anime_or_tmdb_rank(None, ["anilist:1"], "5", "k", "series")),
                             (None, None))

    def test_films_rank_on_their_own_list(self):
        self._store_with_details("anime_movie", ["anilist:3"], {}, "anilist-films")
        self.assertEqual(asyncio.run(main.fetch_anime_trending_rank_entry(None, ["anilist:3"], "k", film=True))[0], 1)

    def test_anime_film_catalog(self):
        self._store_with_details("anime_movie", ["anilist:3"], {"anilist:3": {"name": "Film", "year": "2026"}},
                                 "anilist-films")
        resp = self.client.get("/trending/sekrit/catalog/movie/pp.trending.anime.movie.json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([(m["id"], m["type"], m["name"]) for m in resp.json()["metas"]],
                         [("anilist:3", "movie", "Film")])

    def test_a_custom_anime_source_is_tmdb_ids(self):
        with mock.patch.object(tmdb, "TRENDING_SOURCE_ANIME", "https://example.com/list.json"):
            self._store_with_details("anime", ["100"], {"100": {"name": "Show", "imdb_id": "tt0000100"}},
                                     tmdb.trending_source_signature("anime"))
            resp = self.client.get("/trending/sekrit/catalog/series/pp.trending.anime.json")
        self.assertEqual([m["id"] for m in resp.json()["metas"]], ["tt0000100"])


class AnimeInvalidationTests(_TempTable):
    def setUp(self):
        super().setUp()
        self._load(MAP)
        self._db = tempfile.TemporaryDirectory()
        self._saved_db = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._db.name, "cache.db")
        cache._local.conn = None
        cache.init_db()

    def tearDown(self):
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved_db
        self._db.cleanup()
        super().tearDown()

    def test_every_poster_of_a_changed_entry_is_dropped(self):
        for key in ("anilist:2:tt0000100:100:series:h", "kitsu:20:tt0000100:100:series:h",
                    "tt0000100:100:series:h", "kitsu:10:tt9:999:series:h", "tt5:5:series:h"):
            cache.set_cached_final_poster(key, b"x")
        cache.invalidate_trending_turnover("anime", {"anilist:2"})
        left = {k for (k,) in cache.get_db().execute("SELECT cache_key FROM final_poster_cache")}
        self.assertEqual(left, {"kitsu:10:tt9:999:series:h", "tt5:5:series:h"})


class OneListPerTitleTests(_TempTable):
    """A title on an anime list is on no TMDB list, whatever its language."""

    def setUp(self):
        super().setUp()
        self._load(MAP + [{"type": "ONA", "kitsu_id": 50, "anilist_id": 5, "mal_id": 105,
                            "themoviedb_id": {"tv": 500}}])     # Korean, say
        self._db = tempfile.TemporaryDirectory()
        self._saved_db = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._db.name, "cache.db")
        cache._local.conn = None
        cache.init_db()
        tmdb._trending_source_failed_at.clear()

    def tearDown(self):
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved_db
        self._db.cleanup()
        super().tearDown()

    def test_anime_list_titles_leave_whatever_their_language(self):
        cache.set_cached_trending_snapshot("anime", {"anilist:5": 1}, "anilist")
        on = tmdb._anime_list_tmdb_ids("tv")
        self.assertEqual(on, {"500"})
        self.assertEqual(tmdb._without_anime("tv", ["500", "8"], {"500": {"lang": "ko"}}, on), ["8"])

    def test_an_overlapping_list_is_rebuilt(self):
        with mock.patch.object(tmdb, "anime_split", lambda: True):
            sig = tmdb.trending_source_signature("tv")
        cache.set_cached_trending_snapshot("tv", {"500": 1, "8": 2}, sig)
        cache.set_cached_trending_snapshot("anime", {"anilist:5": 1}, "anilist")
        tmdb._expire_overlapping_lists()
        self.assertIsNone(cache.get_cached_trending_snapshot_entry("tv", sig))
        self.assertIsNotNone(cache.get_cached_trending_snapshot_entry("tv", include_stale=True))

    def test_tmdb_list_is_built_after_the_anime_ones(self):
        async def anilist(client, details_out=None, films=False, limit=None):
            return [] if films else ["anilist:5"]

        async def tmdb_ids(client, key, endpoint, details_out=None):
            for i in ("500", "8"):
                details_out[i] = {"lang": "ko"}
            return ["500", "8"]
        with mock.patch.object(tmdb, "anime_split", lambda: True), \
             mock.patch.object(tmdb, "TRENDING_SOURCE_TV", ""), \
             mock.patch.object(tmdb, "TRENDING_SOURCE_ANIME", ""), \
             mock.patch.object(tmdb, "TRENDING_SOURCE_ANIME_MOVIE", ""), \
             mock.patch("anime.fetch_anilist_trending", anilist), \
             mock.patch.object(tmdb, "_fetch_tmdb_trending_ids", tmdb_ids), \
             mock.patch.object(tmdb, "_TRENDING_RETRY_DELAY_SECS", 0):
            rankings, _ = asyncio.run(tmdb.ensure_trending_snapshot(None, "k", "tv"))
        self.assertEqual(rankings, {"8": 1})


class ListVersionTests(_AddonTest):
    """A catalog's poster URLs name the list they were cut from, so a rebuilt
    list reaches a client that holds the old images for their max-age."""

    def poster_urls(self):
        from urllib.parse import parse_qs, urlsplit
        metas = self.client.get("/trending/sekrit/cfg-dHJlbmRpbmdfc3R5bGU9cmliYm9u/catalog/movie/pp.trending.movie.json").json()["metas"]
        return [parse_qs(urlsplit(m["poster"]).query) for m in metas]

    def test_a_new_order_is_a_new_url(self):
        self._store_with_details("movie", ["11", "22"], {}, TMDB_SIG)
        first = self.poster_urls()
        self.assertEqual(len({q["rv"][0] for q in first}), 1)
        self.assertEqual(self.poster_urls(), first)                       # same list, same URLs
        self._store_with_details("movie", ["22", "11"], {}, TMDB_SIG)
        self.assertNotEqual(self.poster_urls()[0]["rv"], first[0]["rv"])

    def test_the_render_ignores_it(self):
        self.assertEqual(main._render_config_signature(main.build_request_config({"rv": "abc"})),
                         main._render_config_signature(main.build_request_config({})))


class CatalogMaxAgeTests(_AddonTest):
    def test_a_catalog_is_held_briefly(self):
        self._store_with_details("movie", ["11"], {}, TMDB_SIG)
        cc = self.client.get("/trending/sekrit/catalog/movie/pp.trending.movie.json").headers["cache-control"]
        self.assertEqual(cc, f"public, max-age={main._CATALOG_MAX_AGE}")
