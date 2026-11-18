"""Hardening from the security audit: nothing public echoes a credential, and
no request parameter or setting can be turned into a crash or an outsized
render."""

import logging
import time
import unittest
from unittest import mock

import httpx
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import admin
import main
import quality
import settings
import watchlist


class WatchlistErrorTests(unittest.TestCase):
    """status() is served on /server-caps and /stats."""

    def _status_error(self, url):
        request = httpx.Request("GET", url)
        return httpx.HTTPStatusError(
            "Client error '429 Too Many Requests' for url " + url,
            request=request, response=httpx.Response(429, request=request),
        )

    def test_status_error_keeps_the_key_out(self):
        exc = self._status_error("https://api.mdblist.com/watchlist/items?apikey=SERVERKEY&limit=1000")
        described = watchlist._describe_error(exc)
        self.assertEqual(described, "HTTP 429 from https://api.mdblist.com/watchlist/items")
        self.assertNotIn("SERVERKEY", described)

    def test_transport_error_keeps_the_key_out(self):
        request = httpx.Request("GET", "https://api.mdblist.com/watchlist/items?apikey=SERVERKEY")
        described = watchlist._describe_error(httpx.ConnectError("boom ?apikey=SERVERKEY", request=request))
        self.assertEqual(described, "ConnectError contacting https://api.mdblist.com/watchlist/items")

    def test_own_messages_are_kept(self):
        described = watchlist._describe_error(RuntimeError("WATCHLIST_SOURCE=trakt needs TRAKT_CLIENT_ID"))
        self.assertIn("needs TRAKT_CLIENT_ID", described)


class _AppTest(unittest.TestCase):
    def setUp(self):
        self._access_key = main._cfg.ACCESS_KEY
        self.client = TestClient(main.app)

    def tearDown(self):
        main._cfg.ACCESS_KEY = self._access_key


