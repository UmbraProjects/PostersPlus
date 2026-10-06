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
        self.gone = set()

        async def metadata(client, namespace, anime_id):
            lookup = self.metadata.get(anime_id)
            return None if lookup is None else ([16], False, [], None, "", None, None,
                                                {"anime_lookup": lookup})
        for p in (
            mock.patch.object(ar, "get_cached_tvdb_json", self.cache.get),
            mock.patch.object(ar, "set_cached_tvdb_json", lambda k, v, ttl: self.cache.__setitem__(k, v)),
            mock.patch.object(ar.anime, "fetch_anime_metadata", metadata),
            mock.patch.object(ar.anime, "_anilist_quiet_until", 0.0),
            mock.patch("tmdb.tmdb_id_gone", lambda tid, mt: tid in self.gone),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, media_by_id, mapped, search=None, namespace="anilist", anime_id=2, media_type="series",
             imdb=None):
        async def anilist(client, anilist_id=None, mal_id=None):
            return media_by_id.get(anilist_id)

        def lookup(ns, aid, mt):
            return mapped.get(aid)

        async def search_tmdb(client, key, kind, media):
            return (search or {}).get(media["id"])

        async def tmdb_imdb_id(client, key, kind, tmdb_id):
            self.imdb_asked.append(tmdb_id)
            return (imdb or {}).get(tmdb_id, (None, True))

        self.imdb_asked = []
        with mock.patch.object(ar, "_anilist", anilist), \
                mock.patch.object(ar.anime_ids, "lookup", lookup), \
                mock.patch.object(ar, "_search_tmdb", search_tmdb), \
                mock.patch.object(ar, "_tmdb_imdb_id", tmdb_imdb_id):
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

    def test_a_show_found_by_name_brings_its_imdb_id(self):
        # The anime path reads MDBList by IMDb id only: without it, no ratings.
        media = {212888: _media(212888, "Overgeared", "Tempal: Item no Chikara")}
        got = self._run(media, {}, search={212888: "324502"}, anime_id=212888,
                        imdb={"324502": ("tt43691353", True)})
        self.assertEqual(got, anime_ids.MappedIds("324502", "tt43691353"))
        self.assertEqual(self.imdb_asked, ["324502"])
        # Cached: not asked again.
        self._run(media, {}, search={212888: "324502"}, anime_id=212888)
        self.assertEqual(self.imdb_asked, [])

    def test_a_name_match_cached_without_its_imdb_id_is_looked_up_again(self):
        self.cache["animeres:v1:anilist:212888:tv"] = {"tmdb_id": "324502", "imdb_id": None, "via": "search"}
        media = {212888: _media(212888, "Overgeared")}
        got = self._run(media, {}, search={212888: "324502"}, anime_id=212888,
                        imdb={"324502": ("tt43691353", True)})
        self.assertEqual(got.imdb_id, "tt43691353")
        self.assertTrue(self.cache["animeres:v1:anilist:212888:tv"]["imdb_checked"])

    def test_tmdb_not_answering_for_the_imdb_id_keeps_the_tmdb_id(self):
        media = {6: _media(6, "Some Show")}
        got = self._run(media, {}, search={6: "222"}, anime_id=6, imdb={"222": (None, False)})
        self.assertEqual(got, anime_ids.MappedIds("222", None))
        row = self.cache["animeres:v1:anilist:6:tv"]
        self.assertFalse(row["imdb_checked"])
        # Not asked again inside the retry window; asked once it has passed.
        self._run(media, {}, search={6: "222"}, anime_id=6)
        self.assertEqual(self.imdb_asked, [])
        row["imdb_retry_at"] = 0
        got = self._run(media, {}, search={6: "222"}, anime_id=6, imdb={"222": ("tt9", True)})
        self.assertEqual(got, anime_ids.MappedIds("222", "tt9"))

    def test_an_old_name_match_is_not_resolved_again_from_scratch(self):
        # Only the IMDb id is asked for: a throttled AniList can't cost the
        # title the TMDB id it already has.
        self.cache["animeres:v1:anilist:6:tv"] = {"tmdb_id": "222", "imdb_id": None, "via": "search"}

        async def throttled(*a, **k):
            raise ar._Transient("AniList 429")
        with mock.patch.object(ar, "_resolve", throttled):
            got = self._run({}, {}, anime_id=6, imdb={"222": ("tt9", True)})
        self.assertEqual(got, anime_ids.MappedIds("222", "tt9"))

    def test_a_later_season_found_by_prequel_asks_nothing_of_tmdb(self):
        media = {3: _media(3, prequel=1), 1: _media(1)}
        self._run(media, {1: anime_ids.MappedIds("99", "tt1")}, anime_id=3)
        self.assertEqual(self.imdb_asked, [])

    def test_a_deleted_tmdb_id_in_the_mapping_is_passed_over(self):
        # The AniList id's own row names a TMDB entry since deleted: the name
        # search runs instead of handing the deleted id back.
        self.gone.add("111")
        media = {6: _media(6, "Some Show")}
        got = self._run(media, {6: anime_ids.MappedIds("111", None)}, search={6: "222"}, anime_id=6)
        self.assertEqual(got.tmdb_id, "222")

    def test_a_deleted_prequel_id_is_walked_past(self):
        self.gone.add("111")
        media = {3: _media(3, prequel=2), 2: _media(2, prequel=1), 1: _media(1)}
        mapped = {2: anime_ids.MappedIds("111", None), 1: anime_ids.MappedIds("99", None)}
        self.assertEqual(self._run(media, mapped, anime_id=3).tmdb_id, "99")

    def test_a_cached_hit_since_deleted_is_resolved_again(self):
        media = {6: _media(6, "Some Show")}
        self.assertEqual(self._run(media, {}, search={6: "111"}, anime_id=6).tmdb_id, "111")
        self.gone.add("111")
        self.assertEqual(self._run(media, {}, search={6: "222"}, anime_id=6).tmdb_id, "222")

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
    def _imdb(self, status, body=None, exc=None):
        import httpx

        def handler(request):
            if exc:
                raise exc
            assert request.url.path == "/3/tv/324502/external_ids"
            return httpx.Response(status, json=body or {})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await ar._tmdb_imdb_id(client, "k", "tv", "324502")
        return asyncio.run(go())

    def test_imdb_id_from_external_ids(self):
        import httpx
        self.assertEqual(self._imdb(200, {"imdb_id": "tt43691353"}), ("tt43691353", True))
        self.assertEqual(self._imdb(200, {"imdb_id": None}), (None, True))
        self.assertEqual(self._imdb(404), (None, True))
        self.assertEqual(self._imdb(429), (None, False))
        self.assertEqual(self._imdb(0, exc=httpx.ConnectError("down")), (None, False))

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
