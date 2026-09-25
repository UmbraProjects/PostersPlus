"""Either id identifies a title, and Cinemeta carries the ones TMDB can't.

Three things are pinned here:

- The IMDb -> TMDB resolution an imdb_id-only request goes through, and that
  it lands on the same identity (and so the same composite cache key) a client
  sending both ids would have produced.
- The spine decision: TMDB whenever there is a key and a TMDB id; Cinemeta when
  there is no key, or TMDB has no record for the IMDb id; a clear refusal when
  neither can carry the title.
- That the Cinemeta document is shaped like a TMDB title with no textless
  poster, so the ordinary pipeline renders it unchanged.
"""

import asyncio
import io
import unittest

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import cinemeta
import main
import tmdb


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records every request. HEADs (the
    Metahub art probes) answer from ``head_status`` and are recorded too."""

    def __init__(self, *responses, head_status=200):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []
        self.heads: list[str] = []
        self.head_status = head_status

    async def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    async def head(self, url, **kwargs):
        self.heads.append(url)
        if isinstance(self.head_status, Exception):
            raise self.head_status
        return _FakeResponse(self.head_status)


class _MemoryJsonCache:
    """Swaps the tvdb_cache JSON store for a dict on the named modules."""

    def __init__(self, *modules):
        self.modules = modules
        self.store: dict[str, dict] = {}
        self._saved = []

    def __enter__(self):
        for mod in self.modules:
            self._saved.append((mod, mod.get_cached_tvdb_json, mod.set_cached_tvdb_json))
            mod.get_cached_tvdb_json = lambda key: self.store.get(key)
            mod.set_cached_tvdb_json = lambda key, value, ttl: self.store.__setitem__(key, value)
        return self

    def __exit__(self, *exc):
        for mod, get, set_ in self._saved:
            mod.get_cached_tvdb_json = get
            mod.set_cached_tvdb_json = set_


SHAWSHANK = {
    "id": "tt0111161", "name": "The Shawshank Redemption", "year": "1994",
    "released": "1994-10-14T00:00:00.000Z", "runtime": "142 min",
    "genres": ["Drama", "Crime"], "cast": ["Tim Robbins", "Morgan Freeman"],
    "director": ["Frank Darabont"], "moviedb_id": 278,
    "poster": "https://images.metahub.space/poster/small/tt0111161/img",
    "background": "https://images.metahub.space/background/medium/tt0111161/img",
    "logo": "https://images.metahub.space/logo/medium/tt0111161/img",
}


# ---------------------------------------------------------------------------
# cinemeta.py
# ---------------------------------------------------------------------------

class CinemetaNormaliseTests(unittest.TestCase):
    def test_shape_matches_the_tmdb_metadata_tuple(self):
        result = cinemeta.normalise(SHAWSHANK, "tt0111161")
        self.assertEqual(len(result), 8)
        genre_ids, is_textless, logos, year, title, poster, backdrop, td = result

        self.assertEqual(title, "The Shawshank Redemption")
        self.assertEqual(year, "1994")
        self.assertEqual(genre_ids, [18, 80])
        # A one-sheet with the title baked in, plus a textless-by-design
        # background: shaped like a TMDB title with no textless poster, so the
        # backdrop-to-portrait fallback engages and composites our logo.
        self.assertFalse(is_textless)
        self.assertEqual(logos, [])
        self.assertEqual(poster, "https://images.metahub.space/poster/medium/tt0111161/img")
        self.assertEqual(backdrop, "https://images.metahub.space/background/large/tt0111161/img")

        self.assertEqual(td["imdb_id"], "tt0111161")
        self.assertEqual(td["tmdb_release_date"], "1994-10-14")
        self.assertEqual(td["runtime"], 142)
        self.assertEqual(td["cinemeta_tmdb_id"], "278")
        self.assertEqual(td["original_poster_path"], poster)   # textless=false path
        self.assertEqual(td["credits"]["crew"], [{"job": "Director", "name": "Frank Darabont"}])
        self.assertEqual([c["name"] for c in td["credits"]["cast"]], ["Tim Robbins", "Morgan Freeman"])

    def test_blank_data_covers_every_key_the_anime_blank_covers(self):
        # main.py reads the same keys off tmdb_data whichever spine produced it.
        import anime
        anime_keys = set(anime._blank_tmdb_data()) - {
            "anime_source", "anime_score", "anime_age_rating", "anime_media_type",
        }
        self.assertTrue(anime_keys <= set(cinemeta._blank_tmdb_data()))

    def test_series_year_range_and_status(self):
        meta = dict(SHAWSHANK, year="2008–2013", status="Ended", released=None)
        _, _, _, year, _, _, _, td = cinemeta.normalise(meta, "tt0903747")
        self.assertEqual(year, "2008")
        self.assertEqual(td["tmdb_status"], "Ended")
        self.assertIsNone(td["tmdb_release_date"])

    def test_continuing_maps_to_tmdb_vocabulary(self):
        meta = dict(SHAWSHANK, status="Continuing")
        self.assertEqual(cinemeta.normalise(meta, "tt1")[7]["tmdb_status"], "Returning Series")

    def test_missing_art_is_none_not_a_dead_url(self):
        meta = {k: v for k, v in SHAWSHANK.items() if k not in ("poster", "background")}
        _, _, _, _, _, poster, backdrop, td = cinemeta.normalise(meta, "tt0111161")
        self.assertIsNone(poster)
        self.assertIsNone(backdrop)
        self.assertIsNone(td["original_poster_path"])

    def test_imdb_only_genres_map_to_a_tmdb_parent(self):
        self.assertEqual(cinemeta._map_genres(["Biography", "Sport", "Film-Noir"]), [36, 18, 80])
        self.assertEqual(cinemeta._map_genres(["Sci-Fi", "sci-fi", "Nonsense"]), [878])

    def test_every_mapped_genre_is_rankable(self):
        import config
        for name, gid in cinemeta._GENRE_IDS.items():
            with self.subTest(genre=name):
                self.assertIn(gid, config.GENRE_MAP)
                self.assertIn(gid, config.GENRE_PRIORITY)

    def test_runtime_parsing(self):
        self.assertEqual(cinemeta._parse_runtime("142 min"), 142)
        self.assertEqual(cinemeta._parse_runtime("1h 30min"), 90)
        self.assertEqual(cinemeta._parse_runtime(49), 49)
        self.assertIsNone(cinemeta._parse_runtime(None))
        self.assertIsNone(cinemeta._parse_runtime("n/a"))

    def test_empty_metadata_matches_the_shape(self):
        empty = cinemeta.empty_metadata()
        self.assertEqual(len(empty), 8)
        self.assertIsNone(empty[5])
        self.assertIsNone(empty[6])


class CinemetaFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_is_slimmed_and_cached(self):
        with _MemoryJsonCache(cinemeta) as cache:
            client = _FakeClient(_FakeResponse(200, {"meta": dict(SHAWSHANK, videos=[{"x": 1}] * 500)}))
            meta = await cinemeta.fetch_cinemeta_meta(client, "tt0111161", "movie")
            self.assertEqual(client.calls[0][0], "https://v3-cinemeta.strem.io/meta/movie/tt0111161.json")
            self.assertNotIn("videos", meta)
            self.assertEqual(meta["moviedb_id"], 278)
            self.assertEqual(len(cache.store), 1)

            # Second call: served from the cache, no request.
            await cinemeta.fetch_cinemeta_meta(client, "tt0111161", "movie")
            self.assertEqual(len(client.calls), 1)

    async def test_tv_uses_the_series_segment(self):
        with _MemoryJsonCache(cinemeta):
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}))
            await cinemeta.fetch_cinemeta_meta(client, "tt0903747", "tv")
            self.assertEqual(client.calls[0][0], "https://v3-cinemeta.strem.io/meta/series/tt0903747.json")

    async def test_404_and_empty_document_are_negative_cached(self):
        for response in (_FakeResponse(404), _FakeResponse(200, {"meta": {}})):
            with self.subTest(status=response.status_code), _MemoryJsonCache(cinemeta) as cache:
                self.assertIsNone(await cinemeta.fetch_cinemeta_meta(_FakeClient(response), "tt0000001", "movie"))
                self.assertEqual(list(cache.store.values()), [{"__miss__": True}])

    async def test_outage_and_throttle_are_not_negative_cached(self):
        for response in (_FakeResponse(503), _FakeResponse(429), RuntimeError("reset")):
            with self.subTest(response=response), _MemoryJsonCache(cinemeta) as cache:
                self.assertIsNone(await cinemeta.fetch_cinemeta_meta(_FakeClient(response), "tt0000001", "movie"))
                self.assertEqual(cache.store, {})

    async def test_malformed_id_never_reaches_the_network(self):
        client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}))
        self.assertIsNone(await cinemeta.fetch_cinemeta_meta(client, "278", "movie"))
        self.assertEqual(client.calls, [])

    async def test_resolve_tmdb_id_reads_moviedb_id(self):
        with _MemoryJsonCache(cinemeta):
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}))
            self.assertEqual(await cinemeta.resolve_tmdb_id(client, "tt0111161", "movie"), "278")
        with _MemoryJsonCache(cinemeta):
            meta = {k: v for k, v in SHAWSHANK.items() if k != "moviedb_id"}
            client = _FakeClient(_FakeResponse(200, {"meta": meta}))
            self.assertIsNone(await cinemeta.resolve_tmdb_id(client, "tt0111161", "movie"))

    async def test_art_is_probed_on_the_cdn_not_read_off_the_document(self):
        # Cinemeta names art urls for every title it knows; only the CDN knows
        # whether the images exist.
        with _MemoryJsonCache(cinemeta) as cache:
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}), head_status=404)
            _, _, _, _, _, poster, backdrop, td = await cinemeta.fetch_cinemeta_metadata(client, "tt0111161", "movie")
            self.assertIsNone(poster)
            self.assertIsNone(backdrop)
            self.assertIsNone(td["original_poster_path"])
            self.assertEqual(client.heads, [cinemeta.poster_url("tt0111161"), cinemeta.background_url("tt0111161")])
            self.assertEqual(cache.store[cinemeta._art_probe_key("tt0111161")], {"poster": False, "background": False})

            # Probe answers are cached with the document.
            await cinemeta.fetch_cinemeta_metadata(client, "tt0111161", "movie")
            self.assertEqual(len(client.heads), 2)

    async def test_present_art_survives_the_probe(self):
        with _MemoryJsonCache(cinemeta):
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}), head_status=200)
            _, _, _, _, _, poster, backdrop, _ = await cinemeta.fetch_cinemeta_metadata(client, "tt0111161", "movie")
            self.assertEqual(poster, cinemeta.poster_url("tt0111161"))
            self.assertEqual(backdrop, cinemeta.background_url("tt0111161"))

    async def test_probe_blip_is_not_cached(self):
        with _MemoryJsonCache(cinemeta) as cache:
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}), head_status=RuntimeError("reset"))
            self.assertEqual(await cinemeta.probe_art(client, "tt0111161"), (False, False))
            self.assertNotIn(cinemeta._art_probe_key("tt0111161"), cache.store)


# ---------------------------------------------------------------------------
# tmdb.py — art cache keys and the id resolver
# ---------------------------------------------------------------------------

class ArtCacheKeyTests(unittest.TestCase):
    """The fetchers and main.py's deferred-detection queue must agree on the
    key, and every pre-existing TMDB key must be unchanged."""

    def test_tmdb_paths_keep_their_historical_keys(self):
        self.assertEqual(tmdb.poster_image_cache_key("1396", "tv", "/abc.jpg"), "tv_1396_abc.jpg")
        self.assertEqual(
            tmdb.backdrop_image_cache_key("1396", "/bd.jpg", False),
            f"backdrop_1396_bd.jpg_{tmdb._CROP_VERSION}",
        )
        self.assertEqual(
            tmdb.backdrop_image_cache_key("1396", "/bd.jpg", True),
            f"backdrop_1396_bd.jpg_{tmdb._CROP_VERSION}_ta",
        )
        self.assertEqual(
            tmdb.landscape_image_cache_key("1396", "/bd.jpg"),
            f"landscape_1396_bd.jpg_{tmdb.LANDSCAPE_WIDTH}x{tmdb.LANDSCAPE_HEIGHT}",
        )

    def test_anime_absolute_url_keeps_its_historical_key(self):
        import hashlib
        url = "https://cdn/x.jpg"
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        self.assertEqual(tmdb.poster_image_cache_key("kitsu:7442", "tv", url), f"tv_kitsu_7442_{digest}")

    def test_absolute_urls_are_hashed_into_filenames(self):
        url = cinemeta.background_url("tt0111161")
        key = tmdb.backdrop_image_cache_key("tt0111161", url, True)
        self.assertNotIn("/", key)
        self.assertNotIn(":", key)
        self.assertTrue(key.endswith("_ta"))
        self.assertNotEqual(key, tmdb.backdrop_image_cache_key("tt0111161", url, False))


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    FOUND = _FakeResponse(200, {"movie_results": [{"id": 278}], "tv_results": []})
    TV_ONLY = _FakeResponse(200, {"movie_results": [], "tv_results": [{"id": 1396}]})
    NOTHING = _FakeResponse(200, {"movie_results": [], "tv_results": []})

    def setUp(self):
        tmdb._idmap_inflight.clear()

    async def test_keyed_lookup_uses_find_and_is_persisted(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(self.FOUND)
            result = await tmdb.resolve_imdb_to_tmdb(client, "tt0111161", "movie", "k")
            self.assertEqual(result, {"tmdb_id": "278", "media_type": "movie"})
            url, params = client.calls[0]
            self.assertEqual(url, "https://api.themoviedb.org/3/find/tt0111161")
            self.assertEqual(params["external_source"], "imdb_id")

            again = await tmdb.resolve_imdb_to_tmdb(client, "tt0111161", "movie", "k")
            self.assertEqual(again, result)
            self.assertEqual(len(client.calls), 1)
            # The forward answer, and the reverse link /find just proved.
            self.assertEqual(
                sorted(cache.store),
                ["idmap:v1:imdb:tt0111161:movie", "idmap:v1:tmdb:movie:278"],
            )

    async def test_keyed_lookup_corrects_the_media_type(self):
        with _MemoryJsonCache(tmdb, cinemeta):
            result = await tmdb.resolve_imdb_to_tmdb(_FakeClient(self.TV_ONLY), "tt0903747", "movie", "k")
            self.assertEqual(result, {"tmdb_id": "1396", "media_type": "tv"})

    async def test_keyed_miss_is_cached_and_never_asks_cinemeta(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(self.NOTHING)
            self.assertIsNone(await tmdb.resolve_imdb_to_tmdb(client, "tt0000001", "movie", "k"))
            self.assertEqual(list(cache.store.values()), [{"__miss__": True}])
            self.assertIsNone(await tmdb.resolve_imdb_to_tmdb(client, "tt0000001", "movie", "k"))
            # One /find, no Cinemeta call.
            self.assertEqual([u for u, _ in client.calls], ["https://api.themoviedb.org/3/find/tt0000001"])

    async def test_keyed_failure_raises_and_is_not_cached(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            with self.assertRaises(tmdb.IdResolveError):
                await tmdb.resolve_imdb_to_tmdb(_FakeClient(RuntimeError("down")), "tt0111161", "movie", "k")
            self.assertEqual(cache.store, {})

    async def test_keyless_lookup_uses_cinemeta(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(_FakeResponse(200, {"meta": SHAWSHANK}))
            result = await tmdb.resolve_imdb_to_tmdb(client, "tt0111161", "movie", None)
            self.assertEqual(result, {"tmdb_id": "278", "media_type": "movie"})
            self.assertEqual(client.calls[0][0], "https://v3-cinemeta.strem.io/meta/movie/tt0111161.json")
            self.assertIn("idmap:v1:imdb:tt0111161:movie", cache.store)

    async def test_keyless_miss_is_not_cached(self):
        # Cinemeta may simply have been unreachable.
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            self.assertIsNone(await tmdb.resolve_imdb_to_tmdb(_FakeClient(_FakeResponse(503)), "tt0111161", "movie", None))
            self.assertEqual([k for k in cache.store if k.startswith("idmap:")], [])

    async def test_anthology_resolves_to_newest_aired_installment(self):
        # Monster: Lizzie Borden not out yet, so the anthology fronts Ed Gein.
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(
                _FakeResponse(200, {"first_air_date": "2999-01-01"}),
                _FakeResponse(200, {"first_air_date": "2025-10-03"}),
            )
            result = await tmdb.resolve_imdb_to_tmdb(client, "tt13207736", "tv", "k")
            self.assertEqual(result, {"tmdb_id": "286801", "media_type": "tv"})
            self.assertEqual(
                [u for u, _ in client.calls],
                ["https://api.themoviedb.org/3/tv/299939", "https://api.themoviedb.org/3/tv/286801"],
            )
            # Never asks /find, and the answer is kept.
            again = await tmdb.resolve_imdb_to_tmdb(client, "tt13207736", "movie", "k")
            self.assertEqual(again, result)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(list(cache.store), ["idmap:v1:anthology:tt13207736"])

    async def test_anthology_picks_newest_once_it_airs(self):
        with _MemoryJsonCache(tmdb, cinemeta):
            client = _FakeClient(_FakeResponse(200, {"first_air_date": "2020-01-01"}))
            result = await tmdb.resolve_imdb_to_tmdb(client, "tt13207736", "tv", "k")
            self.assertEqual(result["tmdb_id"], "299939")
            self.assertEqual(len(client.calls), 1)

    async def test_anthology_falls_back_to_first_and_skips_cache_on_failure(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(RuntimeError("down"))
            result = await tmdb.resolve_imdb_to_tmdb(client, "tt13207736", "tv", "k")
            self.assertEqual(result, {"tmdb_id": "113988", "media_type": "tv"})
            self.assertEqual(cache.store, {})

    async def test_anthology_without_key_takes_the_cinemeta_route(self):
        with _MemoryJsonCache(tmdb, cinemeta):
            client = _FakeClient(_FakeResponse(503))
            await tmdb.resolve_imdb_to_tmdb(client, "tt13207736", "tv", None)
            self.assertTrue(client.calls[0][0].startswith("https://v3-cinemeta.strem.io/"))

    async def test_reverse_lookup_reads_external_ids_and_is_cached(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            client = _FakeClient(_FakeResponse(200, {"imdb_id": "tt0111161"}))
            self.assertEqual(await tmdb.resolve_tmdb_to_imdb(client, "278", "movie", "k"), "tt0111161")
            self.assertEqual(await tmdb.resolve_tmdb_to_imdb(client, "278", "movie", "k"), "tt0111161")
            self.assertEqual([u for u, _ in client.calls], ["https://api.themoviedb.org/3/movie/278/external_ids"])
            self.assertEqual(list(cache.store), ["idmap:v1:tmdb:movie:278"])

    async def test_reverse_lookup_of_an_unlinked_installment_is_none(self):
        # Monster: Ed Gein's own show on TMDB links no IMDb id.
        with _MemoryJsonCache(tmdb, cinemeta):
            client = _FakeClient(_FakeResponse(200, {"imdb_id": None}))
            self.assertIsNone(await tmdb.resolve_tmdb_to_imdb(client, "286801", "series", "k"))
            self.assertEqual(client.calls[0][0], "https://api.themoviedb.org/3/tv/286801/external_ids")

    async def test_reverse_lookup_failure_raises_and_is_not_cached(self):
        with _MemoryJsonCache(tmdb, cinemeta) as cache:
            with self.assertRaises(tmdb.IdResolveError):
                await tmdb.resolve_tmdb_to_imdb(_FakeClient(_FakeResponse(503)), "278", "movie", "k")
            self.assertEqual(cache.store, {})

    async def test_find_seeds_the_reverse_map(self):
        with _MemoryJsonCache(tmdb, cinemeta):
            await tmdb.resolve_imdb_to_tmdb(_FakeClient(self.FOUND), "tt0111161", "movie", "k")
            client = _FakeClient(RuntimeError("must not be called"))
            self.assertEqual(await tmdb.resolve_tmdb_to_imdb(client, "278", "movie", "k"), "tt0111161")

    async def test_concurrent_lookups_share_one_request(self):
        release = asyncio.Event()

        class _Slow(_FakeClient):
            async def get(self, url, params=None, **kwargs):
                await release.wait()
                return await super().get(url, params=params, **kwargs)

        with _MemoryJsonCache(tmdb, cinemeta):
            client = _Slow(self.FOUND)
            tasks = [
                asyncio.create_task(tmdb.resolve_imdb_to_tmdb(client, "tt0111161", "movie", "k"))
                for _ in range(5)
            ]
            await asyncio.sleep(0.01)
            release.set()
            results = await asyncio.gather(*tasks)
            self.assertEqual({r["tmdb_id"] for r in results}, {"278"})
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(tmdb._idmap_inflight, {})


# ---------------------------------------------------------------------------
# main.py — the spine decision
# ---------------------------------------------------------------------------

class TitleIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cinemeta = main._cfg.CINEMETA_ENABLED
        self._client = main._HTTP_CLIENT
        self._resolve = main.resolve_imdb_to_tmdb
        main._cfg.CINEMETA_ENABLED = True
        main._HTTP_CLIENT = object()

    def tearDown(self):
        main._cfg.CINEMETA_ENABLED = self._cinemeta
        main._HTTP_CLIENT = self._client
        main.resolve_imdb_to_tmdb = self._resolve

    def _stub(self, outcome):
        async def _resolve(client, imdb_id, media_type, key):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        main.resolve_imdb_to_tmdb = _resolve

    async def _identity(self, tmdb_id="", imdb_id="", media_type="movie", key="k"):
        return await main._resolve_title_identity(tmdb_id, imdb_id, media_type, key)

    async def _refusal(self, **kwargs) -> HTTPException:
        with self.assertRaises(HTTPException) as ctx:
            await self._identity(**kwargs)
        return ctx.exception

    # -- a tmdb_id is sent --

    async def test_tmdb_id_with_a_key_is_the_tmdb_path_untouched(self):
        self._stub(RuntimeError("must not be called"))
        self.assertEqual(await self._identity(tmdb_id="278"), ("278", "movie", False))
        self.assertEqual(await self._identity(tmdb_id="278", imdb_id="tt0111161"), ("278", "movie", False))

    async def test_tmdb_id_without_a_key_needs_an_imdb_id_for_cinemeta(self):
        self.assertEqual(
            await self._identity(tmdb_id="278", imdb_id="tt0111161", key=None),
            ("278", "movie", True),
        )
        exc = await self._refusal(tmdb_id="278", key=None)
        self.assertEqual(exc.status_code, 400)
        self.assertIn("No TMDB API key", exc.detail)
        self.assertIn("imdb_id", exc.detail)      # the hint: an IMDb id would do

    async def test_cinemeta_off_restores_the_key_requirement(self):
        main._cfg.CINEMETA_ENABLED = False
        exc = await self._refusal(tmdb_id="278", imdb_id="tt0111161", key=None)
        self.assertEqual(exc.status_code, 400)
        self.assertNotIn("Cinemeta", exc.detail)

    # -- imdb_id only, with a key --

    async def test_imdb_only_resolves_to_the_tmdb_path(self):
        self._stub({"tmdb_id": "278", "media_type": "movie"})
        self.assertEqual(await self._identity(imdb_id="tt0111161"), ("278", "movie", False))

    async def test_tmdb_corrects_the_type(self):
        self._stub({"tmdb_id": "1396", "media_type": "tv"})
        self.assertEqual(await self._identity(imdb_id="tt0903747"), ("1396", "tv", False))
        # "series" is the same thing as "tv": not a correction.
        self.assertEqual(
            await self._identity(imdb_id="tt0903747", media_type="series"),
            ("1396", "series", False),
        )

    async def test_unlinked_imdb_id_falls_to_cinemeta_with_the_imdb_id_standing_in(self):
        self._stub(None)
        self.assertEqual(await self._identity(imdb_id="tt0000001"), ("tt0000001", "movie", True))

    async def test_unlinked_imdb_id_is_404_when_cinemeta_is_off(self):
        main._cfg.CINEMETA_ENABLED = False
        self._stub(None)
        exc = await self._refusal(imdb_id="tt0000001")
        self.assertEqual(exc.status_code, 404)

    async def test_failed_lookup_falls_to_cinemeta_or_is_502(self):
        self._stub(tmdb.IdResolveError("find down"))
        self.assertEqual(await self._identity(imdb_id="tt0111161"), ("tt0111161", "movie", True))
        main._cfg.CINEMETA_ENABLED = False
        exc = await self._refusal(imdb_id="tt0111161")
        self.assertEqual(exc.status_code, 502)

    # -- imdb_id only, no key --

    async def test_keyless_imdb_only_keeps_a_cinemeta_supplied_tmdb_id(self):
        self._stub({"tmdb_id": "278", "media_type": "movie"})
        self.assertEqual(await self._identity(imdb_id="tt0111161", key=None), ("278", "movie", True))

    async def test_keyless_imdb_only_without_a_tmdb_id_stands_in_the_imdb_id(self):
        self._stub(None)
        self.assertEqual(await self._identity(imdb_id="tt0111161", key=None), ("tt0111161", "movie", True))

    async def test_keyless_imdb_only_with_cinemeta_off_is_the_key_error(self):
        main._cfg.CINEMETA_ENABLED = False
        self._stub(None)
        exc = await self._refusal(imdb_id="tt0111161", key=None)
        self.assertEqual(exc.status_code, 400)
        self.assertIn("No TMDB API key", exc.detail)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class ImdbUnderTmdbTests(unittest.IsolatedAsyncioTestCase):
    """Once a TMDB id decides the title, the IMDb id survives only if TMDB links it."""

    def setUp(self):
        self._client = main._HTTP_CLIENT
        self._reverse = main.resolve_tmdb_to_imdb
        main._HTTP_CLIENT = object()

    def tearDown(self):
        main._HTTP_CLIENT = self._client
        main.resolve_tmdb_to_imdb = self._reverse

    def _stub(self, outcome):
        async def _reverse(client, tmdb_id, media_type, key):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        main.resolve_tmdb_to_imdb = _reverse

    async def _kept(self, tmdb_id="286801", imdb_id="tt13207736", key="k", use_cinemeta=False):
        return await main._imdb_id_under_tmdb(tmdb_id, imdb_id, "series", key, use_cinemeta)

    async def test_linked_imdb_id_is_kept(self):
        self._stub("tt0111161")
        self.assertEqual(await self._kept(tmdb_id="278", imdb_id="tt0111161"), "tt0111161")

    async def test_anthology_imdb_id_beside_an_installment_is_dropped(self):
        self._stub(None)
        self.assertEqual(await self._kept(), "")

    async def test_imdb_id_linked_elsewhere_is_dropped(self):
        self._stub("tt9999999")
        self.assertEqual(await self._kept(), "")

    async def test_failed_lookup_drops_the_imdb_id(self):
        self._stub(tmdb.IdResolveError("down"))
        self.assertEqual(await self._kept(), "")

    async def test_cinemeta_spine_and_keyless_requests_keep_it(self):
        self._stub(RuntimeError("must not be called"))
        self.assertEqual(await self._kept(use_cinemeta=True), "tt13207736")
        self.assertEqual(await self._kept(key=None), "tt13207736")
        self.assertEqual(await self._kept(tmdb_id="tt13207736"), "tt13207736")


class RouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "ACCESS_KEY": main._cfg.ACCESS_KEY,
            "SERVER_TMDB_KEY": main._cfg.SERVER_TMDB_KEY,
            "CINEMETA_ENABLED": main._cfg.CINEMETA_ENABLED,
        }
        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = ""
        main._cfg.CINEMETA_ENABLED = True
        self.client = TestClient(main.app)

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)

    def test_poster_tmdb_only_without_a_key_explains_the_imdb_route(self):
        resp = self.client.get("/poster", params={"tmdb_id": "278", "type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cinemeta", resp.json()["detail"])

    def test_poster_malformed_imdb_id_is_still_rejected(self):
        resp = self.client.get("/poster", params={"imdb_id": "278", "type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Invalid imdb_id")

    def test_logo_needs_one_id(self):
        resp = self.client.get("/logo", params={"type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("imdb_id", resp.json()["detail"])

    def test_logo_tmdb_only_without_a_key(self):
        resp = self.client.get("/logo", params={"tmdb_id": "278"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No TMDB API key", resp.json()["detail"])


def _canvas(w=500, h=750):
    return Image.new("RGBA", (w, h), (40, 40, 60, 255))


class RenderPathTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real /poster handler with the upstream fetchers stubbed, and
    check which spine rendered and with what identity."""

    def setUp(self):
        self._saved = {
            "ACCESS_KEY": main._cfg.ACCESS_KEY,
            "SERVER_TMDB_KEY": main._cfg.SERVER_TMDB_KEY,
            "SERVER_MDBLIST_KEYS": main._cfg.SERVER_MDBLIST_KEYS,
            "CINEMETA_ENABLED": main._cfg.CINEMETA_ENABLED,
            "TEXTLESS_TEXT_DETECTION": main._cfg.TEXTLESS_TEXT_DETECTION,
            "DISABLE_COMPOSITE_CACHE": main._cfg.DISABLE_COMPOSITE_CACHE,
        }
        self._stubs = {
            name: getattr(main, name) for name in (
                "_HTTP_CLIENT", "resolve_imdb_to_tmdb", "resolve_tmdb_to_imdb",
                "_coalesced_fetch_poster_metadata",
                "fetch_backdrop_image", "fetch_poster_image", "fetch_logo",
                "_fetch_metahub_logo",
            )
        }
        self._cm_fetch = cinemeta.fetch_cinemeta_metadata
        self._cm_meta = cinemeta.fetch_cinemeta_meta
        # The stubbed client cannot serve TMDB's trending list, so every render
        # here reads it, fails, and retries: no real pause for that, and no
        # cooldown left behind for the next test.
        self._retry_delay = tmdb._TRENDING_RETRY_DELAY_SECS
        tmdb._TRENDING_RETRY_DELAY_SECS = 0
        tmdb._trending_source_failed_at.clear()
        self.addCleanup(tmdb._trending_source_failed_at.clear)
        self.addCleanup(setattr, tmdb, "_TRENDING_RETRY_DELAY_SECS", self._retry_delay)
        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_MDBLIST_KEYS = []
        main._cfg.CINEMETA_ENABLED = True
        main._cfg.TEXTLESS_TEXT_DETECTION = False
        main._cfg.DISABLE_COMPOSITE_CACHE = True
        main._HTTP_CLIENT = object()
        main._render_semaphore = None
        main._render_inflight.clear()

        self.art_calls: list[tuple] = []

        async def _backdrop(client, tmdb_id, path, avoid_text=False):
            self.art_calls.append(("backdrop", tmdb_id, path))
            return _canvas()

        async def _poster(client, tmdb_id, media_type, path):
            self.art_calls.append(("poster", tmdb_id, path))
            return _canvas()

        async def _no_logo(*args, **kwargs):
            return None

        async def _linked(client, tmdb_id, media_type, key):
            return "tt0111161"

        main.resolve_tmdb_to_imdb = _linked
        main.fetch_backdrop_image = _backdrop
        main.fetch_poster_image = _poster
        main.fetch_logo = _no_logo
        main._fetch_metahub_logo = _no_logo

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)
        for name, value in self._stubs.items():
            setattr(main, name, value)
        cinemeta.fetch_cinemeta_metadata = self._cm_fetch
        cinemeta.fetch_cinemeta_meta = self._cm_meta
        main._render_semaphore = None
        main._render_inflight.clear()

    def _stub_resolver(self, outcome):
        async def _resolve(client, imdb_id, media_type, key):
            self.resolved_with = (imdb_id, media_type, key)
            return outcome
        main.resolve_imdb_to_tmdb = _resolve

    def _stub_tmdb_metadata(self, poster="/p.jpg", backdrop="/b.jpg", textless=True):
        self.meta_calls = []

        async def _meta(client, tmdb_id, key, media_type, lang, secondary=""):
            self.meta_calls.append((tmdb_id, media_type))
            td = dict(cinemeta._blank_tmdb_data(), imdb_id="tt0111161", vote_count=5000)
            return [18], textless, [], "1994", "Shawshank", poster, backdrop, td
        main._coalesced_fetch_poster_metadata = _meta

    def _stub_cinemeta(self, meta=SHAWSHANK):
        self.cm_calls = []

        async def _cm(client, imdb_id, media_type):
            self.cm_calls.append((imdb_id, media_type))
            return cinemeta.normalise(meta, imdb_id) if meta else None
        cinemeta.fetch_cinemeta_metadata = _cm

    async def _get(self, **params):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/poster", params=params)

    async def test_imdb_only_with_a_key_renders_from_tmdb_as_the_resolved_id(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        self._stub_resolver({"tmdb_id": "278", "media_type": "movie"})
        self._stub_tmdb_metadata()
        self._stub_cinemeta(None)

        resp = await self._get(imdb_id="tt0111161", type="movie", cb="either1")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("image/"))
        self.assertEqual(self.resolved_with, ("tt0111161", "movie", "k"))
        self.assertEqual(self.meta_calls, [("278", "movie")])
        self.assertEqual(self.cm_calls, [])
        self.assertEqual(self.art_calls, [("poster", "278", "/p.jpg")])

    async def test_imdb_only_with_a_key_shares_the_composite_key_with_a_both_ids_client(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.DISABLE_COMPOSITE_CACHE = False
        self._stub_resolver({"tmdb_id": "278", "media_type": "movie"})
        self._stub_tmdb_metadata()
        seen: list[str] = []
        real_lookup = main.get_cached_final_poster_entry

        def _spy(key):
            seen.append(key)
            return None
        main.get_cached_final_poster_entry = _spy
        # Past the in-memory tier too, or the second request is answered by
        # the first one's freshly cached render before reaching the spy.
        real_l1 = main.get_cached_final_poster_l1
        main.get_cached_final_poster_l1 = lambda key: None
        try:
            await self._get(imdb_id="tt0111161", type="movie", cb="either2")
            await self._get(imdb_id="tt0111161", tmdb_id="278", type="movie", cb="either2")
        finally:
            main.get_cached_final_poster_entry = real_lookup
            main.get_cached_final_poster_l1 = real_l1
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertTrue(seen[0].startswith("tt0111161:278:movie:"))

    async def test_unlinked_imdb_id_beside_a_tmdb_id_keys_like_tmdb_only(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.DISABLE_COMPOSITE_CACHE = False
        self._stub_tmdb_metadata()

        async def _unlinked(client, tmdb_id, media_type, key):
            return None
        main.resolve_tmdb_to_imdb = _unlinked
        seen: list[str] = []
        real_lookup = main.get_cached_final_poster_entry

        def _spy(key):
            seen.append(key)
            return None
        main.get_cached_final_poster_entry = _spy
        # Past the in-memory tier too, or the second request is answered by
        # the first one's freshly cached render before reaching the spy.
        real_l1 = main.get_cached_final_poster_l1
        main.get_cached_final_poster_l1 = lambda key: None
        try:
            await self._get(imdb_id="tt13207736", tmdb_id="286801", type="series", cb="either3")
            await self._get(tmdb_id="286801", type="series", cb="either3")
        finally:
            main.get_cached_final_poster_entry = real_lookup
            main.get_cached_final_poster_l1 = real_l1
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertTrue(seen[0].startswith("tmdb:286801:"))

    async def test_no_key_renders_from_cinemeta_via_the_backdrop_fallback(self):
        main._cfg.SERVER_TMDB_KEY = ""
        self._stub_resolver({"tmdb_id": "278", "media_type": "movie"})
        self._stub_tmdb_metadata()
        self._stub_cinemeta()

        resp = await self._get(imdb_id="tt0111161", type="movie", cb="either3")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.meta_calls, [])                 # TMDB never asked
        self.assertEqual(self.cm_calls, [("tt0111161", "movie")])
        # Background cropped to portrait, keyed by the Cinemeta-supplied TMDB id.
        self.assertEqual(self.art_calls, [("backdrop", "278", cinemeta.background_url("tt0111161"))])

    async def test_no_key_original_art_serves_the_one_sheet(self):
        main._cfg.SERVER_TMDB_KEY = ""
        self._stub_resolver(None)
        self._stub_cinemeta()

        resp = await self._get(imdb_id="tt0111161", type="movie", use_original_art="true", cb="either4")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.art_calls, [("poster", "tt0111161", cinemeta.poster_url("tt0111161"))])

    async def test_no_key_with_nothing_on_cinemeta_serves_the_canvas(self):
        main._cfg.SERVER_TMDB_KEY = ""
        self._stub_resolver(None)
        self._stub_cinemeta(None)

        resp = await self._get(imdb_id="tt0000001", type="movie", cb="either5")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.art_calls, [])
        # A miss is never a keepable render.
        self.assertNotIn("etag", resp.headers)

    async def test_tmdb_title_with_no_art_borrows_metahub_art(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        self._stub_tmdb_metadata(poster=None, backdrop=None)

        async def _probe(client, imdb_id):
            return True, True
        cinemeta.fetch_cinemeta_meta = None   # must not be reached: the probe is stubbed
        real_probe = cinemeta.probe_art
        cinemeta.probe_art = _probe
        try:
            resp = await self._get(tmdb_id="278", imdb_id="tt0111161", type="movie", cb="either6")
        finally:
            cinemeta.probe_art = real_probe

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.art_calls, [("backdrop", "278", cinemeta.background_url("tt0111161"))])

    async def test_tmdb_title_with_no_art_and_cinemeta_off_is_the_canvas(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.CINEMETA_ENABLED = False
        self._stub_tmdb_metadata(poster=None, backdrop=None)

        resp = await self._get(tmdb_id="278", imdb_id="tt0111161", type="movie", cb="either7")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.art_calls, [])

    async def test_ordinary_tmdb_request_never_touches_cinemeta(self):
        main._cfg.SERVER_TMDB_KEY = "k"
        self._stub_tmdb_metadata()
        self._stub_cinemeta()

        async def _boom(*a, **k):
            raise AssertionError("Cinemeta consulted on a healthy TMDB request")
        cinemeta.fetch_cinemeta_meta = _boom

        resp = await self._get(tmdb_id="278", imdb_id="tt0111161", type="movie", cb="either8")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.cm_calls, [])
        self.assertEqual(self.art_calls, [("poster", "278", "/p.jpg")])


if __name__ == "__main__":
    unittest.main()
