"""The landscape band takes the same vignette colour decisions as the portrait
bottom band, and the configurator can preview and emit the landscape shape."""
from pathlib import Path
import re
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageChops

import main
import landscape


def _art() -> Image.Image:
    # Blue sky over a red field.  Blue carries slightly more of the frame, so
    # the whole-poster pick is blue, while the band's seam (around 0.60 h) sits
    # entirely in the red — the two candidates the local rule chooses between.
    im = Image.new("RGBA", (1000, 563), (40, 90, 200, 255))
    im.paste((190, 40, 30, 255), (0, 300, 1000, 563))
    return im


class LandscapeVignetteTests(unittest.TestCase):
    def _cfg(self, **over):
        cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=True)
        for k, v in over.items():
            setattr(cfg, k, v)
        return cfg

    def test_band_colour_goes_through_the_fog_pick(self):
        # Blend Into Nearby Art used to be ignored here: the band took the
        # whole-poster pick regardless.  It routes through the same pick as the
        # portrait bottom band, with the local flag as asked for.
        art = _art()
        with mock.patch.object(main, "_fog_pick", wraps=main._fog_pick) as spy:
            landscape._draw_vignette(art.copy(), art, self._cfg(vignette_color_local=False))
            self.assertEqual(spy.call_args.args[2], False)
            landscape._draw_vignette(art.copy(), art, self._cfg(vignette_color_local=True))
            self.assertEqual(spy.call_args.args[2], True)

    def test_local_changes_the_painted_band(self):
        art = _art()
        off = art.copy(); landscape._draw_vignette(off, art, self._cfg(vignette_color_local=False))
        on  = art.copy(); landscape._draw_vignette(on,  art, self._cfg(vignette_color_local=True))
        self.assertNotEqual(off.getpixel((500, 555)), on.getpixel((500, 555)))

    def test_tint_sampled_at_landscape_cell_counts(self):
        art = _art()
        with mock.patch.object(main, "_vignette_tint_band",
                               wraps=main._vignette_tint_band) as spy:
            landscape._draw_vignette(art.copy(), art, self._cfg())
        self.assertEqual(spy.call_args.kwargs["columns"], landscape._TINT_COLUMNS)
        self.assertEqual(spy.call_args.kwargs["ramp_columns"], landscape._RAMP_COLUMNS)

    def test_untinted_band_is_unchanged(self):
        art = _art()
        cfg = self._cfg(vignette_poster_color_bottom=False)
        out = art.copy(); landscape._draw_vignette(out, art, cfg)
        # Plain black band: the deepest row is a darkened red, not a tinted one.
        r, g, b, _ = out.getpixel((500, 562))
        self.assertGreater(r, g); self.assertGreater(r, b)


class BandRampTests(unittest.TestCase):
    def test_ramp_has_no_kink_at_the_top(self):
        # The onset has to be flat: the first rows inside the band must darken
        # by less than the rows further in, or the edge reads as a line.
        ramp = landscape._band_ramp(200).astype(int)
        self.assertEqual(ramp[0], 0)
        top_step = ramp[10] - ramp[0]
        mid_step = ramp[110] - ramp[100]
        self.assertLess(top_step, mid_step)
        self.assertEqual(ramp[-1], landscape._BAND_ALPHA)
        self.assertTrue(all(b >= a for a, b in zip(ramp, ramp[1:])))


class AnimeLandscapeArtTests(unittest.TestCase):
    def test_anime_landscape_takes_tmdb_backdrops(self):
        # AniList/Kitsu ship one cover and no backdrop, so a landscape render of
        # an anime id fell straight through to the genre canvas.  The TMDB
        # metadata fetched for the logo list carries both backdrops; the anime
        # branch has to hand them to the landscape short-circuit.
        src = Path("main.py").read_text(encoding="utf-8")
        block = re.search(
            r"if using_anime_art and _logo_meta is not None:\n(.*?)\n        elif use_cinemeta:",
            src, re.S)
        self.assertIsNotNone(block)
        self.assertIn('if rcfg.shape == "landscape":', block.group(1))
        self.assertIn("backdrop_path = _logo_meta[6]", block.group(1))
        self.assertIn('"text_backdrop_path": _logo_meta[7].get("text_backdrop_path")', block.group(1))


