"""Render-path speed-ups must not visibly change a poster.

Each cache or shortcut here is checked against the uncached / per-pixel path
it replaced: bit-identical where the shortcut is exact, within a stated
tolerance where it swaps one approximation of a blur or resample for another
(the OpenCV paths; measured at most 4/255 on real art, 52+ dB PSNR).  (The
earlier full-pipeline check — 185 real posters composited bit-identically —
was done against a live cache and is not reproducible offline.)
"""

import io
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

import awards
import face_detect
import main
from tests.test_trending_snapshot_turnover import _TempDb


def _art(w=200, h=300, seed=3):
    rng = np.random.default_rng(seed)
    return Image.fromarray((rng.random((h, w, 4)) * 255).astype(np.uint8))


class WebpEncodeTests(unittest.TestCase):
    def test_webp_uses_the_configured_method(self):
        with mock.patch.object(main._cfg, "IMAGE_FORMAT", "webp"), \
             mock.patch.object(main._cfg, "WEBP_METHOD", 0), \
             mock.patch.object(Image.Image, "save", autospec=True) as save:
            main._encode_poster(Image.new("RGBA", (10, 10)))
        self.assertEqual(save.call_args.kwargs["method"], 0)
        self.assertEqual(save.call_args.kwargs["quality"], main._cfg.WEBP_QUALITY)

    def test_output_is_a_webp(self):
        with mock.patch.object(main._cfg, "IMAGE_FORMAT", "webp"):
            data = main._encode_poster(_art())
        self.assertEqual(Image.open(io.BytesIO(data)).format, "WEBP")


class FrostBandTests(unittest.TestCase):
    """The per-row blend must equal the per-pixel gather it replaced."""

    BOX = (0, 120, 160, 240)

    def _both(self, ramp):
        fast = _art(160, 240)
        main._vignette_frost_band(fast, self.BOX, ramp, 0.8)
        return np.asarray(fast), _per_pixel_frost(_art(160, 240), self.BOX, ramp, 0.8)

    def test_vertical_ramp_matches_the_per_pixel_path(self):
        col = np.linspace(255, 0, 120).astype(np.uint8)
        ramp = Image.fromarray(np.broadcast_to(col[:, None], (120, 160)).copy())
        np.testing.assert_array_equal(*self._both(ramp))

    def test_ramp_varying_along_a_row_takes_the_per_pixel_path(self):
        ramp = Image.fromarray((np.random.default_rng(1).random((120, 160)) * 255).astype(np.uint8))
        np.testing.assert_array_equal(*self._both(ramp))


def _per_pixel_frost(image, box, ramp, blur):
    """The pre-optimisation _vignette_frost_band, verbatim bar the blend and the
    bilinear upscale (both paths now upscale blurred levels bilinearly)."""
    from PIL import ImageFilter
    x0, y0, x1, y1 = box
    radius = (x1 - x0) * main._VIGNETTE_BLUR_MAX_RATIO * blur
    peak = ramp.getextrema()[1]
    depth = (np.asarray(ramp, dtype=np.float32) / peak) ** 2
    band = image.crop(box)

    def _blurred(r):
        shrink = max(1, int(r / 4))
        if shrink > 2:
            small = band.resize((max(1, band.width // shrink), max(1, band.height // shrink)),
                                Image.Resampling.BOX)
            out = small.filter(ImageFilter.GaussianBlur(r / shrink)).resize(
                band.size, Image.Resampling.BILINEAR)
        else:
            out = band.filter(ImageFilter.GaussianBlur(r))
        return np.asarray(out, dtype=np.float32)

    levels = main._VIGNETTE_FROST_LEVELS
    stack = np.stack([np.asarray(band, dtype=np.float32)] + [_blurred(radius * f) for f in levels[1:]])
    pos = depth * (len(levels) - 1)
    lo = np.minimum(pos.astype(np.int32), len(levels) - 2)
    frac = (pos - lo)[..., None]
    a = np.take_along_axis(stack, lo[None, ..., None], axis=0)[0]
    b = np.take_along_axis(stack, lo[None, ..., None] + 1, axis=0)[0]
    out = a + (b - a) * frac
    image.paste(Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)), (x0, y0))
    return np.asarray(image)


class TextMeasureTests(unittest.TestCase):
    def test_memoised_bbox_matches_the_posters_own_draw(self):
        path = str(main._FONTS_DIR) + "/Oswald-Bold.ttf"
        for mode in ("RGBA", "RGB"):
            draw = ImageDraw.Draw(Image.new(mode, (500, 750)))
            for size in (22, 48, 90):
                for text in ("The", "The Lord of the Rings:", "Amélie", "Ωmega 7"):
                    with self.subTest(mode=mode, size=size, text=text):
                        self.assertEqual(
                            main._text_bbox(path, size, text),
                            draw.textbbox((0, 0), text, font=main._load_font(path, size)),
                        )