class FallbackGalleryTests(_AppTest):
    PAYLOAD = '"><script>alert(1)</script>'

    def test_open_instance_does_not_echo_the_key(self):
        main._cfg.ACCESS_KEY = None
        resp = self.client.get("/debug/fallback-gallery", params={"access_key": self.PAYLOAD})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("<script>", resp.text)
        self.assertNotIn("access_key=", resp.text)

    def test_the_key_is_encoded_where_it_is_carried(self):
        main._cfg.ACCESS_KEY = 'k"<&'
        resp = self.client.get("/debug/fallback-gallery", params={"access_key": 'k"<&'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access_key=k%22%3C%26", resp.text)
        self.assertNotIn('k"<', resp.text)


class AccessKeyTests(_AppTest):
    def test_non_ascii_key_is_refused_not_a_500(self):
        main._cfg.ACCESS_KEY = "secret-key"
        for path in ("/server-caps", "/stats", "/debug/canvas"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, params={"access_key": "é"}).status_code, 403)

    def test_key_ok(self):
        main._cfg.ACCESS_KEY = "secret-key"
        self.assertTrue(main._key_ok("secret-key"))
        self.assertFalse(main._key_ok("secret-kez"))
        self.assertFalse(main._key_ok(""))
        self.assertFalse(main._key_ok(None))
        self.assertFalse(main._key_ok("é"))
        main._cfg.ACCESS_KEY = None
        self.assertTrue(main._key_ok(""))


class GradientClampTests(unittest.TestCase):
    def _cfg(self, **params):
        return main.build_request_config({"top_gradient": "custom", "bottom_gradient": "custom", **params})

    def test_height_and_opacity_are_clamped(self):
        cfg = self._cfg(top_gradient_height="500", bottom_gradient_height="-3",
                        top_gradient_opacity="9000", bottom_gradient_opacity="-1")
        self.assertEqual(cfg.top_gradient_height, 1.0)
        self.assertEqual(cfg.bottom_gradient_height, 0.0)
        self.assertEqual(cfg.top_gradient_opacity, 255.0)
        self.assertEqual(cfg.bottom_gradient_opacity, 0.0)

    def test_in_range_values_are_kept(self):
        cfg = self._cfg(top_gradient_height="0.3", top_gradient_opacity="0.8")
        self.assertEqual((cfg.top_gradient_height, cfg.top_gradient_opacity), (0.3, 0.8))

    def test_nan_and_garbage_fall_back_to_the_default(self):
        default = main.RequestConfig()
        cfg = self._cfg(top_gradient_height="nan", top_gradient_opacity="NaN",
                        bottom_gradient_height="tall")
        self.assertEqual(cfg.top_gradient_height, default.top_gradient_height)
        self.assertEqual(cfg.top_gradient_opacity, default.top_gradient_opacity)
        self.assertEqual(cfg.bottom_gradient_height, default.bottom_gradient_height)

    def test_nan_no_longer_slips_through_other_float_params(self):
        default = main.RequestConfig()
        cfg = main.build_request_config({"badge_anchor_x": "nan"})
        self.assertEqual(cfg.badge_anchor_x, default.badge_anchor_x)


class LogRedactionTests(unittest.TestCase):
    def test_scraper_logs_show_only_the_host(self):
        url = "https://torrentio.strem.fun/realdebrid=DEBRIDKEY/stream/movie/tt1.json"
        self.assertEqual(quality._scraper_host(url), "https://torrentio.strem.fun")

    def test_query_keys_in_an_upstream_error_are_redacted(self):
        # The existing filter on the root handler already covers httpx errors
        # logged with their message: api_key= and apikey= are masked.
        record = logging.LogRecord(
            "main", logging.ERROR, __file__, 1,
            "Upstream HTTP 401: Client error for url "
            "'https://api.themoviedb.org/3/movie/1?api_key=USERKEY&language=en'", None, None,
        )
        main._TruncateUrlFilter().filter(record)
        self.assertNotIn("USERKEY", record.getMessage())

    def test_the_admin_key_in_the_dashboard_address_is_redacted(self):
        # uvicorn's access line for the one page load that carries it.
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("203.0.113.9:1", "GET", "/admin?admin_key=OPERATORSECRET", "1.1", 200), None,
        )
        main._TruncateUrlFilter().filter(record)
        self.assertNotIn("OPERATORSECRET", record.getMessage())
        self.assertEqual(main._TruncateUrlFilter._redact("/admin?admin_key=S3CRET&x=1"),
                         "/admin?admin_key=***&x=1")


