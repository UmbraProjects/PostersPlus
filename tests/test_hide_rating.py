from pathlib import Path
import re
import unittest

import numpy as np
from PIL import Image

import landscape
from main import RequestConfig, build_poster, build_request_config


def _ink(image: Image.Image, top_ratio: float = 0.70) -> np.ndarray:
    """Mask of the overlay ink in the bottom of a poster drawn on flat art.

    The base is a single dark colour, so anything brighter than it is something
    the label pass put there — text, a pip, a bar or a frosted panel.
    """
    arr = np.array(image.convert("RGB")).astype(int)
    band = arr[int(arr.shape[0] * top_ratio):]
    return band.sum(axis=2) > 120


def _art(size: tuple[int, int] = (500, 750)) -> Image.Image:
    return Image.new("RGBA", size, (16, 16, 24, 255))


class HideRatingRenderTests(unittest.TestCase):
    """Every mode has to lose its own way of showing the score."""

    def _render(self, **kwargs) -> Image.Image:
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return build_poster(_art(), 87, "Drama", cfg, release_year="2019")

    def test_every_display_mode_renders_differently(self):
        # Mode 1 loses the accent bar, 2 and 4 the "★ 87", 3 its score segment
        # (or, in Year, the colour the separator was carrying).  Whichever it
        # is, no mode may come out drawing the same poster.
        for mode in (1, 2, 3, 4):
            with self.subTest(mode=mode):
                shown  = np.array(self._render(rating_display_mode=mode).convert("RGB"))
                hidden = np.array(self._render(rating_display_mode=mode,
                                               hide_rating=True).convert("RGB"))
                self.assertFalse(np.array_equal(shown, hidden))

    def test_modes_that_print_a_score_draw_less_ink_without_it(self):
        # Measured on the dark-bodied variants, where every overlay is light on
        # dark and a missing glyph is simply less ink.  (The frosted bar prints
        # dark text on a light panel, so counting bright pixels says nothing
        # there — the mode-4 assertions below use Pure Black for that reason.)
        for mode, extra in ((1, {}),
                            (2, {}),
                            (3, {"minimalist_append_mode": 1}),
                            (4, {"bar_style": "pure_black"})):
            with self.subTest(mode=mode):
                shown  = _ink(self._render(rating_display_mode=mode, **extra))
                hidden = _ink(self._render(rating_display_mode=mode, hide_rating=True, **extra))
                self.assertLess(hidden.sum(), shown.sum())

    def test_minimalist_layouts_drop_the_score_and_keep_the_rest(self):
        # Rating, Both and Split all print a score; each closes up around the
        # missing one exactly as it does for a title that has none.
        for append in (1, 2, 3):
            with self.subTest(append=append):
                shown  = _ink(self._render(rating_display_mode=3, minimalist_append_mode=append))
                hidden = _ink(self._render(rating_display_mode=3, minimalist_append_mode=append,
                                           hide_rating=True))
                self.assertLess(hidden.sum(), shown.sum())
                self.assertTrue(hidden.any(), "the genre / year group should survive")

    def test_minimalist_year_separator_stops_carrying_the_score(self):
        # Year mode shows the rating only as the separator's colour, so hiding
        # it has to change that mark's colour rather than the layout.
        def _separator_colours(hide: bool) -> set[tuple[int, int, int]]:
            img = self._render(rating_display_mode=3, minimalist_append_mode=0,
                               hide_rating=hide)
            arr = np.array(img.convert("RGB")).astype(int)
            band = arr[int(arr.shape[0] * 0.70):]
            mask = band.sum(axis=2) > 120
            return {tuple(px) for px in band[mask]}

        def _saturated(colours) -> bool:
            # The label's own ink is neutral; only a score colour has a hue.
            return any(max(px) - min(px) > 40 for px in colours)

        self.assertTrue(_saturated(_separator_colours(False)))
        self.assertFalse(_saturated(_separator_colours(True)))

    def test_clean_mode_draws_nothing_when_both_fields_are_hidden(self):
        # "★ 87" is the whole label here, so Hide Genre and Hide Rating
        # together are a legitimate way to ask for a bare poster.
        bare = self._render(rating_display_mode=2, hide_genre=True, hide_rating=True)
        self.assertFalse(_ink(bare).any())

    def test_bar_rating_styles_fall_back_to_their_plain_body(self):
        # A Rating Bar style with nothing to fill would draw an empty stripe,
        # which reads as a score of zero rather than as no score.
        for style, plain in (("rating_black", "pure_black"), ("rating_frosted", "frosted")):
            with self.subTest(style=style):
                hidden = _ink(self._render(rating_display_mode=4, bar_style=style,
                                           hide_rating=True))
                as_plain = _ink(self._render(rating_display_mode=4, bar_style=plain,
                                             hide_rating=True))
                self.assertTrue(np.array_equal(hidden, as_plain))

    def test_landscape_info_strip_drops_the_score(self):
        def _strip(hide: bool) -> np.ndarray:
            cfg = RequestConfig(shape="landscape", sash_mode="hidden", hide_rating=hide)
            img = landscape.build_landscape(_art((1000, 563)), 87, "Drama", cfg,
                                            release_year="2019")
            return _ink(img)

        self.assertLess(_strip(True).sum(), _strip(False).sum())


