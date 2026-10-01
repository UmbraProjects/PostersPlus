"""Frosted quality badges, quality before digital release, TVDB revivals of
shows TMDB has closed, TVDB landscape art, and the portrait settings brought
to landscape."""

import asyncio
import unittest
from datetime import date
from unittest import mock

from PIL import Image

import graphic_badges as gb
import landscape
import main
import tvdb
from discovery import DiscoveryMeta, tvdb_revival


TODAY = date(2026, 10, 1)


class TvdbRevivalTests(unittest.TestCase):
    """A show TMDB calls Ended that TVDB carries on with."""

    # Cyberpunk: Edgerunners as both had it on 2026-10-01.
    TMDB = {"last_episode": {"season_number": 1, "air_date": "2022-09-13"},
            "seasons": [{"season_number": 1}]}

    def _tvdb(self, **kw):
        return {"status": "continuing", "next_aired": "2026-10-20",
                "last_aired": "2026-10-20", "seasons": [1, 2], **kw}

    def test_a_later_dated_season_is_a_dated_renewal(self):
        self.assertEqual(tvdb_revival("Ended", self.TMDB, self._tvdb(), today=TODAY),
                         ("Renewed", "2026-10-20", "Season 2"))

    def test_cancelled_shows_are_revived_too(self):
        self.assertEqual(tvdb_revival("Cancelled", self.TMDB, self._tvdb(), today=TODAY)[0], "Renewed")

    def test_needs_a_season_past_tmdbs_last(self):
        self.assertIsNone(tvdb_revival("Ended", self.TMDB, self._tvdb(seasons=[1]), today=TODAY))

    def test_needs_a_sign_of_life(self):
        # TVDB splits some anime into more seasons than TMDB; ended is ended.
        self.assertIsNone(tvdb_revival(
            "Ended", self.TMDB, self._tvdb(status="ended", next_aired=None, last_aired="2022-09-13"),
            today=TODAY))

    def test_a_future_episode_is_a_sign_of_life_on_its_own(self):
        self.assertIsNotNone(tvdb_revival("Ended", self.TMDB, self._tvdb(status="ended"), today=TODAY))

    def test_a_season_airing_now_is_on_air(self):
        self.assertEqual(tvdb_revival("Ended", self.TMDB,
                                      self._tvdb(next_aired="2026-10-08", last_aired="2026-09-24"),
                                      today=TODAY), ("Airing", None, None))
        self.assertEqual(tvdb_revival("Ended", self.TMDB,
                                      self._tvdb(next_aired=None, last_aired="2026-09-24"),
                                      today=TODAY), ("Airing", None, None))

    def test_continuing_without_a_date_is_not_enough(self):
        # TVDB leaves "continuing" on some finished shows.
        self.assertIsNone(tvdb_revival("Ended", self.TMDB,
                                       self._tvdb(next_aired=None, last_aired="2022-09-13"),
                                       today=TODAY))

    def test_only_closed_shows_and_only_with_episode_data(self):
        self.assertIsNone(tvdb_revival("Airing", self.TMDB, self._tvdb(), today=TODAY))
        self.assertIsNone(tvdb_revival("Ended", self.TMDB, None, today=TODAY))
        # An anime provider's series: one entry per season, no episode list.
        self.assertIsNone(tvdb_revival("Ended", {}, self._tvdb(), today=TODAY))


class SeriesStatusFetchTests(unittest.TestCase):
    def test_keeps_status_days_and_official_seasons(self):
        record = {"id": 384541, "status": {"name": "Continuing"},
                  "nextAired": "2026-10-20", "lastAired": "2026-10-20",
                  "seasons": [{"number": 1, "type": {"type": "absolute"}},
                              {"number": 1, "type": {"type": "official"}},
                              {"number": 0, "type": {"type": "official"}},
                              {"number": 2, "type": {"type": "official"}}]}
        with mock.patch.object(tvdb, "tvdb_enabled", return_value=True), \
             mock.patch.object(tvdb, "resolve_tvdb_id", mock.AsyncMock(return_value=384541)), \
             mock.patch.object(tvdb, "get_cached_tvdb_json", return_value=None), \
             mock.patch.object(tvdb, "set_cached_tvdb_json") as store, \
             mock.patch.object(tvdb, "_authed_get", mock.AsyncMock(return_value=record)):
            got = asyncio.run(tvdb.fetch_series_status(None, tmdb_id="105248"))
        self.assertEqual(got, {"status": "continuing", "next_aired": "2026-10-20",
                               "last_aired": "2026-10-20", "seasons": [1, 2]})
        self.assertEqual(store.call_args[0][0], "status:v1:384541")


