"""IMDb is optional at the poster boundary; TMDB is the primary identity.

(Either id now identifies a title — see test_either_id.py for the resolution
paths. These tests cover the enrichment role of imdb_id alongside tmdb_id.)

These tests pin the three identities apart — the cache/coalescing identity, the
MDBList lookup route, and the optional real IMDb id — because conflating them is
what made an absent IMDb id break quietly rather than loudly.
"""

import asyncio
import unittest

import main
import ratings
from fastapi.testclient import TestClient


class OptionalIdNormalisationTests(unittest.TestCase):
    def test_unsubstituted_placeholder_reads_as_absent(self):
        self.assertEqual(main._normalise_optional_id("{imdb_id}", "imdb_id"), "")
        self.assertEqual(main._normalise_optional_id("{imdb_id?}", "imdb_id"), "")

    def test_real_value_survives(self):
        self.assertEqual(main._normalise_optional_id(" tt0903747 ", "imdb_id"), "tt0903747")

    def test_only_this_param_s_placeholder_is_absent(self):
        # Narrow literals only: a brace-wrapped value that isn't this
        # parameter's placeholder stays put so it fails validation loudly
        # rather than being silently dropped.
        self.assertEqual(main._normalise_optional_id("{tmdb_id}", "imdb_id"), "{tmdb_id}")
        self.assertEqual(main._normalise_optional_id("{whatever}", "imdb_id"), "{whatever}")

    def test_empty_and_none_are_absent(self):
        self.assertEqual(main._normalise_optional_id("", "imdb_id"), "")
        self.assertEqual(main._normalise_optional_id(None, "imdb_id"), "")


class CanonicalRatingIdTests(unittest.TestCase):
    def test_imdb_id_wins_when_supplied(self):
        self.assertEqual(
            main._canonical_rating_id("tt0903747", "", "1396"), "tt0903747"
        )

    def test_a_real_tmdb_id_outranks_the_anime_key(self):
        # Shares MDBList's row with a plain TMDB-id request for the show.
        self.assertEqual(
            main._canonical_rating_id("", "kitsu:7442", "1396"), "tmdb:1396"
        )

    def test_tmdb_only_anime_asks_mdblist_by_tmdb_id(self):
        from pathlib import Path
        src = Path("main.py").read_text(encoding="utf-8")
        route = src[src.index('rating_provider, rating_media_id = "imdb", imdb_id'):][:200]
        self.assertIn("elif has_tmdb_id:", route)

    def test_anime_key_when_there_is_no_tmdb_id(self):
        # On the anime path tmdb_id stands in as the anime key itself.
        self.assertEqual(
            main._canonical_rating_id("", "kitsu:7442", "kitsu:7442"), "kitsu:7442"
        )

    def test_tmdb_namespace_is_the_fallback(self):
        self.assertEqual(main._canonical_rating_id("", "", "1698026"), "tmdb:1698026")

    def test_tmdb_form_cannot_collide_with_an_imdb_id(self):
        self.assertFalse(main._canonical_rating_id("", "", "1698026").startswith("tt"))


class TmdbOnlyStateIsolationTests(unittest.TestCase):
    """The regression test for the empty-string key.

    Before IMDb became optional, every TMDB-only request would have keyed the
    shared rating state on "" — so two unrelated titles would coalesce onto one
    another's fetch and share one another's back-off.
    """

    def setUp(self):
        main._rating_backoff.clear()
        main._rating_fail_count.clear()
        main._rating_fetch_inflight.clear()

    tearDown = setUp

    def test_two_tmdb_only_titles_do_not_share_an_identity(self):
        first = main._canonical_rating_id("", "", "1698026")
        second = main._canonical_rating_id("", "", "1647910")
        self.assertNotEqual(first, second)
        self.assertTrue(first and second)

    def test_two_tmdb_only_titles_do_not_share_backoff_state(self):
        first = main._rating_retry_key(main._canonical_rating_id("", "", "1698026"), "key")
        second = main._rating_retry_key(main._canonical_rating_id("", "", "1647910"), "key")

        main._rating_backoff[first] = 3600.0

        self.assertIn(first, main._rating_backoff)
        self.assertNotIn(second, main._rating_backoff)

    def test_two_tmdb_only_titles_do_not_share_an_inflight_slot(self):
        first = main._canonical_rating_id("", "", "1698026")
        second = main._canonical_rating_id("", "", "1647910")

        main._rating_fetch_inflight[first] = asyncio.Event()

        self.assertIn(first, main._rating_fetch_inflight)
        self.assertNotIn(second, main._rating_fetch_inflight)


class QualityIdentityTests(unittest.TestCase):
    """Quality identity is an upstream-lookup identity, so it may use an IMDb id
    discovered from TMDB metadata — but never at the cost of re-keying a title
    that already has a stable identity."""

    def test_request_imdb_id_wins(self):
        self.assertEqual(
            main._quality_identity("tt0903747", "kitsu:7442", "tt9999999"), "tt0903747"
        )

    def test_anime_native_id_outranks_a_tmdb_discovered_imdb_id(self):
        # Torrentio/Comet/AIOStreams are sent the anime-native id because that
        # is what Stremio sends them. Preferring a TMDB-discovered tt id here
        # would orphan every cached anime quality row.
        self.assertEqual(
            main._quality_identity("", "kitsu:7442", "tt0903747"), "kitsu:7442"
        )

    def test_tmdb_discovered_imdb_id_enables_lookup_for_a_linked_title(self):
        self.assertEqual(main._quality_identity("", "", "tt0903747"), "tt0903747")

    def test_unlinked_tmdb_only_title_has_no_quality_identity(self):
        self.assertIsNone(main._quality_identity("", "", None))

    def test_a_tmdb_namespace_id_is_never_sent_upstream(self):
        # There is no accepted "tmdb:<id>" stream id for the ordinary sources,
        # so an unlinked title must skip the lookup rather than invent one.
        self.assertIsNone(main._quality_identity("", "", ""))


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


