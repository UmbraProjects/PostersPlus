"""Key-less configurator search: Cinemeta stands in for TMDB's search, the
TMDB id is resolved on selection, and a verbatim {tmdb_id} placeholder is
read as absent so an IMDb id alone renders."""
import asyncio
import unittest
from unittest import mock

import main
import cinemeta


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class CinemetaSearchTests(unittest.TestCase):
    def test_rows_are_tmdb_shaped_and_interleaved(self):
        async def fake_get(url, **_):
            if "/catalog/movie/" in url:
                return _Resp(200, {"metas": [
                    {"id": "tt1", "imdb_id": "tt1", "type": "movie", "name": "A", "releaseInfo": "2019", "poster": "https://x/a.jpg"},
                    {"id": "tt3", "type": "movie", "name": "C", "releaseInfo": "2001–"},
                    {"id": "notimdb", "type": "movie", "name": "junk"},
                ]})
            return _Resp(200, {"metas": [
                {"id": "tt2", "imdb_id": "tt2", "type": "series", "name": "B", "releaseInfo": "2022"},
            ]})
        client = mock.Mock(); client.get = fake_get
        rows = asyncio.run(cinemeta.search(client, "q"))
        self.assertEqual([r["imdb_id"] for r in rows], ["tt1", "tt2", "tt3"])
        self.assertEqual(rows[0]["media_type"], "movie")
        self.assertEqual(rows[0]["title"], "A")
        self.assertEqual(rows[0]["release_date"], "2019-01-01")
        self.assertEqual(rows[0]["poster_url"], "https://x/a.jpg")
        self.assertIsNone(rows[0]["id"])
        self.assertEqual(rows[1]["media_type"], "tv")
        self.assertEqual(rows[1]["name"], "B")
        self.assertEqual(rows[1]["first_air_date"], "2022-01-01")
        self.assertEqual(rows[2]["release_date"], "2001-01-01")

    def test_a_failed_catalogue_is_just_empty(self):
        async def fake_get(url, **_):
            if "/catalog/movie/" in url:
                raise RuntimeError("boom")
            return _Resp(503, {})
        client = mock.Mock(); client.get = fake_get
        self.assertEqual(asyncio.run(cinemeta.search(client, "q")), [])
        self.assertEqual(asyncio.run(cinemeta.search(client, "  ")), [])


class PlaceholderTests(unittest.TestCase):
    def test_tmdb_id_placeholder_reads_as_absent(self):
        for literal in ("{tmdb_id}", "{tmdb_id?}", " {tmdb_id} "):
            self.assertEqual(main._normalise_optional_id(literal, "tmdb_id"), "")
        self.assertEqual(main._normalise_optional_id("496243", "tmdb_id"), "496243")
        self.assertEqual(main._normalise_optional_id("{imdb_id}", "tmdb_id"), "{imdb_id}")
        src = open("main.py", encoding="utf-8").read()
        self.assertEqual(src.count('_normalise_optional_id(tmdb_id, "tmdb_id")'), 2)


class ConfiguratorKeylessTests(unittest.TestCase):
    def test_search_and_preview_no_longer_require_a_key(self):
        html = open("configurator.html", encoding="utf-8").read()
        self.assertNotIn("Enter your TMDB API key above to search", html)
        self.assertIn("async function fetchTmdbId(", html)
        self.assertIn("if (tmdbId) params.set('tmdb_id', tmdbId);", html)
        self.assertIn("if (resolvedTmdbId || resolvedImdbId) loadPreview();", html)
        self.assertIn("if (!data.tmdbId && !data.imdbId) return;", html)


if __name__ == "__main__":
    unittest.main()


class CinemetaStructureTests(unittest.TestCase):
    def test_episode_summary_matches_tmdb_shape(self):
        videos = [
            {"season": 0, "episode": 1, "released": "2010-01-01T00:00:00.000Z"},   # special, ignored
            {"season": 1, "episode": 1, "released": "2020-01-05T00:00:00.000Z"},
            {"season": 1, "episode": 2, "released": "2020-01-12T00:00:00.000Z"},
            {"season": 2, "episode": 1, "released": "2021-03-01T00:00:00.000Z"},
            {"season": 2, "episode": 2, "released": "2999-03-08T00:00:00.000Z"},   # future
        ]
        s = cinemeta.summarise_episodes(videos)
        self.assertEqual(s["number_of_seasons"], 2)
        self.assertEqual(s["number_of_episodes"], 4)
        self.assertEqual([x["episode_count"] for x in s["seasons"]], [2, 2])
        self.assertEqual(s["seasons"][1]["air_date"], "2021-03-01")
        self.assertEqual(s["last_episode"], {"season_number": 2, "episode_number": 1, "air_date": "2021-03-01"})
        self.assertEqual(s["next_episode"], {"season_number": 2, "episode_number": 2, "air_date": "2999-03-08"})
        self.assertIsNone(cinemeta.summarise_episodes([]))
        self.assertIsNone(cinemeta.summarise_episodes(None))

    def test_normalise_carries_dates_and_structure(self):
        meta = {
            "id": "tt1", "name": "T", "year": "2019", "released": "2019-11-08T00:00:00.000Z",
            "dvdRelease": "2020-01-28T00:00:00.000Z", "runtime": "133 min", "poster": "x", "background": "y",
            "videos": [{"season": 1, "episode": 1, "released": "2019-11-08T00:00:00.000Z"}],
        }
        *_, tmdb_data = cinemeta.normalise(meta, "tt1")
        self.assertEqual(tmdb_data["cinemeta_theatrical_date"], "2019-11-08")
        self.assertEqual(tmdb_data["cinemeta_physical_date"], "2020-01-28")
        self.assertEqual(tmdb_data["number_of_seasons"], 1)
        self.assertEqual(tmdb_data["number_of_episodes"], 1)

    def test_release_status_rescue_is_wired_for_the_cinemeta_spine(self):
        src = open("main.py", encoding="utf-8").read()
        self.assertIn('elif use_cinemeta and tmdb_data.get("cinemeta_theatrical_date"):', src)
        self.assertIn("_trending_by_tmdb = bool(has_tmdb_id and (effective_tmdb_key or trending_source_url(type)))", src)