class TvdbBackgroundPickTests(unittest.TestCase):
    ARTS = {"backgrounds": [
        {"url": "eng-clean", "language": "eng", "score": 9, "text": False},
        {"url": "eng-title", "language": "eng", "score": 8, "text": True},
        {"url": "neutral", "language": None, "score": 7, "text": False},
    ], "posters": []}

    def _pick(self, languages):
        with mock.patch.object(tvdb, "poster_source_enabled", return_value=True), \
             mock.patch.object(tvdb, "resolve_tvdb_id", mock.AsyncMock(return_value=1)), \
             mock.patch.object(tvdb, "fetch_tvdb_artworks", mock.AsyncMock(return_value=self.ARTS)):
            return asyncio.run(tvdb.tvdb_poster_url(None, media_type="series", tmdb_id="1",
                                                    languages=languages, kind="backgrounds"))

    def test_textless_takes_the_untagged_background(self):
        self.assertEqual(self._pick(None), "neutral")

    def test_original_passes_over_a_tagged_background_marked_clean(self):
        self.assertEqual(self._pick(["en"]), "eng-title")


class FrostedQualityTests(unittest.TestCase):
    def test_style_parses_and_defaults_to_solid(self):
        self.assertEqual(main.build_request_config({}).badge_quality_style, "solid")
        self.assertEqual(main.build_request_config({"badge_quality_style": "frosted"}).badge_quality_style,
                         "frosted")
        self.assertEqual(main.build_request_config({"badge_quality_style": "x"}).badge_quality_style, "solid")

    def test_quality_marks_become_chips_and_the_certificate_does_not(self):
        items = dict(gb.row_items(["4K", "HDR10"], "R", None, 30, ("video", "res", "cert"), True,
                                  quality_look="rgb:200,210,230"))
        self.assertEqual(items["res"].info["frost_chip"][0], "rgb:200,210,230")
        self.assertEqual(items["video"].info["frost_chip"][0], "rgb:200,210,230")
        self.assertNotIn("frost_chip", items["cert"].info)
        solid = dict(gb.row_items(["4K"], None, None, 30, ("res",), True))
        self.assertNotIn("frost_chip", solid["res"].info)

    def test_a_chip_is_the_solid_box_size_and_resolves_to_glass(self):
        chip = dict(gb.row_items(["4K"], None, None, 30, ("res",), True, quality_look="auto"))["res"]
        self.assertEqual(chip.size, gb._box("4K", 30, True).size)
        img = Image.new("RGBA", (200, 100), (40, 90, 160, 255))
        gb.draw_row(img, [("res", chip)], left_x=10, center_y=50, gap=4)
        # Something landed, and it isn't the smoked placeholder.
        self.assertNotEqual(img.getpixel((10 + chip.width // 2, 40)), (40, 90, 160, 255))

    def test_marks_go_on_chips_too(self):
        mark = Image.new("RGBA", (40, 20), (255, 255, 255, 255))
        with mock.patch.object(gb, "_mark", side_effect=lambda name, h: mark.resize((h * 2, h))):
            item = dict(gb.row_items(["DV"], None, None, 30, ("video",), True,
                                     quality_look="rgb:200,100,50"))["video"]
        self.assertEqual(item.info["frost_chip"][0], "rgb:200,100,50")
        self.assertEqual(item.height, 30)

    def test_quality_look_is_none_for_solid(self):
        self.assertIsNone(gb.quality_look("solid", (1, 2, 3)))
        self.assertEqual(gb.quality_look("frosted", None), "auto")
        self.assertEqual(gb.quality_look("frosted", (1.2, 2.6, 3)), "rgb:1,3,3")


class GreyscaleTests(unittest.TestCase):
    def _meta(self, status):
        m = DiscoveryMeta()
        m.release_status = status
        return m

    def test_cinema_switch_is_the_shapes_own(self):
        cfg = main.RequestConfig()
        self.assertTrue(main._greyscale_wanted(cfg, self._meta("Cinema"), [], True))
        self.assertFalse(main._greyscale_wanted(cfg, self._meta("Cinema"), [], False))
        self.assertFalse(main._greyscale_wanted(cfg, self._meta("Streaming"), [], True))

    def test_a_digital_source_keeps_colour_when_asked(self):
        cfg = main.RequestConfig(cinema_greyscale_skip_if_available=True)
        self.assertFalse(main._greyscale_wanted(cfg, self._meta("Cinema"), ["WEBDL"], True))

    def test_landscape_greyscale_turns_the_bands_black(self):
        cfg = main.build_request_config({"shape": "landscape", "landscape_greyscale": "true",
                                         "vignette_poster_color_bottom": "true"})
        meta = self._meta("Cinema")
        meta.trending_rank = None
        art = Image.new("RGB", (1000, 563), (200, 40, 40))
        out = landscape.build_landscape(art, 80, "Horror", cfg, discovery_meta=meta)
        r, g, b, _ = out.getpixel((500, 560))
        self.assertTrue(abs(r - g) <= 2 and abs(g - b) <= 2, (r, g, b))


class LandscapeSettingsTests(unittest.TestCase):
    def test_defaults_leave_the_cache_key_alone(self):
        before = main._render_config_signature(main.build_request_config({"shape": "landscape"}))
        for name in ("landscape_greyscale", "landscape_badge_style", "landscape_badge_text_color",
                     "landscape_winner_star", "landscape_logo_scale", "landscape_rating_badges",
                     "landscape_art_source", "quality_after_digital", "badge_quality_style"):
            self.assertNotIn(name, before)

    def test_frost_is_sampled_only_for_a_drawn_quality_badge(self):
        cfg = main.build_request_config({"badge_display_mode": "7", "badge_quality_style": "frosted",
                                         "badge_group1": "chip:4:cert"})
        with mock.patch.object(main, "dominant_frost_rgb", wraps=main.dominant_frost_rgb) as sample:
            main.build_poster(Image.new("RGBA", (500, 750), (40, 60, 90, 255)), 80, "Drama", cfg,
                              quality_tokens=["4K"], certification="R")
        self.assertFalse(sample.called)

    def test_parse(self):
        cfg = main.build_request_config({
            "shape": "landscape", "landscape_badge_style": "gold", "landscape_badge_text_color": "F5C518",
            "landscape_logo_scale": "9", "landscape_winner_star": "true", "landscape_rating_badges": "true"})
        self.assertEqual(cfg.landscape_badge_style, "gold")
        self.assertEqual(cfg.landscape_badge_text_color, (0xF5, 0xC5, 0x18))
        self.assertEqual(cfg.landscape_logo_scale, 1.5)
        self.assertTrue(cfg.landscape_winner_star and cfg.landscape_rating_badges)

    def test_tvdb_art_source_needs_the_operator_switch(self):
        params = {"shape": "landscape", "landscape_art_source": "tvdb"}
        with mock.patch.object(tvdb, "poster_source_enabled", return_value=False):
            self.assertEqual(main.build_request_config(params).landscape_art_source, "tmdb")
        with mock.patch.object(tvdb, "poster_source_enabled", return_value=True):
            self.assertEqual(main.build_request_config(params).landscape_art_source, "tvdb")
            self.assertEqual(main.build_request_config({**params, "shape": "portrait"}).landscape_art_source,
                             "tmdb")

    def test_rating_badges_take_the_scores_place_and_fall_back_to_text(self):
        cfg = main.build_request_config({"shape": "landscape", "landscape_rating_badges": "true",
                                         "rating_badges": "imdb,tomatoes"})
        art = Image.new("RGB", (1000, 563), (30, 30, 30))
        # Marks not on disk: each score is drawn as text alone, never dropped.
        with mock.patch("rating_badges.badge", return_value=None):
            out = landscape.build_landscape(art, 80, "Drama", cfg,
                                            ratings={"imdb": 81, "tomatoes": 92})
        self.assertEqual(out.size, (1000, 563))

    def test_winner_star(self):
        cfg = main.build_request_config({"shape": "landscape", "landscape_winner_star": "true"})
        drawn = []
        with mock.patch.object(landscape, "_draw_badge", side_effect=lambda img, label, *a, **k: drawn.append(label)), \
             mock.patch("main.pick_sash", return_value=("Emmy Winner", "win")):
            landscape.build_landscape(Image.new("RGB", (1000, 563)), 80, "Drama", cfg,
                                      discovery_meta=DiscoveryMeta())
        self.assertEqual(drawn, ["★ EMMY WINNER"])


if __name__ == "__main__":
    unittest.main()
