import os
import unittest

from PIL import Image

import age_badge
import main


class QualityBookmarkRenderingTests(unittest.TestCase):
    def _transparent(self):
        return Image.new("RGBA", (100, 150), (0, 0, 0, 0))

    def test_bookmark_is_a_top_left_corner_fold(self):
        poster = self._transparent()
        age_badge.draw_quality_corner_bookmark(
            poster, ["4K", "REMUX", "DV"], bookmark_size=16
        )

        self.assertGreater(poster.getpixel((2, 2))[3], 150)
        self.assertLess(poster.getpixel((14, 14))[3], 40)
        self.assertIsNone(poster.crop((24, 24, 100, 150)).getbbox())

    def test_size_control_scales_the_corner_mark(self):
        small = self._transparent()
        large = self._transparent()
        age_badge.draw_quality_corner_bookmark(
            small, ["4K", "REMUX", "DV"], bookmark_size=16
        )
        age_badge.draw_quality_corner_bookmark(
            large, ["4K", "REMUX", "DV"], bookmark_size=32
        )

        small_box = small.getbbox()
        large_box = large.getbbox()
        self.assertIsNotNone(small_box)
        self.assertIsNotNone(large_box)
        self.assertGreater(large_box[2], small_box[2] * 1.7)
        self.assertGreater(large_box[3], small_box[3] * 1.7)

    def test_outer_corner_stays_flush_with_the_poster_edges(self):
        # Clients round the poster corner themselves, so the mark has to run all
        # the way into (0, 0) — a rounded outer corner would leave artwork
        # showing through the crescent between the fold and the card edge.
        poster = self._transparent()
        age_badge.draw_quality_corner_bookmark(
            poster, ["4K", "REMUX", "DV"], bookmark_size=16
        )

        for xy in ((0, 0), (0, 1), (1, 0), (3, 3)):
            self.assertGreater(poster.getpixel(xy)[3], 150, xy)

    def test_tips_are_rounded_and_reach_the_requested_size(self):
        poster = self._transparent()
        size = 16
        age_badge.draw_quality_corner_bookmark(
            poster, ["4K", "REMUX", "DV"], bookmark_size=size
        )

        top  = [poster.getpixel((x, 0))[3] for x in range(size + 4)]
        left = [poster.getpixel((0, y))[3] for y in range(size + 4)]

        # The fold reaches `size` along each edge and stops there.
        self.assertGreater(top[size - 2], 150)
        self.assertLess(top[size + 1], 40)
        # Rounded rather than mitred: coverage tapers over the last few pixels.
        self.assertLess(top[size - 1], top[size - 4])
        # Both tips are drawn from the same geometry (the faint glow tail can
        # differ by a rounding step).
        for x, (a, b) in enumerate(zip(top, left)):
            self.assertLessEqual(abs(a - b), 2, x)

    def test_bookmark_reuses_quality_tier_colours(self):
        bronze = self._transparent()
        gold = self._transparent()
        age_badge.draw_quality_corner_bookmark(
            bronze, ["4K"], bookmark_size=16
        )
        age_badge.draw_quality_corner_bookmark(
            gold, ["4K", "REMUX", "DV"], bookmark_size=16
        )

        self.assertGreater(gold.getpixel((3, 3))[1], bronze.getpixel((3, 3))[1])


class QualityBookmarkConfigurationTests(unittest.TestCase):
    def test_mode_six_is_accepted(self):
        cfg = main.build_request_config({
            "badge_display_mode": "6",
            "badge_height": "18",
            "badge_min_score": "5",
        })
        self.assertEqual(cfg.badge_display_mode, 6)
        self.assertEqual(cfg.badge_height, 18)
        self.assertEqual(cfg.badge_min_score, 5)

    def test_configurator_exposes_fixed_corner_bookmark(self):
        path = os.path.join(os.path.dirname(main.__file__), "configurator.html")
        with open(path, encoding="utf-8") as source:
            html = source.read()
        self.assertIn("Quality Bookmark", html)
        # 30, not the 16 this shipped with: at 16 the mark is barely visible on
        # the poster sizes most clients render at. The right value still varies
        # by client, which the mode tooltip now says.
        self.assertIn("mode === 6 ? 30", html)
        self.assertIn("badge-x-field", html)
        self.assertIn("badge-y-field", html)
        self.assertGreaterEqual(html.count("mode === 6"), 3)
        self.assertIn("[1,2,4,5,6,7]", html)


if __name__ == "__main__":
    unittest.main()