class HideRatingRequestTests(unittest.TestCase):
    def test_parameter_is_read_and_defaults_off(self):
        self.assertFalse(build_request_config({}).hide_rating)
        self.assertTrue(build_request_config({"hide_rating": "true"}).hide_rating)
        self.assertFalse(build_request_config({"hide_rating": "false"}).hide_rating)


class HideRatingConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_switch_sits_in_the_labels_group_beside_hide_genre(self):
        self.assertIn('id="tog-hide-rating"', self.html)
        self.assertLess(self.html.index('id="tog-hide-genre"'),
                        self.html.index('id="tog-hide-rating"'))

    def test_build_and_import_round_trip_the_parameter(self):
        self.assertIn("set('hide_rating', on('tog-hide-rating') ? 'true' : 'false');", self.html)
        self.assertRegex(self.html, r"p\.has\('hide_rating'\)")

    def test_the_switch_is_shared_with_landscape_like_hide_genre(self):
        # Both shapes print the score in a metadata line, so there is one
        # control whichever is showing, kept per shape like Hide Genre — and
        # sent as hide_rating / landscape_hide_rating accordingly.
        shared = re.search(r"const _SHARED_PARAM = \{(.*?)\};", self.html, re.S).group(1)
        self.assertIn("'tog-hide-rating':           'hide_rating'", shared)
        # One row for both shapes, in the Rating tab: not marked portrait-only.
        self.assertIn('<div class="toggle-row" id="hide-rating-row">', self.html)

    def test_score_only_controls_hide_with_the_rating(self):
        # A control that cannot change anything should not be on screen to be
        # tried — the same rule the landscape tabs follow.
        fields = re.search(r"function updateRatingFields\(\) \{(.*?)\n\}", self.html, re.S).group(1)
        for row in ("rating-glow-fields",
                    "numeric-score10-row", "minimalist-score10-row", "bar-score10-row"):
            with self.subTest(row=row):
                line = re.search(rf"getElementById\('{row}'\)\.style\.display\s*=([^\n]*)", fields)
                self.assertIsNotNone(line, f"{row} is not painted by updateRatingFields")
                self.assertIn("!hideRating", line.group(1))
        # The palette row asks _paletteUsed, which answers no with it hidden.
        used = re.search(r"function _paletteUsed\(\) \{(.*?)\n\}", self.html, re.S).group(1)
        # Except landscape's Rating Bar, whose bar stays with the rating hidden.
        self.assertIn("if (c('tog-hide-rating') && !_scoreBarKept()) return false;", used)
        self.assertIn("getElementById('rating-alt-colors-toggle').style.display = _paletteUsed()", fields)

    def test_hiding_the_rating_leaves_the_rating_bar_choice_alone(self):
        # The fallback is display-only. Writing it into the select made the
        # switch lossy: turning Hide Rating back off (or turning it on for
        # landscape only, where it is kept per shape) left the portrait Bar
        # style on the plain body for good.
        self.assertRegex(
            self.html,
            r"const _RATING_BAR_FALLBACK = \{ rating_frosted: 'frosted', rating_black: 'pure_black' \};",
        )
        rows = re.search(r"function _updateBarRatingRows\(\) \{(.*?)\n\}", self.html, re.S).group(1)
        self.assertNotIn("_setEl('cfg-bar-style'", rows)
        self.assertIn("(c('tog-hide-rating') && _RATING_BAR_FALLBACK[picked]) || picked", rows)
        # Nor may the accent colour be reset while its row is merely hidden.
        self.assertIn("if ((isFrosted || isBlack) && visible.length", rows)

    def test_returning_to_portrait_repaints_the_rating_rows(self):
        shape = re.search(r"function updateShapeFields\(\) \{(.*?)\n\}", self.html, re.S).group(1)
        # Both ways: each shape's Display Mode picks the rating rows on show.
        self.assertRegex(shape, r"\n  updateRatingFields\(\);")


if __name__ == "__main__":
    unittest.main()
