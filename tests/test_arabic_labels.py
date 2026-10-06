"""Arabic, Persian and Urdu labels: their fonts, shaping with raqm, native
digits, and original_labels (a title's labels in its own language).

original_labels and the digits were first built by @aRamadi (issue 40)."""
import dataclasses
import json
import os
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import awards
import fonts
import i18n
import main
from i18n import load_languages, native_digits, translate_genre, translate_sash, visual
from main import RequestConfig, build_poster, build_request_config

INTER = os.path.join(fonts.FONTS_DIR, "Inter-Bold.ttf")
RUBIK = os.path.join(fonts.FONTS_DIR, "Rubik-Bold.ttf")
ALMARAI = os.path.join(fonts.FONTS_DIR, "Almarai-Bold.ttf")
LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "languages")


def _art(size=(500, 750)):
    return Image.new("RGBA", size, (16, 16, 24, 255))


class LanguageFileTests(unittest.TestCase):
    def test_every_key_is_translated(self):
        with open(os.path.join(LANG_DIR, "en.json"), encoding="utf-8") as f:
            en = json.load(f)
        for code in ("ar", "fa", "ur"):
            with self.subTest(code), open(os.path.join(LANG_DIR, f"{code}.json"), encoding="utf-8") as f:
                data = json.load(f)
                self.assertEqual(data["code"], code)
                self.assertEqual(set(data["genreLabels"]), set(en["genreLabels"]))
                self.assertEqual(set(data["sashLabels"]), set(en["sashLabels"]))
                self.assertEqual(len(data["monthsShort"]), 12)

    def test_persian_and_urdu_use_their_own_letters(self):
        # Arabic yeh and kaf look alike but are not what Persian or Urdu
        # write, and Urdu's heh is U+06C1, not Arabic U+0647.
        for code, wrong in (("fa", "يك"), ("ur", "يكه")):
            with self.subTest(code), open(os.path.join(LANG_DIR, f"{code}.json"), encoding="utf-8") as f:
                text = f.read()
                self.assertFalse(set(wrong) & set(text))


class FontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_arabic_script_is_drawn_in_almarai(self):
        for choice in ("inter", "montserrat", "almarai"):
            for lang in ("ar", "fa", "ur", "ar-eg"):
                with self.subTest(choice=choice, lang=lang):
                    self.assertEqual(fonts.resolve_label_font(choice, lang), ALMARAI)

    def test_rubik_keeps_the_arabic_and_persian_it_has(self):
        self.assertEqual(fonts.resolve_label_font("rubik", "ar"), RUBIK)
        self.assertEqual(fonts.resolve_label_font("rubik", "fa"), RUBIK)
        # Rubik has no ٹ ڈ ڑ ھ ہ ے.
        self.assertEqual(fonts.resolve_label_font("rubik", "ur"), ALMARAI)

    def test_hebrew_keeps_rubik(self):
        # Almarai comes first in the fallback order but has no Hebrew.
        self.assertEqual(fonts.resolve_label_font("inter", "he"), RUBIK)

    def test_a_persian_joiner_needs_no_glyph(self):
        # Almarai has no U+200C; raqm acts on it without one.
        self.assertTrue(fonts.covers(ALMARAI, "هیجان\u200cانگیز"))
        # Unshaped, it would draw as a box in a font without it.
        self.assertNotIn("\u200c", visual("ادامه\u200cدار"))

    def test_an_arabic_script_title_takes_almarai(self):
        bebas = os.path.join(fonts.FONTS_DIR, "BebasNeue-Bold.ttf")
        self.assertEqual(fonts.font_for_text(bebas, "زندگی گلزار ہے"), ALMARAI)
        self.assertEqual(fonts.font_for_text(bebas, "حين لا يرانا أحد"), ALMARAI)
        self.assertTrue(fonts.drawable("حين لا يرانا أحد"))
        self.assertFalse(fonts.drawable("기생충"))