class LandscapeLogoAndBadgeTests(unittest.TestCase):
    def test_wide_logo_preferred_only_in_landscape(self):
        import tmdb
        stacked = {"file_path": "/a.png", "aspect_ratio": 1.2, "vote_average": 5.4}
        wide    = {"file_path": "/b.png", "aspect_ratio": 3.1, "vote_average": 5.0}
        wider   = {"file_path": "/c.png", "aspect_ratio": 6.0, "vote_average": 5.2}
        logos = [stacked, wide, wider]
        portrait = sorted(logos, key=tmdb._logo_rank_key(False), reverse=True)
        self.assertEqual(portrait[0], stacked)          # votes alone
        landscape_ = sorted(logos, key=tmdb._logo_rank_key(True), reverse=True)
        self.assertEqual([l["file_path"] for l in landscape_], ["/c.png", "/b.png", "/a.png"])

    def test_aspect_falls_back_to_dimensions(self):
        import tmdb
        self.assertAlmostEqual(tmdb._logo_aspect({"width": 400, "height": 100}), 4.0)
        self.assertEqual(tmdb._logo_aspect({}), 0.0)

    def test_render_path_asks_for_wide_logos_in_landscape(self):
        src = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("prefer_wide=_is_landscape,", src)

    def test_badge_scale_grows_the_pill(self):
        art = Image.new("RGBA", (1000, 563), (200, 200, 200, 255))
        with mock.patch.object(landscape, "_glass_pill", wraps=landscape._glass_pill) as spy:
            landscape._draw_badge(art.copy(), "OSCAR WINNER", "top_left", art,
                                  main.RequestConfig(shape="landscape"))
            x0, y0, x1, y1 = spy.call_args.args[1]
            landscape._draw_badge(art.copy(), "OSCAR WINNER", "top_left", art,
                                  main.RequestConfig(shape="landscape", landscape_badge_scale=1.5))
            X0, Y0, X1, Y1 = spy.call_args.args[1]
        self.assertAlmostEqual((X1 - X0) / (x1 - x0), 1.5, delta=0.08)
        self.assertAlmostEqual((Y1 - Y0) / (y1 - y0), 1.5, delta=0.08)
        cfg = main.build_request_config({"landscape_badge_scale": "1.5"})
        self.assertEqual(cfg.landscape_badge_scale, 1.5)
        self.assertEqual(main.build_request_config({"landscape_badge_scale": "9"}).landscape_badge_scale, 2.5)

    def test_info_scale_grows_the_strip(self):
        # The strip's ink grows with the scale, pinned to the right edge.
        def ink_bbox(scale, logo_right=300):
            art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
            landscape._draw_info_strip(art, "Drama", "2019", 87, scale=scale,
                                       logo_right=logo_right)
            diff = ImageChops.difference(art.convert("RGB"), Image.new("RGB", art.size, (20, 20, 20)))
            return diff.convert("L").point(lambda v: 255 if v > 60 else 0).getbbox()
        x0, y0, x1, y1 = ink_bbox(1.0)
        X0, Y0, X1, Y1 = ink_bbox(1.5)
        self.assertAlmostEqual((Y1 - Y0) / (y1 - y0), 1.5, delta=0.15)
        self.assertLess(X0, x0)
        self.assertAlmostEqual(X1, x1, delta=3)
        # Beside a narrow logo the enlarged strip keeps its genre; beside one
        # that really is wide it still sheds it rather than colliding.
        shed = ink_bbox(1.5, logo_right=560)[0]
        self.assertGreater(shed, 560)
        self.assertLess(X0, shed - 100)             # the genre is the difference
        cfg = main.build_request_config({"landscape_info_scale": "1.5"})
        self.assertEqual(cfg.landscape_info_scale, 1.5)
        self.assertEqual(main.build_request_config({"landscape_info_scale": "9"}).landscape_info_scale, 2.0)

    def test_logo_position_moves_logo_and_strip(self):
        # Left puts the strip on the right; right mirrors it; centre stacks the
        # logo on a centred strip.  Only the drawn pixels are compared.
        logo = Image.new("RGBA", (400, 100), (255, 255, 255, 255))

        def ink(pos):
            art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
            cfg = main.RequestConfig(shape="landscape", landscape_logo_pos=pos,
                                     vignette_poster_color_bottom=False, sash_mode="hidden")
            out = landscape.build_landscape(art, 87, "Drama", cfg, logo=logo, release_year="2019")
            m = np.asarray(out.convert("L")) > 200
            cols = np.flatnonzero(m.any(axis=0))
            rows = np.flatnonzero(m.any(axis=1))
            return cols[0], cols[-1], rows[0], rows[-1]
        l0, l1, _, _ = ink("left")
        r0, r1, _, _ = ink("right")
        c0, c1, ct, cb = ink("center")
        self.assertLess(l0, 80)
        self.assertGreater(r1, 920)
        self.assertLess(abs((c0 + c1) / 2 - 500), 20)
        # Logo is stacked above the centred line, so its ink reaches higher.
        self.assertLess(ct, ink("left")[2])
        self.assertEqual(main.build_request_config({"landscape_logo_pos": "right"}).landscape_logo_pos, "right")
        self.assertEqual(main.build_request_config({"landscape_logo_pos": "top_center"}).landscape_logo_pos,
                         "top_center")
        # Top row: the logo hangs near the top, the strip keeps the bottom row
        # on the logo's side.
        t0, t1, tt, tb = ink("top_right")
        self.assertGreater(t1, 920)
        self.assertLess(tt, 60)
        self.assertGreater(tb, 480)
        self.assertEqual(main.build_request_config({"landscape_logo_pos": "up"}).landscape_logo_pos, "left")

    def test_info_line_placement(self):
        # The line alone on a plain canvas (no logo): its ink goes where asked.
        def ink(**over):
            art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
            cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=False,
                                     sash_mode="hidden", **over)
            out = landscape.build_landscape(art, 87, "Drama", cfg, release_year="2019")
            m = np.asarray(out.convert("L")) > 120
            cols, rows = np.flatnonzero(m.any(axis=0)), np.flatnonzero(m.any(axis=1))
            return cols[0], cols[-1], rows[0], rows[-1]
        l0, _, t0, _ = ink(landscape_info_pos="top_left")
        self.assertLess(l0, 80)
        self.assertLess(t0, 80)
        _, r1, _, b1 = ink(landscape_info_pos="bottom_right")
        self.assertGreater(r1, 920)
        self.assertGreater(b1, 480)
        c0, c1, _, _ = ink(landscape_info_pos="bottom_center")
        self.assertLess(abs((c0 + c1) / 2 - 500), 20)
        # Auto, with no logo or title drawn (original art): the whole row is
        # the line's, so a long genre isn't dropped to clear a missing logo.
        art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
        cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=False,
                                 sash_mode="hidden", landscape_info_scale=1.65)
        with mock.patch.object(landscape, "_draw_info_strip", wraps=landscape._draw_info_strip) as spy:
            out = landscape.build_landscape(art, 87, "Sci-Fi", cfg, release_year="2019")
        self.assertEqual(spy.call_args.kwargs["bounds"], (1000 * landscape._SIDE_PAD,
                                                          1000 * (1 - landscape._RIGHT_PAD)))
        cols = np.flatnonzero((np.asarray(out.convert("L")) > 120).any(axis=0))
        self.assertLess(cols[0], 500)       # the genre made it in
        self.assertEqual(main.build_request_config({"landscape_info_pos": "top_center"}).landscape_info_pos,
                         "top_center")
        self.assertEqual(main.build_request_config({"landscape_info_pos": "middle"}).landscape_info_pos, "auto")

    def test_badge_moves_off_what_it_would_cover(self):
        # A bottom-corner badge over the info line climbs above it.
        art = Image.new("RGBA", (1000, 563), (20, 20, 20, 255))
        cfg = main.RequestConfig(shape="landscape", landscape_badge_pos="bottom_right")
        with mock.patch.object(landscape, "_glass_pill", wraps=landscape._glass_pill) as spy:
            landscape._draw_badge(art.copy(), "OSCAR WINNER", "bottom_right", art, cfg,
                                  obstacles=((700, 480, 955, 520),))
            x0, y0, x1, y1 = spy.call_args.args[1]
        self.assertGreater(x1, 900)
        self.assertLessEqual(y1, 480)
        self.assertEqual(main.build_request_config({"badge_pos": "bottom_left"}).landscape_badge_pos,
                         "bottom_left")

    def test_graphic_badges_are_opt_in(self):
        # badge_display_mode is the portrait's; landscape has its own switch,
        # and a "{shape}" URL's portrait mode must not turn it on.
        cfg = main.build_request_config({"shape": "landscape", "badge_display_mode": "7"})
        self.assertFalse(cfg.landscape_graphic_badges)
        self.assertFalse(main._uses_quality(cfg))
        cfg = main.build_request_config({"shape": "landscape", "landscape_badge_display_mode": "7",
                                         "badge_group1": "bl:2:cert",
                                         "landscape_badge_group1": "tr:4:video,res"})
        self.assertTrue(cfg.landscape_graphic_badges)
        self.assertEqual(cfg.badge_group1, "tr:4:video,res")
        self.assertTrue(main._uses_quality(cfg))
        # Portrait never reads the landscape twins.
        self.assertEqual(main.build_request_config({"landscape_badge_group1": "tr:4:res"}).badge_group1,
                         main.RequestConfig().badge_group1)

    def test_top_band_only_when_asked(self):
        art = Image.new("RGBA", (1000, 563), (60, 140, 220, 255))

        def top_mean(params):
            cfg = main.build_request_config({"shape": "landscape", "sash_mode": "hidden", **params})
            out = landscape.build_landscape(art.copy(), 87, "Drama", cfg)
            return np.asarray(out.convert("RGB"))[:20].mean()
        off = top_mean({})
        self.assertLess(top_mean({"landscape_top_gradient": "high"}), off - 20)
        # The pre-levels toggle is the tinted "high" band.
        self.assertEqual(top_mean({"landscape_vignette_top": "true"}),
                         top_mean({"landscape_top_gradient": "high"}))
        # New settings stay out of the cache key at their defaults.
        base = main._render_config_signature(main.build_request_config({}))
        self.assertEqual(base, main._render_config_signature(main.build_request_config(
            {"landscape_logo_pos": "left"})))

    def test_landscape_reads_only_its_own_levels(self):
        # Landscape URLs written before the levels carry the portrait's under
        # the plain names; those must not reach the landscape bands.
        cfg = main.build_request_config({"shape": "landscape", "top_gradient": "medium",
                                         "bottom_gradient": "low", "vignette_poster_color_top": "false",
                                         "top_vignette_sash_only": "true"})
        self.assertEqual((cfg.top_gradient, cfg.bottom_gradient), ("off", "high"))
        self.assertTrue(cfg.vignette_poster_color_top)
        self.assertFalse(cfg.top_vignette_sash_only)
        cfg = main.build_request_config({"shape": "landscape", "top_gradient": "medium",
                                         "landscape_top_gradient": "custom",
                                         "landscape_top_gradient_height": "0.4",
                                         "landscape_top_gradient_opacity": "0.5",
                                         "landscape_bottom_gradient": "off"})
        self.assertEqual((cfg.top_gradient, cfg.top_gradient_height, cfg.top_gradient_opacity),
                         ("custom", 0.4, 0.5))
        self.assertEqual(cfg.bottom_gradient, "off")
        # And portrait never reads the landscape ones.
        self.assertEqual(main.build_request_config({"landscape_top_gradient": "off"}).top_gradient,
                         main.RequestConfig().top_gradient)

    def test_bands_follow_levels_and_tint_toggles(self):
        art = _art()

        def drawn(**over):
            cfg = main.RequestConfig(shape="landscape", **{"top_gradient": "off", **over})
            out = art.copy(); landscape._draw_vignette(out, art, cfg)
            return np.asarray(out.convert("RGB")).astype(int)
        src = np.asarray(art.convert("RGB")).astype(int)
        # Bottom off: nothing drawn at all.
        self.assertTrue((drawn(bottom_gradient="off") == src).all())
        # Lower levels are shallower and lighter.
        high, low = drawn(bottom_gradient="high"), drawn(bottom_gradient="low")
        self.assertLess(high[-1].sum(), low[-1].sum())
        self.assertTrue((low[int(563 * 0.6)] == src[int(563 * 0.6)]).all())
        # A black top band, now the top need not be tinted.
        top = drawn(top_gradient="high", vignette_poster_color_top=False, bottom_gradient="off")
        r, g, b = top[0, 500]
        self.assertLess(b, src[0, 500, 2]); self.assertGreater(b, r)

    def test_top_band_waits_for_sash_when_asked(self):
        art = _art()
        cfg = main.RequestConfig(shape="landscape", top_gradient="high", bottom_gradient="off",
                                 top_vignette_sash_only=True)
        out = art.copy(); landscape._draw_vignette(out, art, cfg, sash_shown=False)
        self.assertEqual(out.getpixel((500, 0)), art.getpixel((500, 0)))
        out = art.copy(); landscape._draw_vignette(out, art, cfg, sash_shown=True)
        self.assertNotEqual(out.getpixel((500, 0)), art.getpixel((500, 0)))

    def test_info_strip_score_can_read_out_of_10(self):
        # Same formatting as portrait's out-of-10 switches: one decimal, and a
        # bare "10" at the top.  Off by default.
        def drawn(score, out_of_10):
            texts = []
            real_draw = landscape.ImageDraw.Draw

            def spy(im):
                d = real_draw(im)
                real_text = d.text
                d.text = lambda xy, text, *a, **k: (texts.append(text), real_text(xy, text, *a, **k))[1]
                return d

            with mock.patch.object(landscape.ImageDraw, "Draw", spy):
                landscape._draw_info_strip(Image.new("RGBA", (1000, 563)), "", None,
                                           score, out_of_10=out_of_10)
            return texts

        self.assertIn("87", drawn(87, False))
        self.assertIn("8.7", drawn(87, True))
        self.assertIn("8.0", drawn("80", True))
        self.assertIn("10", drawn(100, True))
        self.assertEqual(drawn("N/A", True), [])
        self.assertFalse(main.build_request_config({}).landscape_score_out_of_10)
        cfg = main.build_request_config({"landscape_score_out_of_10": "true"})
        self.assertTrue(cfg.landscape_score_out_of_10)

    def test_info_strip_star_labels_the_score(self):
        # The star takes the separator's place in front of the score, as
        # Clean has it on a portrait; a lone score gets one of its own, and
        # without a score there is nothing for it to label.
        def drawn(genre, year, score, sep="star", field_sep="bullet"):
            texts = []
            real_draw = landscape.ImageDraw.Draw

            def spy(im, *a, **k):
                d = real_draw(im, *a, **k)
                real_text = d.text
                d.text = lambda xy, text, *a, **k: (texts.append(text), real_text(xy, text, *a, **k))[1]
                return d

            with mock.patch.object(landscape.ImageDraw, "Draw", spy):
                landscape._draw_info_strip(Image.new("RGBA", (1000, 563)), genre, year,
                                           score, rating_sep=sep, field_sep=field_sep)
            return list(reversed(texts))

        self.assertEqual(drawn("Drama", "2024", 87), ["Drama", "  •  ", "2024", "  ★ ", "87"])
        self.assertEqual(drawn("", None, 87), ["★ ", "87"])
        self.assertEqual(drawn("Drama", "2024", "N/A"), ["Drama", "  •  ", "2024"])
        self.assertNotIn("  ★ ", drawn("Drama", "2024", 87, sep="bullet"))
        # The bar is drawn rather than typed, so only the bullet before the
        # year is text; it takes room between the year and the score.
        self.assertEqual(drawn("Drama", "2024", 87, sep="pip"), ["Drama", "  •  ", "2024", "87"])
        bullet = landscape._draw_info_strip(Image.new("RGBA", (1000, 563)), "Drama", "2024", 87)
        piped = landscape._draw_info_strip(Image.new("RGBA", (1000, 563)), "Drama", "2024", 87,
                                           rating_sep="pip")
        self.assertNotEqual(bullet[0], piped[0])
        # The field separator is its own: a bar between genre and year, and
        # the rating separator's glyph still in front of the score.
        self.assertEqual(drawn("Drama", "2024", 87, field_sep="pip"), ["Drama", "2024", "  ★ ", "87"])
        self.assertEqual(drawn("Drama", "2024", 87, sep="bullet", field_sep="pip"),
                         ["Drama", "2024", "  •  ", "87"])
        self.assertEqual(main.build_request_config({}).landscape_separator, "bullet")
        self.assertEqual(main.build_request_config({"landscape_separator": "pip"}).landscape_separator, "pip")
        self.assertNotIn("landscape_separator", main._render_config_signature(main.build_request_config({})))
        self.assertEqual(main.build_request_config({}).landscape_rating_separator, "bullet")
        self.assertEqual(main.build_request_config(
            {"landscape_score_star": "true"}).landscape_rating_separator, "star")
        self.assertEqual(main.build_request_config(
            {"landscape_rating_separator": "pip"}).landscape_rating_separator, "pip")

    def test_rating_separator_keeps_old_star_keys(self):
        # Bullet and star key as the switch they replace did; only the bar is new.
        sig = main._render_config_signature
        plain = sig(main.build_request_config({}))
        self.assertIn('"landscape_score_star": false', plain)
        self.assertNotIn("landscape_rating_separator", plain)
        star = sig(main.build_request_config({"landscape_score_star": "true"}))
        self.assertIn('"landscape_score_star": true', star)
        self.assertIn('"landscape_rating_separator": "pip"',
                      sig(main.build_request_config({"landscape_rating_separator": "pip"})))

    def test_badge_shadow_clips_at_the_canvas_edge(self):
        # A top-left pill's shadow spills past x=0 / y=0; it must be clipped
        # rather than refused, and it must darken the art around the pill.
        art = Image.new("RGBA", (1000, 563), (200, 200, 200, 255))
        cfg = main.RequestConfig(shape="landscape")
        before = art.getpixel((60, 118))
        landscape._draw_badge(art, "OSCAR WINNER", "top_left", art.copy(), cfg)
        after = art.getpixel((60, 118))                # just under the pill
        self.assertLess(sum(after[:3]), sum(before[:3]))
        # And a pill hard against the corner does not raise.
        edge = Image.new("RGBA", (1000, 563), (200, 200, 200, 255))
        mask = Image.new("L", (80, 30), 255)
        landscape._drop_shadow(edge, mask, -10, -10, 8.0, 150)


