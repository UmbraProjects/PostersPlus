"""trending_style=number / ribbon draws the trending rank as its own mark, and
the sash moves on to the next label in the priority list."""
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import discovery
import i18n
import main
import trending_rank


def _poster(w=500, h=750):
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _alpha_sum(img, box):
    return int(np.asarray(img.crop(box))[..., 3].sum())


class TrendingStyleConfigTests(unittest.TestCase):
    def test_default_is_the_sash_label(self):
        self.assertEqual(main.build_request_config({}).trending_style, "sash")

    def test_number_and_ribbon_parse(self):
        for style in ("number", "ribbon", "NUMBER"):
            with self.subTest(style=style):
                cfg = main.build_request_config({"trending_style": style})
                self.assertEqual(cfg.trending_style, style.lower())

    def test_unknown_value_keeps_the_default(self):
        self.assertEqual(main.build_request_config({"trending_style": "banner"}).trending_style, "sash")

    def test_default_leaves_the_cache_key_alone(self):
        sig = main._render_config_signature(main.build_request_config({}))
        self.assertNotIn("trending_style", sig)
        sig = main._render_config_signature(main.build_request_config({"trending_style": "ribbon"}))
        self.assertIn("trending_style", sig)


class ShownTrendingRankTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("TRENDING_FETCH_COUNT", 40), ("TRENDING_BROAD_FETCH_COUNT", 100)):
            patcher = mock.patch.object(discovery._cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rank(self, rank, priority):
        return discovery.shown_trending_rank(discovery.DiscoveryMeta(trending_rank=rank), priority)

    def test_trending_slot_anywhere_in_the_list(self):
        # Below a slot that would win the sash: the mark is drawn regardless.
        self.assertEqual(self._rank(3, ["wins", "new_season", "trending"]), 3)

    def test_broad_ranks_need_the_broad_slot(self):
        self.assertIsNone(self._rank(60, ["trending"]))
        self.assertEqual(self._rank(60, ["trending_broad"]), 60)
        self.assertIsNone(self._rank(3, ["trending_broad"]))

    def test_nothing_without_a_trending_slot_or_rank(self):
        self.assertIsNone(self._rank(3, ["wins", "new_season"]))
        self.assertIsNone(self._rank(None, ["trending"]))
        self.assertIsNone(self._rank(101, ["trending", "trending_broad"]))


class RankMarkRenderingTests(unittest.TestCase):
    def test_number_sits_top_left_and_mirrors_right(self):
        left = trending_rank.draw_rank_number(_poster(), 7)
        right = trending_rank.draw_rank_number(_poster(), 7, right=True)
        self.assertGreater(_alpha_sum(left, (0, 0, 150, 150)), 0)
        self.assertEqual(_alpha_sum(left, (350, 0, 500, 150)), 0)
        self.assertGreater(_alpha_sum(right, (350, 0, 500, 150)), 0)
        self.assertEqual(_alpha_sum(right, (0, 0, 150, 150)), 0)
        # Nothing below the numeral's band but its shadow's fringe.
        self.assertEqual(_alpha_sum(left, (0, 200, 500, 750)), 0)

    def test_number_is_silver_to_white(self):
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        out = np.asarray(trending_rank.draw_rank_number(art, 1)).astype(int)
        inset, bottom = trending_rank.number_box(500)
        head = out[inset + 4, :150, :3].max()
        # A few rows up: the box allows for the round digits' overshoot.
        foot = out[bottom - 10, :150, :3].max()
        self.assertGreater(head, foot)
        self.assertGreater(foot, 120)

    def test_number_shrinks_to_max_w(self):
        wide = trending_rank.draw_rank_number(_poster(), 24).getbbox()
        narrow = trending_rank.draw_rank_number(_poster(), 24, max_w=80).getbbox()
        self.assertLess(narrow[2] - narrow[0], wide[2] - wide[0])
        # Never below 60 % of its size, however little room there is.
        tiny = trending_rank.draw_rank_number(_poster(), 24, max_w=1).getbbox()
        self.assertGreater(tiny[3] - tiny[1], 0.5 * (wide[3] - wide[1]))

    def test_ribbon_hangs_from_the_top_edge_in_from_the_corner(self):
        out = trending_rank.draw_rank_ribbon(_poster(), 3)
        top_row = np.asarray(out)[0, :, 3]
        opaque = np.flatnonzero(top_row > 200)
        self.assertGreater(opaque.size, 0)
        self.assertGreaterEqual(opaque[0], round(trending_rank._RIB_INSET * 500) - 1)
        self.assertEqual(_alpha_sum(out, (250, 0, 500, 750)), 0)
        right = trending_rank.draw_rank_ribbon(_poster(), 3, right=True)
        self.assertEqual(_alpha_sum(right, (0, 0, 250, 750)), 0)

    def test_ribbon_widens_for_three_digits(self):
        two = trending_rank.draw_rank_ribbon(_poster(), 40)
        three = trending_rank.draw_rank_ribbon(_poster(), 100)
        row = lambda im: np.flatnonzero(np.asarray(im)[0, :, 3] > 200)
        self.assertGreater(len(row(three)), len(row(two)))


class RibbonOptionTests(unittest.TestCase):
    def _foot(self, im):
        # Lowest row with the ribbon's body in it (the shadow is fainter).
        rows = np.flatnonzero((np.asarray(im)[:, :250, 3] > 200).any(axis=1))
        return rows[-1]

    def test_label_lengthens_the_ribbon(self):
        plain = trending_rank.draw_rank_ribbon(_poster(), 3)
        labelled = trending_rank.draw_rank_ribbon(_poster(), 3, label="SERIES")
        self.assertGreater(self._foot(labelled), self._foot(plain))

    def test_top_inset_grows_the_ribbon_under_the_crop(self):
        plain = trending_rank.draw_rank_ribbon(_poster(), 3)
        grown = trending_rank.draw_rank_ribbon(_poster(), 3, top_inset=6)
        self.assertEqual(self._foot(grown), self._foot(plain) + 6)
        # Still hangs from the edge, and the rank moves down with the body.
        self.assertGreater(np.asarray(grown)[0, :250, 3].max(), 200)
        # The rank's first row of ink, across the middle of the ribbon (its
        # sides carry a light hairline).
        ink = lambda im: np.flatnonzero((np.asarray(im)[:, 50:75, :3].max(axis=2) > 150).any(axis=1))[0]
        self.assertEqual(ink(grown), ink(plain) + 6)

    def test_ribbon_takes_the_primary_clients_top_inset(self):
        def foot(client):
            cfg = main.build_request_config({"trending_style": "ribbon", "primary_client": client})
            return self._foot(main._draw_trending_rank(_poster(), cfg, 3, None)[0])
        self.assertEqual(foot("stremio_desktop_web"), foot("stremio_tv_nuvio") + round(750 * 0.004))

    def test_long_label_stays_inside_the_ribbon(self):
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        out = np.asarray(trending_rank.draw_rank_ribbon(art, 3, label="MFULULIZO")).astype(int)
        x0 = round(trending_rank._RIB_INSET * 500)
        x1 = x0 + round(trending_rank._RIB_W * 500)
        text = np.flatnonzero(out[:, :, :3].max(axis=(0, 2)) > 150)
        self.assertGreaterEqual(text[0], x0)
        self.assertLessEqual(text[-1], x1)

    def test_corner_nests_against_the_edge(self):
        for right in (False, True):
            with self.subTest(right=right):
                out = np.asarray(trending_rank.draw_rank_ribbon(_poster(), 3, right=right, corner=True))
                edge = out[5, -1 if right else 0, 3]
                self.assertGreater(edge, 200)
                inset = np.asarray(trending_rank.draw_rank_ribbon(_poster(), 3, right=right))
                self.assertLess(inset[5, -1 if right else 0, 3], 60)

    def test_scale_sizes_both_marks(self):
        for draw in (trending_rank.draw_rank_ribbon, trending_rank.draw_rank_number):
            with self.subTest(draw=draw.__name__):
                small = draw(_poster(), 8, scale=0.6).getbbox()
                big = draw(_poster(), 8, scale=1.5).getbbox()
                self.assertGreater(big[2] - big[0], 1.8 * (small[2] - small[0]))
                self.assertGreater(big[3], small[3])

    def test_config_parses_and_clamps(self):
        cfg = main.build_request_config({"trending_scale": "9", "trending_label": "true",
                                         "trending_corner": "1"})
        self.assertEqual(cfg.trending_scale, 2.0)
        self.assertTrue(cfg.trending_label and cfg.trending_corner)
        sig = main._render_config_signature(main.build_request_config({}))
        for name in ("trending_scale", "trending_label", "trending_corner"):
            self.assertNotIn(name, sig)

    def test_label_follows_kind_and_language(self):
        i18n.load_languages()
        cfg = main.build_request_config({"trending_style": "ribbon", "trending_label": "true"})
        seen = []
        real = trending_rank.draw_rank_ribbon
        with mock.patch.object(trending_rank, "draw_rank_ribbon",
                               side_effect=lambda *a, **kw: seen.append(kw["label"]) or real(*a, **kw)):
            for kind in ("movie", "series", "anime", None):
                main._draw_trending_rank(_poster(), cfg, 3, None, kind)
            cfg.logo_language = "de"
            main._draw_trending_rank(_poster(), cfg, 3, None, "series")
        self.assertEqual(seen, ["FILM", "SERIES", "ANIME", None, "SERIE"])


class RibbonStyleTests(unittest.TestCase):
    def _art(self):
        return Image.new("RGBA", (500, 750), (40, 90, 160, 255))

    def _pixel(self, im, xy):
        return np.asarray(im)[xy[1], xy[0], :3].astype(int)

    def test_every_style_draws(self):
        for style in trending_rank.RIBBON_STYLES:
            with self.subTest(style=style):
                out = trending_rank.draw_rank_ribbon(self._art(), 3, label="FILM", style=style)
                self.assertFalse(np.array_equal(np.asarray(out), np.asarray(self._art())))

    def test_unknown_style_is_charcoal(self):
        a = np.asarray(trending_rank.draw_rank_ribbon(self._art(), 3, style="plaid"))
        b = np.asarray(trending_rank.draw_rank_ribbon(self._art(), 3))
        np.testing.assert_array_equal(a, b)

    def test_frosted_is_a_light_tinted_panel_with_dark_ink(self):
        out = trending_rank.draw_rank_ribbon(self._art(), 3, style="frosted", tint_rgb=(40, 90, 160))
        # Just inside the ribbon's top-left, clear of the number.
        body = self._pixel(out, (round(trending_rank._RIB_INSET * 500) + 3, 3))
        self.assertGreater(body.mean(), 140)
        self.assertGreater(body[2], body[0])     # the blue of the art shows through

    def test_gold_trim_down_the_side(self):
        out = np.asarray(trending_rank.draw_rank_ribbon(self._art(), 3, style="gold")).astype(int)
        x0 = round(trending_rank._RIB_INSET * 500)
        side = out[30, x0:x0 + 4, :3]
        # Some pixel in from the edge is gold: red and green well above blue.
        self.assertTrue(((side[:, 0] - side[:, 2]) > 60).any())

    def test_label_sits_clear_of_the_notch_point(self):
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        out = np.asarray(trending_rank.draw_rank_ribbon(art, 3, label="FILM")).astype(int)
        rib_w = trending_rank._RIB_W * 500
        apex = rib_w * (trending_rank._RIB_BODY + trending_rank._RIB_LABEL_BAND - trending_rank._RIB_NOTCH)
        cx = round(trending_rank._RIB_INSET * 500 + rib_w / 2)
        col = out[:, cx - 12:cx + 12, :3].max(axis=(1, 2))
        text_rows = np.flatnonzero(col > 150)
        # The label's foot sits _RIB_LABEL_GAP of the width above the point.
        self.assertAlmostEqual(apex - text_rows[-1], trending_rank._RIB_LABEL_GAP * rib_w, delta=2)

    def test_config_parses(self):
        self.assertEqual(main.build_request_config({}).trending_ribbon_style, "charcoal")
        self.assertEqual(main.build_request_config({"trending_ribbon_style": "gold"}).trending_ribbon_style, "gold")
        self.assertEqual(main.build_request_config({"trending_ribbon_style": "x"}).trending_ribbon_style, "charcoal")
        self.assertNotIn("trending_ribbon_style", main._render_config_signature(main.build_request_config({})))

    def test_frosted_ribbon_has_its_own_opacity_and_saturation(self):
        cfg = main.build_request_config({"trending_style": "ribbon", "trending_ribbon_style": "frosted",
                                         "trending_frost_opacity": "0.3", "trending_frost_saturation": "5",
                                         "sash_badge_frost_opacity": "0.9"})
        self.assertEqual((cfg.trending_frost_opacity, cfg.trending_frost_saturation), (0.3, 2.0))
        seen = {}
        with mock.patch.object(trending_rank, "draw_rank_ribbon", side_effect=lambda *a, **kw: seen.update(kw) or a[0]):
            main._draw_trending_rank(_poster(), cfg, 3, None, "movie", frost=((40, 90, 160), False))
        self.assertEqual((seen["frost_opacity"], seen["frost_saturation"]), (0.3, 2.0))
        self.assertEqual(seen["tint_rgb"], (40, 90, 160))

    def test_opacity_changes_the_frosted_body(self):
        art = self._art()
        thin = trending_rank.draw_rank_ribbon(art, 3, style="frosted", tint_rgb=(40, 90, 160), frost_opacity=0.2)
        thick = trending_rank.draw_rank_ribbon(art, 3, style="frosted", tint_rgb=(40, 90, 160), frost_opacity=0.9)
        xy = (round(trending_rank._RIB_INSET * 500) + 3, 3)
        self.assertGreater(self._pixel(thick, xy).mean(), self._pixel(thin, xy).mean() + 30)


class RankSideTests(unittest.TestCase):
    def test_side_is_the_setting_alone(self):
        cases = (
            ({}, False),
            ({"trending_side": "right"}, True),
            ({"trending_side": "RIGHT"}, True),
            ({"trending_side": "middle"}, False),
            # The sash no longer pushes the mark: trending_sash decides what
            # the sash does instead.
            ({"sash_side": "left"}, False),
            ({"sash_mode": "notch", "sash_badge_pos": "left"}, False),
        )
        for params, right in cases:
            with self.subTest(**params):
                self.assertEqual(main._rank_on_right(main.build_request_config(params)), right)

    def test_do_nothing_leaves_the_sash_alone(self):
        cfg = main.build_request_config({"sash_mode": "notch", "sash_badge_pos": "left"})
        self.assertEqual(cfg.trending_sash, "keep")
        self.assertIs(main._sash_beside_rank(cfg), cfg)

    def test_hide_turns_the_sash_off(self):
        for mode in ("sash", "notch"):
            with self.subTest(mode=mode):
                cfg = main.build_request_config({"sash_mode": mode, "trending_sash": "hide"})
                self.assertEqual(main._sash_beside_rank(cfg).sash_mode, "hidden")

    def test_opposite_moves_the_sash_to_the_free_corner(self):
        cases = (
            # (params, sash_side after, sash_badge_pos after)
            ({"sash_side": "left"}, "right", "center"),
            ({"sash_side": "right", "trending_side": "right"}, "left", "center"),
            ({"sash_mode": "notch"}, "right", "right"),
            ({"sash_mode": "notch", "sash_badge_pos": "right", "trending_side": "right"}, "right", "left"),
            ({"sash_mode": "notch", "sash_badge_pos": "left"}, "right", "right"),
        )
        for params, side, pos in cases:
            with self.subTest(**params):
                cfg = main._sash_beside_rank(main.build_request_config({**params, "trending_sash": "opposite"}))
                self.assertEqual((cfg.sash_side, cfg.sash_badge_pos), (side, pos))

    def test_opposite_leaves_a_hidden_sash_hidden(self):
        cfg = main.build_request_config({"sash_mode": "hidden", "trending_sash": "opposite"})
        self.assertEqual(main._sash_beside_rank(cfg).sash_mode, "hidden")

    def test_defaults_stay_out_of_the_cache_key(self):
        sig = main._render_config_signature(main.build_request_config({}))
        self.assertNotIn("trending_side", sig)
        self.assertNotIn("trending_sash", sig)

    def test_numeral_clears_what_the_sash_drew_beside_it(self):
        cfg = main.build_request_config({"trending_style": "number"})
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        free = main._draw_trending_rank(art.copy(), cfg, 24, None)[0]
        before = np.asarray(art)[:trending_rank.number_box(500)[1]].copy()
        # A block standing in for a notch, from x=150 across the top band.
        blocked = art.copy()
        blocked.paste((255, 255, 255, 255), (150, 0, 350, 60))
        out = main._draw_trending_rank(blocked, cfg, 24, before)[0]
        lit = lambda im: np.flatnonzero(np.asarray(im)[40:120, :150, :3].max(axis=(0, 2)) > 100)
        self.assertLess(lit(out)[-1], lit(free)[-1])


class SashBesideRankRenderTests(unittest.TestCase):
    """Through build_poster: a titled trending at #3 with a New Season sash."""

    def setUp(self):
        patcher = mock.patch.object(discovery._cfg, "TRENDING_FETCH_COUNT", 40)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _render(self, **params):
        cfg = main.build_request_config({"trending_style": "ribbon", "sash_mode": "notch",
                                         "sash_badge_style": "black", "top_gradient": "off",
                                         "bottom_gradient": "off", "rating_display_mode": "0",
                                         "badge_display_mode": "0", "hide_genre": "true",
                                         "hide_year": "true",
                                         "sash_priority": "trending,new_season", **params})
        meta = discovery.DiscoveryMeta(trending_rank=3, is_new_season=True)
        art = Image.new("RGBA", (500, 750), (120, 120, 120, 255))
        return np.asarray(main.build_poster(art, 80, "Drama", cfg, discovery_meta=meta).convert("RGB")).astype(int)

    def _dark(self, out, x0, x1):
        # Dark pixels in the top band between x0 and x1 (the art is mid-grey):
        # the ribbon's charcoal, or the notch's black whether centred or a chip.
        return int((out[5:60, x0:x1].max(axis=2) < 60).sum())

    def test_keep_draws_the_centred_notch(self):
        out = self._render()
        self.assertGreater(self._dark(out, 200, 300), 0)

    def test_hide_drops_the_notch_but_keeps_the_ribbon(self):
        out = self._render(trending_sash="hide")
        self.assertEqual(self._dark(out, 150, 500), 0)
        self.assertGreater(self._dark(out, 0, 120), 0)

    def test_opposite_sends_the_notch_to_the_free_corner(self):
        out = self._render(trending_sash="opposite")
        # The centre the notch left (the chip starts a little right of it).
        self.assertEqual(self._dark(out, 180, 260), 0)
        self.assertGreater(self._dark(out, 380, 500), 0)
        out = self._render(trending_sash="opposite", trending_side="right")
        self.assertGreater(self._dark(out, 0, 120), 0)      # the chip, now on the left
        self.assertGreater(self._dark(out, 400, 480), 0)    # the ribbon, on the right


if __name__ == "__main__":
    unittest.main()
