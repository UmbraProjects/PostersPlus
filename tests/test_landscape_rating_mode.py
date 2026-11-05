"""landscape_rating_mode: the info line, the Rating Bar along the bottom edge,
or portrait's Bar across the bottom."""
import unittest

import numpy as np
from PIL import Image

import landscape
import main


def _render(score=87, logo=None, **over):
    art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
    cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=False,
                             sash_mode="hidden", **over)
    return np.asarray(landscape.build_landscape(art, score, "Drama", cfg, logo=logo,
                                                release_year="2019").convert("RGB")).astype(int)


class RatingModeTests(unittest.TestCase):
    def test_parsed_line_by_default(self):
        self.assertEqual(main.build_request_config({}).landscape_rating_mode, "line")
        for mode in ("rating_bar", "bar"):
            self.assertEqual(main.build_request_config({"landscape_rating_mode": mode}).landscape_rating_mode,
                             mode)
        self.assertEqual(main.build_request_config({"landscape_rating_mode": "x"}).landscape_rating_mode, "line")

    def test_landscape_reads_its_own_bar_settings(self):
        # A portrait's Bar or Rating Bar settings don't restyle the landscape.
        cfg = main.build_request_config({"shape": "landscape", "bar_style": "gold",
                                         "bar_height_ratio": "0.15", "score_color_mode": "0"})
        self.assertEqual((cfg.bar_style, cfg.bar_height_ratio, cfg.score_color_mode), ("frosted", 0.08, 2))
        cfg = main.build_request_config({"shape": "landscape", "landscape_bar_style": "gold",
                                         "landscape_score_color_mode": "0"})
        self.assertEqual((cfg.bar_style, cfg.score_color_mode), ("gold", 0))
        # Frost reference falls back to the plain one (landscape chips read it).
        self.assertTrue(main.build_request_config({"shape": "landscape", "frost_reference": "true"}).frost_reference)
        self.assertFalse(main.build_request_config({"shape": "landscape", "frost_reference": "true",
                                                    "landscape_frost_reference": "false"}).frost_reference)

    def test_default_keeps_cache_key(self):
        sig = main._render_config_signature
        self.assertNotIn("landscape_rating_mode", sig(main.build_request_config({})))
        self.assertIn('"landscape_rating_mode": "bar"',
                      sig(main.build_request_config({"landscape_rating_mode": "bar"})))

    def test_rating_bar_fills_the_bottom_edge_to_the_score(self):
        img = _render(landscape_rating_mode="rating_bar", score_glow_threshold=101)
        row = img[-2]
        lit = np.flatnonzero(row.sum(axis=1) > 200)
        # Flush left, ending at the score's share of the width.
        self.assertEqual(lit[0], 0)
        self.assertLess(abs(lit[-1] - 870), 8)
        # The rest of the edge is the dark track.
        self.assertLess(row[950].sum(), 60)

    def test_rating_bar_keeps_the_score_on_the_line(self):
        line = _render()
        bar = _render(landscape_rating_mode="rating_bar", score_glow_threshold=101)
        band = slice(440, 540)
        self.assertEqual(np.abs(line[band] - bar[band]).max(), 0)

    def test_hide_rating_keeps_the_rating_bar(self):
        # Only the line's printed score goes; Info Line is how to drop the bar.
        img = _render(landscape_rating_mode="rating_bar", hide_rating=True, score_glow_threshold=101)
        self.assertGreater(img[-2].sum(axis=1).max(), 100)
        line = _render(hide_rating=True)
        band = slice(440, 540)
        self.assertEqual(np.abs(line[band] - img[band]).max(), 0)

    def test_withheld_score_drops_the_rating_bar(self):
        # hide_unreleased_rating renders a Rating Bar as the Info Line.
        import inspect
        src = inspect.getsource(main)
        self.assertIn('"line" if rcfg.landscape_rating_mode == "rating_bar"', src)

    def test_hidden_rating_bar_composites_re_render(self):
        cfg = main.build_request_config({"shape": "landscape", "landscape_rating_mode": "rating_bar",
                                         "landscape_hide_rating": "true"})
        self.assertIn(34, [r.rev for r in main._revisions_applying(cfg)])
        self.assertNotIn(34, [r.rev for r in main._revisions_applying(
            main.build_request_config({"shape": "landscape", "landscape_rating_mode": "rating_bar"}))])

    def test_bar_takes_the_line_and_raises_the_logo(self):
        logo = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
        line = _render(logo=logo)
        bar = _render(logo=logo, landscape_rating_mode="bar")
        white = lambda img: np.flatnonzero((img[:, 60:200] == 255).all(axis=2).any(axis=1))
        # The logo stands clear above the bar rather than on the baseline.
        self.assertLess(white(bar)[-1], white(line)[-1] - 20)
        bar_h = int(563 * main.RequestConfig().bar_height_ratio * landscape._BAR_SCALE)
        self.assertLess(white(bar)[-1], 563 - bar_h)

    def test_bar_ignores_the_portrait_client_inset(self):
        # bar_bottom_inset is primary_client's portrait crop compensation.
        flush = _render(landscape_rating_mode="bar")
        inset = _render(landscape_rating_mode="bar", bar_bottom_inset=0.05)
        self.assertEqual(np.abs(flush - inset).max(), 0)


if __name__ == "__main__":
    unittest.main()
