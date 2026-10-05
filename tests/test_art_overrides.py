"""Operator art overrides (dashboard → Artwork) and the TVDB poster source."""

import asyncio
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import httpx
from PIL import Image

import art_overrides as ao
import main
import tvdb


def _ov(slot, language="", path="/p.jpg", sources=("tmdb", "fanart", "tvdb")):
    return ao.Override(slot, language, path, "tmdb", frozenset(sources), "", 0.0)


class ValidationTests(unittest.TestCase):
    def test_provider_comes_from_the_path(self):
        self.assertEqual(ao.provider_of("/abc123.jpg"), "tmdb")
        self.assertEqual(ao.provider_of("/logo.svg"), "tmdb")
        self.assertEqual(ao.provider_of("https://assets.fanart.tv/fanart/movies/1/p.jpg"), "fanart")
        self.assertEqual(ao.provider_of("https://artworks.thetvdb.com/banners/p.jpg"), "tvdb")

    def test_other_hosts_and_decorated_urls_are_refused(self):
        for bad in (
            "", "abc.jpg", "/../etc/passwd", "https://example.com/p.jpg",
            "https://assets.fanart.tv:8443/p.jpg", "https://u:p@assets.fanart.tv/p.jpg",
            "https://artworks.thetvdb.com/p.jpg?x=1", "file:///etc/passwd",
            "https://artworks.thetvdb.com.evil.test/p.jpg",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ao.provider_of(bad)

    def test_languages_per_slot(self):
        self.assertEqual(ao.normalise_language("textless", "en"), "")
        self.assertEqual(ao.normalise_language("original", "PT_BR"), "pt-br")
        self.assertEqual(ao.normalise_language("logo", "null"), "null")
        with self.assertRaises(ValueError):
            ao.normalise_language("original", "null")
        with self.assertRaises(ValueError):
            ao.normalise_language("logo", "")

    def test_poster_needs_a_source_and_logo_has_none(self):
        self.assertEqual(ao.normalise_sources("logo", ["tmdb"]), frozenset())
        self.assertEqual(ao.normalise_sources("textless", ["tvdb", "junk"]), frozenset({"tvdb"}))
        with self.assertRaises(ValueError):
            ao.normalise_sources("original", [])


class PickPosterTests(unittest.TestCase):
    def test_textless_only_for_ticked_sources(self):
        entry = {"textless": {"": _ov("textless", sources=("tmdb",))}}
        pick = lambda source: ao.pick_poster(
            entry, original=False, source=source, language_order=["en"],
            has_language=lambda _: True)
        self.assertIsNotNone(pick("tmdb"))
        self.assertIsNone(pick("fanart"))
        self.assertIsNone(pick(None))  # anime cover / Cinemeta art

    def test_textless_override_does_not_touch_original_art(self):
        entry = {"textless": {"": _ov("textless")}}
        self.assertIsNone(ao.pick_poster(
            entry, original=True, source="tmdb", language_order=["en"],
            has_language=lambda _: False))

    def test_original_follows_the_language_order(self):
        entry = {"original": {"en": _ov("original", "en", "/en.jpg")}}
        # A German user whose title has a German poster keeps it.
        self.assertIsNone(ao.pick_poster(
            entry, original=True, source="tmdb", language_order=["de", "en"],
            has_language=lambda lang: lang == "de"))
        # With no German poster the order reaches English: the override.
        self.assertEqual(ao.pick_poster(
            entry, original=True, source="tmdb", language_order=["de", "en"],
            has_language=lambda lang: lang == "en").path, "/en.jpg")
        # An override for a language before the TMDB one wins.
        entry["original"]["de"] = _ov("original", "de", "/de.jpg")
        self.assertEqual(ao.pick_poster(
            entry, original=True, source="tmdb", language_order=["de", "en"],
            has_language=lambda _: True).path, "/de.jpg")


class CinemetaOverrideTests(unittest.TestCase):
    def test_cinemeta_is_an_original_only_source(self):
        self.assertEqual(ao.normalise_sources("original", ["cinemeta", "tmdb"]),
                         frozenset({"cinemeta", "tmdb"}))
        self.assertEqual(ao.normalise_sources("textless", ["cinemeta", "tmdb"]),
                         frozenset({"tmdb"}))
        with self.assertRaises(ValueError):
            ao.normalise_sources("textless", ["cinemeta"])

    def test_original_override_only_for_cinemeta_when_ticked(self):
        entry = {"original": {"en": _ov("original", "en", "/en.jpg", sources=("tmdb",))}}
        pick = lambda: ao.pick_poster(
            entry, original=True, source="cinemeta", language_order=["en"],
            has_language=lambda _: True)
        self.assertIsNone(pick())
        entry["original"]["en"] = _ov("original", "en", "/en.jpg", sources=("tmdb", "cinemeta"))
        self.assertEqual(pick().path, "/en.jpg")


class CropTests(unittest.TestCase):
    def test_box_on_a_backdrop(self):
        # 1920x1080: the full-height 2:3 window is 720 wide.
        self.assertEqual(ao.Crop(0, 0.5, 1).box(1920, 1080), (0, 0, 720, 1080))
        self.assertEqual(ao.Crop(1, 0.5, 1).box(1920, 1080), (1200, 0, 1920, 1080))
        self.assertEqual(ao.Crop(0.5, 0.5, 2).box(1920, 1080), (780, 270, 1140, 810))
        # A tall image: the width limits the window instead.
        self.assertEqual(ao.Crop(0.5, 0, 1).box(1000, 2000), (0, 0, 1000, 1500))

    def test_parse(self):
        self.assertEqual(ao.parse_crop({"x": 0.25, "y": 1, "zoom": 1.5}), ao.Crop(0.25, 1.0, 1.5))
        self.assertEqual(ao.parse_crop("0.1,0.2,3"), ao.Crop(0.1, 0.2, 3.0))
        self.assertIsNone(ao.parse_crop(None))
        for bad in ({"x": 2}, {"zoom": 0.5}, {"zoom": 9}, "a,b,c", {"x": "nope"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ao.parse_crop(bad)

    def test_rendered_crop_is_the_chosen_window(self):
        import tmdb
        src = Image.new("RGB", (1920, 1080), (0, 0, 255))
        src.paste((255, 0, 0), (1200, 0, 1920, 1080))      # the right-hand 720 px
        buf = io.BytesIO(); src.save(buf, format="PNG")

        class Resp:
            content = buf.getvalue()
            def raise_for_status(self): pass

        class Client:
            async def get(self, url, follow_redirects=False): return Resp()

        with mock.patch.object(tmdb, "_cached_art", lambda *a: None), \
                mock.patch.object(tmdb, "_store_art", lambda *a: None):
            image = asyncio.run(tmdb.fetch_cropped_art(Client(), "550", "/bd.jpg", ao.Crop(1, 0.5, 1)))
        self.assertEqual(image.size, tmdb.poster_canvas())
        self.assertEqual(image.convert("RGB").getpixel((10, 10)), (255, 0, 0))


class PickLandscapeTests(unittest.TestCase):
    def test_landscape_slots(self):
        entry = {
            "landscape": {"": _ov("landscape", path="/bd.jpg", sources=())},
            "landscape_original": {"en": _ov("landscape_original", "en", "/en_bd.jpg", ())},
        }
        pick = lambda original, has: ao.pick_landscape(
            entry, original=original, language_order=["de", "en"], has_language=has)
        self.assertEqual(pick(False, lambda _: True).path, "/bd.jpg")
        self.assertEqual(pick(True, lambda _: False).path, "/en_bd.jpg")
        self.assertIsNone(pick(True, lambda lang: lang == "de"))
        self.assertIsNone(ao.pick_landscape(None, original=False, language_order=[],
                                            has_language=lambda _: False))

    def test_landscape_takes_no_sources_and_textless_no_language(self):
        self.assertEqual(ao.normalise_sources("landscape_original", ["tmdb"]), frozenset())
        self.assertEqual(ao.normalise_language("landscape", "de"), "")
        self.assertEqual(ao.normalise_language("landscape_original", "de"), "de")
        self.assertEqual(ao.image_kind("landscape_original"), "landscape")
        self.assertEqual(ao.image_kind("original"), "poster")


class PickLogoTests(unittest.TestCase):
    def test_logo_walks_steps_and_skips_metahub(self):
        entry = {"logo": {"en": _ov("logo", "en", "/en.png"), "null": _ov("logo", "null", "/n.png")}}
        steps = ["fr", "en", "metahub", "null"]
        self.assertEqual(ao.pick_logo(entry, steps, lambda s: False).path, "/en.png")
        self.assertIsNone(ao.pick_logo(entry, steps, lambda s: s == "fr"))
        self.assertEqual(ao.pick_logo(entry, ["fr", "null"], lambda s: False).path, "/n.png")
        self.assertIsNone(ao.pick_logo(None, steps, lambda s: False))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("""
            CREATE TABLE art_overrides (
                media_type TEXT NOT NULL, tmdb_id TEXT NOT NULL, slot TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
                provider TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, crop TEXT,
                PRIMARY KEY (media_type, tmdb_id, slot, language))
        """)
        self.db.execute("CREATE TABLE art_overrides_remote AS SELECT * FROM art_overrides WHERE 0")
        self.state = {}
        self.invalidated = []
        patches = [
            mock.patch.object(ao, "get_db", lambda: self.db),
            mock.patch.object(ao, "get_app_state", self.state.get),
            mock.patch.object(ao, "set_app_state", self.state.__setitem__),
            mock.patch.object(
                ao, "invalidate_final_posters",
                lambda tid, mt=None, l1_only=False: self.invalidated.append((tid, mt, l1_only))),
            mock.patch.object(ao, "_snapshot", {}),
            mock.patch.object(ao, "_rev", None),
            mock.patch.object(ao, "_checked_at", float("-inf")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_set_replace_and_clear(self):
        ao.set_override("series", "1399", "textless", "", "/a.jpg", ["tmdb"], "GoT")
        self.assertEqual(self.invalidated[-1], ("1399", "tv", False))
        entry = ao.for_title("tv", "1399")
        self.assertEqual(entry["textless"][""].path, "/a.jpg")
        ao.set_override("tv", "1399", "textless", None, "/b.jpg", ["tmdb", "tvdb"])
        self.assertEqual(ao.for_title("series", "1399")["textless"][""].sources,
                         frozenset({"tmdb", "tvdb"}))
        ao.set_override("tv", "1399", "logo", "en",
                        "https://artworks.thetvdb.com/banners/l.png")
        self.assertEqual(len(ao.title_overrides("tv", "1399")), 2)
        self.assertEqual(ao.list_overrides()[0]["title"], "GoT")
        self.assertEqual(ao.clear_override("tv", "1399", "logo", "en"), 1)
        self.assertEqual(ao.clear_override("tv", "1399"), 1)
        self.assertIsNone(ao.for_title("tv", "1399"))

    def test_replaced_or_cleared_custom_images_are_deleted(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(ao._cfg, "CUSTOM_ART_DIR", folder):
            first = ao.store_custom_image(_png(color=(1, 2, 3)), kind="poster")
            second = ao.store_custom_image(_png(color=(9, 9, 9)), kind="poster")
            ao.set_override("movie", "550", "textless", "", first, ["tmdb"])
            ao.set_override("movie", "551", "textless", "", second, ["tmdb"])
            ao.set_override("movie", "550", "textless", "", second, ["tmdb"])
            self.assertIsNone(ao.custom_art_bytes(first))        # replaced, unused
            ao.clear_override("movie", "550")
            self.assertIsNotNone(ao.custom_art_bytes(second))    # 551 still uses it
            ao.clear_override("movie", "551")
            self.assertEqual(os.listdir(folder), [])

    def test_crop_is_stored_for_textless_only(self):
        ao.set_override("movie", "550", "textless", "", "/bd.jpg", ["tmdb"], crop={"x": 0.2, "y": 0.5, "zoom": 1.25})
        self.assertEqual(ao.for_title("movie", "550")["textless"][""].crop, ao.Crop(0.2, 0.5, 1.25))
        self.assertEqual(ao.title_overrides("movie", "550")[0]["crop"], {"x": 0.2, "y": 0.5, "zoom": 1.25})
        ao.set_override("movie", "550", "textless", "", "/bd.jpg", ["tmdb"])
        self.assertIsNone(ao.for_title("movie", "550")["textless"][""].crop)
        with self.assertRaises(ValueError):
            ao.set_override("movie", "550", "original", "en", "/p.jpg", ["tmdb"], crop={"x": 0.5})

    def test_bad_input_writes_nothing(self):
        for args in (
            ("movie", "550", "cover", "", "/a.jpg", ["tmdb"]),
            ("movie", "abc", "textless", "", "/a.jpg", ["tmdb"]),
            ("movie", "550", "textless", "", "https://evil.test/a.jpg", ["tmdb"]),
            ("movie", "550", "original", "en", "/a.jpg", []),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ao.set_override(*args)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM art_overrides").fetchone()[0], 0)

    def test_other_workers_drop_their_copies_when_the_revision_moves(self):
        ao.for_title("movie", "550")                   # first load
        self.db.execute(
            "INSERT INTO art_overrides VALUES ('movie','550','textless','','/x.jpg','tmdb','tmdb','',1,NULL)")
        self.state[ao._REV_KEY] = "2"                  # another worker wrote
        ao._checked_at = float("-inf")
        self.assertEqual(ao.for_title("movie", "550")["textless"][""].path, "/x.jpg")
        self.assertIn(("550", "movie", True), self.invalidated)


def _png(size=(300, 450), mode="RGB", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


class CustomImageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(ao._cfg, "CUSTOM_ART_DIR", self.dir.name)
        p.start()
        self.addCleanup(p.stop)

    def test_poster_is_stored_as_capped_jpeg_by_content(self):
        path = ao.store_custom_image(_png((2400, 3600)), kind="poster")
        self.assertRegex(path, r"^custom:[0-9a-f]{16}\.jpg$")
        self.assertEqual(ao.provider_of(path), "custom")
        with Image.open(io.BytesIO(ao.custom_art_bytes(path))) as im:
            self.assertEqual((im.format, im.size), ("JPEG", (2000, 3000)))
        self.assertEqual(ao.store_custom_image(_png((2400, 3600)), kind="poster"), path)

    def test_landscape_is_capped_wider(self):
        path = ao.store_custom_image(_png((3840, 2160)), kind="landscape")
        with Image.open(io.BytesIO(ao.custom_art_bytes(path))) as im:
            self.assertEqual(im.size, (2560, 1440))

    def test_logo_is_trimmed_png(self):
        img = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        img.paste((255, 255, 255, 255), (100, 50, 300, 150))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        path = ao.store_custom_image(buf.getvalue(), kind="logo")
        with Image.open(io.BytesIO(ao.custom_art_bytes(path))) as im:
            self.assertEqual((im.format, im.size), ("PNG", (200, 100)))

    def test_unusable_data_is_refused(self):
        for data in (b"", b"<html>nope</html>", _png((20, 20))):
            with self.subTest(n=len(data)), self.assertRaises(ValueError):
                ao.store_custom_image(data, kind="poster")
        with self.assertRaises(ValueError):
            ao.store_custom_image(_png((100, 100), "RGBA", (0, 0, 0, 0)), kind="logo")
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_custom_paths_cannot_escape_the_folder(self):
        for bad in ("custom:../../cache.db", "custom:abc.jpg", "custom:0123456789abcdef.gif"):
            self.assertIsNone(ao.custom_file(bad))
            with self.assertRaises(ValueError):
                ao.provider_of(bad)

    def test_fetch_poster_image_reads_the_stored_file(self):
        import tmdb
        path = ao.store_custom_image(_png(), kind="poster")
        with mock.patch.object(tmdb, "_cached_art", lambda *a: None), \
                mock.patch.object(tmdb, "_store_art", lambda *a: None):
            image = asyncio.run(tmdb.fetch_poster_image(None, "550", "movie", path))
        self.assertEqual(image.size, tmdb.poster_canvas())


def _addrinfo(address):
    async def fake(host, port, type=0):
        return [(None, None, None, "", (address, port))]
    return fake


class DownloadTests(unittest.TestCase):
    def _run(self, handler, url, address="93.184.216.34"):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def go():
            loop = asyncio.get_running_loop()
            with mock.patch.object(loop, "getaddrinfo", _addrinfo(address)):
                try:
                    return await ao.download_custom_url(client, url)
                finally:
                    await client.aclose()
        return asyncio.run(go())

    def test_image_downloads(self):
        png = _png()
        data = self._run(lambda r: httpx.Response(200, content=png, headers={"content-type": "image/png"}),
                         "https://theposterdb.com/api/assets/1")
        self.assertEqual(data, png)

    def test_private_addresses_and_bad_links_are_refused(self):
        ok = lambda r: httpx.Response(200, content=b"x")
        for address in ("127.0.0.1", "10.0.0.5", "172.18.0.3", "169.254.169.254", "::1"):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, "public"):
                self._run(ok, "http://qualicache:8000/x.png", address)
        for url in ("ftp://example.com/a.png", "https://u:p@example.com/a.png", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self._run(ok, url)

    def test_each_redirect_is_checked(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://internal.test/secret.png"})

        async def resolve(host, port, type=0):
            return [(None, None, None, "", ("10.1.2.3" if host == "internal.test" else "93.184.216.34", port))]

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def go():
            with mock.patch.object(asyncio.get_running_loop(), "getaddrinfo", resolve):
                return await ao.download_custom_url(client, "https://example.com/a.png")
        with self.assertRaisesRegex(ValueError, "public"):
            asyncio.run(go())
        self.assertEqual(seen, ["https://example.com/a.png"])

    def test_html_and_oversize_are_refused(self):
        with self.assertRaisesRegex(ValueError, "not an image"):
            self._run(lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
                      "https://example.com/p")
        with mock.patch.object(ao, "MAX_CUSTOM_BYTES", 10), self.assertRaisesRegex(ValueError, "over"):
            self._run(lambda r: httpx.Response(200, content=b"x" * 50, headers={"content-type": "image/png"}),
                      "https://example.com/p.png")


class TvdbPosterTests(unittest.TestCase):
    ARTWORKS = {"posters": [
        {"url": "https://artworks.thetvdb.com/eng1.jpg", "language": "eng", "score": 9},
        {"url": "https://artworks.thetvdb.com/null1.jpg", "language": None, "score": 5},
        {"url": "https://artworks.thetvdb.com/deu1.jpg", "language": "deu", "score": 4},
        {"url": "https://artworks.thetvdb.com/null2.jpg", "language": None, "score": 3},
    ]}

    def _url(self, enabled=True, **kw):
        with mock.patch.object(tvdb, "poster_source_enabled", lambda: enabled), \
                mock.patch.object(tvdb, "resolve_tvdb_id", mock.AsyncMock(return_value=1)), \
                mock.patch.object(tvdb, "fetch_tvdb_artworks",
                                  mock.AsyncMock(return_value=self.ARTWORKS)):
            return asyncio.run(tvdb.tvdb_poster_url(None, media_type="tv", tmdb_id="1", **kw))

    def test_textless_is_the_best_no_language_poster(self):
        self.assertTrue(self._url().endswith("/null1.jpg"))
        picks = {self._url(random_top=True) for _ in range(40)}
        self.assertEqual({p.rsplit("/", 1)[1] for p in picks}, {"null1.jpg", "null2.jpg"})

    def test_original_art_walks_the_language_order(self):
        self.assertTrue(self._url(languages=["fr", "de-de", "en"]).endswith("/deu1.jpg"))
        self.assertIsNone(self._url(languages=["fr"]))

    def test_off_unless_offered(self):
        self.assertIsNone(self._url(enabled=False))

    def test_rescue_prefers_no_language_and_can_insist_on_it(self):
        chosen = []

        async def run(**kw):
            with mock.patch.object(tvdb, "get_cached_tmdb_poster",
                                   lambda key: chosen.append(key)):
                with mock.patch.object(tvdb, "_download", mock.AsyncMock(return_value=None)):
                    return await tvdb.fetch_tvdb_poster(None, self.ARTWORKS, 1, "en", **kw)

        asyncio.run(run())
        asyncio.run(run(textless_only=True))
        self.assertTrue(all("null1" in key for key in chosen))
        only_lang = {"posters": [self.ARTWORKS["posters"][0]]}
        with mock.patch.object(tvdb, "_download", mock.AsyncMock(return_value=None)):
            self.assertIsNone(asyncio.run(tvdb.fetch_tvdb_poster(
                None, only_lang, 1, "en", textless_only=True)))


class PosterSourceParsingTests(unittest.TestCase):
    def test_tvdb_source_is_gated_by_the_operator(self):
        for enabled, want in ((True, "tvdb"), (False, "tmdb")):
            with self.subTest(enabled=enabled), \
                    mock.patch.object(main.tvdb, "poster_source_enabled", lambda: enabled):
                self.assertEqual(
                    main.build_request_config({"poster_source": "tvdb"}).poster_source_tv, want)
        with mock.patch.object(main.tvdb, "poster_source_enabled", lambda: True):
            self.assertEqual(main.build_request_config(
                {"poster_source": "tvdb", "shape": "landscape"}).poster_source_tv, "tmdb")


if __name__ == "__main__":
    unittest.main()
