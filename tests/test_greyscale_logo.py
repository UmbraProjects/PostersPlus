"""greyscale_logo / landscape_greyscale_logo: the logo greyed with the art."""
import unittest

import numpy as np
from PIL import Image

import landscape
import main
import param_aliases
from discovery import DiscoveryMeta


def _meta(status="Cinema"):
    m = DiscoveryMeta()
    m.release_status = status
    return m


_RED_LOGO = Image.new("RGBA", (200, 80), (220, 30, 30, 255))


def _max_chroma(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB")).astype(int)
    return int((a.max(axis=2) - a.min(axis=2)).max())


class GreyscaleLogoTests(unittest.TestCase):
    def test_parsed_and_off_by_default(self):
        self.assertFalse(main.build_request_config({}).greyscale_logo)
        self.assertTrue(main.build_request_config({"greyscale_logo": "true"}).greyscale_logo)
        expand = lambda q: dict(param_aliases.expand(q.items()))
        self.assertEqual(list(expand({"gl": "1"})), ["greyscale_logo"])
        self.assertEqual(list(expand({"_gl": "1"})), ["landscape_greyscale_logo"])
        cfg = main.build_request_config({"shape": "landscape", "landscape_greyscale_logo": "true"})
        self.assertTrue(cfg.landscape_greyscale_logo)

    def test_defaults_leave_the_cache_key_alone(self):
        for shape in ("portrait", "landscape"):
            sig = main._render_config_signature(main.build_request_config({"shape": shape}))
            for name in ("greyscale_logo", "landscape_greyscale_logo"):
                self.assertNotIn(name, sig)

    def test_greyscale_rgba_keeps_alpha(self):
        logo = Image.new("RGBA", (4, 4), (255, 0, 0, 120))
        grey = main.greyscale_rgba(logo)
        r, g, b, a = grey.getpixel((1, 1))
        self.assertEqual((r, a), (g, 120))
        self.assertEqual(g, b)

    def _portrait(self, **over):
        cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=0, **over)
        art = Image.new("RGBA", (500, 750), (40, 40, 40, 255))
        return main.build_poster(art, 80, "Drama", cfg, logo=_RED_LOGO.copy(),
                                 discovery_meta=_meta())

    def test_portrait_logo_greys_only_when_asked(self):
        self.assertGreater(_max_chroma(self._portrait(cinema_greyscale_without_sash=True)), 100)
        self.assertLess(_max_chroma(self._portrait(cinema_greyscale_without_sash=True,
                                                   greyscale_logo=True)), 20)
        # Art in colour: the logo stays in colour whatever the switch.
        self.assertGreater(_max_chroma(self._portrait(cinema_greyscale=False, greyscale_logo=True)), 100)

    def _landscape(self, **over):
        cfg = main.RequestConfig(shape="landscape", sash_mode="hidden",
                                 vignette_poster_color_bottom=False, landscape_greyscale=True, **over)
        art = Image.new("RGBA", (1000, 563), (40, 40, 40, 255))
        return landscape.build_landscape(art, 80, "Drama", cfg, logo=_RED_LOGO.copy(),
                                         discovery_meta=_meta(), release_year="2026")

    def test_landscape_logo_greys_only_when_asked(self):
        self.assertGreater(_max_chroma(self._landscape()), 100)
        self.assertLess(_max_chroma(self._landscape(landscape_greyscale_logo=True)), 20)


if __name__ == "__main__":
    unittest.main()
