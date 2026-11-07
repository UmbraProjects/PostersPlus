"""art_gain, bottom_gradient_curve, logo_punch, logo_shadow, logo_shadow_radius:
off by default (the render and the cache key are what they were), and each
does what it says when set."""
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import main
import param_aliases
import tmdb

ROOT = Path(__file__).resolve().parent.parent
NEW = ("art_gain", "bottom_gradient_curve", "logo_punch", "logo_shadow", "logo_shadow_radius")


def _cfg(**over):
    """A portrait config with only the logo and the bottom band drawn."""
    base = dict(sash_mode="hidden", rating_display_mode=0, badge_display_mode=0,
                top_gradient="off", bottom_gradient="off", hide_genre=True)
    base.update(over)
    return main.RequestConfig(**base)


def _render(cfg, art_value=180, logo=True, art=None):
    if art is None:
        art = Image.new("RGBA", (500, 750), (art_value, art_value, art_value, 255))
    lg = Image.new("RGBA", (300, 90), (200, 60, 60, 255)) if logo else None
    return np.asarray(main.build_poster(art, 80, "Drama", cfg, logo=lg).convert("RGB")).astype(int)


def _coloured_art():
    """Art with real colour in it, for the tinted band to pick from."""
    y = np.linspace(0, 1, 750, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, 500, dtype=np.float32)[None, :]
    rgb = np.stack([40 + 60 * x + 0 * y, 90 + 80 * y + 0 * x, 160 + 60 * (1 - y) + 0 * x], axis=2)
    return Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")


class ParsingTests(unittest.TestCase):
    def test_defaults(self):
        cfg = main.build_request_config({})
        self.assertEqual((cfg.art_gain, cfg.bottom_gradient_curve, cfg.logo_punch,
                          cfg.logo_shadow, cfg.logo_shadow_radius), (1.0, 0.0, 0.0, 0.0, 0.12))

    def test_values_are_read_and_clamped(self):
        cfg = main.build_request_config({"art_gain": "0.93", "bottom_gradient_curve": "0.8",
                                         "logo_punch": "0.5", "logo_shadow": "0.7",
                                         "logo_shadow_radius": "0.3"})
        self.assertEqual((cfg.art_gain, cfg.bottom_gradient_curve, cfg.logo_punch,
                          cfg.logo_shadow, cfg.logo_shadow_radius), (0.93, 0.8, 0.5, 0.7, 0.3))
        cfg = main.build_request_config({"art_gain": "0.1", "bottom_gradient_curve": "99",
                                         "logo_punch": "-1", "logo_shadow": "7",
                                         "logo_shadow_radius": "9"})
        self.assertEqual((cfg.art_gain, cfg.bottom_gradient_curve, cfg.logo_punch,
                          cfg.logo_shadow, cfg.logo_shadow_radius), (0.5, 4.0, 0.0, 1.0, 0.5))
        # A near-zero curve is a flat block, not "the default shape": raised to 0.2.
        self.assertEqual(main.build_request_config({"bottom_gradient_curve": "0.01"}).bottom_gradient_curve, 0.2)
        for bad in ("nan", "x", ""):
            self.assertEqual(main.build_request_config({"art_gain": bad}).art_gain, 1.0)

    def test_short_names(self):
        expand = lambda q: dict(param_aliases.expand(q.items()))
        self.assertEqual(expand({"ag": "0.93", "bgc": "0.8", "lpu": "0.5", "lsh": "0.7", "lshr": "0.3"}),
                         {"art_gain": "0.93", "bottom_gradient_curve": "0.8", "logo_punch": "0.5",
                          "logo_shadow": "0.7", "logo_shadow_radius": "0.3"})

    def test_defaults_leave_the_cache_key_alone(self):
        sig = main._render_config_signature(main.build_request_config({}))
        for name in NEW:
            self.assertNotIn(name, sig)
        sig = main._render_config_signature(main.build_request_config({"logo_shadow": "0.7"}))
        self.assertIn("logo_shadow", sig)


class DefaultsRenderAsBeforeTests(unittest.TestCase):
    def test_art_gain_one_returns_the_image_itself(self):
        img = Image.new("RGBA", (4, 4), (10, 20, 30, 40))
        self.assertIs(main._apply_art_gain(img, 1.0), img)

    def test_curve_zero_is_the_black_band_s_own_shape(self):
        # The black band's own ease-out is the exponent 1.5, so 0 and 1.5 draw the same.
        band = dict(bottom_gradient="custom", bottom_gradient_opacity=1.0, bottom_gradient_height=0.4,
                    vignette_poster_color_bottom=False)
        np.testing.assert_array_equal(_render(_cfg(**band), logo=False),
                                      _render(_cfg(bottom_gradient_curve=1.5, **band), logo=False))

    def test_zero_punch_and_shadow_draw_the_logo_as_before(self):
        art = Image.new("RGBA", (500, 750), (180, 180, 180, 255))
        logo = Image.new("RGBA", (300, 90), (200, 60, 60, 255))
        plain, finished = art.copy(), art.copy()
        tmdb.composite_logo(plain, logo.copy())
        tmdb.composite_logo(finished, logo.copy(), punch=0.0, shadow=0.0, shadow_radius=0.3)
        self.assertEqual(plain.tobytes(), finished.tobytes())