class NotchCacheTests(unittest.TestCase):
    def _badge(self, **kw):
        return np.asarray(awards.draw_award_badge(_art(500, 750), "Oscar Nominee", "nom", **kw))

    def test_cached_parts_draw_the_same_notch(self):
        # Drawn cold, then from the caches: a cached layer that got drawn on
        # (or a stale shape) would show as a difference.
        for style in ("frosted", "black", "silver"):
            with self.subTest(style=style):
                awards._notch_font_at.cache_clear()
                awards._notch_shape.cache_clear()
                awards._notch_shape_1x.cache_clear()
                awards._notch_label_layer_1x_in.cache_clear()
                first = self._badge(notch_style=style)
                again = self._badge(notch_style=style)
                np.testing.assert_array_equal(first, again)


class FaceBoxCacheTests(_TempDb):
    def test_boxes_are_detected_once_per_image(self):
        art = _art()
        calls = []

        def _detect(image):
            calls.append(image.size)
            return [(10.0, 20.0, 30.0, 40.0, 0.99), (0.0, 0.0, 5.0, 5.0, 0.1)]
        with mock.patch.object(face_detect, "detect_face_boxes", _detect), \
             mock.patch.object(face_detect, "available", lambda: True):
            first = main._fog_faces(art)
            second = main._fog_faces(art.copy())
            other = main._fog_faces(_art(seed=4))
        self.assertEqual(first, second)
        self.assertEqual(first, [(10.0, 20.0, 30.0, 40.0)])   # low-score box dropped
        self.assertEqual(len(calls), 2)                        # the copy hit, new art did not
        self.assertEqual(other, first)

    def test_unavailable_detector_is_not_cached_as_no_faces(self):
        art = _art()
        with mock.patch.object(face_detect, "detect_face_boxes", lambda image: []), \
             mock.patch.object(face_detect, "available", lambda: False):
            self.assertEqual(main._fog_faces(art), [])
        with mock.patch.object(face_detect, "detect_face_boxes",
                               lambda image: [(1.0, 2.0, 3.0, 4.0, 0.99)]), \
             mock.patch.object(face_detect, "available", lambda: True):
            self.assertEqual(main._fog_faces(art), [(1.0, 2.0, 3.0, 4.0)])


class DilationSpeedupTests(unittest.TestCase):
    def test_dilate_mask_matches_pil_maxfilter(self):
        from PIL import ImageFilter
        import tmdb
        rng = np.random.default_rng(7)
        mask = Image.fromarray((rng.random((120, 200)) * 255).astype(np.uint8), "L")
        for k in (3, 7, 17):
            with self.subTest(k=k):
                fast = np.asarray(tmdb._dilate_mask_l(mask, k))
                ref = np.asarray(mask.filter(ImageFilter.MaxFilter(k)))
                np.testing.assert_array_equal(fast, ref)


class DitherAndOklabSpeedupTests(unittest.TestCase):
    def test_dither_noise_and_inplace_match_reference(self):
        tile = main._dither_tile()
        for h, w in ((120, 200), (300, 500), (600, 1100)):
            with self.subTest(shape=(h, w)):
                rows = np.arange(h) % main._DITHER_TILE
                cols = np.arange(w) % main._DITHER_TILE
                expected = tile[rows[:, None], cols[None, :], :3]
                np.testing.assert_array_equal(main._dither_noise((h, w, 3)), expected)
                buf = np.zeros((h, w, 3), dtype=np.float32)
                main._add_dither_inplace(buf)
                np.testing.assert_array_equal(buf, expected)

    def test_srgb_to_oklab_uint8_lut_matches_float(self):
        rng = np.random.default_rng(9)
        u8 = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
        fast = main._srgb_to_oklab(u8)
        ref = main._srgb_to_oklab(u8.astype(np.float32))
        np.testing.assert_allclose(fast, ref, atol=1e-6)