class ConfiguratorPageHeaderTests(unittest.TestCase):
    """The configurator holds the user's API keys and the access key, so it
    may be framed only by this instance and the origins the operator names."""

    def setUp(self):
        self.client = TestClient(main.app)
        p = mock.patch.object(main._cfg, "ACCESS_KEY", None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_page_and_its_revalidation_carry_the_headers(self):
        first = self.client.get("/")
        again = self.client.get("/", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        for response in (first, again):
            with self.subTest(status=response.status_code):
                self.assertIn("frame-ancestors 'self';", response.headers["content-security-policy"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["referrer-policy"], "same-origin")

    def test_the_fallback_gallery_carries_them_too(self):
        r = self.client.get("/debug/fallback-gallery")
        self.assertIn("frame-ancestors", r.headers["content-security-policy"])

    def test_named_origins_may_frame_it_and_junk_is_dropped(self):
        with mock.patch.object(main._cfg, "CONFIGURATOR_FRAME_ANCESTORS",
                               "https://home.example.com/, http://10.0.0.5:7575 javascript:x 'none'"):
            csp = main._configurator_page_headers()["Content-Security-Policy"]
        self.assertTrue(csp.startswith(
            "frame-ancestors 'self' https://home.example.com http://10.0.0.5:7575;"), csp)
        with mock.patch.object(main._cfg, "CONFIGURATOR_FRAME_ANCESTORS", "*"):
            self.assertIn("frame-ancestors 'self' *;",
                          main._configurator_page_headers()["Content-Security-Policy"])


class AdminLockoutTests(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(admin._failures), dict(admin._lockouts))
        admin._failures.clear()
        admin._lockouts.clear()

    def tearDown(self):
        admin._failures.clear()
        admin._lockouts.clear()
        admin._failures.update(self._saved[0])
        admin._lockouts.update(self._saved[1])

    def test_tables_stay_bounded(self):
        with mock.patch.object(admin, "_MAX_TRACKED", 10):
            for i in range(200):
                admin._record_failure(f"198.51.100.{i}")
            self.assertLessEqual(len(admin._failures), 11)

    def test_sweep_drops_aged_out_entries_and_keeps_live_lockouts(self):
        now = time.monotonic()
        admin._failures["old"] = [now - admin._FAIL_WINDOW - 1]
        admin._failures["new"] = [now]
        admin._lockouts["lapsed"] = now - 1
        admin._lockouts["live"] = now + 60
        admin._sweep(now)
        self.assertEqual(set(admin._failures), {"new"})
        self.assertEqual(set(admin._lockouts), {"live"})

    def test_lockout_from_a_private_address_names_the_proxy_setting(self):
        with self.assertLogs("admin", level="WARNING") as logs:
            for _ in range(admin._FAIL_LIMIT):
                admin._record_failure("172.18.0.5")
        self.assertIn("FORWARDED_ALLOW_IPS", "\n".join(logs.output))
        self.assertFalse(admin._looks_like_proxy("8.8.8.8"))
        self.assertFalse(admin._looks_like_proxy("127.0.0.1"))


class PublicBaseTests(unittest.TestCase):
    def setUp(self):
        self._saved = (main._cfg.PUBLIC_URL, main._cfg.TRENDING_CATALOGS_ENABLED, main._cfg.ACCESS_KEY)
        main._cfg.TRENDING_CATALOGS_ENABLED = True
        main._cfg.ACCESS_KEY = None
        self.client = TestClient(main.app)

    def tearDown(self):
        main._cfg.PUBLIC_URL, main._cfg.TRENDING_CATALOGS_ENABLED, main._cfg.ACCESS_KEY = self._saved

    def _catalog(self, headers):
        seen = {}

        async def _stub(key, ctype, cid, extra, poster_cfg, lists=None, landscape_catalogs=frozenset()):
            seen["base"] = poster_cfg[0]
            return JSONResponse({"metas": []})
        cfg_seg = main._ADDON_CFG_PREFIX + "e30"
        with mock.patch.object(main, "_trending_catalog", _stub):
            resp = self.client.get(f"/trending/{cfg_seg}/catalog/movie/pp.trending.movie.json",
                                   headers=headers)
        return resp, seen.get("base")

    def test_forwarded_host_is_used_but_varied_on_without_a_public_url(self):
        main._cfg.PUBLIC_URL = ""
        resp, base = self._catalog({"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"})
        self.assertEqual(base, "https://evil.example")
        self.assertIn("X-Forwarded-Host", resp.headers.get("vary", ""))

    def test_public_url_wins_over_headers(self):
        main._cfg.PUBLIC_URL = "https://posters.example.com"
        resp, base = self._catalog({"X-Forwarded-Host": "evil.example"})
        self.assertEqual(base, "https://posters.example.com")
        self.assertNotIn("X-Forwarded-Host", resp.headers.get("vary", ""))


class SettingsFiniteTests(unittest.TestCase):
    def test_non_finite_floats_are_rejected(self):
        bounded = settings.Setting(key="X", default="1", group="g", kind="float", min=0, max=10)
        unbounded = settings.Setting(key="Y", default="1", group="g", kind="float")
        for setting in (bounded, unbounded):
            for raw in ("nan", "NaN", "inf", "-inf"):
                with self.subTest(setting=setting.key, raw=raw), self.assertRaises(ValueError):
                    settings.normalise(setting, raw)
        self.assertEqual(settings.normalise(bounded, "2.5"), "2.5")


if __name__ == "__main__":
    unittest.main()