class EffectTests(unittest.TestCase):
    def test_art_gain_scales_colour_not_alpha(self):
        img = Image.new("RGBA", (2, 2), (200, 100, 50, 77))
        self.assertEqual(main._apply_art_gain(img, 0.5).getpixel((0, 0)), (100, 50, 25, 77))
        self.assertEqual(main._apply_art_gain(img.convert("RGB"), 0.5).getpixel((0, 0)), (100, 50, 25))

    def test_art_gain_darkens_the_art(self):
        plain, dark = _render(_cfg(), logo=False), _render(_cfg(art_gain=0.9), logo=False)
        self.assertAlmostEqual(dark[10, 10, 0] / plain[10, 10, 0], 0.9, places=2)

    def test_a_low_curve_keeps_more_of_the_art_mid_band(self):
        band = dict(bottom_gradient="custom", bottom_gradient_opacity=1.0, bottom_gradient_height=0.4,
                    vignette_poster_color_bottom=False)
        stock = _render(_cfg(**band), logo=False)
        gentle = _render(_cfg(bottom_gradient_curve=0.8, **band), logo=False)
        mid = 750 - int(750 * 0.4) // 2          # halfway down the band
        self.assertGreater(gentle[mid, 250, 0], stock[mid, 250, 0] + 10)
        self.assertEqual(gentle[-1, 250, 0], stock[-1, 250, 0])   # same depth at the edge

    def test_the_curve_also_shapes_a_tinted_band(self):
        # Opacity below 1: at a full-strength fog the levelling step divides by zero (as it does without a curve).
        band = dict(bottom_gradient="custom", bottom_gradient_opacity=0.9, bottom_gradient_height=0.4,
                    vignette_poster_color_bottom=True)
        stock = _render(_cfg(**band), logo=False, art=_coloured_art())
        gentle = _render(_cfg(bottom_gradient_curve=0.8, **band), logo=False, art=_coloured_art())
        mid = 750 - int(750 * 0.4) // 2
        self.assertFalse(np.array_equal(stock, gentle))
        # Smoothstep starts flat, a 0.8 power starts steep: an early row differs.
        early = 750 - int(750 * 0.4) + 20
        self.assertNotEqual(stock[early, 250].tolist(), gentle[early, 250].tolist())
        self.assertTrue(np.isfinite(gentle[mid]).all())

    def test_punch_raises_the_logo_s_saturation(self):
        chroma = lambda a: int((a.max(axis=2) - a.min(axis=2)).max())
        self.assertGreater(chroma(_render(_cfg(logo_punch=1.0))), chroma(_render(_cfg())) + 20)

    def test_shadow_darkens_around_the_logo_and_the_radius_spreads_it(self):
        plain = _render(_cfg())
        tight = _render(_cfg(logo_shadow=1.0, logo_shadow_radius=0.05))
        wide = _render(_cfg(logo_shadow=1.0, logo_shadow_radius=0.3))
        no_logo = _render(_cfg(), logo=False)
        logo_rows = np.where((np.abs(plain - no_logo).sum(axis=2) > 0).any(axis=1))[0]
        above = logo_rows.min() - 15             # just above the logo
        self.assertLess(wide[above, 250, 0], plain[above, 250, 0] - 5)
        self.assertLess(wide[above, 250, 0], tight[above, 250, 0])
        far = logo_rows.min() - 150              # well clear of any halo
        self.assertEqual(wide[far, 250, 0], plain[far, 250, 0])


class ConfiguratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "configurator.html").read_text(encoding="utf-8")

    def test_sent_and_imported(self):
        for param, control in (("logo_punch", "cfg-logo-punch"), ("logo_shadow", "cfg-logo-shadow"),
                               ("logo_shadow_radius", "cfg-logo-shadow-radius"),
                               ("art_gain", "cfg-art-gain"), ("bottom_gradient_curve", "cfg-bg-curve")):
            self.assertIn(f'id="{control}"', self.html)
            self.assertRegex(self.html, rf"params\.set\('{param}',\s+n\('{control}'\)")
            self.assertRegex(self.html, rf"p\.has\('{param}'\)\)\s+_setEl\('{control}'")

    def test_sliders_start_at_the_server_defaults(self):
        defaults = main._render_param_defaults()
        for control, param in (("cfg-logo-punch", "logo_punch"), ("cfg-logo-shadow", "logo_shadow"),
                               ("cfg-logo-shadow-radius", "logo_shadow_radius"),
                               ("cfg-art-gain", "art_gain"), ("cfg-bg-curve", "bottom_gradient_curve")):
            value = float(self.html.split(f'id="{control}"', 1)[1].split('value="', 1)[1].split('"', 1)[0])
            self.assertAlmostEqual(value, defaults[param])


if __name__ == "__main__":
    unittest.main()
