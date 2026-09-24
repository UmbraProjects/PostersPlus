from datetime import date, timedelta
from pathlib import Path
import re
import unittest

from main import _unreleased_for_rating, build_request_config


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


class UnreleasedForRatingTests(unittest.TestCase):
    def test_only_production_counts(self):
        # A film in cinemas has an audience; its score is a real one.
        for status in ("Cinema", "Streaming", "Physical", "Airing", "Renewed",
                       "Ended", "Cancelled", None):
            with self.subTest(status=status):
                self.assertFalse(_unreleased_for_rating(status, "movie", {}))
                self.assertFalse(_unreleased_for_rating(status, "series", {}))

    def test_unaired_series_and_unreleased_film_hide(self):
        self.assertTrue(_unreleased_for_rating("Production", "movie", {}))
        self.assertTrue(_unreleased_for_rating("Production", "series", {}))
        self.assertTrue(_unreleased_for_rating(
            "Production", "tv", {"next_episode": {"air_date": _day(400)}}))

    def test_series_with_an_aired_episode_keeps_its_score(self):
        # TMDB leaves some shows at "In Production" between seasons.
        aired = {"last_episode": {"air_date": _day(-30)}}
        self.assertFalse(_unreleased_for_rating("Production", "series", aired))
        self.assertFalse(_unreleased_for_rating("Production", "tv", aired))

    def test_unaired_series_hides_whatever_the_status_says(self):
        # East of Eden: "Returning Series" on TMDB before S1E1 aired.
        upcoming = {"next_episode": {"air_date": _day(7)}, "seasons": [{"season_number": 1}]}
        for status in ("Renewed", "Airing", None):
            with self.subTest(status=status):
                self.assertTrue(_unreleased_for_rating(status, "series", upcoming))
        self.assertFalse(_unreleased_for_rating("Cancelled", "series", upcoming))
        self.assertFalse(_unreleased_for_rating("Renewed", "movie", upcoming))

    def test_a_future_last_episode_is_not_an_aired_one(self):
        self.assertTrue(_unreleased_for_rating(
            "Production", "series", {"last_episode": {"air_date": _day(10)}}))


class HideUnreleasedRatingRequestTests(unittest.TestCase):
    def test_parameter_is_read_and_defaults_off(self):
        self.assertFalse(build_request_config({}).hide_unreleased_rating)
        self.assertTrue(build_request_config({"hide_unreleased_rating": "true"}).hide_unreleased_rating)
        self.assertFalse(build_request_config({"hide_unreleased_rating": "false"}).hide_unreleased_rating)

    def test_it_does_not_turn_on_hide_rating(self):
        # The per-render decision is made in the pipeline; the setting itself
        # must leave every released title's score alone.
        self.assertFalse(build_request_config({"hide_unreleased_rating": "true"}).hide_rating)


class HideUnreleasedRatingPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Path("main.py").read_text(encoding="utf-8")

    def test_status_is_resolved_for_the_setting_alone(self):
        self.assertIn("if _status_sash or rcfg.hide_unreleased_rating:", self.src)

    def test_status_asked_for_only_by_the_setting_stays_off_the_sash(self):
        # The status also drives the sash and the greyscale art; neither was
        # asked for when only the rating switch wanted it.
        self.assertRegex(self.src, r"if not _status_sash:\n\s+_release_status = None")
        self.assertLess(self.src.index("_hide_unreleased = (rcfg.hide_unreleased_rating"),
                        self.src.index("if not _status_sash:"))

    def test_render_uses_hide_rating_and_the_composite_expires_with_the_status(self):
        self.assertIn("dataclasses.replace(rcfg, hide_rating=True) if _hide_unreleased else rcfg",
                      self.src)
        self.assertIn("release_status_ttl_seconds(_status_for_ttl)", self.src)


class HideUnreleasedRatingConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_switch_sits_after_hide_rating(self):
        self.assertIn('id="tog-hide-unreleased-rating"', self.html)
        self.assertLess(self.html.index('id="tog-hide-rating"'),
                        self.html.index('id="tog-hide-unreleased-rating"'))

    def test_build_and_import_round_trip_the_parameter(self):
        self.assertIn("params.set('hide_unreleased_rating', c('tog-hide-unreleased-rating') ? 'true' : 'false');",
                      self.html)
        self.assertRegex(self.html, r"p\.has\('hide_unreleased_rating'\)")

    def test_switch_follows_hide_rating_into_landscape(self):
        docked = re.search(r"const _DOCKED_ROWS = \[(.*?)\];", self.html, re.S).group(1)
        self.assertIn("'hide-unreleased-rating-row'", docked)
        self.assertIn('id="hide-unreleased-rating-home"', self.html)


if __name__ == "__main__":
    unittest.main()
