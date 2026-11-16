"""greyscale_rating / landscape_greyscale_rating: the rating greyed when unavailable."""
import unittest
import unittest.mock

import numpy as np
from PIL import Image

import landscape
import main
from discovery import DiscoveryMeta
from param_aliases import expand


def _meta(status="Cinema"):
    m = DiscoveryMeta()
    m.release_status = status
    return m


def _chroma(arr: np.ndarray) -> np.ndarray:
    return arr.max(axis=-1) - arr.min(axis=-1)


class GreyscaleDrawnTests(unittest.TestCase):
    def test_solid_goes_grey_faint_keeps_most_colour(self):
        before = Image.new("RGBA", (40, 40), (20, 60, 200, 255))
        image = before.copy()
        image.paste((255, 200, 0, 255), (0, 0, 40, 10))        # a solid fill
        px = np.asarray(image).astype(int)
        px[20:30, :, 0] += 10                                   # a faint glow tail
        image = Image.fromarray(px.clip(0, 255).astype(np.uint8), "RGBA")
        main.greyscale_drawn(image, before)
        out = np.asarray(image.convert("RGB")).astype(int)
        self.assertLess(_chroma(out[:10]).max(), 2)             # solid: grey
        self.assertGreater(_chroma(out[20:30]).min(), 100)      # faint: still blue
        self.assertTrue((out[35] == (20, 60, 200)).all())       # untouched: as it was

    def test_full_greys_faint_edges_too(self):
        before = Image.new("RGBA", (10, 10), (90, 90, 90, 255))
        image = before.copy()
        image.paste((100, 90, 80, 255), (0, 0, 10, 5))         # a faint gold edge
        main.greyscale_drawn(image, before, full=True)
        self.assertLess(_chroma(np.asarray(image.convert("RGB")).astype(int)).max(), 2)

    def test_nothing_drawn_is_a_no_op(self):
        before = Image.new("RGBA", (10, 10), (20, 60, 200, 255))
        image = before.copy()
        main.greyscale_drawn(image, before)
        self.assertEqual(image.tobytes(), before.tobytes())


class GreyscaleRatingTests(unittest.TestCase):
    def test_params(self):
        self.assertFalse(main.build_request_config({}).greyscale_rating)
        self.assertTrue(main.build_request_config({"greyscale_rating": "true"}).greyscale_rating)
        self.assertEqual(expand([("grs", "1"), ("_grs", "1")]),
                         [("greyscale_rating", "1"), ("landscape_greyscale_rating", "1")])
        base = main._render_config_signature(main.build_request_config({}))
        self.assertNotEqual(base, main._render_config_signature(
            main.build_request_config({"greyscale_rating": "true"})))

    def _portrait(self, status="Cinema", **over):
        cfg = main.RequestConfig(sash_mode="hidden", rating_display_mode=1, accent_bar_style="edge",
                                 cinema_greyscale=False, score_glow_alpha=0, **over)
        art = Image.new("RGBA", (500, 750), (20, 60, 200, 255))
        out = main.build_poster(art, 90, "Drama", cfg, discovery_meta=_meta(status))
        return np.asarray(out.convert("RGB")).astype(int)

    def test_portrait_bar_greys_only_when_asked_and_unavailable(self):
        self.assertGreater(_chroma(self._portrait()[-4, :300]).max(), 100)          # gold bar
        grey = self._portrait(greyscale_rating=True)
        self.assertLess(_chroma(grey[-4, :300]).max(), 3)                           # grey bar
        self.assertGreater(_chroma(grey[300]).min(), 100)                           # art in colour
        self.assertGreater(_chroma(self._portrait("Streaming", greyscale_rating=True)[-4, :300]).max(), 100)

    def test_landscape_bar(self):
        def render(**over):
            cfg = main.RequestConfig(shape="landscape", sash_mode="hidden", vignette_poster_color_bottom=False,
                                     landscape_rating_mode="rating_bar", score_glow_alpha=0, **over)
            art = Image.new("RGBA", (1000, 563), (20, 60, 200, 255))
            out = landscape.build_landscape(art, 90, "Drama", cfg, discovery_meta=_meta())
            return np.asarray(out.convert("RGB")).astype(int)
        self.assertGreater(_chroma(render()[-3, :500]).max(), 100)
        self.assertLess(_chroma(render(landscape_greyscale_rating=True)[-3, :500]).max(), 3)

    @staticmethod
    def _sash_chroma(status="Cinema", **over):
        cfg = main.RequestConfig(sash_mode="sash", rating_display_mode=0, cinema_greyscale=True,
                                 **over)
        meta = _meta(status)
        meta.matched_studios = ["A24"]
        out = main.build_poster(Image.new("RGBA", (500, 750), (90, 90, 90, 255)), 80, "Drama",
                                cfg, discovery_meta=meta)
        top_right = np.asarray(out.convert("RGB")).astype(int)[:150, 350:]
        return _chroma(top_right).max()

    def test_sash_greys_only_with_all_three(self):
        all_three = dict(greyscale_logo=True, greyscale_rating=True)
        with unittest.mock.patch.object(main, "pick_sash", lambda m, p: ("#1 Today", "trending")):
            self.assertGreater(self._sash_chroma(), 60)                                  # coloured sash
            self.assertGreater(self._sash_chroma(greyscale_rating=True), 60)             # two of three
            self.assertLess(self._sash_chroma(**all_three), 3)                           # all three: grey
            self.assertGreater(self._sash_chroma("Streaming", **all_three), 60)          # available

    def test_landscape_badge_greys_with_all_three(self):
        def render(**over):
            cfg = main.RequestConfig(shape="landscape", sash_mode="sash", landscape_badge_style="gold",
                                     landscape_greyscale=True, landscape_rating_mode="line", **over)
            art = Image.new("RGBA", (1000, 563), (90, 90, 90, 255))
            with unittest.mock.patch.object(main, "pick_sash", lambda m, p: ("#1 Today", "trending")):
                out = landscape.build_landscape(art, 80, "Drama", cfg, discovery_meta=_meta())
            return _chroma(np.asarray(out.convert("RGB")).astype(int)[:120, :400]).max()
        self.assertGreater(render(), 30)
        self.assertLess(render(landscape_greyscale_logo=True, landscape_greyscale_rating=True), 3)

    def test_pipeline_fetches_the_status_for_the_rating_alone(self):
        import inspect
        src = inspect.getsource(main)
        self.assertIn("or rcfg.landscape_greyscale_rating) if _is_landscape", src)
        self.assertIn("rcfg.greyscale_logo or rcfg.greyscale_rating)", src)


if __name__ == "__main__":
    unittest.main()
