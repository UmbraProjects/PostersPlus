"""Cache-Control on a poster response should follow the composite's deadline."""

import time
import unittest

from fastapi import Response

import main


KEY = "tt0087332:620:movie:abc123"


class CompositeCacheControlTests(unittest.TestCase):
    def setUp(self):
        self.disable_composite = main._cfg.DISABLE_COMPOSITE_CACHE
        self.cdn_ttl = main._cfg.CDN_CACHE_TTL
        self.cdn_auto = main._cfg.CDN_CACHE_TTL_AUTO
        main._cfg.DISABLE_COMPOSITE_CACHE = False
        main._cfg.CDN_CACHE_TTL = 0
        main._cfg.CDN_CACHE_TTL_AUTO = False

    def tearDown(self):
        main._cfg.DISABLE_COMPOSITE_CACHE = self.disable_composite
        main._cfg.CDN_CACHE_TTL = self.cdn_ttl
        main._cfg.CDN_CACHE_TTL_AUTO = self.cdn_auto

    def _cache_control(self, expires_in=None, provisional=False, key=KEY):
        resp = Response(content=b"")
        expires_at = None if expires_in is None else int(time.time()) + expires_in
        main._apply_poster_cache_headers(resp, key, provisional, expires_at)
        return resp.headers.get("cache-control")

    def _max_age(self, cache_control):
        self.assertIsNotNone(cache_control)
        for part in cache_control.split(","):
            part = part.strip()
            if part.startswith("max-age="):
                return int(part.split("=", 1)[1])
        self.fail(f"no max-age in {cache_control!r}")

    def test_auto_advertises_the_composite_deadline(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        self.assertAlmostEqual(self._max_age(self._cache_control(86400)), 86400, delta=2)

    def test_auto_lets_a_stable_title_keep_the_full_composite_ttl(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        self.assertAlmostEqual(self._max_age(self._cache_control(604800)), 604800, delta=2)

    def test_auto_says_nothing_when_there_is_no_composite(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        self.assertIsNone(self._cache_control(None, key=None))

    def test_an_expired_composite_is_served_but_must_be_revalidated(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        self.assertEqual(
            self._cache_control(-60), "public, max-age=0, must-revalidate"
        )

    def test_a_configured_ttl_is_capped_by_the_composite_deadline(self):
        main._cfg.CDN_CACHE_TTL = 604800
        self.assertAlmostEqual(self._max_age(self._cache_control(86400)), 86400, delta=2)

    def test_the_composite_deadline_never_extends_a_configured_ttl(self):
        main._cfg.CDN_CACHE_TTL = 300
        self.assertEqual(self._max_age(self._cache_control(604800)), 300)

    def test_a_configured_ttl_still_applies_with_no_deadline_to_hand(self):
        main._cfg.CDN_CACHE_TTL = 86400
        self.assertEqual(self._cache_control(None), "public, max-age=86400")

    def test_zero_still_means_send_nothing(self):
        self.assertIsNone(self._cache_control(86400))

    def test_a_provisional_render_is_still_never_given_freshness(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        self.assertEqual(
            self._cache_control(86400, provisional=True),
            "no-store, no-cache, must-revalidate",
        )

    def test_composite_caching_off_still_wins(self):
        main._cfg.CDN_CACHE_TTL_AUTO = True
        main._cfg.DISABLE_COMPOSITE_CACHE = True
        self.assertEqual(
            self._cache_control(86400), "no-store, no-cache, must-revalidate"
        )


if __name__ == "__main__":
    unittest.main()