class ShapingScopeTests(unittest.TestCase):
    def test_only_arabic_script_languages_shape(self):
        for lang in ("ar", "fa", "ur", "ar-SA", "fa-ir"):
            self.assertTrue(i18n.needs_shaping(lang), lang)
        for lang in ("he", "en", "el", None, ""):
            self.assertFalse(i18n.needs_shaping(lang), lang)

    def test_the_scope_sets_and_restores_shaping(self):
        self.assertFalse(fonts.shaping())
        with fonts.label_font_scope("inter", "ar"):
            self.assertEqual(fonts.shaping(), fonts.HAVE_RAQM)
        with fonts.label_font_scope("inter", "he"):
            self.assertFalse(fonts.shaping())
        self.assertFalse(fonts.shaping())

    def test_labels_lay_out_basic_outside_a_shaped_render(self):
        self.assertEqual(fonts.label_font(30).layout_engine, ImageFont.Layout.BASIC)
        with fonts.label_font_scope("inter", "he"):
            self.assertEqual(fonts.label_font(30).layout_engine, ImageFont.Layout.BASIC)

    @unittest.skipUnless(fonts.HAVE_RAQM, "raqm unavailable (libfribidi missing)")
    def test_a_shaped_render_lays_labels_out_with_raqm(self):
        with fonts.label_font_scope("inter", "ar"):
            self.assertEqual(fonts.label_font(30).layout_engine, ImageFont.Layout.RAQM)
            self.assertEqual(awards._notch_font(30).layout_engine, ImageFont.Layout.RAQM)
            self.assertEqual(main._load_font(ALMARAI, 30).layout_engine, ImageFont.Layout.RAQM)
        self.assertEqual(awards._notch_font(30).layout_engine, ImageFont.Layout.BASIC)
        self.assertEqual(main._load_font(ALMARAI, 30).layout_engine, ImageFont.Layout.BASIC)

    @unittest.skipUnless(fonts.HAVE_RAQM, "raqm unavailable (libfribidi missing)")
    def test_raqm_reorders_so_visual_leaves_the_line_alone(self):
        with fonts.label_font_scope("inter", "ar"):
            self.assertEqual(visual("دراما · 2019 ★ 87"), "دراما · 2019 ★ 87")
            # Hebrew text in an Arabic render is raqm's to order too.
            self.assertEqual(visual("דרמה"), "דרמה")
        self.assertEqual(visual("דרמה"), "המרד")

    @unittest.skipUnless(fonts.HAVE_RAQM, "raqm unavailable (libfribidi missing)")
    def test_shaped_letters_join(self):
        # Joined, each seen is its narrower medial or final form, not the
        # isolated one drawn three times.
        basic = fonts.truetype(ALMARAI, 60, shaped=False)
        raqm = fonts.truetype(ALMARAI, 60, shaped=True)
        self.assertLess(raqm.getlength("سسس"), basic.getlength("سسس"))


