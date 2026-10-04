"""TMDB ids for anime the community mapping doesn't know yet (anime_resolve)."""

import asyncio
import unittest
from unittest import mock

import anime_ids
import anime_resolve as ar


def _media(anilist_id, english=None, romaji=None, year=2026, prequel=None, fmt="TV"):
    edges = [{"relationType": "PREQUEL", "node": {"id": prequel, "type": "ANIME", "format": "TV"}}] if prequel else []
    return {"id": anilist_id, "format": fmt, "title": {"english": english, "romaji": romaji},
            "synonyms": [], "startDate": {"year": year}, "relations": {"edges": edges}}


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.cache = {}
        self.metadata = {}

        async def metadata(client, namespace, anime_id):
            lookup = self.metadata.get(anime_id)
            return None if lookup is None else ([16], False, [], None, "", None, None,
                                                {"anime_lookup": lookup})
        for p in (
            mock.patch.object(ar, "get_cached_tvdb_json", self.cache.get),
            mock.patch.object(ar, "set_cached_tvdb_json", lambda k, v, ttl: self.cache.__setitem__(k, v)),
            mock.patch.object(ar.anime, "fetch_anime_metadata", metadata),
            mock.patch.object(ar.anime, "_anilist_quiet_until", 0.0),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, media_by_id, mapped, search=None, namespace="anilist", anime_id=2, media_type="series"):
        async def anilist(client, anilist_id=None, mal_id=None):
            return media_by_id.get(anilist_id)

        def lookup(ns, aid, mt):
            return mapped.get(aid)

        async def search_tmdb(client, key, kind, media):
            return (search or {}).get(media["id"])

        with mock.patch.object(ar, "_anilist", anilist), \
                mock.patch.object(ar.anime_ids, "lookup", lookup), \
                mock.patch.object(ar, "_search_tmdb", search_tmdb):
            return asyncio.run(ar.resolve(None, namespace, anime_id, media_type, "key"))

    def test_a_later_season_takes_its_prequels_ids(self):
        # TOUGEN ANKI's second cour (204650) -> the first (177474), mapped.
        media = {204650: _media(204650, "TOUGEN ANKI: Nikko Kegon Falls Arc", prequel=177474),
                 177474: _media(177474, "TOUGEN ANKI", year=2025)}
        mapped = {177474: anime_ids.MappedIds("253811", "tt32344704")}
        got = self._run(media, mapped, anime_id=204650)
        self.assertEqual(got, anime_ids.MappedIds("253811", "tt32344704"))
        self.assertEqual(self.cache["animeres:v1:anilist:204650:tv"]["via"], "prequel:1")

    def test_a_chain_of_seasons_is_walked(self):
        media = {3: _media(3, prequel=2), 2: _media(2, prequel=1), 1: _media(1)}
        got = self._run(media, {1: anime_ids.MappedIds("99", None)}, anime_id=3)
        self.assertEqual(got.tmdb_id, "99")

    def test_a_new_show_by_name(self):
        media = {212888: _media(212888, "Overgeared", "Tempal: Item no Chikara")}
        got = self._run(media, {}, search={212888: "324502"}, anime_id=212888)
        self.assertEqual(got.tmdb_id, "324502")

    def test_films_do_not_follow_prequels(self):
        media = {5: _media(5, prequel=4, fmt="MOVIE"), 4: _media(4)}
        self.assertIsNone(self._run(media, {4: anime_ids.MappedIds("1", None)},
                                    anime_id=5, media_type="movie"))

    def test_misses_are_cached_and_not_retried(self):
        calls = []

        async def anilist(client, anilist_id=None, mal_id=None):
            calls.append(anilist_id)
            return _media(anilist_id)

        async def no_search(*a):
            return None
        with mock.patch.object(ar, "_anilist", anilist), \
                mock.patch.object(ar.anime_ids, "lookup", lambda *a: None), \
                mock.patch.object(ar, "_search_tmdb", no_search):
            self.assertIsNone(asyncio.run(ar.resolve(None, "anilist", 7, "series", "k")))
            self.assertIsNone(asyncio.run(ar.resolve(None, "anilist", 7, "series", "k")))
        self.assertEqual(calls, [7])

    def test_the_renders_cached_metadata_saves_an_anilist_call(self):
        calls = []

        async def anilist(client, anilist_id=None, mal_id=None):
            calls.append(anilist_id)
            return None
        self.metadata[204650] = _media(204650, "TOUGEN ANKI: Nikko Kegon Falls Arc", prequel=177474)

        async def no_search(*a):
            return None
        with mock.patch.object(ar, "_anilist", anilist), \
                mock.patch.object(ar.anime_ids, "lookup",
                                  lambda ns, aid, mt: anime_ids.MappedIds("253811", None) if aid == 177474 else None), \
                mock.patch.object(ar, "_search_tmdb", no_search):
            got = asyncio.run(ar.resolve(None, "anilist", 204650, "series", "k"))
        self.assertEqual(got.tmdb_id, "253811")
        self.assertEqual(calls, [])

    def test_anilists_wait_is_honoured(self):
        calls = []

        async def anilist(client, anilist_id=None, mal_id=None):
            calls.append(anilist_id)
            return _media(anilist_id)
        ar.anime.note_anilist_throttle("48")
        self.assertTrue(ar.anime.anilist_cooling())
        with mock.patch.object(ar, "_anilist", anilist):
            self.assertIsNone(asyncio.run(ar.resolve(None, "anilist", 9, "series", "k")))
        self.assertEqual(calls, [])
        self.assertEqual(self.cache["animeres:v1:anilist:9:tv"], {})

    def test_throttling_is_held_only_briefly(self):
        ttls = {}

        async def throttled(*a, **k):
            raise ar._Transient("AniList 429")
        with mock.patch.object(ar, "_anilist", throttled), \
                mock.patch.object(ar, "set_cached_tvdb_json", lambda k, v, ttl: ttls.__setitem__(k, ttl)):
            self.assertIsNone(asyncio.run(ar.resolve(None, "anilist", 8, "series", "k")))
        self.assertEqual(ttls, {"animeres:v1:anilist:8:tv": ar._FAIL_TTL})


class SearchTests(unittest.TestCase):
    def _search(self, results, media):
        import httpx

        def handler(request):
            return httpx.Response(200, json={"results": results})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await ar._search_tmdb(client, "k", "tv", media)
        return asyncio.run(go())

    def test_only_an_exact_animated_name_in_the_right_year(self):
        media = _media(1, "Overgeared", "Tempal: Item no Chikara", year=2026)
        hit = {"id": 324502, "name": "Overgeared", "original_name": "テムパル",
               "genre_ids": [16, 10759], "first_air_date": "2026-10-02"}
        self.assertEqual(self._search([hit], media), "324502")
        self.assertIsNone(self._search([{**hit, "genre_ids": [18]}], media))           # live action
        self.assertIsNone(self._search([{**hit, "first_air_date": "2019-01-01"}], media))  # another show
        self.assertIsNone(self._search([{**hit, "name": "Overgeared Returns"}], media))   # near miss

    def test_names_compare_without_case_or_punctuation(self):
        self.assertEqual(ar._norm("A Wild Last Boss Appeared!"), ar._norm("a wild last-boss appeared"))


if __name__ == "__main__":
    unittest.main()
