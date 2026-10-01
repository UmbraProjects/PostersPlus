"""The Kitsu/AniList -> TMDB/IMDb mapping that lets an anime request which
only carries its anime id (Nuvio's own pattern resolver sends "kitsu:7442" and
nothing else) render the same poster as one that came through AIOMetadata with
tmdb_id and imdb_id alongside.

Without it, a kitsu-only landscape request had no backdrop to draw on — the
anime providers ship one cover image and nothing else — and fell through to
the genre canvas.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

import anime_ids

# Shapes lifted from the real list: themoviedb_id is a {kind: id} object,
# imdb_id a list, and most entries carry only some of the ids.
SAMPLE = [
    {"type": "TV", "kitsu_id": 12, "anilist_id": 21, "mal_id": 20,
     "themoviedb_id": {"tv": 37854}, "imdb_id": ["tt0388629"]},
    {"type": "ONA", "kitsu_id": 49847, "anilist_id": 190327,
     "themoviedb_id": {"tv": 45790}, "imdb_id": ["tt2359704"],
     "season": {"tvdb": 6, "tmdb": 6}},
    {"type": "MOVIE", "kitsu_id": 1376, "anilist_id": 199,
     "themoviedb_id": {"movie": 129}, "imdb_id": ["tt0245429"]},
    {"type": "TV", "kitsu_id": 7442, "imdb_id": ["tt2560140"]},        # no TMDB
    {"type": "TV", "kitsu_id": 99999, "mal_id": 5},                   # nothing usable
    {"type": "TV", "anilist_id": 555, "mal_id": 30,
     "themoviedb_id": {"tv": 1}},                                     # anilist only
    {"type": "TV", "mal_id": 40, "themoviedb_id": {"tv": 2}},         # no provider id
    {"type": "TV", "kitsu_id": 12, "themoviedb_id": {"tv": 777}},     # duplicate id
]


class _TempTable(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (anime_ids.ANIME_ID_MAP_PATH, anime_ids.ANIME_ID_MAP_ENABLED,
                       anime_ids._row_count)
        anime_ids.ANIME_ID_MAP_PATH = os.path.join(self._dir.name, "anime_ids.db")
        anime_ids.ANIME_ID_MAP_ENABLED = True
        anime_ids._local.conn = None

    def tearDown(self):
        conn = getattr(anime_ids._local, "conn", None)
        if conn is not None:
            conn.close()
        anime_ids._local.conn = None
        (anime_ids.ANIME_ID_MAP_PATH, anime_ids.ANIME_ID_MAP_ENABLED,
         anime_ids._row_count) = self._saved
        self._dir.cleanup()

    def _load(self, entries=SAMPLE) -> int:
        body = json.dumps(entries).encode()
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                return await anime_ids.refresh_mapping(client)
        return asyncio.run(go())


class MappingTests(_TempTable):
    def test_a_kitsu_only_request_gets_the_ids_aiometadata_would_send(self):
        self._load()
        self.assertEqual(anime_ids.lookup("kitsu", 12, "series"),
                         anime_ids.MappedIds("37854", "tt0388629"))
        self.assertEqual(anime_ids.lookup("anilist", 21, "tv"),
                         anime_ids.MappedIds("37854", "tt0388629"))

    def test_a_sequel_season_maps_to_its_parent_show(self):
        self._load()
        self.assertEqual(anime_ids.lookup("kitsu", 49847, "series").tmdb_id, "45790")

    def test_the_tmdb_id_must_be_the_kind_being_rendered(self):
        # A movie id fetched as a series (or the reverse) is another title.
        self._load()
        self.assertEqual(anime_ids.lookup("kitsu", 1376, "movie").tmdb_id, "129")
        movie_as_series = anime_ids.lookup("kitsu", 1376, "series")
        self.assertIsNone(movie_as_series.tmdb_id)
        self.assertEqual(movie_as_series.imdb_id, "tt0245429")

    def test_partial_and_empty_entries(self):
        self._load()
        self.assertEqual(anime_ids.lookup("kitsu", 7442, "series"),
                         anime_ids.MappedIds(None, "tt2560140"))
        self.assertIsNone(anime_ids.lookup("kitsu", 99999, "series"))
        self.assertIsNone(anime_ids.lookup("kitsu", 424242, "series"))
        self.assertEqual(anime_ids.lookup("anilist", 555, "series").tmdb_id, "1")

    def test_first_entry_wins_on_a_duplicate_id(self):
        self._load()
        self.assertEqual(anime_ids.lookup("kitsu", 12, "series").tmdb_id, "37854")

    def test_only_the_namespaces_we_source_art_from(self):
        self._load()
        self.assertIsNone(anime_ids.lookup("mal", 5, "series"))

    def test_disabled_is_inert(self):
        self._load()
        anime_ids.ANIME_ID_MAP_ENABLED = False
        self.assertIsNone(anime_ids.lookup("kitsu", 12, "series"))

    def test_an_empty_or_broken_download_keeps_the_previous_table(self):
        self.assertEqual(self._load(), 8)   # one row per (namespace, id)
        self.assertEqual(self._load([]), 0)
        self.assertIn("mapped nothing", anime_ids.status()["last_refresh_error"])
        self.assertEqual(anime_ids.lookup("kitsu", 12, "series").tmdb_id, "37854")

    def test_before_the_first_download_nothing_is_mapped(self):
        anime_ids.init_db()
        self.assertFalse(anime_ids.is_ready())
        self.assertIsNone(anime_ids.lookup("kitsu", 12, "series"))


class MalTranslationTests(_TempTable):
    """MAL is never an art source (its API needs auth); its id is rendered as
    the provider id of the same entry."""

    def test_kitsu_is_preferred_for_its_larger_covers(self):
        self._load()
        self.assertEqual(anime_ids.mal_to_provider(20), ("kitsu", 12))

    def test_anilist_when_the_entry_has_no_kitsu_id(self):
        self._load()
        self.assertEqual(anime_ids.mal_to_provider(30), ("anilist", 555))

    def test_a_provider_id_alone_is_enough(self):
        # No TMDB or IMDb id, so no anime_id_map row — but Kitsu can render it.
        self._load()
        self.assertEqual(anime_ids.mal_to_provider(5), ("kitsu", 99999))

    def test_unknown_or_providerless_ids_are_not_mapped(self):
        self._load()
        self.assertIsNone(anime_ids.mal_to_provider(40))
        self.assertIsNone(anime_ids.mal_to_provider(424242))

    def test_disabled_or_not_yet_downloaded_is_inert(self):
        self.assertIsNone(anime_ids.mal_to_provider(20))
        self._load()
        anime_ids.ANIME_ID_MAP_ENABLED = False
        self.assertIsNone(anime_ids.mal_to_provider(20))

    def test_a_reload_replaces_the_mal_table_too(self):
        self._load()
        self._load([{"kitsu_id": 1, "mal_id": 20, "themoviedb_id": {"tv": 3}}])
        self.assertEqual(anime_ids.mal_to_provider(20), ("kitsu", 1))
        self.assertIsNone(anime_ids.mal_to_provider(30))


class RequestWiringTests(unittest.TestCase):
    def test_get_poster_fills_only_the_ids_the_request_lacks(self):
        src = Path("main.py").read_text(encoding="utf-8")
        block = src[src.index("    if is_anime:\n        # A client that resolves its own pattern"):]
        block = block[:block.index("has_tmdb_id = bool(tmdb_id)")]
        self.assertIn("anime_ids.lookup(anime_namespace, anime_id, type)", block)
        self.assertIn("tmdb_id = tmdb_id or _mapped.tmdb_id", block)
        self.assertIn("imdb_id = imdb_id or _mapped.imdb_id", block)
        # Filled before the format checks and before canonical_id / the
        # composite cache key read them, so a mapped request keys like the
        # AIOMetadata request it now matches.
        self.assertLess(block.index("anime_ids.lookup"), block.index("_check_imdb_id"))


if __name__ == "__main__":
    unittest.main()
