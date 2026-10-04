"""A later anime season's own landscape art (anime_season, ANIME_SEASON_ART)."""

import asyncio
import io
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import httpx
import numpy as np
from PIL import Image

import anime_ids
import anime_resolve
import anime_season as asn
import text_detect

COVER = "https://media.kitsu.app/anime/45857/cover_image/cover.jpg"


def _jpeg(size) -> bytes:
    # A gradient: a flat colour is taken for a blank rendition.
    w, h = size
    pixels = np.zeros((h, w, 3), dtype=np.uint8)
    pixels[..., 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    pixels[..., 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    buf = io.BytesIO()
    Image.fromarray(pixels).save(buf, format="JPEG")
    return buf.getvalue()


class SeasonArtTests(unittest.TestCase):
    def setUp(self):
        self.cache, self.ttls, self.stored = {}, {}, {}
        self.covers = {45857: COVER}
        self.cover_bytes = _jpeg((4096, 1210))
        self.has_text = False
        self.old_rows = False
        self.seasons = {}
        self.calls = []

        async def metadata(client, namespace, anime_id):
            self.calls.append(("kitsu", anime_id))
            cover = self.covers.get(anime_id)
            if cover is None:
                return None
            return ([16], False, [], None, "", None, None,
                    {} if self.old_rows else {"anime_banner": cover})

        def store(key, value, ttl):
            self.cache[key], self.ttls[key] = value, ttl
        for p in (
            mock.patch.object(asn, "get_cached_tvdb_json", self.cache.get),
            mock.patch.object(asn, "set_cached_tvdb_json", store),
            mock.patch.object(asn.anime, "fetch_anime_metadata", metadata),
            mock.patch.object(asn.anime_ids, "kitsu_for_anilist", {145064: 45857}.get),
            mock.patch("tmdb._store_art", lambda key, image: self.stored.__setitem__(key, image.size)),
            mock.patch.object(text_detect, "cover_has_text", lambda image: self.has_text),
            mock.patch.object(asn._cfg, "TEXTLESS_TEXT_DETECTION", True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _client(self):
        def handler(request):
            self.calls.append(str(request.url.path))
            if request.url.path.startswith("/api/edge/anime/"):
                return httpx.Response(200, json={"data": {"attributes": {
                    "coverImage": {"original": COVER}}}})
            if request.url.host == "media.kitsu.app":
                return httpx.Response(200, content=self.cover_bytes)
            if request.url.path.startswith("/3/tv/"):
                season = int(request.url.path.rsplit("/", 1)[1])
                if season not in self.seasons:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={"episodes": self.seasons[season]})
            return httpx.Response(404)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def _art(self, namespace="kitsu", anime_id=45857, place=anime_ids.SeasonPlace(2, 0)):
        async def go():
            async with self._client() as client:
                return await asn.season_art(client, namespace=namespace, anime_id=anime_id,
                                            tmdb_id="95479", place=place, tmdb_key="k")
        return asyncio.run(go())

    def test_the_seasons_kitsu_cover_first(self):
        self.assertEqual(self._art(), COVER)
        # Stored at the canvas, under the key the render fetches it by.
        import tmdb
        self.assertIn(tmdb.landscape_image_cache_key("95479", COVER), self.stored)

    def test_metadata_cached_before_it_carried_the_cover(self):
        self.old_rows = True
        self.assertEqual(self._art(), COVER)
        self.assertIn("/api/edge/anime/45857", self.calls)

    def test_an_anilist_request_borrows_the_kitsu_cover(self):
        self.assertEqual(self._art(namespace="anilist", anime_id=145064), COVER)
        self.assertIn(("kitsu", 45857), self.calls)

    def test_a_cover_with_lettering_falls_to_the_first_episodes_still(self):
        self.has_text = True
        self.seasons = {2: [{"still_path": "/ep1.jpg"}, {"still_path": "/ep2.jpg"}]}
        self.assertEqual(self._art(), "/ep1.jpg")

    def test_a_small_cover_falls_through(self):
        self.cover_bytes = _jpeg((844, 306))     # its 16:9 cut is 306 px tall
        self.seasons = {2: [{"still_path": "/ep1.jpg"}]}
        self.assertEqual(self._art(), "/ep1.jpg")
        self.assertEqual(self.stored, {})

    def test_a_cours_still_is_its_first_episode(self):
        self.covers = {}
        self.seasons = {1: [{"still_path": f"/ep{n}.jpg"} for n in range(1, 25)]}
        self.assertEqual(self._art(place=anime_ids.SeasonPlace(1, 12)), "/ep13.jpg")

    def test_a_season_tmdb_runs_as_one_keeps_the_show_backdrop(self):
        self.covers = {}
        self.assertIsNone(self._art())          # /season/2 is a 404
        self.assertEqual(list(self.ttls.values()), [asn._MISS_TTL])

    def test_a_special_without_an_offset_isnt_guessed(self):
        self.covers = {}
        self.seasons = {0: [{"still_path": "/sp1.jpg"}]}
        self.assertIsNone(self._art(place=anime_ids.SeasonPlace(0, 0)))

    def test_a_sequel_found_by_prequel_has_no_place_and_takes_only_the_cover(self):
        self.assertEqual(self._art(place=None), COVER)
        self.covers = {}
        self.assertIsNone(self._art(anime_id=1, place=None))

    def test_the_pick_is_cached(self):
        self.assertEqual(self._art(), COVER)
        self.calls.clear()
        self.assertEqual(self._art(), COVER)
        self.assertEqual(self.calls, [])

    def test_a_failure_is_never_the_renders_and_is_held_briefly(self):
        self.covers = {}
        self.seasons = {2: [{"still_path": "/ep1.jpg"}]}

        def broken(request):
            return httpx.Response(500)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
                return await asn.season_art(client, namespace="kitsu", anime_id=45857,
                                            tmdb_id="95479", place=anime_ids.SeasonPlace(2, 0),
                                            tmdb_key="k")
        self.assertIsNone(asyncio.run(go()))
        self.assertEqual(list(self.ttls.values()), [asn._FAIL_TTL])


class SequelFlagTests(unittest.TestCase):
    def test_only_a_prequel_resolution_counts(self):
        cache = {"animeres:v1:anilist:1:tv": {"tmdb_id": "9", "via": "prequel:1"},
                 "animeres:v1:anilist:2:tv": {"tmdb_id": "9", "via": "search"}}
        with mock.patch.object(anime_resolve, "get_cached_tvdb_json", cache.get):
            self.assertTrue(anime_resolve.resolved_as_sequel("anilist", 1, "series"))
            self.assertFalse(anime_resolve.resolved_as_sequel("anilist", 2, "series"))
            self.assertFalse(anime_resolve.resolved_as_sequel("anilist", 3, "series"))


class CoverTextTests(unittest.TestCase):
    """cover_has_text, against a fake detector and recogniser."""

    def _scan(self, boxes, reads):
        def box(x0, y0, x1, y1):
            return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
        image = Image.new("RGB", (1000, 563))
        found = [box(*b) for b, _s in boxes]

        class OCR:
            def __call__(self, crop, **kw):
                txts, scores = reads.pop(0)
                return SimpleNamespace(txts=txts, scores=scores)

        @contextmanager
        def borrow():
            yield OCR()
        with mock.patch.object(text_detect, "_detect",
                               lambda img: (found, [s for _b, s in boxes], 1000, 563)), \
                mock.patch.object(text_detect, "_borrow_ocr", borrow):
            return text_detect.cover_has_text(image)

    def test_a_kanji_logo_is_text(self):
        self.assertTrue(self._scan([((200, 300, 800, 345), 0.80)],
                                   [(("異世界行ったら本気だす",), (0.95,))]))

    def test_a_latin_title_is_text(self):
        self.assertTrue(self._scan([((0, 100, 450, 340), 0.76)], [(("lsgt",), (0.66,))]))

    def test_a_stray_glyph_or_a_weak_read_is_scenery(self):
        self.assertFalse(self._scan([((100, 100, 400, 300), 0.77)], [(("M",), (0.34,))]))
        self.assertFalse(self._scan([((100, 100, 400, 300), 0.71)], [(("江",), (0.12,))]))

    def test_small_or_unsure_boxes_arent_read(self):
        # 0.5% of the frame ("ZURMEDS." on a clean cover), and a 0.45 box.
        self.assertFalse(self._scan([((10, 10, 80, 50), 0.71), ((100, 100, 400, 300), 0.45)], []))


if __name__ == "__main__":
    unittest.main()