class ColourLinkTests(unittest.TestCase):
    def test_band_reports_its_tint_and_takes_a_source(self):
        art = _art()
        cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=True)
        painted = landscape._draw_vignette(art.copy(), art, cfg)
        self.assertIsNotNone(painted)
        forced = landscape._draw_vignette(art.copy(), art, cfg, source=(200.0, 40.0, 30.0))
        self.assertEqual(forced, (200.0, 40.0, 30.0))
        plain = landscape._draw_vignette(art.copy(), art, main.RequestConfig(shape="landscape"))
        self.assertIsNone(plain)

    def test_badge_follows_vignette_hands_the_band_colour_to_the_pill(self):
        art = _art()
        cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=True,
                                 landscape_color_link="badge_follows_vignette")
        with mock.patch.object(landscape, "_draw_badge", wraps=landscape._draw_badge) as spy, \
             mock.patch("main.pick_sash", return_value=("Oscar Winner", "win")):
            landscape.build_landscape(art.copy(), 80, "Drama", cfg,
                                      discovery_meta=object(), fallback_title="T")
        self.assertIsNotNone(spy.call_args.kwargs["source"])

    def test_vignette_follows_badge_shares_the_frame_colour(self):
        art = _art()
        cfg = main.RequestConfig(shape="landscape", vignette_poster_color_bottom=True,
                                 landscape_color_link="vignette_follows_badge")
        with mock.patch.object(landscape, "_draw_vignette", wraps=landscape._draw_vignette) as spy:
            landscape.build_landscape(art.copy(), 80, "Drama", cfg)
        self.assertIsNotNone(spy.call_args.kwargs["source"])

    def test_landscape_defaults_seed_a_bare_request(self):
        # A bare shape=landscape URL renders the look the layout was designed
        # around; an explicit parameter still wins; portrait is untouched.
        cfg = main.build_request_config({"shape": "landscape"})
        self.assertTrue(cfg.vignette_poster_color_bottom)
        self.assertFalse(cfg.vignette_color_local)
        self.assertEqual(cfg.vignette_color_saturation, 2.0)
        self.assertEqual(cfg.landscape_color_link, "badge_follows_vignette")
        cfg = main.build_request_config({"shape": "landscape",
                                         "vignette_poster_color_bottom": "false",
                                         "vignette_color_local": "true"})
        self.assertFalse(cfg.vignette_poster_color_bottom)
        self.assertTrue(cfg.vignette_color_local)
        cfg = main.build_request_config({})
        self.assertFalse(cfg.vignette_poster_color_bottom)
        self.assertTrue(cfg.vignette_color_local)
        self.assertEqual(cfg.landscape_color_link, "off")

    def test_landscape_defaults_are_published(self):
        ls = main._render_param_defaults("landscape")
        pt = main._render_param_defaults()
        self.assertTrue(ls["vignette_poster_color_bottom"])
        self.assertFalse(pt["vignette_poster_color_bottom"])
        self.assertEqual(ls["landscape_color_link"], "badge_follows_vignette")
        # The configurator must judge each URL against its own shape's set.
        html = Path("configurator.html").read_text(encoding="utf-8")
        self.assertIn("param_defaults_landscape", html)

    def test_link_is_parsed(self):
        cfg = main.build_request_config({"landscape_color_link": "vignette_follows_badge"})
        self.assertEqual(cfg.landscape_color_link, "vignette_follows_badge")
        cfg = main.build_request_config({"landscape_color_link": "nonsense"})
        self.assertEqual(cfg.landscape_color_link, "off")


class ConfiguratorLandscapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_toggle_sits_left_of_the_title_link_menu(self):
        shape = self.html.index('id="shape-toggle"')
        links = self.html.index('id="external-link"')
        self.assertLess(shape, links)
        self.assertIn('id="tog-landscape"', self.html)

    def test_build_emits_the_landscape_trio_whenever_the_shape_is(self):
        # The trio rides with the shape, and there are two ways to emit one now:
        # the landscape view, and a "{shape}" client whose single URL is
        # resolved for both layouts however the preview is pointing.
        # The saved settings (full) carry it too, or a reload from the portrait
        # view resets the landscape sizes.
        self.assertIn("const emitLandscape = landscape || dualShape;", self.html)
        block = re.search(r"if \(emitLandscape \|\| full\) \{(.*?)\n  \}", self.html, re.S)
        self.assertIsNotNone(block)
        for param in ("'landscape_art'", "'badge_pos'", "'landscape_badge_scale'",
                      "'landscape_info_scale'"):
            self.assertIn(param, block.group(1))
        self.assertIn("if (dualShape) params.set('shape', '{shape}');", self.html)
        self.assertIn("else if (landscape) params.set('shape', 'landscape');", self.html)

    def test_import_round_trips_and_presets_follow_their_shape(self):
        # A preset carries its shape (landscape ones say shape=landscape) and
        # the preview follows it, so loadPreset must not pin the current one.
        self.assertNotRegex(self.html, r"loadPreset[\s\S]*?preserveShape: true[\s\S]*?closePresetModal")
        self.assertIn("_setEl('tog-landscape', (p.get('shape') || '').toLowerCase() === 'landscape')", self.html)
        # Neither choice waits on the published defaults: an absent param is
        # the default, whether or not /server-caps has answered yet.
        self.assertRegex(self.html, r"_setEl\('cfg-landscape-art',\s*p\.get\('landscape_art'\)\s*\|\| 'textless'\)")
        self.assertRegex(self.html, r"_setEl\('cfg-landscape-badge-pos',\s*p\.get\('badge_pos'\)\s*\|\| 'top_left'\)")
        self.assertRegex(self.html, r"_setEl\('cfg-landscape-color-link',\s*p\.get\('landscape_color_link'\)\s*\|\| 'badge_follows_vignette'\)")

    def test_landscape_default_on_switches_are_written_both_ways(self):
        # A switch whose landscape default is ON must write its off state:
        # an absent parameter is the server's default, which would turn the
        # option back on.  Same rule as test_configurator_default_on_toggles,
        # for the defaults that only apply to shape=landscape.
        for name, value in main._LANDSCAPE_DEFAULTS.items():
            if value is not True:
                continue
            with self.subTest(param=name):
                match = re.search(rf"\bset\(\s*'{name}',([^\n]*)", self.html)
                self.assertIsNotNone(match, f"{name} is never written by build()")
                self.assertIn("'false'", match.group(1),
                              f"{name} must be written as false when its switch is off")

    def test_landscape_url_is_filtered_but_the_save_is_not(self):
        # A landscape URL carries only what landscape.py reads; the persisted
        # settings carry everything, or a reload in landscape view would lose
        # the portrait configuration.  A "{shape}" URL is both layouts at once,
        # so it is not filtered either.
        self.assertIn("const emitAll    = full || !landscape || dualShape;", self.html)
        self.assertIn("buildBaseParams({ usePlaceholders: true, full: true })", self.html)
        self.assertIn("if (emitAll) params.set('rating_display_mode', ratingMode)", self.html)
        self.assertIn("if (emitAll) params.set('badge_display_mode', badgeMode)", self.html)
        # Landscape's own badge mode, opt-in, under its own name.
        self.assertIn("params.set('landscape_badge_display_mode', '7')", self.html)
        # The saved settings and a "{shape}" URL serve landscape, whose info
        # strip always prints the score, so they carry the weights even when
        # portrait hides its rating (and the rating block skips them).
        self.assertIn("if (!emitAll || ((full || dualShape) && ratingMode === 0)) emitWeights();", self.html)

    def test_landscape_controls_live_in_their_tabs(self):
        # No tab is hidden in landscape: each shape's own controls are marked
        # and follow the body class, landscape's in the tabs portrait uses.
        self.assertNotIn("_PORTRAIT_ONLY_TABS", self.html)
        self.assertIn("body.shape-landscape .portrait-only { display: none !important; }", self.html)
        self.assertIn("document.body.classList.toggle('shape-landscape', landscape);", self.html)
        def tab(name):
            start = self.html.index(f'<div class="section" data-tab="{name}">')
            return self.html[start:self.html.index('<div class="section" data-tab=', start + 10)]
        for control in ('id="cfg-landscape-art"', 'id="cfg-landscape-logo-pos"', 'id="textless-row"'):
            self.assertIn(control, tab("logo"))
        for control in ('id="cfg-landscape-info-scale"', 'id="tog-landscape-score-10"', 'id="hide-genre-row"'):
            self.assertIn(control, tab("rating"))
        # Core no longer carries a Landscape group of its own.
        self.assertNotIn('id="landscape-fields"', self.html)
        self.assertNotIn('id="cfg-landscape-info-scale"', tab("core"))
        self.assertIn('id="cfg-landscape-color-link"', self.html)

    def test_landscape_presets_carry_their_shape(self):
        # Three landscape presets, each marked so the gallery groups them and
        # loadPreset scopes the import to what landscape reads.
        block = re.search(r"const PRESETS = \[(.*?)\n\];", self.html, re.S).group(1)
        entries = re.findall(r"\{\s*id: '([^']+)',(.*?)\n  \}", block, re.S)
        landscape_ids = [i for i, body in entries if "shape: 'landscape'" in body]
        self.assertEqual(landscape_ids, ["landscape_tinted", "landscape_dark", "landscape_original"])
        for i, body in entries:
            with self.subTest(preset=i):
                self.assertEqual("shape=landscape" in body, i in landscape_ids)
        self.assertIn("_portraitOnlyControlIds()", self.html)
        self.assertIn("const _LANDSCAPE_ONLY_CONTROLS", self.html)

    def test_vignette_levels_are_kept_per_shape(self):
        # Landscape mirrors the portrait's vignette controls: no longer hidden
        # behind a portrait-only wrapper, and each shape keeps its own values.
        self.assertNotIn("vignette-portrait-fields", self.html)
        self.assertNotIn("tog-landscape-vignette-top", self.html)
        block = self.html[self.html.index("const _SHARED_PARAM = {"):self.html.index("const _SHARED_NO_FALLBACK")]
        for ctl in ("cfg-top-gradient", "cfg-bottom-gradient", "cfg-tg-op", "cfg-bg-ht",
                    "tog-top-vignette-sash-only", "tog-vignette-color-top"):
            self.assertIn(f"'{ctl}'", block)


