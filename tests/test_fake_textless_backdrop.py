"""TEXTLESS_BACKDROP_FALLBACK: a detected fake textless poster is swapped for
the title's backdrop crop, with our logo on it, when the crop scans clean.

Drives the real /poster handler with the fetchers, the detection cache and
build_poster stubbed, and checks which art and logo reached the compositor.
"""

import unittest

import httpx
from PIL import Image

import cinemeta
import main
import tmdb

POSTER = (200, 0, 0, 255)
BACKDROP = (0, 0, 200, 255)


def _art(colour):
    return Image.new("RGBA", (500, 750), colour)


class FakeTextlessBackdropTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._saved = {
            name: getattr(main._cfg, name) for name in (
                "ACCESS_KEY", "SERVER_TMDB_KEY", "SERVER_MDBLIST_KEYS",
                "CINEMETA_ENABLED", "TEXTLESS_TEXT_DETECTION",
                "TEXTLESS_BACKDROP_FALLBACK", "TEXTLESS_FAKE_REPORT",
                "DISABLE_COMPOSITE_CACHE",
            )
        }
        self._stubs = {
            name: getattr(main, name) for name in (
                "_HTTP_CLIENT", "resolve_tmdb_to_imdb",
                "_coalesced_fetch_poster_metadata", "fetch_backdrop_image",
                "fetch_poster_image", "fetch_logo", "_fetch_metahub_logo",
                "get_cached_text_detection", "build_poster",
            )
        }
        self._retry_delay = tmdb._TRENDING_RETRY_DELAY_SECS
        tmdb._TRENDING_RETRY_DELAY_SECS = 0
        tmdb._trending_source_failed_at.clear()
        self.addCleanup(tmdb._trending_source_failed_at.clear)
        self.addCleanup(setattr, tmdb, "_TRENDING_RETRY_DELAY_SECS", self._retry_delay)

        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.SERVER_MDBLIST_KEYS = []
        main._cfg.CINEMETA_ENABLED = False
        main._cfg.TEXTLESS_TEXT_DETECTION = True
        main._cfg.TEXTLESS_BACKDROP_FALLBACK = True
        main._cfg.TEXTLESS_FAKE_REPORT = False
        main._cfg.DISABLE_COMPOSITE_CACHE = True
        main._HTTP_CLIENT = object()
        main._render_semaphore = None
        main._render_inflight.clear()

        self.logo = Image.new("RGBA", (300, 100), (255, 255, 255, 255))
        self.backdrop_has_text = False
        self.rendered: list[tuple] = []

        async def _meta(client, tmdb_id, key, media_type, lang, secondary=""):
            # Above the foreground vote gate, so every scan result here comes
            # from the (stubbed) detection cache and no OCR runs.
            td = dict(cinemeta._blank_tmdb_data(), imdb_id="tt1129423", vote_count=5000)
            return [18], True, [], "2008", "Fireproof", "/p.jpg", "/b.jpg", td

        async def _poster(client, tmdb_id, media_type, path):
            return _art(POSTER)

        async def _backdrop(client, tmdb_id, path, avoid_text=False):
            return _art(BACKDROP)

        async def _logo(*args, **kwargs):
            return self.logo

        async def _no_logo(*args, **kwargs):
            return None

        async def _linked(client, tmdb_id, media_type, key):
            return "tt1129423"

        def _detection(key):
            if key.startswith("ps:"):
                return True
            if key.startswith("bd:"):
                return self.backdrop_has_text
            return None

        def _build(image, score, genre, cfg, **kwargs):
            self.rendered.append((image.getpixel((0, 0)), kwargs["logo"]))
            return image

        main._coalesced_fetch_poster_metadata = _meta
        main.fetch_poster_image = _poster
        main.fetch_backdrop_image = _backdrop
        main.fetch_logo = _logo
        main._fetch_metahub_logo = _no_logo
        main.resolve_tmdb_to_imdb = _linked
        main.get_cached_text_detection = _detection
        main.build_poster = _build

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)
        for name, value in self._stubs.items():
            setattr(main, name, value)
        main._render_semaphore = None
        main._render_inflight.clear()

    async def _render(self, **extra):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(
                "/poster", params={"tmdb_id": "14438", "type": "movie", **extra})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(self.rendered), 1)
        return self.rendered[0]

    async def test_off_serves_the_fake_poster_without_a_logo(self):
        main._cfg.TEXTLESS_BACKDROP_FALLBACK = False
        pixel, logo = await self._render()
        self.assertEqual(pixel, POSTER)
        self.assertIsNone(logo)

    async def test_on_swaps_in_the_backdrop_with_the_logo(self):
        pixel, logo = await self._render()
        self.assertEqual(pixel, BACKDROP)
        self.assertIs(logo, self.logo)

    async def test_backdrop_crop_with_text_keeps_the_poster(self):
        self.backdrop_has_text = True
        pixel, logo = await self._render()
        self.assertEqual(pixel, POSTER)
        self.assertIsNone(logo)

    async def test_no_logo_keeps_the_poster(self):
        self.logo = None
        pixel, logo = await self._render()
        self.assertEqual(pixel, POSTER)
        self.assertIsNone(logo)

    async def test_textless_request_takes_the_backdrop_without_a_logo(self):
        self.logo = None
        pixel, logo = await self._render(textless="true")
        self.assertEqual(pixel, BACKDROP)
        self.assertIsNone(logo)


if __name__ == "__main__":
    unittest.main()
