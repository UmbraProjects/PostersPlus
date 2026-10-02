"""Original art as a Logo Priority source ("art"): a title with no logo from
the sources above it is served on its original art (title baked in) instead
of carrying on to the sources below it, text among them.

Drives the real /poster handler with the fetchers and build_poster stubbed,
and checks which art, logo and text title reached the compositor.
"""

import unittest
from unittest import mock

import httpx
from PIL import Image

import cinemeta
import main
import tmdb

TEXTLESS = (200, 0, 0, 255)
ORIGINAL = (0, 200, 0, 255)
BACKDROP = (0, 0, 200, 255)
TEXT_BACKDROP = (200, 200, 0, 255)


def _art(colour):
    return Image.new("RGBA", (500, 750), colour)


class LogoPriorityArtTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._saved = {
            name: getattr(main._cfg, name) for name in (
                "ACCESS_KEY", "SERVER_TMDB_KEY", "SERVER_MDBLIST_KEYS",
                "CINEMETA_ENABLED", "TEXTLESS_TEXT_DETECTION",
                "DISABLE_COMPOSITE_CACHE",
            )
        }
        self._stubs = {
            name: getattr(main, name) for name in (
                "_HTTP_CLIENT", "resolve_tmdb_to_imdb",
                "_coalesced_fetch_poster_metadata", "fetch_poster_image",
                "fetch_logo", "_fetch_metahub_logo", "build_poster",
                "fetch_landscape_image", "build_landscape",
            )
        }
        self._retry_delay = tmdb._TRENDING_RETRY_DELAY_SECS
        tmdb._TRENDING_RETRY_DELAY_SECS = 0
        tmdb._trending_source_failed_at.clear()
        self.addCleanup(tmdb._trending_source_failed_at.clear)
        self.addCleanup(setattr, tmdb, "_TRENDING_RETRY_DELAY_SECS", self._retry_delay)
        tvdb_patch = mock.patch.object(main.tvdb, "tvdb_logo", mock.AsyncMock(return_value=None))
        tvdb_patch.start()
        self.addCleanup(tvdb_patch.stop)

        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = "k"
        main._cfg.SERVER_MDBLIST_KEYS = []
        main._cfg.CINEMETA_ENABLED = False
        main._cfg.TEXTLESS_TEXT_DETECTION = False
        main._cfg.DISABLE_COMPOSITE_CACHE = True
        main._HTTP_CLIENT = object()
        main._render_semaphore = None
        main._render_inflight.clear()

        self.logo = Image.new("RGBA", (300, 100), (255, 255, 255, 255))
        # Languages fetch_logo has a logo in; "en" stands for English.
        self.logo_languages: set[str] = set()
        self.original_poster = "/orig.jpg"
        self.text_backdrop = "/tb.jpg"
        self.priorities: list[str] = []
        self.rendered: list[tuple] = []

        async def _meta(client, tmdb_id, key, media_type, lang, secondary=""):
            td = dict(cinemeta._blank_tmdb_data(), imdb_id="tt1129423",
                      original_language="en",
                      original_poster_path=self.original_poster,
                      text_backdrop_path=self.text_backdrop)
            return [18], True, [], "2008", "Fireproof", "/p.jpg", "/b.jpg", td

        async def _poster(client, tmdb_id, media_type, path):
            return _art(ORIGINAL if path == "/orig.jpg" else TEXTLESS)

        async def _logo(client, logos, logo_language, *, logo_priority, **kwargs):
            self.priorities.append(logo_priority)
            steps = tmdb.logo_language_steps(
                logo_language, kwargs.get("original_language"), logo_priority)
            return self.logo if self.logo_languages & set(steps) else None

        async def _no_logo(*args, **kwargs):
            return None

        async def _linked(client, tmdb_id, media_type, key):
            return "tt1129423"

        def _build(image, score, genre, cfg, **kwargs):
            self.rendered.append((image.getpixel((0, 0)), kwargs["logo"],
                                  kwargs["fallback_title"]))
            return image

        async def _landscape(client, tmdb_id, path):
            return _art(TEXT_BACKDROP if path == "/tb.jpg" else BACKDROP)

        main._coalesced_fetch_poster_metadata = _meta
        main.fetch_landscape_image = _landscape
        main.build_landscape = _build
        main.fetch_poster_image = _poster
        main.fetch_logo = _logo
        main._fetch_metahub_logo = _no_logo
        main.resolve_tmdb_to_imdb = _linked
        main.build_poster = _build

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)
        for name, value in self._stubs.items():
            setattr(main, name, value)
        main._render_semaphore = None
        main._render_inflight.clear()

    async def _render(self, logo_priority, **extra):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/poster", params={
                "tmdb_id": "14438", "type": "movie", "logo_language": "fr",
                "logo_priority": logo_priority, **extra})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(self.rendered), 1)
        return self.rendered[0]

    async def test_without_art_a_missing_logo_draws_text(self):
        pixel, logo, title = await self._render("native,english,text")
        self.assertEqual(pixel, TEXTLESS)
        self.assertIsNone(logo)
        self.assertEqual(title, "Fireproof")

    async def test_art_above_text_serves_original_art(self):
        pixel, logo, title = await self._render("native,english,art,text")
        self.assertEqual(pixel, ORIGINAL)
        self.assertIsNone(logo)
        self.assertIsNone(title)

    async def test_a_logo_above_art_wins(self):
        self.logo_languages = {"en"}
        pixel, logo, title = await self._render("native,english,art,text")
        self.assertEqual(pixel, TEXTLESS)
        self.assertIs(logo, self.logo)
        # Looked up once, before the art was picked, not again alongside it.
        self.assertEqual(self.priorities, ["native,english"])

    async def test_art_above_a_logo_source_beats_it(self):
        self.logo_languages = {"en"}
        pixel, logo, _ = await self._render("native,art,english,text")
        self.assertEqual(pixel, ORIGINAL)
        self.assertIsNone(logo)

    async def test_no_original_art_carries_on_below_art(self):
        self.original_poster = None
        self.logo_languages = {"en"}
        pixel, logo, _ = await self._render("native,art,english,text")
        self.assertEqual(pixel, TEXTLESS)
        self.assertIs(logo, self.logo)
        self.assertEqual(self.priorities, ["native", "english,text"])

    async def test_no_original_art_and_no_logo_draws_text(self):
        self.original_poster = None
        pixel, logo, title = await self._render("native,art,english,text")
        self.assertEqual(pixel, TEXTLESS)
        self.assertIsNone(logo)
        self.assertEqual(title, "Fireproof")

    async def test_art_first_skips_logos_entirely(self):
        self.logo_languages = {"fr", "en"}
        pixel, logo, _ = await self._render("art,native,english,text")
        self.assertEqual(pixel, ORIGINAL)
        self.assertIsNone(logo)
        self.assertEqual(self.priorities, [])

    async def test_textless_mode_ignores_art(self):
        pixel, logo, title = await self._render("native,art,text", textless="true")
        self.assertEqual(pixel, TEXTLESS)
        self.assertIsNone(logo)
        self.assertIsNone(title)

    async def test_landscape_art_serves_the_text_backdrop(self):
        pixel, logo, title = await self._render("native,english,art,text", shape="landscape")
        self.assertEqual(pixel, TEXT_BACKDROP)
        self.assertIsNone(logo)
        self.assertIsNone(title)

    async def test_landscape_logo_above_art_wins(self):
        self.logo_languages = {"en"}
        pixel, logo, _ = await self._render("native,english,art,text", shape="landscape")
        self.assertEqual(pixel, BACKDROP)
        self.assertIs(logo, self.logo)
        self.assertEqual(self.priorities, ["native,english"])

    async def test_landscape_without_a_text_backdrop_carries_on(self):
        self.text_backdrop = None
        self.logo_languages = {"en"}
        pixel, logo, _ = await self._render("native,art,english,text", shape="landscape")
        self.assertEqual(pixel, BACKDROP)
        self.assertIs(logo, self.logo)
        self.assertEqual(self.priorities, ["native", "english,text"])

    async def test_landscape_without_art_draws_text(self):
        pixel, logo, title = await self._render("native,english,text", shape="landscape")
        self.assertEqual(pixel, BACKDROP)
        self.assertIsNone(logo)
        self.assertEqual(title, "Fireproof")


if __name__ == "__main__":
    unittest.main()