class FusedVignetteTests(unittest.TestCase):
    def test_fused_band_matches_three_pass_pipeline(self):
        # Smooth photo-like art so Gaussian vs 3-box blur agrees tightly.
        h, w = 240, 200
        y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        x = np.linspace(0, 1, w, dtype=np.float32)[None, :]
        arr = np.stack([
            (40 + 140 * (1 - y) + 30 * np.sin(x * 5)).astype(np.uint8),
            (60 + 90 * x + 0 * y).astype(np.uint8),
            (80 + 110 * y + 0 * x).astype(np.uint8),
            np.full((h, w), 255, dtype=np.uint8),
        ], axis=-1)
        src = Image.fromarray(arr, "RGBA")
        box = (0, 120, w, h)
        alpha = main._vignette_fog_ramp(120, 220, rising=True)
        ramp_1d = np.round(alpha).astype(np.uint8)
        ramp_2d = Image.fromarray(np.broadcast_to(ramp_1d[:, None], (120, w)).copy())
        tint = main._vignette_tint_band(src, box, (40.0, 90.0, 160.0), 1.0, 2.5, 0.65)

        fused = src.copy()
        main._apply_tinted_vignette_band(fused, box, ramp_1d, alpha, tint, 0.65, 0.8)

        three = src.copy()
        main._vignette_frost_band(three, box, ramp_2d, 0.65)
        main._vignette_level_band(three, box, ramp_2d, 0.8)
        main._vignette_composite(three, 120, tint, alpha)

        diff = np.abs(np.asarray(fused, dtype=np.int16) - np.asarray(three, dtype=np.int16))
        self.assertLess(diff.mean(), 1.0)
        self.assertLessEqual(int(np.percentile(diff, 99)), 3)

    def test_occupied_mask_cv2_matches_numpy_fallback(self):
        rng = np.random.default_rng(11)
        before = rng.integers(0, 256, (100, 150, 4), dtype=np.uint8)
        now = before.copy()
        now[20:40, 30:80, :3] = (now[20:40, 30:80, :3].astype(np.int16) + 25).clip(0, 255).astype(np.uint8)
        fast = main._occupied_cols(now, before, 10, 60)
        with mock.patch.object(main, "_HAS_CV2", False):
            slow = main._occupied_cols(now, before, 10, 60)
        np.testing.assert_array_equal(fast, slow)


class OverlayCacheAndInPlaceTests(unittest.TestCase):
    def test_sash_and_rank_caches_match_cold_and_inplace(self):
        import trending_rank
        art = _art(500, 750)
        awards._sash_layers_cached.cache_clear()
        awards._sash_shadow_cached.cache_clear()
        s1 = awards.draw_award_sash(art, "Oscar Winner", sash_type="win")
        s2 = awards.draw_award_sash(art, "Oscar Winner", sash_type="win")
        s3 = awards.draw_award_sash(art.copy(), "Oscar Winner", sash_type="win", in_place=True)
        np.testing.assert_array_equal(np.asarray(s1), np.asarray(s2))
        np.testing.assert_array_equal(np.asarray(s1), np.asarray(s3))

        trending_rank._rank_number_layers.cache_clear()
        trending_rank._ribbon_layers_cached.cache_clear()
        n1 = trending_rank.draw_rank_number(art, 3)
        n2 = trending_rank.draw_rank_number(art, 3)
        n3 = trending_rank.draw_rank_number(art.copy(), 3, in_place=True)
        np.testing.assert_array_equal(np.asarray(n1), np.asarray(n2))
        np.testing.assert_array_equal(np.asarray(n1), np.asarray(n3))

        r1 = trending_rank.draw_rank_ribbon(art, 3, label="FILM", style="silver")
        r2 = trending_rank.draw_rank_ribbon(art, 3, label="FILM", style="silver")
        r3 = trending_rank.draw_rank_ribbon(art.copy(), 3, label="FILM", style="silver", in_place=True)
        np.testing.assert_array_equal(np.asarray(r1), np.asarray(r2))
        np.testing.assert_array_equal(np.asarray(r1), np.asarray(r3))


class PosterQueryTests(unittest.TestCase):
    """/poster reads its query from get_poster's signature, not FastAPI's
    per-parameter resolution; what reaches get_poster must not change."""

    def test_defaults_strings_and_ints(self):
        from starlette.datastructures import QueryParams
        kw = main._poster_query_kwargs(QueryParams("tmdb_id=603&season=2&season=3&top_gradient=low"))
        self.assertEqual(kw["tmdb_id"], "603")
        self.assertEqual(kw["season"], 3)            # the last of a repeated key, as before
        self.assertEqual(kw["episode"], 1)           # signature default
        self.assertEqual(kw["type"], "movie")
        self.assertIsNone(kw["shape"])
        self.assertEqual(set(kw), {n for n in __import__("inspect").signature(main.get_poster).parameters} - {"request"})

    def test_a_bad_int_is_fastapis_own_422(self):
        from fastapi.testclient import TestClient
        r = TestClient(main.app).get("/poster?tmdb_id=603&season=x&episode=2.5")
        self.assertEqual(r.status_code, 422)
        self.assertEqual([e["loc"] for e in r.json()["detail"]], [["query", "season"], ["query", "episode"]])
        self.assertEqual(r.json()["detail"][0]["type"], "int_parsing")


if __name__ == "__main__":
    unittest.main()
