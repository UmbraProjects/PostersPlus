from pathlib import Path
import re
import unittest

import numpy as np
from PIL import Image

import landscape
from main import RequestConfig, build_poster, build_request_config, _LANDSCAPE_SPLIT_PARAMS


def _ink(image: Image.Image, top_ratio: float = 0.70) -> np.ndarray:
    """Mask of the overlay ink in the bottom of a poster drawn on flat art."""
    arr = np.array(image.convert("RGB")).astype(int)
    band = arr[int(arr.shape[0] * top_ratio):]
    return band.sum(axis=2) > 120


def _art(size: tuple[int, int] = (500, 750)) -> Image.Image:
    return Image.new("RGBA", size, (16, 16, 24, 255))


class HideYearRenderTests(unittest.TestCase):
    def _render(self, year: str | None = "2019", **kwargs) -> Image.Image:
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return build_poster(_art(), 87, "Drama", cfg, release_year=year)

    def test_reads_exactly_like_a_title_with_no_year(self):
        # Every layout already closes up around a missing year, so hiding it
        # must give the same poster as not having one.
        for mode, extra in ((1, {"accent_bar_append_mode": 0}),
                            (1, {"accent_bar_append_mode": 2}),
                            (3, {"minimalist_append_mode": 2}),
                            (3, {"minimalist_append_mode": 3}),
                            (4, {"bar_append": "rating_year"}),
                            (4, {"bar_append": "year"})):
            with self.subTest(mode=mode, **extra):
                hidden = np.array(self._render(rating_display_mode=mode, hide_year=True, **extra))
                absent = np.array(self._render(year=None, rating_display_mode=mode, **extra))
                shown  = np.array(self._render(rating_display_mode=mode, **extra))
                self.assertTrue(np.array_equal(hidden, absent))
                self.assertFalse(np.array_equal(hidden, shown))

    def test_minimalist_year_mode_prints_the_score_instead(self):
        # Year mode shows the score only as the colour of the separator before
        # the year; with no year it must print the score, as Rating mode does,
        # rather than silently dropping it.
        hidden = np.array(self._render(rating_display_mode=3, minimalist_append_mode=0,
                                       hide_year=True))
        as_rating = np.array(self._render(rating_display_mode=3, minimalist_append_mode=1))
        self.assertTrue(np.array_equal(hidden, as_rating))

    def test_minimalist_year_mode_with_the_rating_hidden_too_is_just_the_genre(self):
        both = _ink(self._render(rating_display_mode=3, minimalist_append_mode=0,
                                 hide_year=True, hide_rating=True))
        genre = _ink(self._render(rating_display_mode=3, minimalist_append_mode=1,
                                  hide_rating=True))
        self.assertTrue(np.array_equal(both, genre))

    def test_landscape_info_strip_drops_the_year(self):
        def _strip(hide: bool) -> np.ndarray:
            cfg = RequestConfig(shape="landscape", sash_mode="hidden", hide_year=hide)
            img = landscape.build_landscape(_art((1000, 563)), 87, "Drama", cfg,
                                            release_year="2019")
            return _ink(img)

        self.assertLess(_strip(True).sum(), _strip(False).sum())


class HideYearRequestTests(unittest.TestCase):
    def test_parameter_is_read_and_defaults_off(self):
        self.assertFalse(build_request_config({}).hide_year)
        self.assertTrue(build_request_config({"hide_year": "true"}).hide_year)
        self.assertFalse(build_request_config({"hide_year": "false"}).hide_year)

    def test_it_is_kept_per_shape(self):
        self.assertIn("hide_year", _LANDSCAPE_SPLIT_PARAMS)


class HideYearConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_switch_sits_between_hide_genre_and_hide_rating(self):
        self.assertLess(self.html.index('id="tog-hide-genre"'), self.html.index('id="tog-hide-year"'))
        self.assertLess(self.html.index('id="tog-hide-year"'), self.html.index('id="tog-hide-rating"'))

    def test_build_and_import_round_trip_the_parameter(self):
        self.assertIn("set('hide_year',   on('tog-hide-year')   ? 'true' : 'false');", self.html)
        self.assertRegex(self.html, r"p\.has\('hide_year'\)")

    def test_the_switch_is_shared_with_landscape(self):
        shared = re.search(r"const _SHARED_PARAM = \{(.*?)\};", self.html, re.S).group(1)
        self.assertIn("'tog-hide-year':             'hide_year'", shared)
        docked = re.search(r"const _DOCKED_ROWS = \[(.*?)\];", self.html, re.S).group(1)
        self.assertIn("'hide-year-row'", docked)
        self.assertIn('id="hide-year-home"', self.html)


if __name__ == "__main__":
    unittest.main()
