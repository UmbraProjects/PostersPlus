"""Notch vertical padding (sash_badge_pad / notch_pad_ratio).

The notch sizes its font from the height size_ratio_h asks for, so shrinking the
badge to tighten the empty space above and below the label used to shrink the
text with it.  notch_pad_ratio scales only the drawn height, leaving the font and
the badge width alone.  These tests pin that separation, the no-op default, and
the floor that stops an aggressive value from clipping glyphs.
"""

import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import awards
import main


def _render(pad: float, *, label: str = "Oscar Nominee", size_ratio_h: float = 1.15,
            font_size_ratio: float = 0.43) -> Image.Image:
    # Black backdrop + the "black" notch style keeps the badge body dark and the
    # text near-white, so both are separable by luminance alone.
    bg = Image.new("RGBA", (1000, 1500), (0, 0, 0, 255))
    return awards.draw_award_badge(
        bg, label, sash_type="nom",
        size_ratio_w=1.40, size_ratio_h=size_ratio_h,
        notch_style="black", notch_pad_ratio=pad,
        font_size_ratio=font_size_ratio, notch_inset=0.0,
    )


def _metrics(img: Image.Image) -> dict:
    lum = np.array(img.convert("RGB")).sum(axis=2)
    body_rows = np.where(lum.max(axis=1) > 20)[0]
    body_cols = np.where(lum.max(axis=0) > 20)[0]
    text_rows = np.where(lum.max(axis=1) > 300)[0]
    return {
        "badge_h": int(body_rows.max() - body_rows.min() + 1),
        "badge_w": int(body_cols.max() - body_cols.min() + 1),
        "text_h": int(text_rows.max() - text_rows.min() + 1),
        "pad_top": int(text_rows.min() - body_rows.min()),
        "pad_bottom": int(body_rows.max() - text_rows.max()),
    }


class NotchPaddingDefaultTests(unittest.TestCase):
    def test_drawing_helper_default_is_a_no_op(self):
        self.assertEqual(
            inspect.signature(awards.draw_award_badge)
            .parameters["notch_pad_ratio"].default,
            1.0,
        )

    def test_backend_request_default(self):
        self.assertEqual(main.RequestConfig().sash_badge_pad, 1.0)
        self.assertEqual(main.build_request_config({}).sash_badge_pad, 1.0)

    def test_omitting_the_argument_matches_an_explicit_one(self):
        # Guards the no-op default: an untouched caller must render exactly what
        # it rendered before notch_pad_ratio existed.  Parametrised over the font
        # scale because the padding floor is derived from the font — an unclamped
        # floor overtakes the nominal height above ~0.78 and silently re-renders
        # saved URLs.
        bg = Image.new("RGBA", (1000, 1500), (0, 0, 0, 255))
        for font_size_ratio in (0.43, 0.70, 1.00):
            with self.subTest(font_size_ratio=font_size_ratio):
                kwargs = dict(sash_type="nom", size_ratio_w=1.40, size_ratio_h=1.15,
                              notch_style="black", font_size_ratio=font_size_ratio,
                              notch_inset=0.0)
                omitted = awards.draw_award_badge(bg, "Oscar Nominee", **kwargs)
                explicit = awards.draw_award_badge(bg, "Oscar Nominee",
                                                   notch_pad_ratio=1.0, **kwargs)
                self.assertEqual(omitted.tobytes(), explicit.tobytes())

    def test_default_pad_height_is_independent_of_font_size(self):
        # The nominal height comes from size_ratio_h alone, so at the 1.0 default
        # the drawn height must not move when the font scale does.  This is what
        # keeps pre-existing URLs rendering at the height they always did.
        heights = {
            fsr: _metrics(_render(1.0, font_size_ratio=fsr))["badge_h"]
            for fsr in (0.43, 0.70, 0.90, 1.00)
        }
        self.assertEqual(len(set(heights.values())), 1, heights)

    def test_param_is_clamped_and_survives_junk(self):
        for raw, expected in (("0.70", 0.70), ("0.1", 0.5), ("9", 1.5), ("abc", 1.0)):
            with self.subTest(raw=raw):
                cfg = main.build_request_config({"sash_badge_pad": raw})
                self.assertAlmostEqual(cfg.sash_badge_pad, expected)