class MdblistReleaseDatesTests(unittest.TestCase):
    def test_digital_date_is_remembered_and_read_back(self):
        import ratings, cache
        store = {}
        def fake_set(key, value, ttl): store[key] = (value, ttl)
        def fake_get(key): return store.get(key, (None,))[0]
        with mock.patch.object(cache, "set_cached_tvdb_json", fake_set), \
             mock.patch.object(cache, "get_cached_tvdb_json", fake_get):
            ratings.remember_mdblist_release_dates("tt1", "movie",
                {"released": "2026-03-15", "released_digital": "2026-05-12T00:00:00"})
            self.assertEqual(ratings.mdblist_release_dates("tt1", "movie"),
                             {"released": "2026-03-15", "released_digital": "2026-05-12"})
            self.assertEqual(store["mdblist_dates:movie:tt1"][1], ratings._MDBLIST_DATES_TTL_KNOWN)
            # No digital date yet: kept briefly so it is re-asked soon.
            ratings.remember_mdblist_release_dates("tt2", "movie", {"released": "2026-09-01"})
            self.assertEqual(store["mdblist_dates:movie:tt2"][1], ratings._MDBLIST_DATES_TTL_PENDING)
            # Series and empty records write nothing; series read nothing.
            ratings.remember_mdblist_release_dates("tt3", "tv", {"released": "2026-09-01"})
            ratings.remember_mdblist_release_dates("tt4", "movie", {})
            self.assertNotIn("mdblist_dates:show:tt3", store)
            self.assertNotIn("mdblist_dates:movie:tt4", store)
            self.assertIsNone(ratings.mdblist_release_dates("tt1", "tv"))
            self.assertIsNone(ratings.mdblist_release_dates(None, "movie"))

    def test_keyless_status_uses_the_digital_date(self):
        import tmdb
        from datetime import date, timedelta
        past = (date.today() - timedelta(days=60)).isoformat()
        digital = (date.today() - timedelta(days=10)).isoformat()
        self.assertEqual(tmdb._compute_movie_status_from_dates(
            tmdb._parse_tmdb_date(past), tmdb._parse_tmdb_date(digital), None, None), "Streaming")
        self.assertEqual(tmdb._compute_movie_status_from_dates(
            tmdb._parse_tmdb_date(past), None, None, None), "Cinema")
        src = open("main.py", encoding="utf-8").read()
        self.assertIn('_mdb_dates.get("released_digital")', src)


class AssumedDigitalWindowTests(unittest.TestCase):
    def test_setting_and_wiring(self):
        import config
        self.assertEqual(config.CINEMA_ASSUMED_DIGITAL_DAYS, 60)
        src = open("main.py", encoding="utf-8").read()
        block = src[src.index('elif use_cinemeta and tmdb_data.get("cinemeta_theatrical_date"):'):]
        block = block[:block.index("# r/movieleaks confirmation")]
        # Only a "Cinema" verdict with no digital date is ever promoted, and
        # only past the window; 0 disables it.
        self.assertIn('_release_status == "Cinema" and _cm_digital is None', block)
        self.assertIn("_cfg.CINEMA_ASSUMED_DIGITAL_DAYS > 0", block)
        self.assertIn(".days > _cfg.CINEMA_ASSUMED_DIGITAL_DAYS", block)
        self.assertIn('_release_status = "Streaming"', block)


class KeyChangeCompositeTests(unittest.TestCase):
    def test_mdblist_less_composites_are_keyed_apart(self):
        # Adding an MDBList key later must re-render, not serve N/A for a TTL.
        src = open("main.py", encoding="utf-8").read()
        self.assertIn('_mdb_sig = "|mdb=0" if (not effective_mdblist_key and not is_anime) else ""', src)
        self.assertIn("+ _spine_sig\n                + _mdb_sig\n                + _tmdb_sig", src)
        # Anime keeps its provider spine without a TMDB key, so the key's
        # absence (no TMDB logos) has to be in the composite key on its own.
        self.assertIn('_tmdb_sig = "|tmdb=0" if (not effective_tmdb_key and is_anime) else ""', src)
        # And the Cinemeta spine is keyed apart from the TMDB one already.
        self.assertIn('_spine_sig = "|art=cinemeta" if use_cinemeta else ""', src)

    def test_keyless_status_reuses_tmdb_dates_left_by_a_removed_key(self):
        src = open("main.py", encoding="utf-8").read()
        self.assertIn('get_cached_movie_release_info(f"movie_{tmdb_id}") or {}) if has_tmdb_id else {}', src)
        self.assertIn('_tmdb_dates.get("digital_date") or _mdb_dates.get("released_digital")', src)