class BadgeCentringTests(unittest.TestCase):
    """Badge text is centred on its line by _text_center.  The nudge tuned
    for Latin capitals left Arabic low, so Arabic is placed halfway between
    centring its ink and centring the band from baseline to alef top."""

    def _drawn(self, text, font, cy=100):
        im = Image.new("L", (400, 200))
        draw = ImageDraw.Draw(im)
        x, y = awards._text_center(draw, text, font, 200, cy)
        draw.text((x, y), text, font=font, fill=255)
        return y, im.getbbox()

    def test_has_arabic(self):
        self.assertTrue(fonts.has_arabic("دراما"))
        self.assertTrue(fonts.has_arabic("Drama · دراما"))
        self.assertTrue(fonts.has_arabic("ﻻ"))  # presentation forms too
        self.assertFalse(fonts.has_arabic("Drama"))
        self.assertFalse(fonts.has_arabic("דרמה"))
        self.assertFalse(fonts.has_arabic(""))
        self.assertFalse(fonts.has_arabic(None))

    @unittest.skipUnless(fonts.HAVE_RAQM, "raqm unavailable (libfribidi missing)")
    def test_arabic_is_centred_between_its_ink_and_its_alef_band(self):
        font = fonts.truetype(ALMARAI, 40, shaped=True)
        ascent = font.getmetrics()[0]
        alef_top = font.getbbox("\u0627", anchor="ls")[1]
        # With and without an alef, and with dots and tails below the line.
        for text in ("عربي", "الفائز", "ترشيح", "دراما"):
            with self.subTest(text):
                y, ink = self._drawn(text, font)
                ink_centre = (ink[1] + ink[3]) / 2
                band_centre = y + ascent + alef_top / 2
                self.assertAlmostEqual((ink_centre + band_centre) / 2, 100, delta=1)

    @unittest.skipUnless(fonts.HAVE_RAQM, "raqm unavailable (libfribidi missing)")
    def test_arabic_sits_higher_than_the_latin_nudge_put_it(self):
        font = fonts.truetype(ALMARAI, 40, shaped=True)
        ascent, descent = font.getmetrics()
        latin_y = 100 - (ascent + descent) / 2 - descent + awards.px(ascent * 0.22)
        y, _ = self._drawn("عربي", font)
        self.assertLess(y, latin_y - 2)

    def test_arabic_script_posters_cached_before_now_re_render(self):
        rev = next(r for r in main._RENDER_REVISIONS if r.rev == 32)
        for params in ({"logo_language": "ar"}, {"logo_language": "fa"}, {"logo_language": "ur-PK"},
                       {"logo_language": "en", "original_labels": "ar"}):
            self.assertTrue(rev.applies(build_request_config(params)), params)
        for params in ({"logo_language": "en"}, {"logo_language": "he"},
                       {"logo_language": "en", "original_labels": "he"}):
            self.assertFalse(rev.applies(build_request_config(params)), params)

    def test_latin_and_hebrew_are_placed_as_before(self):
        for path, text in ((INTER, "DRAMA ★ 87"), (RUBIK, "דרמה")):
            font = fonts.truetype(path, 40, shaped=False)
            ascent, descent = font.getmetrics()
            with self.subTest(text):
                y, _ = self._drawn(text, font)
                self.assertEqual(y, 100 - (ascent + descent) / 2 - descent + awards.px(ascent * 0.22))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def _render(self, **kwargs):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return np.array(build_poster(_art(), 87, "Drama", cfg, release_year="2019"))

    def test_labels_translate(self):
        self.assertEqual(translate_genre("Drama", "ar"), "دراما")
        self.assertEqual(translate_sash("Oscar Winner", "fa"), "برنده اسکار")
        self.assertEqual(translate_sash("Oct 16 Cinema", "ur"), "سینما میں 16 اکتوبر")
        self.assertEqual(translate_sash("Oct 16 Cinema", "ar"), "في السينما في ١٦ أكتوبر")

    def test_arabic_script_posters_draw_their_own_labels(self):
        english = self._render(rating_display_mode=1)
        for lang in ("ar", "fa", "ur"):
            with self.subTest(lang):
                self.assertFalse(np.array_equal(english, self._render(rating_display_mode=1, logo_language=lang)))

    def test_the_shaped_sash_draws_on_the_pil_path(self):
        art = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        with fonts.label_font_scope("inter", "ar"):
            with mock.patch.object(awards, "_sash_skia", wraps=awards._sash_skia) as skia:
                sash = awards.draw_award_sash(art, translate_sash("Oscar Winner", "ar"))
        self.assertEqual(skia.called, awards._HAS_SKIA and not fonts.HAVE_RAQM)
        self.assertGreater(np.asarray(sash)[..., 3].sum(), 0)

    def test_arabic_script_posters_cached_before_now_re_render(self):
        rev = next(r for r in main._RENDER_REVISIONS if r.rev == 30)
        for lang in ("ar", "fa", "ur", "ar-sa"):
            self.assertTrue(rev.applies(build_request_config({"logo_language": lang})), lang)
        self.assertFalse(rev.applies(build_request_config({"logo_language": "he"})))



class NativeDigitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_rank_dates_and_seasons_take_the_languages_digits(self):
        self.assertEqual(native_digits("12", "ar"), "١٢")
        self.assertEqual(native_digits("12", "fa"), "۱۲")
        self.assertEqual(native_digits("12", "ur"), "12")
        self.assertEqual(native_digits("12", "en"), "12")
        self.assertEqual(translate_sash("#3 Today", "ar"), "#٣ اليوم")
        self.assertEqual(translate_sash("#12 Today", "fa"), "#۱۲ امروز")
        self.assertEqual(translate_sash("Dec 2027 Cinema", "ar"), "في السينما في ديسمبر ٢٠٢٧")
        self.assertEqual(translate_sash("Mar 4 Season 3", "ar"), "الموسم ٣ في ٤ مارس")
        self.assertEqual(translate_sash("Oct 16 Cinema", "en"), "Oct 16 Cinema")
        self.assertEqual(translate_sash("#3 Today", "en"), "#3 Today")

    def test_the_year_takes_the_languages_digits(self):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", rating_display_mode=3)

        def render(year, lang):
            return np.array(build_poster(_art(), 87, "Drama", dataclasses.replace(cfg, label_language=lang),
                                         release_year=year))
        self.assertTrue(np.array_equal(render("2026", "ar"), render("٢٠٢٦", "ar")))
        self.assertFalse(np.array_equal(render("2026", "ar"), render("2026", "ur")))


class OriginalLabelsConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_parses_to_sorted_base_languages(self):
        self.assertEqual(build_request_config({}).original_labels, "")
        self.assertEqual(build_request_config({"original_labels": "ar"}).original_labels, "ar")
        self.assertEqual(build_request_config({"original_labels": "HE, ar-EG,ar,x!"}).original_labels,
                         "ar,he")

    def test_the_default_leaves_cache_keys_alone(self):
        self.assertNotIn("original_labels", main._render_config_signature(build_request_config({})))
        self.assertIn("original_labels", main._render_config_signature(
            build_request_config({"original_labels": "ar"})))

    def test_the_per_render_language_is_not_in_the_cache_key(self):
        cfg = build_request_config({"original_labels": "ar"})
        self.assertEqual(main._render_config_signature(cfg),
                         main._render_config_signature(dataclasses.replace(cfg, label_language="ar")))

    def test_only_a_listed_language_with_a_translation_switches(self):
        cfg = build_request_config({"original_labels": "ar,ko"})
        self.assertEqual(main._own_label_language(cfg, "ar"), "ar")
        self.assertEqual(main._own_label_language(cfg, "en"), "")
        self.assertEqual(main._own_label_language(cfg, "ko"), "")    # no ko.json
        self.assertEqual(main._own_label_language(cfg, None), "")
        self.assertEqual(main._own_label_language(build_request_config({}), "ar"), "")

    def test_a_title_already_in_the_poster_language_needs_no_switch(self):
        cfg = build_request_config({"original_labels": "ar", "logo_language": "ar"})
        self.assertEqual(main._own_label_language(cfg, "ar"), "")

    def test_label_lang_follows_the_switch(self):
        cfg = build_request_config({"original_labels": "ar"})
        self.assertEqual(cfg.label_lang, "en")
        self.assertEqual(dataclasses.replace(cfg, label_language="ar").label_lang, "ar")


class OriginalLabelsRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def _render(self, title=None, **kwargs):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return np.array(build_poster(_art(), 87, "Drama", cfg, release_year="2026",
                                     fallback_title=title))

    def test_switched_labels_draw_as_a_poster_in_that_language_does(self):
        for lang in ("ar", "fa", "he"):
            for mode in (1, 2, 3, 4):
                with self.subTest(lang=lang, mode=mode):
                    switched = self._render(rating_display_mode=mode, label_language=lang)
                    self.assertFalse(np.array_equal(switched, self._render(rating_display_mode=mode)))
                    self.assertTrue(np.array_equal(
                        switched, self._render(rating_display_mode=mode, logo_language=lang)))

    def test_an_arabic_title_is_drawn(self):
        blank = self._render(rating_display_mode=1)
        titled = self._render("حين لا يرانا أحد", rating_display_mode=1)
        self.assertFalse(np.array_equal(blank, titled))


if __name__ == "__main__":
    unittest.main()