class _RecordingClient:
    def __init__(self, response):
        self._response = response
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        return self._response


class MdblistRouteTests(unittest.TestCase):
    def _fetch(self, client, **kwargs):
        return asyncio.run(
            ratings.fetch_rating(client, "key", [], "movie", **kwargs)
        )

    def test_imdb_route_is_used_when_an_imdb_id_is_available(self):
        client = _RecordingClient(_FakeResponse(payload={"ratings": []}))
        self._fetch(client, media_id="tt0903747", provider="imdb")
        self.assertEqual(client.urls, ["https://api.mdblist.com/imdb/movie/tt0903747"])

    def test_tmdb_route_is_used_for_a_title_with_no_imdb_id(self):
        client = _RecordingClient(_FakeResponse(payload={"ratings": []}))
        self._fetch(client, media_id="1698026", provider="tmdb")
        self.assertEqual(client.urls, ["https://api.mdblist.com/tmdb/movie/1698026"])

    def test_show_media_type_maps_to_the_show_segment(self):
        client = _RecordingClient(_FakeResponse(payload={"ratings": []}))
        asyncio.run(
            ratings.fetch_rating(
                client, "key", [], "tv", media_id="1399", provider="tmdb"
            )
        )
        self.assertEqual(client.urls, ["https://api.mdblist.com/tmdb/show/1399"])

    def test_404_on_the_tmdb_route_is_an_empty_result_not_a_failure(self):
        # A poster still renders from TMDB metadata with an N/A score.
        client = _RecordingClient(_FakeResponse(status_code=404))
        result = self._fetch(client, media_id="1698026", provider="tmdb")
        self.assertEqual(result, ({}, "Unknown", None, [], None))
        # A TMDB id is per type: never asked again as a show.
        self.assertEqual(client.urls, ["https://api.mdblist.com/tmdb/movie/1698026"])

    def test_imdb_404_is_asked_again_as_the_other_type(self):
        # Fleabag asked as a movie (its IMDb id once resolved to a duplicate
        # TMDB movie): MDBList files it as a show only.
        class _ByType(_RecordingClient):
            async def get(self, url, **kwargs):
                self.urls.append(url)
                if "/show/" in url:
                    return _FakeResponse(payload={"ratings": [{"source": "imdb", "value": 8.7}]})
                return _FakeResponse(status_code=404)

        client = _ByType(None)
        result = self._fetch(client, media_id="tt5687612", provider="imdb")
        self.assertEqual(client.urls, ["https://api.mdblist.com/imdb/movie/tt5687612",
                                       "https://api.mdblist.com/imdb/show/tt5687612"])
        self.assertEqual(result[0], {"imdb": 8.7})

    def test_imdb_unknown_as_either_type_is_an_empty_result(self):
        client = _RecordingClient(_FakeResponse(status_code=404))
        result = self._fetch(client, media_id="tt0000001", provider="imdb")
        self.assertEqual(result, ({}, "Unknown", None, [], None))
        self.assertEqual(len(client.urls), 2)


class PosterBoundaryTests(unittest.TestCase):
    """Validation only. No TMDB key is configured, so a request that clears
    validation stops at the key check — which is exactly the signal we want."""

    def setUp(self):
        self.access_key = main._cfg.ACCESS_KEY
        self.tmdb_key = main._cfg.SERVER_TMDB_KEY
        self.cinemeta = main._cfg.CINEMETA_ENABLED
        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = ""
        # Off, so an IMDb-only request has no key-less spine to fall to and
        # stops at the key check like a TMDB-only one.
        main._cfg.CINEMETA_ENABLED = False
        self.client = TestClient(main.app)

    def tearDown(self):
        main._cfg.ACCESS_KEY = self.access_key
        main._cfg.SERVER_TMDB_KEY = self.tmdb_key
        main._cfg.CINEMETA_ENABLED = self.cinemeta

    def test_tmdb_only_request_clears_validation(self):
        resp = self.client.get("/poster", params={"tmdb_id": "1698026", "type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("TMDB API key", resp.json()["detail"])

    def test_imdb_only_request_clears_validation(self):
        resp = self.client.get("/poster", params={"imdb_id": "tt0903747", "type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("TMDB API key", resp.json()["detail"])

    def test_missing_both_ids_names_both(self):
        resp = self.client.get("/poster", params={"type": "movie"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("tmdb_id", resp.json()["detail"])
        self.assertIn("imdb_id", resp.json()["detail"])

    def test_malformed_optional_imdb_id_still_fails_validation(self):
        resp = self.client.get(
            "/poster", params={"tmdb_id": "1698026", "imdb_id": "nonsense", "type": "movie"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Invalid imdb_id")

    def test_unsubstituted_imdb_placeholder_is_not_a_validation_error(self):
        for literal in ("{imdb_id}", "{imdb_id?}"):
            with self.subTest(literal=literal):
                resp = self.client.get(
                    "/poster",
                    params={"tmdb_id": "1698026", "imdb_id": literal, "type": "movie"},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertIn("TMDB API key", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
