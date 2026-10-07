"""Mini Series: TMDB's own Miniseries type, or one season of at most eight episodes."""
import unittest

from discovery import extract_discovery_meta, pick_sash


def _meta(tmdb_data, media_type="tv"):
    return extract_discovery_meta(tmdb_data=tmdb_data, media_type=media_type,
                                  award_wins=[], award_noms=[], trending_rank=None)


class MiniSeriesTests(unittest.TestCase):
    def test_tmdbs_miniseries_type_counts_whatever_its_length(self):
        # ولد وبنت وشايب (TMDB tv/296043): a Miniseries of 10 episodes.
        meta = _meta({"tmdb_type": "Miniseries", "number_of_seasons": 1, "number_of_episodes": 10})
        self.assertTrue(meta.is_mini_series)
        self.assertEqual(pick_sash(meta, ["mini_series"]), ("Mini Series", "info"))

    def test_a_short_single_season_still_counts_without_the_type(self):
        self.assertTrue(_meta({"number_of_seasons": 1, "number_of_episodes": 6}).is_mini_series)
        self.assertTrue(_meta({"tmdb_type": "Scripted", "number_of_seasons": 1,
                               "number_of_episodes": 8}).is_mini_series)

    def test_a_long_scripted_season_is_not_a_mini_series(self):
        # لعبة نيوتن (TMDB tv/121487): Scripted, 1 season of 30 episodes.
        meta = _meta({"tmdb_type": "Scripted", "number_of_seasons": 1, "number_of_episodes": 30})
        self.assertFalse(meta.is_mini_series)
        self.assertIsNone(pick_sash(meta, ["mini_series"]))

    def test_films_are_never_mini_series(self):
        self.assertFalse(_meta({"tmdb_type": "Miniseries"}, media_type="movie").is_mini_series)


if __name__ == "__main__":
    unittest.main()
