"""Series release status and dates, from the episode data TMDB ships with the
series details.

TMDB's "Returning Series" means "not declared finished", not "on air", and it
used to map straight to "Airing": The Last of Us (last episode May 2025) and
Severance (March 2025) both read Airing in September 2026. The cases below are
the shapes those shows actually had.
"""
import unittest
from datetime import date

import main
from discovery import (
    DiscoveryMeta,
    RELEASE_STATUS_SLOTS,
    _release_status_label,
    release_date_label,
    tv_release_facts,
)
from i18n import load_languages, translate_sash

TODAY = date(2026, 9, 22)


def ep(season, number, air_date):
    return {"season_number": season, "episode_number": number, "air_date": air_date}


def seasons(*pairs):
    return [{"season_number": n, "air_date": d} for n, d in pairs]


def facts(status, **data):
    return tv_release_facts(status, data, today=TODAY)


class ReturningSeriesTests(unittest.TestCase):
    def test_airing_weekly(self):
        # Slow Horses: S6E1 last week, S6E2 tomorrow.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(6, 1, "2026-09-16"),
                  next_episode=ep(6, 2, "2026-09-23"), seasons=seasons((6, "2026-09-16"))),
            ("Airing", None, None))

    def test_the_last_episode_of_a_run_still_airs_for_a_fortnight(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(3, 8, "2026-09-12")),
            ("Airing", None, None))

    def test_dormant_with_nothing_announced_has_no_status(self):
        # The Last of Us: last episode May 2025, no next episode, no S3 entry.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 7, "2025-05-25"),
                  seasons=seasons((1, "2023-01-15"), (2, "2025-04-13"))),
            (None, None, None))

    def test_an_undated_listed_season_is_a_renewal(self):
        # Severance: S3 listed with no air date.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 10, "2025-03-20"),
                  seasons=seasons((1, "2022-02-17"), (2, "2025-01-17"), (3, None))),
            ("Renewed", None, None))

    def test_a_dated_next_season(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 8, "2026-02-03"),
                  next_episode=ep(3, 1, "2027-03-04")),
            ("Renewed", "2027-03-04", "Season 3"))

    def test_a_dated_listed_season_without_a_next_episode(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 8, "2026-02-03"),
                  seasons=seasons((2, "2026-01-01"), (3, "2026-11-05"))),
            ("Renewed", "2026-11-05", "Season 3"))

    def test_a_mid_season_break(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(4, 8, "2026-08-01"),
                  next_episode=ep(4, 9, "2027-01-08")),
            ("Airing", "2027-01-08", "Returns"))

    def test_a_short_break_is_just_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(4, 8, "2026-08-20"),
                  next_episode=ep(4, 9, "2026-10-01")),
            ("Airing", None, None))

    def test_back_to_back_seasons_do_not_stop_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(1, 12, "2026-09-18"),
                  next_episode=ep(2, 1, "2026-09-25")),
            ("Airing", None, None))

    def test_no_episode_data_keeps_the_plain_mapping(self):
        # The anime providers ship a status and nothing to refine it with.
        self.assertEqual(facts("Returning Series"), ("Airing", None, None))


class OtherStatusTests(unittest.TestCase):
    def test_an_unaired_show_is_dated_with_its_premiere(self):
        # Harry Potter (HBO): In Production, first air date Dec 25.
        self.assertEqual(
            facts("In Production", tmdb_release_date="2026-12-25",
                  next_episode=ep(1, 1, "2026-12-25"), seasons=seasons((1, "2026-12-25"))),
            ("Production", "2026-12-25", "Premiere"))

    def test_an_unaired_returning_series_is_a_premiere_not_a_renewal(self):
        # East of Eden: TMDB flipped it to Returning Series before S1E1 aired.
        self.assertEqual(
            facts("Returning Series", tmdb_release_date="2026-10-01",
                  next_episode=ep(1, 1, "2026-10-01"), seasons=seasons((1, "2026-09-30"))),
            ("Production", "2026-09-30", "Premiere"))

    def test_an_unaired_returning_series_without_a_date(self):
        self.assertEqual(facts("Returning Series", seasons=seasons((1, None))),
                         ("Production", None, None))

    def test_an_undated_unaired_show(self):
        self.assertEqual(facts("Planned"), ("Production", None, None))

    def test_finished_shows_pass_through(self):
        self.assertEqual(facts("Ended", last_episode=ep(5, 8, "2026-06-25")), ("Ended", None, None))
        self.assertEqual(facts("Canceled"), ("Cancelled", None, None))


class LabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_labels(self):
        self.assertEqual(release_date_label("2027-03-04", "Season 3", today=TODAY), "Mar 4 Season 3")
        self.assertEqual(release_date_label("2026-12-25", "Premiere", today=TODAY), "Dec 25 Premiere")
        self.assertEqual(release_date_label("2027-11-01", "Season 2", today=TODAY), "Nov 2027 Season 2")

    def test_the_status_label_uses_the_date_for_any_status_that_has_one(self):
        meta = DiscoveryMeta(release_status="Renewed")
        self.assertEqual(_release_status_label(meta), "Renewed")
        meta.upcoming_release_date, meta.upcoming_release_window = "2099-03-04", "Season 3"
        self.assertEqual(_release_status_label(meta), "Mar 2099 Season 3")

    def test_translations_keep_numbers_apart(self):
        # Most locales put the window first; "Temporada 3 4 mar" would run the
        # season into the day.
        self.assertEqual(translate_sash("Mar 4 Season 3", "es-ES"), "T3 4 Mar")
        self.assertEqual(translate_sash("Mar 4 Season 3", "fr-FR"), "S3 4 Mar")
        self.assertEqual(translate_sash("Dec 25 Premiere", "pt-BR"), "Estreia 25 Dez")
        self.assertEqual(translate_sash("Renewed", "it"), "Rinnovata")


class SlotTests(unittest.TestCase):
    def test_renewed_is_a_release_status_slot(self):
        self.assertIn("renewed", RELEASE_STATUS_SLOTS)
        self.assertIn("renewed", main.ALL_PRIORITY_SLOTS)

    def test_it_is_last_in_the_default_so_saved_moves_keep_their_positions(self):
        self.assertEqual(list(main._cfg.SASH_PRIORITY)[-1], "renewed")

    def test_an_explicit_list_with_airing_gains_renewed_behind_it(self):
        self.assertEqual(main._parse_sash_priority("wins,airing,ended"),
                         ["wins", "airing", "renewed", "ended"])
        self.assertEqual(main._parse_sash_priority("wins,airing,-renewed"), ["wins", "airing"])
        self.assertEqual(main._parse_sash_priority("wins,ended"), ["wins", "ended"])

    def test_release_status_expands_to_include_it(self):
        self.assertIn("renewed", main._parse_sash_priority("release_status"))


if __name__ == "__main__":
    unittest.main()
