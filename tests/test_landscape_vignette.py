"""The landscape band takes the same vignette colour decisions as the portrait
bottom band, and the configurator can preview and emit the landscape shape."""
from pathlib import Path
import re
import unittest
from unittest import mock

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
        for gated in ("params.set('rating_display_mode', ratingMode)",
                      "params.set('badge_display_mode', badgeMode)"):
            self.assertIn(f"if (emitAll) {gated}", self.html)
        # The saved settings and a "{shape}" URL serve landscape, whose info
        # strip always prints the score, so they carry the weights even when
        # portrait hides its rating (and the rating block skips them).
        self.assertIn("if (!emitAll || ((full || dualShape) && ratingMode === 0)) emitWeights();", self.html)

    def test_portrait_only_tabs_hide_in_landscape(self):
        self.assertIn("const _PORTRAIT_ONLY_TABS = ['rating', 'logo', 'quality'];", self.html)
        for row in ("hide-genre-row", "textless-row"):
            self.assertIn(f'id="{row}"', self.html)
        self.assertIn('id="landscape-docked-rows"', self.html)
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

    def test_portrait_only_vignette_controls_are_wrapped(self):
        start = self.html.index('id="vignette-portrait-fields"')
        end = self.html.index("/vignette-portrait-fields")
        inside = self.html[start:end]
        for ctl in ("cfg-top-gradient", "cfg-bottom-gradient",
                    "tog-top-vignette-sash-only", "tog-vignette-color-top"):
            self.assertIn(ctl, inside)
        self.assertNotIn("tog-vignette-color-bottom", inside)


if __name__ == "__main__":
    unittest.main()
