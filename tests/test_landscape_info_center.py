"""landscape_info_center: the whole info line centred on the poster."""
import unittest

import numpy as np
from PIL import Image

import landscape
import main


def _render(logo=None, ratings=None, **over):
    art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
    cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=False,
                             sash_mode="hidden", **over)
    return landscape.build_landscape(art, 87, "Drama", cfg, logo=logo, release_year="2019",
                                     ratings=ratings)


def _line_cols(img, y0=450, y1=563):
    # The line's muted grey ink (a logo here is pure white).
    a = np.asarray(img.convert("L"))[y0:y1]
    cols = np.flatnonzero(((a > 120) & (a < 250)).any(axis=0))
    return cols[0], cols[-1]


class InfoCenterTests(unittest.TestCase):
    def test_parsed_and_off_by_default(self):
        self.assertFalse(main.build_request_config({}).landscape_info_center)
        self.assertTrue(main.build_request_config({"landscape_info_center": "true"}).landscape_info_center)

    def test_centred_on_the_poster_beside_a_side_logo(self):
        logo = Image.new("RGBA", (100, 100), (255, 255, 255, 255))   # ~170px drawn
        for pos in ("left", "right"):
            with self.subTest(pos=pos):
                c0, c1 = _line_cols(_render(logo=logo, landscape_logo_pos=pos,
                                            landscape_info_center=True))
                self.assertLess(abs((c0 + c1) / 2 - 500), 6)
                # Without it the line hangs off the far side, as before.
                o0, o1 = _line_cols(_render(logo=logo, landscape_logo_pos=pos))
                self.assertGreater(abs((o0 + o1) / 2 - 500), 100)

    def test_rating_badges_are_centred_with_the_line(self):
        ratings = {"imdb": 8.1, "tomatoes": 92}
        c0, c1 = _line_cols(_render(ratings=ratings, landscape_info_center=True,
                                    landscape_rating_badges=True, rating_badges="imdb,tomatoes"))
        self.assertLess(abs((c0 + c1) / 2 - 500), 8)

    def test_a_logo_reaching_the_middle_stands_above_the_centred_line(self):
        logo = Image.new("RGBA", (400, 100), (255, 255, 255, 255))
        for pos in ("left", "right"):
            with self.subTest(pos=pos):
                out = _render(logo=logo, landscape_logo_pos=pos, landscape_info_center=True)
                c0, c1 = _line_cols(out)
                self.assertLess(abs((c0 + c1) / 2 - 500), 6)
                a = np.asarray(out.convert("L"))
                logo_bottom = np.flatnonzero((a >= 250).sum(axis=1) > 100).max()
                line_top = 450 + np.flatnonzero(((a[450:] > 120) & (a[450:] < 250)).any(axis=1)).min()
                self.assertLess(logo_bottom, line_top)
                # Off, the same logo keeps the baseline.
                plain = np.asarray(_render(logo=logo, landscape_logo_pos=pos).convert("L"))
                self.assertGreater(np.flatnonzero((plain >= 250).sum(axis=1) > 100).max(),
                                   logo_bottom + 10)

    def test_keeps_its_row(self):
        out = _render(landscape_info_center=True, landscape_info_pos="top_left")
        c0, c1 = _line_cols(out, 0, 120)
        self.assertLess(abs((c0 + c1) / 2 - 500), 6)


if __name__ == "__main__":
    unittest.main()