class LandscapeBadgeSettingsTests(unittest.TestCase):
    """Portrait's Notch Settings, as the landscape pill's Badge Settings."""

    def _pill(self, **params):
        # The pill's box: where a plain render and one with the badge differ.
        art = Image.new("RGBA", (1000, 563), (60, 140, 220, 255))
        cfg = main.build_request_config({"shape": "landscape", **params})
        out = art.copy()
        landscape._draw_badge(out, "NEW", cfg.landscape_badge_pos, art, cfg)
        return ImageChops.difference(out.convert("RGB"), art.convert("RGB")).point(
            lambda v: 255 if v > 40 else 0).getbbox()

    def test_defaults_render_as_before(self):
        cfg = main.RequestConfig()
        self.assertEqual(cfg.landscape_badge_glass_opacity, landscape._LIFT_OPACITY)
        self.assertEqual(cfg.landscape_badge_opacity, landscape._DARK_OPACITY)
        scale, th, pad_x, pad_y, bh = landscape._badge_metrics(cfg, 563)
        self.assertEqual((th, pad_x, pad_y), (int(563 * landscape._BADGE_FONT),
                                              landscape._BADGE_PAD_X, landscape._BADGE_PAD_Y))
        # And they stay out of the cache key.
        self.assertEqual(main._render_config_signature(main.build_request_config({})),
                         main._render_config_signature(main.build_request_config(
                             {"landscape_badge_width": "1.0", "landscape_badge_y": "0"})))

    def test_width_and_height_resize_the_pill(self):
        base = self._pill()
        wide = self._pill(landscape_badge_width="2")
        tall = self._pill(landscape_badge_height="1.5")
        self.assertGreater(wide[2] - wide[0], base[2] - base[0])
        self.assertGreater(tall[3] - tall[1], base[3] - base[1])
        self.assertAlmostEqual(tall[2] - tall[0], base[2] - base[0], delta=2)
        # A bigger label grows the pill instead of eating its padding.
        cfg = main.build_request_config({"shape": "landscape", "landscape_badge_font": "1.4"})
        _, th, _, pad_y, bh = landscape._badge_metrics(cfg, 563)
        self.assertEqual(pad_y, landscape._BADGE_PAD_Y)
        self.assertGreater(bh, landscape._badge_metrics(main.RequestConfig(), 563)[4])

    def test_position_moves_it_in_from_its_corner(self):
        base = self._pill()
        moved = self._pill(landscape_badge_x="0.1", landscape_badge_y="0.1")
        self.assertAlmostEqual(moved[0] - base[0], 100, delta=2)
        self.assertAlmostEqual(moved[1] - base[1], 56, delta=2)
        right = self._pill(badge_pos="top_right")
        right_moved = self._pill(badge_pos="top_right", landscape_badge_x="0.1")
        self.assertAlmostEqual(right[0] - right_moved[0], 100, delta=2)
        bottom = self._pill(badge_pos="bottom_left")
        bottom_moved = self._pill(badge_pos="bottom_left", landscape_badge_y="0.1")
        self.assertAlmostEqual(bottom[1] - bottom_moved[1], 56, delta=2)

    def test_opacity_and_saturation(self):
        art = Image.new("RGBA", (1000, 563), (200, 60, 40, 255))

        def centre(**params):
            cfg = main.build_request_config({"shape": "landscape", **params})
            out = art.copy()
            landscape._draw_badge(out, "N", "top_left", art, cfg)
            return out.getpixel((int(1000 * landscape._SIDE_PAD) + 8, int(563 * landscape._BADGE_TOP) + 20))[:3]
        clear = centre(landscape_badge_glass_opacity="0")
        self.assertNotEqual(centre(), clear)
        grey = centre(landscape_badge_saturation="0")
        self.assertLess(max(grey) - min(grey), max(centre()) - min(centre()))
        self.assertNotEqual(centre(landscape_badge_style="black", landscape_badge_opacity="0.3"),
                            centre(landscape_badge_style="black"))

    def test_chip_shape_rounds_only_its_corners(self):
        import ratings

        def radii(**params):
            with mock.patch.object(ratings, "_cairo_pill_mask", wraps=ratings._cairo_pill_mask) as spy:
                self._pill(**params)
            return [(c.args[1], c.args[2]) for c in spy.call_args_list]
        for style in ("glass", "black", "gold"):
            self.assertTrue(radii(landscape_badge_style=style))
            for h, r in radii(landscape_badge_style=style):
                self.assertEqual(r, h // 2)
            for h, r in radii(landscape_badge_style=style, landscape_badge_shape="chip"):
                self.assertEqual(r, round(h * 0.30))

    def test_label_case_follows_the_shape_unless_set(self):
        def caps(**params):
            return landscape._badge_caps(main.build_request_config({"shape": "landscape", **params}))
        self.assertTrue(caps())
        self.assertFalse(caps(landscape_badge_shape="chip"))
        self.assertTrue(caps(landscape_badge_shape="chip", landscape_badge_case="upper"))
        self.assertFalse(caps(landscape_badge_case="mixed"))
        self.assertTrue(caps(landscape_badge_shape="oval", landscape_badge_case="lower"))

    def test_ranges_are_clamped(self):
        cfg = main.build_request_config({"landscape_badge_width": "99", "landscape_badge_x": "-5"})
        self.assertEqual((cfg.landscape_badge_width, cfg.landscape_badge_x), (2.0, -0.05))


if __name__ == "__main__":
    unittest.main()
