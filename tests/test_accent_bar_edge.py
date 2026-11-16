"""accent_bar_style=edge: the portrait Rating Bar as landscape's edge strip."""
import unittest

import numpy as np
from PIL import Image

import main
from param_aliases import expand


def _render(**over):
    cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=1, **over)
    out = main.build_poster(Image.new("RGBA", (500, 750), (40, 40, 40, 255)), 80, "Drama", cfg)
    return np.asarray(out.convert("RGB")).astype(int)


class AccentBarEdgeTests(unittest.TestCase):
    def test_param(self):
        self.assertEqual(main.RequestConfig().accent_bar_style, "floating")
        self.assertEqual(main.build_request_config({"accent_bar_style": "edge"}).accent_bar_style, "edge")
        self.assertEqual(main.build_request_config({"accent_bar_style": "bogus"}).accent_bar_style, "floating")
        self.assertEqual(expand([("abst", "edge")]), [("accent_bar_style", "edge")])

    def test_default_leaves_the_signature_alone(self):
        base = main._render_config_signature(main.build_request_config({}))
        self.assertEqual(base, main._render_config_signature(
            main.build_request_config({"accent_bar_style": "floating"})))
        self.assertNotEqual(base, main._render_config_signature(
            main.build_request_config({"accent_bar_style": "edge"})))

    def test_edge_sits_on_the_bottom_edge_from_the_left(self):
        arr = _render(accent_bar_style="edge")
        bottom = arr[-3]
        lit = np.where(bottom.max(axis=1) > 120)[0]
        self.assertEqual(lit.min(), 0)                    # flush left
        self.assertAlmostEqual(lit.max() / 500, 0.80, delta=0.03)   # as long as the score
        floating = _render()
        self.assertLess(floating[-3].max(), 120)          # floating leaves the edge bare

    def test_client_inset_raises_it(self):
        arr = _render(accent_bar_style="edge", bar_bottom_inset=0.02)
        self.assertLess(arr[-3, 10].max(), 120)
        self.assertGreater(arr[750 - 15 - 3, 10].max(), 120)

    def test_fractional_inset_at_another_resolution(self):
        # A client inset scaled to a non-500 canvas is a fractional pixel.
        import landscape
        cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=1, accent_bar_style="edge")
        img = Image.new("RGBA", (1000, 1500), (40, 40, 40, 255))
        box = landscape.draw_edge_score_bar(img, 80, cfg, bottom=1500 - 10.5)
        self.assertEqual(box[3], 1490)
        self.assertTrue(all(isinstance(v, int) for v in box))

    @staticmethod
    def _glow_rows(width, **over):
        """Rows above the bar the glow lights, as a share of the height."""
        cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=1, accent_bar_style="edge",
                                 poster_width=width, score_glow_alpha=255, score_glow_color="FFFFFF",
                                 **over)
        cfg = main._scale_render_cfg(cfg)
        h = width * 3 // 2
        out = main.build_poster(Image.new("RGBA", (width, h), (40, 40, 40, 255)), 95, "Drama", cfg)
        col = np.asarray(out.convert("RGB")).astype(int)[:, width // 4].max(axis=1)
        lit = np.where(col[: int(h * 0.97)] > 60)[0]
        return (h - lit.min()) / h, col

    def test_glow_is_the_same_share_at_any_resolution(self):
        small, _ = self._glow_rows(500)
        large, _ = self._glow_rows(1000)
        self.assertAlmostEqual(small, large, delta=0.006)

    def test_raised_bar_keeps_its_glow_below(self):
        _, col = self._glow_rows(500, bar_bottom_inset=0.02)
        self.assertGreater(col[-3], 60)       # glow, not bare art, under the bar

    def test_hide_rating_draws_no_bar(self):
        arr = _render(accent_bar_style="edge", hide_rating=True)
        self.assertLess(arr[-3].max(), 120)



class FrostedPaletteTests(unittest.TestCase):
    """score_color_mode=4: the Rating Bar in the poster's frost tint."""

    @staticmethod
    def _bar_colour(style, mode):
        art = Image.new("RGBA", (500, 750), (30, 70, 170, 255))      # a blue poster
        cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=1, accent_bar_style=style,
                                 score_color_mode=mode, score_glow_alpha=0)
        arr = np.asarray(main.build_poster(art, 70, "Drama", cfg).convert("RGB")).astype(int)
        if style == "edge":
            return arr[-4, 40]
        y = 750 - round(750 * 0.04) - 5
        return arr[y, 150]

    def test_parses(self):
        self.assertEqual(main.build_request_config({"score_color_mode": "4"}).score_color_mode, 4)

    def test_fill_follows_the_poster_in_both_styles(self):
        for style in ("edge", "floating"):
            with self.subTest(style=style):
                frosted = self._bar_colour(style, 4)
                self.assertGreater(frosted[2], frosted[0] + 30)     # blue like the poster
                prestige = self._bar_colour(style, 2)               # silver at 70
                self.assertGreater(abs(frosted - prestige).sum(), 60)

    def test_matches_the_frosted_tint(self):
        import ratings
        from awards import _frosted_tint
        left, right = ratings.frosted_score_colors((30.0, 70.0, 170.0), 1.2, False)
        self.assertEqual(left, right)
        self.assertEqual(left, _frosted_tint(30.0, 70.0, 170.0, 1.2, False))

    def test_other_readers_get_prestige(self):
        import ratings
        self.assertEqual(ratings.score_color_for_mode(70, 4), ratings.score_color_for_mode(70, 2))

    def test_landscape_rating_bar(self):
        import landscape
        art = Image.new("RGBA", (1000, 563), (30, 70, 170, 255))
        cfg = main.build_request_config({"shape": "landscape", "landscape_rating_mode": "rating_bar",
                                         "landscape_score_color_mode": "4",
                                         "landscape_score_glow_alpha": "0"})
        arr = np.asarray(landscape.build_landscape(art, 70, "Drama", cfg).convert("RGB")).astype(int)
        r, g, b = arr[-3, 40]
        self.assertGreater(b, r + 30)


if __name__ == "__main__":
    unittest.main()