class NotchPaddingGeometryTests(unittest.TestCase):
    def test_lower_pad_reduces_height_and_padding(self):
        loose, tight = _metrics(_render(1.0)), _metrics(_render(0.7))
        self.assertLess(tight["badge_h"], loose["badge_h"])
        self.assertLess(tight["pad_top"], loose["pad_top"])
        self.assertLess(tight["pad_bottom"], loose["pad_bottom"])

    def test_font_size_is_unaffected(self):
        # The whole point: the label must render at exactly the same scale.
        # Measured as ink height, which can move by a row without the font
        # changing: the label is centred, so a pad can land it on a half pixel
        # (y=88.5 at 1.0, whole pixels at 0.9 / 0.75), and Pillow 11 — what
        # requirements.txt installs — anti-aliases that edge row across the
        # threshold where Pillow 12 does not.  A font that scaled with the badge
        # would be off by a fifth or more, so a pixel of slack still catches it.
        baseline = _metrics(_render(1.0))["text_h"]
        for pad in (0.9, 0.75, 0.6, 0.5):
            with self.subTest(pad=pad):
                self.assertAlmostEqual(_metrics(_render(pad))["text_h"], baseline, delta=1)

    def test_badge_width_is_unaffected(self):
        # Horizontal padding derives from base_h, so width must not move either.
        baseline = _metrics(_render(1.0))["badge_w"]
        for pad in (0.9, 0.75, 0.6, 0.5):
            with self.subTest(pad=pad):
                self.assertEqual(_metrics(_render(pad))["badge_w"], baseline)

    def test_raising_pad_adds_space(self):
        loose, extra = _metrics(_render(1.0)), _metrics(_render(1.3))
        self.assertGreater(extra["badge_h"], loose["badge_h"])
        self.assertGreater(extra["pad_bottom"], loose["pad_bottom"])

    def test_floor_prevents_clipping_at_extreme_values(self):
        # Below the floor the badge stops shrinking rather than eating glyphs.
        # The floor is measured from the label's ink, so compare against the
        # fully-collapsed render rather than assuming which ratio reaches it.
        floored = _metrics(_render(0.0))
        for pad in (0.2, 0.1):
            with self.subTest(pad=pad):
                m = _metrics(_render(pad))
                self.assertEqual(m["badge_h"], floored["badge_h"])
                self.assertEqual(m["text_h"], floored["text_h"])
                self.assertGreater(m["pad_top"], 0)
                self.assertGreater(m["pad_bottom"], 0)

    def test_floor_holds_for_a_taller_notch(self):
        # The floor tracks the font, so it must scale with size_ratio_h too.
        m = _metrics(_render(0.0, size_ratio_h=2.0))
        self.assertGreater(m["pad_top"], 0)
        self.assertGreater(m["pad_bottom"], 0)

    def test_floor_is_the_same_for_every_label(self):
        # The floor comes from a fixed reference string, not the label, so a
        # library of mixed-language awards trims to a uniform height instead of
        # one that jumps around with each title's tallest glyph.
        heights = {
            label: _metrics(_render(0.0, label=label))["badge_h"]
            for label in ("Oscar Winner", "Ganadora del Óscar",
                          "Prêmio Ganhador ÅÄÖ", "À binge-watcher")
        }
        self.assertEqual(len(set(heights.values())), 1, heights)

    def test_floor_measures_ink_so_diacritics_survive(self):
        # The reference string carries the tallest accented capitals, so accents
        # that sit above the cap height stay inside the badge.
        for label in ("Ganadora del Óscar", "Prêmio Ganhador ÅÄÖ", "À binge-watcher"):
            with self.subTest(label=label):
                m = _metrics(_render(0.0, label=label))
                self.assertGreater(m["pad_top"], 0)
                self.assertGreater(m["pad_bottom"], 0)

    def test_all_styles_render_at_a_tight_pad(self):
        # Silver/gold draw their trim through Cairo with a PIL fallback, so the
        # tightened height has to hold on both paths (cf. test_notch_open_top_border).
        bg = Image.new("RGBA", (1000, 1500), (70, 90, 120, 255))
        for style in ("frosted", "black", "silver", "gold"):
            for use_cairo in (True, False):
                with self.subTest(style=style, cairo=use_cairo):
                    with patch.object(awards, "_HAS_CAIRO", use_cairo):
                        out = awards.draw_award_badge(
                            bg, "Oscar Nominee", sash_type="win",
                            size_ratio_w=1.40, size_ratio_h=1.15,
                            notch_style=style, notch_pad_ratio=0.6,
                            font_size_ratio=0.43, notch_inset=0.004,
                        )
                    self.assertEqual(out.size, bg.size)


class NotchPaddingConfiguratorTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_slider_exists_with_neutral_default(self):
        self.assertIn(
            'id="cfg-sash-badge-pad" min="0.50" max="1.50" step="0.05" value="1.00"',
            self.html,
        )

    def test_slider_is_wired_both_ways(self):
        self.assertIn("params.set('sash_badge_pad'", self.html)
        self.assertIn("_setEl('cfg-sash-badge-pad'", self.html)

    def test_param_is_emitted_only_in_notch_mode(self):
        # sash_badge_pad has no meaning for the diagonal sash, so it must sit in
        # the same notch-gated block as the other sash_badge_* params.
        notch_block = self.html.split("if (sashMode === 'notch') {", 1)[1]
        notch_block = notch_block.split("params.set('sash_priority'", 1)[0]
        self.assertIn("params.set('sash_badge_pad'", notch_block)


if __name__ == "__main__":
    unittest.main()
