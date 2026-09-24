import unittest

import cache
import main
import quality


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient that records the last request."""

    def __init__(self, response: _FakeResponse | Exception):
        self.response = response
        self.url: str | None = None
        self.params: dict | None = None
        self.headers: dict | None = None

    async def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.url = url
        self.params = params
        self.headers = headers
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class QualiCacheFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cached: list[tuple] = []
        self._real_set_cached = quality.set_cached_quality
        self._real_min_trust = main._cfg.QUALICACHE_MIN_TRUST
        quality.set_cached_quality = lambda *args: self.cached.append(args)

    def tearDown(self):
        quality.set_cached_quality = self._real_set_cached
        main._cfg.QUALICACHE_MIN_TRUST = self._real_min_trust

    async def _fetch(self, response, **kwargs):
        client = _FakeClient(response)
        result = await quality.fetch_quality_from_qualicache(
            client, "http://qualicache:8000", "tt0068646", **kwargs
        )
        return result, client

    async def test_ready_returns_tokens_in_badge_order_and_caches(self):
        result, _ = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["ATMOS", "DV", "4K", "REMUX"]}),
            release_date="1972-03-14",
        )
        self.assertEqual(result, ["4K", "REMUX", "DV", "ATMOS"])
        self.assertEqual(self.cached, [("tt0068646", ["4K", "REMUX", "DV", "ATMOS"], "1972-03-14")])

    async def test_tokens_without_a_postersplus_badge_are_dropped(self):
        # QualiCache's vocabulary is wider than ours: 8K/720P/HDTV have no
        # badge and nothing to fold into, so they vanish rather than guess.
        result, _ = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["8K", "HDTV", "HDR10"]})
        )
        self.assertEqual(result, ["HDR10"])
        result, _ = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["720P", "WEBDL"]})
        )
        self.assertEqual(result, ["WEBDL"])

    async def test_bluray_encode_and_webrip_fold_into_the_web_badge(self):
        # The badge promises "1080p web-quality or better". A BluRay encode
        # and a WEBRip both clear that bar, so they render as Web rather than
        # disappearing; only a true remux keeps the gold badge.
        for tokens in (["1080P", "BLURAY"], ["1080P", "WEBRIP"]):
            with self.subTest(tokens=tokens):
                self.cached.clear()
                result, _ = await self._fetch(
                    _FakeResponse(200, {"status": "ready", "tokens": tokens})
                )
                self.assertEqual(result, ["1080P", "WEBDL"])
        result, _ = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["1080P", "REMUX"]})
        )
        self.assertEqual(result, ["1080P", "REMUX"])

    async def test_empty_is_an_authoritative_no_result_and_is_cached(self):
        result, _ = await self._fetch(_FakeResponse(200, {"status": "empty", "tokens": []}))
        self.assertEqual(result, [])
        self.assertEqual(len(self.cached), 1)

    async def test_pending_is_not_cached_and_is_not_a_failure(self):
        result, _ = await self._fetch(_FakeResponse(200, {"status": "pending", "tokens": []}))
        self.assertIs(result, quality.QUALITY_PENDING)
        self.assertIsNot(result, main.FETCH_FAILED)
        self.assertEqual(self.cached, [])

    async def test_per_title_error_is_reported_as_pending_not_failure(self):
        # QualiCache owns the retry for that title; failing the whole source
        # would stall every other title behind one bad one.
        result, _ = await self._fetch(
            _FakeResponse(200, {"status": "error", "tokens": [], "last_error": "addon timeout"})
        )
        self.assertIs(result, quality.QUALITY_PENDING)
        self.assertEqual(self.cached, [])

    async def test_unreachable_qualicache_is_a_failure(self):
        result, _ = await self._fetch(_FakeResponse(503))
        self.assertIs(result, main.FETCH_FAILED)
        self.assertEqual(self.cached, [])

    async def test_bad_credentials_are_a_failure(self):
        result, _ = await self._fetch(_FakeResponse(401))
        self.assertIs(result, main.FETCH_FAILED)

    async def test_transport_error_is_a_failure(self):
        result, _ = await self._fetch(ConnectionError("refused"))
        self.assertIs(result, main.FETCH_FAILED)

    async def test_unknown_status_is_a_failure(self):
        result, _ = await self._fetch(_FakeResponse(200, {"status": "wat", "tokens": []}))
        self.assertIs(result, main.FETCH_FAILED)

    async def test_series_sends_season_and_episode(self):
        _, client = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["1080P"]}),
            media_type="series",
            season=2,
            episode=5,
        )
        self.assertEqual(client.url, "http://qualicache:8000/v1/quality/series/tt0068646")
        self.assertEqual(client.params["season"], 2)
        self.assertEqual(client.params["episode"], 5)
        self.assertEqual(client.params["min_trust"], "medium")

    async def test_configured_minimum_trust_is_forwarded(self):
        main._cfg.QUALICACHE_MIN_TRUST = "low"
        _, client = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["1080P"]})
        )
        self.assertEqual(client.params["min_trust"], "low")

    async def test_movie_omits_season_and_episode(self):
        _, client = await self._fetch(_FakeResponse(200, {"status": "ready", "tokens": ["4K"]}))
        self.assertEqual(client.url, "http://qualicache:8000/v1/quality/movie/tt0068646")
        self.assertNotIn("season", client.params)

    async def test_full_iso_release_date_is_forwarded(self):
        _, client = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["4K"]}),
            release_date="1972-03-14",
        )
        self.assertEqual(client.params["release_date"], "1972-03-14")

    async def test_bare_year_release_date_is_not_forwarded(self):
        # The cache-warm path passes a 4-digit year. QualiCache's endpoint types
        # this as a date and 422s on a year, which would look like a source
        # failure and trip the backoff for every warmed title.
        _, client = await self._fetch(
            _FakeResponse(200, {"status": "ready", "tokens": ["4K"]}),
            release_date="1972",
        )
        self.assertNotIn("release_date", client.params)

    async def test_api_key_is_sent_when_configured(self):
        original = main._cfg.QUALICACHE_API_KEY
        main._cfg.QUALICACHE_API_KEY = "secret"
        try:
            _, client = await self._fetch(_FakeResponse(200, {"status": "ready", "tokens": ["4K"]}))
            self.assertEqual(client.headers["X-API-Key"], "secret")
        finally:
            main._cfg.QUALICACHE_API_KEY = original

    async def test_unconfigured_url_returns_empty_without_caching(self):
        client = _FakeClient(_FakeResponse(200, {}))
        result = await quality.fetch_quality_from_qualicache(client, "", "tt0068646")
        self.assertEqual(result, [])
        self.assertIsNone(client.url)
        self.assertEqual(self.cached, [])


class QualiCacheUrlNormalisationTests(unittest.TestCase):
    def test_trailing_paths_and_slashes_are_stripped(self):
        for raw in (
            "http://qualicache:8000",
            "http://qualicache:8000/",
            "http://qualicache:8000/v1",
            "http://qualicache:8000/v1/quality",
            "http://qualicache:8000/v1/quality/",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    quality._normalize_qualicache_url(raw), "http://qualicache:8000"
                )

    def test_query_and_fragment_are_not_treated_as_part_of_the_base_url(self):
        self.assertEqual(
            quality._normalize_qualicache_url(
                "https://quality.example/?min_trust=low#settings"
            ),
            "https://quality.example",
        )


class QualitySourceSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = main._cfg.QUALITY_SOURCE
        self.qc_url = main._cfg.QUALICACHE_URL
        self.scraper_url = main._cfg.SCRAPER_URL
        self.aio_url = main._cfg.AIOSTREAMS_URL
        self.aio_auth = main._cfg.AIOSTREAMS_AUTH

    def tearDown(self):
        main._cfg.QUALITY_SOURCE = self.source
        main._cfg.QUALICACHE_URL = self.qc_url
        main._cfg.SCRAPER_URL = self.scraper_url
        main._cfg.AIOSTREAMS_URL = self.aio_url
        main._cfg.AIOSTREAMS_AUTH = self.aio_auth

    def test_unknown_source_falls_back_to_aiostreams(self):
        main._cfg.QUALITY_SOURCE = "nonsense"
        self.assertEqual(quality.active_quality_source(), "aiostreams")

    def test_qualicache_needs_only_a_url(self):
        main._cfg.QUALITY_SOURCE = "qualicache"
        main._cfg.QUALICACHE_URL = ""
        self.assertFalse(quality.quality_source_configured())
        main._cfg.QUALICACHE_URL = "http://qualicache:8000"
        self.assertTrue(quality.quality_source_configured())

    def test_qualicache_ignores_aiostreams_settings(self):
        main._cfg.QUALITY_SOURCE = "qualicache"
        main._cfg.QUALICACHE_URL = ""
        main._cfg.AIOSTREAMS_URL = "http://aiostreams:3000"
        main._cfg.AIOSTREAMS_AUTH = "dXNlcjpwYXNz"
        self.assertFalse(quality.quality_source_configured())

    async def test_dispatcher_routes_to_the_selected_backend(self):
        main._cfg.QUALITY_SOURCE = "qualicache"
        main._cfg.QUALICACHE_URL = "http://qualicache:8000"
        client = _FakeClient(_FakeResponse(200, {"status": "pending", "tokens": []}))
        result = await quality.fetch_quality(client, "tt0068646")
        self.assertIs(result, quality.QUALITY_PENDING)
        self.assertEqual(client.url, "http://qualicache:8000/v1/quality/movie/tt0068646")


class QualityCacheContextTests(unittest.TestCase):
    def setUp(self):
        self.source = main._cfg.QUALITY_SOURCE
        self.minimum_trust = main._cfg.QUALICACHE_MIN_TRUST

    def tearDown(self):
        main._cfg.QUALITY_SOURCE = self.source
        main._cfg.QUALICACHE_MIN_TRUST = self.minimum_trust

    def test_qualicache_minimum_trust_is_part_of_cache_policy(self):
        main._cfg.QUALITY_SOURCE = "qualicache"
        main._cfg.QUALICACHE_MIN_TRUST = "high"
        self.assertEqual(cache._quality_cache_context(), "qualicache:high:fold1")
        main._cfg.QUALICACHE_MIN_TRUST = "low"
        self.assertEqual(cache._quality_cache_context(), "qualicache:low:fold1")


class QualityPendingBackoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = main._cfg.QUALITY_SOURCE
        main._cfg.QUALITY_SOURCE = "qualicache"
        main._quality_source_backoff_until.clear()
        main._quality_source_fail_count.clear()

    def tearDown(self):
        main._cfg.QUALITY_SOURCE = self.source
        main._quality_source_backoff_until.clear()
        main._quality_source_fail_count.clear()

    async def test_pending_does_not_create_a_source_cooldown(self):
        main._record_quality_result(quality.QUALITY_PENDING)
        self.assertEqual(main._quality_backoff_remaining(), 0)
        self.assertNotIn("qualicache", main._quality_source_fail_count)

    async def test_pending_does_not_clear_an_existing_cooldown(self):
        main._record_quality_result(main.FETCH_FAILED)
        self.assertGreater(main._quality_backoff_remaining(), 0)
        main._record_quality_result(quality.QUALITY_PENDING)
        self.assertGreater(main._quality_backoff_remaining(), 0)
        self.assertEqual(main._quality_source_fail_count["qualicache"], 1)


if __name__ == "__main__":
    unittest.main()
