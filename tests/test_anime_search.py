"""Anime seasons and titles in the configurator's search (anime_search, /search)."""

import asyncio
import json
import unittest
from unittest import mock

import httpx

import anime_ids
import anime_search as srch

# Kitsu id -> (mapped TMDB id or None, IMDb id, SeasonPlace or None)
MAPPING = {
    42765: ("95479", "tt12343534", None),                              # JJK, season 1
    45857: ("95479", "tt12343534", anime_ids.SeasonPlace(2, 0)),       # JJK Season 2
    47880: ("95479", "tt12343534", anime_ids.SeasonPlace(0, 0)),       # a special
    45619: ("120089", None, anime_ids.SeasonPlace(1, 12)),             # Spy x Family cour 2
    44212: ("810693", "tt14331144", None),                             # JJK 0, a film
}


def _entry(kitsu_id, title, subtype="TV", date="2023-07-06", en=None):
    titles = {"en_jp": title}
    if en:
        titles["en"] = en
    return {"id": str(kitsu_id), "type": "anime", "attributes": {
        "canonicalTitle": title, "titles": titles, "subtype": subtype, "startDate": date,
        "posterImage": {"small": f"https://media.kitsu.app/anime/{kitsu_id}/small.jpg"}}}


def _tmdb(tmdb_id, name, media_type="tv"):
    return {"id": tmdb_id, "media_type": media_type, "name": name, "first_air_date": "2020-10-03"}


class MergeTests(unittest.TestCase):
    def setUp(self):
        def lookup(ns, kid, mt):
            row = MAPPING.get(kid)
            return None if row is None else anime_ids.MappedIds(row[0], row[1])

        def place(ns, kid):
            row = MAPPING.get(kid)
            return None if row is None else row[2]
        for p in (mock.patch.object(srch.anime_ids, "lookup", lookup),
                  mock.patch.object(srch.anime_ids, "season_place", place)):
            p.start()
            self.addCleanup(p.stop)

    def test_later_seasons_sit_under_their_show_and_season_one_is_dropped(self):
        out = srch.merge(
            [_tmdb(95479, "JUJUTSU KAISEN"), _tmdb(810693, "Jujutsu Kaisen 0", "movie")],
            [_entry(42765, "Jujutsu Kaisen", date="2020-10-03"),
             _entry(45857, "Jujutsu Kaisen Season 2"),
             _entry(44212, "Jujutsu Kaisen 0", "movie"),
             _entry(43814, "Kaikai Kitan", "music")],
        )
        self.assertEqual([r.get("anime_id") or r["id"] for r in out],
                         [95479, "kitsu:45857", 810693])
        season = out[1]
        self.assertEqual((season["anime_label"], season["id"], season["imdb_id"]),
                         ("Kitsu · Season", 95479, "tt12343534"))
        self.assertTrue(season["anime_nested"])

    def test_titles_tmdb_lacks_come_after_its_results(self):
        out = srch.merge([_tmdb(95479, "JUJUTSU KAISEN")],
                         [_entry(99001, "Some Kitsu Only Show", en="Some Show")])
        self.assertEqual(out[1]["anime_id"], "kitsu:99001")
        self.assertEqual((out[1]["id"], out[1]["name"], out[1]["anime_label"]),
                         (None, "Some Show", "Kitsu"))
        self.assertFalse(out[1]["anime_season"] or out[1]["anime_nested"])

    def test_a_show_tmdb_missed_is_offered_from_kitsu_with_its_seasons(self):
        # "solo levelling": TMDB matches nothing; Kitsu has season 1 and 2.
        out = srch.merge([], [_entry(45857, "Jujutsu Kaisen Season 2"),
                              _entry(42765, "Jujutsu Kaisen", date="2020-10-03")])
        self.assertEqual([(r["anime_id"], r["anime_nested"]) for r in out],
                         [("kitsu:42765", False), ("kitsu:45857", True)])
        self.assertEqual((out[0]["id"], out[0]["anime_label"]), (95479, "Kitsu"))

    def test_a_season_whose_show_isnt_listed_stands_on_its_own(self):
        # "solo levelling": TMDB matches nothing, Kitsu the second season.
        out = srch.merge([], [_entry(45857, "Jujutsu Kaisen Season 2")])
        self.assertEqual([r["anime_id"] for r in out], ["kitsu:45857"])
        self.assertFalse(out[0]["anime_nested"])

    def test_an_unlinked_title_tmdb_lists_by_name_isnt_repeated(self):
        tmdb_film = {"id": 1357633, "media_type": "movie", "title": "Solo Leveling -ReAwakening-",
                     "release_date": "2024-11-26"}
        out = srch.merge([tmdb_film], [
            _entry(49220, "Solo Leveling: ReAwakening", "movie", date="2024-12-06"),
            _entry(50837, "Solo Leveling: Beyond the System", "movie", date=""),
            _entry(99001, "Solo Leveling -ReAwakening-", "movie", date="2019-01-01"),
        ])
        self.assertEqual([r.get("anime_id") or r["id"] for r in out],
                         [1357633, "kitsu:50837", "kitsu:99001"])   # another film by year

    def test_cinemeta_rows_match_by_imdb_id(self):
        cinemeta = {"id": None, "imdb_id": "tt12343534", "media_type": "tv", "name": "Jujutsu Kaisen"}
        out = srch.merge([cinemeta], [_entry(45857, "Jujutsu Kaisen Season 2")])
        self.assertEqual(out[1]["anime_id"], "kitsu:45857")

    def test_labels(self):
        out = srch.merge([], [_entry(47880, "JJK Recap", "special"), _entry(45619, "Spy x Family 2")])
        self.assertEqual([r["anime_label"] for r in out], ["Kitsu · Special", "Kitsu · Season"])

    def test_unplaced_specials_are_left_out_and_unlinked_rows_capped(self):
        entries = [_entry(99000, "A Recap", "special")] + [
            _entry(99001 + n, f"Kitsu Only {n}") for n in range(6)] + [
            _entry(45857, "Jujutsu Kaisen Season 2")]
        out = srch.merge([], entries)
        self.assertEqual(out[0]["anime_id"], "kitsu:45857")      # seasons first
        self.assertEqual(len(out), 1 + srch._UNLINKED_ROWS)
        self.assertNotIn("kitsu:99000", [r["anime_id"] for r in out])

    def test_people_and_extra_rows_are_trimmed(self):
        people = [{"id": 1, "media_type": "person", "name": "Someone"}]
        shows = [_tmdb(n, f"Show {n}") for n in range(1, 15)]
        out = srch.merge(people + shows, [])
        self.assertEqual(len(out), srch._TMDB_ROWS)
        self.assertNotIn("person", {r["media_type"] for r in out})


