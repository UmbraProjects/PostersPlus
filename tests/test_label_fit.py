"""Clean and Bar labels shrink to fit when a translation runs long."""
import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

import fonts
import main
from i18n import load_languages, translate_genre
from main import RequestConfig, build_poster, build_request_config


def _art():
    return Image.new("RGBA", (500, 750), (40, 40, 50, 255))


def _text_overruns(render) -> list[tuple[str, float]]:
    """Each text *render* draws past the edges of the canvas it draws on,
    with how far."""
    overruns, draw_text = [], ImageDraw.ImageDraw.text

    def text(self, xy, t, *args, **kwargs):
        if isinstance(t, str) and t.strip() and kwargs.get("font") is not None:
            box = self.textbbox(xy, t, font=kwargs["font"], anchor=kwargs.get("anchor"))
            over = max(-box[0], box[2] - self.im.size[0])
            if over > 0:
                overruns.append((t, over))
        return draw_text(self, xy, t, *args, **kwargs)

    with mock.patch.object(ImageDraw.ImageDraw, "text", text):
        render()
    return overruns


class FitLabelSizeTests(unittest.TestCase):
    def test_a_line_that_fits_keeps_its_size(self):
        self.assertEqual(fonts.fit_label_size("Drama ★ 79", 40, 460), 40)

    def test_a_long_line_shrinks_to_the_width(self):
        size = fonts.fit_label_size("Документальный ★ 79", 40, 300)
        self.assertLess(size, 40)
        self.assertLessEqual(fonts.label_font(size).getlength("Документальный ★ 79"), 300)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def _render(self, mode, lang, genre):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", rating_display_mode=mode,
                            logo_language=lang)
        return np.array(build_poster(_art(), 79, genre, cfg, release_year="2024"))

    def test_long_genres_stay_inside_the_poster(self):
        for mode in (2, 4):
            for lang, genre in (("ru", "Documentary"), ("sw", "Sci-Fi"), ("el", "Animation")):
                with self.subTest(mode=mode, lang=lang):
                    self.assertGreater(len(translate_genre(genre, lang)), 12)
                    self.assertEqual(_text_overruns(lambda: self._render(mode, lang, genre)), [])

    def test_the_accent_bars_genre_year_and_sash_stay_inside_the_poster(self):
        meta = types.SimpleNamespace(release_status=None)
        for lang, genre in (("en", "Documentary"), ("el", "Animation")):
            with self.subTest(lang=lang), mock.patch.object(main, "pick_sash",
                                                            lambda *a, **k: ("Globe Nominee", "nom")):
                cfg = RequestConfig(top_gradient="off", bottom_gradient="off", rating_display_mode=1,
                                    accent_bar_append_mode=2, sash_mode="hidden", logo_language=lang)
                self.assertEqual(_text_overruns(lambda: build_poster(
                    _art(), 79, genre, cfg, release_year="2024", discovery_meta=meta)), [])

    def test_a_covered_title_keeps_the_genre_font_unless_a_zero_width_char_is_missing(self):
        bebas = fonts.os.path.join(fonts.FONTS_DIR, "BebasNeue-Bold.ttf")
        self.assertEqual(fonts.font_for_text(bebas, "Heat"), bebas)
        # Only the joiners are let through; any other unmapped character
        # still sends the title to a font that has it.
        if not fonts.covers(bebas, "\u200b"):
            self.assertNotEqual(fonts.font_for_text(bebas, "Heat\u200b"), bebas)

    def test_translated_clean_and_bar_posters_re_render(self):
        rev = next(r for r in main._RENDER_REVISIONS if r.rev == 31)
        self.assertTrue(rev.applies(build_request_config({"logo_language": "ru", "rating_display_mode": "2"})))
        self.assertTrue(rev.applies(build_request_config({"logo_language": "sw", "rating_display_mode": "4"})))
        self.assertFalse(rev.applies(build_request_config({"logo_language": "en", "rating_display_mode": "2"})))
        self.assertFalse(rev.applies(build_request_config({"logo_language": "ru", "rating_display_mode": "3"})))
        self.assertFalse(rev.applies(build_request_config({"logo_language": "ja", "rating_display_mode": "2"})))
        self.assertTrue(rev.applies(build_request_config({"rating_display_mode": "1",
                                                          "accent_bar_append_mode": "2"})))
        self.assertFalse(rev.applies(build_request_config({"rating_display_mode": "1"})))


if __name__ == "__main__":
    unittest.main()
