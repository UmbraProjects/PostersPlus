"""Render-path speed-ups must not change a pixel.

Each cache or shortcut here is checked against the uncached / per-pixel path
it replaced.  (The full-pipeline check — 185 real posters composited
bit-identically before and after — was done against a live cache and is not
reproducible offline.)
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
    def test_webp_uses_the_faster_method(self):
        with mock.patch.object(main._cfg, "IMAGE_FORMAT", "webp"), \
             mock.patch.object(Image.Image, "save", autospec=True) as save:
            main._encode_poster(Image.new("RGBA", (10, 10)))
        self.assertEqual(save.call_args.kwargs["method"], main._WEBP_METHOD)
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
    """The pre-optimisation _vignette_frost_band, verbatim bar the blend."""
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
                band.size, Image.Resampling.BICUBIC)
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
                awards._notch_font.cache_clear()
                awards._notch_shape.cache_clear()
                awards._notch_label_layer.cache_clear()
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


if __name__ == "__main__":
    unittest.main()