class KitsuSearchTests(unittest.TestCase):
    def setUp(self):
        self.cache = {}
        for p in (mock.patch.object(srch, "get_cached_tvdb_json", self.cache.get),
                  mock.patch.object(srch, "set_cached_tvdb_json",
                                    lambda k, v, ttl: self.cache.__setitem__(k, v))):
            p.start()
            self.addCleanup(p.stop)

    def _search(self, handler, q="Jujutsu  Kaisen"):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await srch.kitsu_search(client, q)
        return asyncio.run(go())

    def test_results_are_cached_per_query(self):
        seen = []

        def handler(request):
            seen.append(request.url.params["filter[text]"])
            return httpx.Response(200, json={"data": [_entry(45857, "Jujutsu Kaisen Season 2")]})
        self.assertEqual(len(self._search(handler)), 1)
        self.assertEqual(len(self._search(handler, "jujutsu kaisen")), 1)
        self.assertEqual(seen, ["jujutsu kaisen"])

    def test_an_unreachable_kitsu_adds_nothing_and_isnt_cached(self):
        self.assertEqual(self._search(lambda r: httpx.Response(503)), [])

        def down(request):
            raise httpx.ConnectError("down")
        self.assertEqual(self._search(down), [])
        self.assertEqual(self.cache, {})


class SearchEndpointTests(unittest.TestCase):
    def _run(self, kitsu, tmdb_status=200, enabled=True):
        import main

        async def proxy(url, params):
            body = {"results": [_tmdb(95479, "JUJUTSU KAISEN")]} if tmdb_status == 200 else {"status_message": "x"}
            return httpx.Response(tmdb_status, content=json.dumps(body).encode())

        async def kitsu_search(client, q):
            if isinstance(kitsu, Exception):
                raise kitsu
            return kitsu
        with mock.patch.object(main, "_HTTP_CLIENT", object()), \
                mock.patch.object(main, "_configurator_key_ok", lambda k: True), \
                mock.patch.object(main, "_resolve_tmdb_key", lambda k: "server-key"), \
                mock.patch.object(main, "_proxy_tmdb_get", proxy), \
                mock.patch.object(main.anime_search, "enabled", lambda: enabled), \
                mock.patch.object(main.anime_search, "kitsu_search", kitsu_search), \
                mock.patch.object(main.anime_search.anime_ids, "lookup",
                                  lambda ns, kid, mt: anime_ids.MappedIds("95479", None)), \
                mock.patch.object(main.anime_search.anime_ids, "season_place",
                                  lambda ns, kid: anime_ids.SeasonPlace(2, 0)):
            return asyncio.run(main.search_proxy(q="jujutsu"))

    def test_kitsu_seasons_are_merged_in(self):
        out = self._run([_entry(45857, "Jujutsu Kaisen Season 2")])
        self.assertEqual([r.get("anime_id") for r in out["results"]], [None, "kitsu:45857"])

    def test_without_the_mapping_the_tmdb_answer_passes_through(self):
        out = self._run([_entry(45857, "Jujutsu Kaisen Season 2")], enabled=False)
        self.assertEqual(json.loads(out.body)["results"][0]["id"], 95479)

    def test_a_tmdb_error_passes_through(self):
        out = self._run([], tmdb_status=401)
        self.assertEqual(out.status_code, 401)


if __name__ == "__main__":
    unittest.main()
