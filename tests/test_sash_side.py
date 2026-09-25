"""sash_side=left moves the diagonal sash into the top-left corner, and the
quality bookmark (normally top-left) steps over to the top-right for it."""
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import age_badge
import awards
import main


def _poster():
    return Image.new("RGBA", (300, 450), (0, 0, 0, 0))


def _alpha_sum(img, box):
    return int(np.asarray(img.crop(box))[..., 3].sum())


class SashSideRenderingTests(unittest.TestCase):
    def test_right_is_the_default_corner(self):
        out = awards.draw_award_sash(_poster(), "Oscar Winner")
        self.assertGreater(_alpha_sum(out, (250, 0, 300, 50)), 0)
        self.assertEqual(_alpha_sum(out, (0, 0, 50, 50)), 0)

    def test_left_mirrors_the_right(self):
        right = awards.draw_award_sash(_poster(), " ", side="right")
        left  = awards.draw_award_sash(_poster(), " ", side="left")
        self.assertGreater(_alpha_sum(left, (0, 0, 50, 50)), 0)
        self.assertEqual(_alpha_sum(left, (250, 0, 300, 50)), 0)
        # With a blank label the band is symmetric, so the two sides land on
        # mirror-image footprints.
        mirrored = right.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        self.assertEqual(left.getbbox(), mirrored.getbbox())


class SashFallbackRenderingTests(SashSideRenderingTests):
    """The same corner checks on the PIL path, which draws every sash when
    skia cannot be loaded."""

    def setUp(self):
        patcher = mock.patch.object(awards, "_HAS_SKIA", False)
        patcher.start()
        self.addCleanup(patcher.stop)


@unittest.skipUnless(awards._HAS_SKIA, "skia unavailable")
class SkiaMatchesFallbackTests(unittest.TestCase):
    """Skia and the PIL fallback lay the sash out identically; only edge and
    glyph anti-aliasing may differ."""

    def test_same_sash_either_way(self):
        art = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        for side in ("right", "left"):
            for kw in ({}, {"muted": True}, {"sash_type": "cast", "star": True}):
                with self.subTest(side=side, **kw):
                    fast = np.asarray(awards.draw_award_sash(art, "Oscar Winner", side=side, **kw), dtype=np.int16)
                    with mock.patch.object(awards, "_HAS_SKIA", False):
                        slow = np.asarray(awards.draw_award_sash(art, "Oscar Winner", side=side, **kw), dtype=np.int16)
                    diff = np.abs(fast - slow)
                    self.assertLess(diff.mean(), 1.0)
                    # Anything beyond anti-aliasing (a band or label in a
                    # different place) would change far more than 2% of pixels.
                    self.assertLess((diff.max(axis=2) > 48).mean(), 0.02)


class BookmarkSideTests(unittest.TestCase):
    def test_right_side_mirrors_into_the_top_right_corner(self):
        left = _poster()
        right = _poster()
        age_badge.draw_quality_corner_bookmark(left, ["4K", "REMUX", "DV"], bookmark_size=16)
        age_badge.draw_quality_corner_bookmark(right, ["4K", "REMUX", "DV"], bookmark_size=16, side="right")
        np.testing.assert_array_equal(
            np.asarray(right.transpose(Image.Transpose.FLIP_LEFT_RIGHT)), np.asarray(left)
        )


class SashSideConfigTests(unittest.TestCase):
    def test_parses_left_and_ignores_junk(self):
        self.assertEqual(main.build_request_config({}).sash_side, "right")
        self.assertEqual(main.build_request_config({"sash_side": "Left"}).sash_side, "left")
        self.assertEqual(main.build_request_config({"sash_side": "middle"}).sash_side, "right")


if __name__ == "__main__":
    unittest.main()
