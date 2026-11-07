"""logo_fit: "box" (default) sizes and places a logo by its bounding box, as
before; "optical" goes by its solid ink — one optical size for every logo,
centred on the ink, the ink's bottom on the baseline — so lopsided designs and
faint built-in glows no longer pull a logo off-centre or up."""
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import main
import param_aliases
import tmdb

ROOT = Path(__file__).resolve().parent.parent
W, H = 500, 750
KW = dict(max_w_ratio=0.78, max_h_ratio=0.22, bottom_ratio=0.11, bottom_anchor=True)


def _logo(size, blocks, glow=None):
    """A transparent logo with solid (alpha 255) rectangles, and optionally a
    faint (alpha 60) rectangle standing in for a built-in glow or shadow."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    if glow:
        im.paste((255, 255, 255, 60), glow)
    for box in blocks:
        im.paste((255, 255, 255, 255), box)
    return im


def _placed(logo, **kw):
    """The ink (alpha > 128) a composite leaves on a transparent canvas."""
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tmdb.composite_logo(canvas, logo, **{**KW, **kw})
    a = np.asarray(canvas.getchannel("A"), dtype=np.float64)
    ink = np.where(a > 128, 1.0, 0.0)
    ys, xs = np.nonzero(ink)
    return dict(cx=(ink.sum(0) * np.arange(W)).sum() / ink.sum(), left=xs.min(), right=xs.max(),
                top=ys.min(), bottom=ys.max(), w=xs.max() - xs.min() + 1, h=ys.max() - ys.min() + 1,
                optical=(ink.sum() * (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)) ** 0.25)


# A lopsided design: a big block on the left, a small one on the right.
LOPSIDED = _logo((400, 120), [(0, 0, 220, 120), (300, 80, 400, 120)])
# A centred word with a faint glow spilling far to the right and below it.
GLOWING = _logo((400, 160), [(0, 0, 300, 100)], glow=(0, 0, 400, 160))
WIDE = _logo((600, 120), [(0, 0, 600, 120)])
STACKED = _logo((240, 300), [(0, 0, 240, 80), (30, 110, 210, 190), (0, 220, 240, 300)])


class SettingTests(unittest.TestCase):
    def test_default_and_values(self):
        self.assertEqual(main.build_request_config({}).logo_fit, "box")
        self.assertEqual(main.build_request_config({"logo_fit": "optical"}).logo_fit, "optical")
        self.assertEqual(main.build_request_config({"logo_fit": "OPTICAL "}).logo_fit, "optical")
        self.assertEqual(main.build_request_config({"logo_fit": "stretch"}).logo_fit, "box")
        self.assertEqual(dict(param_aliases.expand([("lfit", "optical")])), {"logo_fit": "optical"})

    def test_default_leaves_the_cache_key_alone(self):
        self.assertNotIn("logo_fit", main._render_config_signature(main.build_request_config({})))
        self.assertIn("logo_fit", main._render_config_signature(main.build_request_config({"logo_fit": "optical"})))

    def test_box_is_the_old_placement(self):
        for logo in (LOPSIDED, GLOWING, WIDE, STACKED):
            a, b = Image.new("RGBA", (W, H), (90, 90, 90, 255)), Image.new("RGBA", (W, H), (90, 90, 90, 255))
            tmdb.composite_logo(a, logo.copy(), **KW)
            tmdb.composite_logo(b, logo.copy(), fit="box", **KW)
            self.assertEqual(a.tobytes(), b.tobytes())


class OpticalTests(unittest.TestCase):
    def test_a_lopsided_logo_is_centred_on_its_ink(self):
        box, optical = _placed(LOPSIDED.copy()), _placed(LOPSIDED.copy(), fit="optical")
        self.assertGreater(abs(box["cx"] - W / 2), 20)        # the box fit leaves it off-centre
        self.assertLess(abs(optical["cx"] - W / 2), 1.0)

    def test_a_faint_glow_neither_pulls_it_sideways_nor_lifts_it(self):
        box, optical = _placed(GLOWING.copy()), _placed(GLOWING.copy(), fit="optical")
        baseline = H - int(H * KW["bottom_ratio"]) - 1
        self.assertGreater(abs(box["cx"] - W / 2), 10)
        self.assertLess(box["bottom"], baseline - 5)          # the glow below took the baseline
        self.assertLess(abs(optical["cx"] - W / 2), 1.0)
        self.assertEqual(optical["bottom"], baseline)

    def test_stacked_and_wide_logos_come_out_closer_in_size(self):
        ratio = lambda fit: (_placed(WIDE.copy(), fit=fit)["optical"] /
                             _placed(STACKED.copy(), fit=fit)["optical"])
        self.assertLess(abs(np.log(ratio("optical"))), abs(np.log(ratio("box"))))

    def test_caps_hold(self):
        wide, stacked = _placed(WIDE.copy(), fit="optical"), _placed(STACKED.copy(), fit="optical")
        self.assertLessEqual(wide["w"], int(W * KW["max_w_ratio"]))
        max_h = min(int(H * KW["max_h_ratio"]), tmdb.LOGO_ABS_MAX_H)
        self.assertLessEqual(stacked["h"], max_h * tmdb.LOGO_STACKED_HEIGHT + 1)
        self.assertGreater(stacked["h"], max_h)               # a stacked logo may pass the plain cap
        self.assertGreaterEqual(wide["left"], round(W * tmdb.LOGO_EDGE_MARGIN))

    def test_centre_anchor_uses_the_ink_centre(self):
        placed = _placed(GLOWING.copy(), fit="optical", bottom_anchor=False)
        self.assertLess(abs((placed["top"] + placed["bottom"]) / 2 - tmdb.logo_centre_y(H, KW["bottom_ratio"])), 1.5)

    def test_a_logo_without_solid_ink_keeps_the_box_fit(self):
        faint = _logo((300, 100), [], glow=(0, 0, 300, 100))
        a, b = Image.new("RGBA", (W, H), (90, 90, 90, 255)), Image.new("RGBA", (W, H), (90, 90, 90, 255))
        tmdb.composite_logo(a, faint.copy(), **KW)
        tmdb.composite_logo(b, faint.copy(), fit="optical", **KW)
        self.assertEqual(a.tobytes(), b.tobytes())


class ConfiguratorTests(unittest.TestCase):
    def test_sent_and_imported(self):
        html = (ROOT / "configurator.html").read_text(encoding="utf-8")
        self.assertIn('id="cfg-logo-fit"', html)
        self.assertIn('<option value="box" selected>Box</option>', html)
        self.assertRegex(html, r"params\.set\('logo_fit',\s+vEl\('cfg-logo-fit'\) \|\| 'box'\)")
        self.assertRegex(html, r"p\.has\('logo_fit'\)\)\s+_setEl\('cfg-logo-fit'")


if __name__ == "__main__":
    unittest.main()
