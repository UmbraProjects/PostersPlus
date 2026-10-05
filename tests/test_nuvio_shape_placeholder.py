"""One URL for both layouts, via Nuvio's "{shape}" placeholder.

Nuvio's pattern resolver fills "{shape}" with the shape the catalogue asked
for — "poster", "landscape" or "square" — so a single configured URL can serve
the 2:3 slot, the 16:9 slot and the Continue Watching backdrop. The
placeholder's presence is itself the switch: without it Nuvio overrides only
portrait items and leaves the other shapes with their own art.

Three things follow, and they are what this file pins down:

  * "poster" is Nuvio's word for our "portrait". It already rendered correctly
    by accident, because an unrecognised shape fell through to the portrait
    default — so the risk here is not that it breaks, it is that it silently
    stops being an accident nobody notices. It also has to reach the *same*
    composite cache entry as a URL with no shape at all, or one render is
    cached under four spellings.

  * "square" is refused. It is only ever asked for when an addon declares
    posterShape: "square" on a catalogue item, we have no square renderer, and
    coercing it would push a 2:3 poster into a 1:1 tile. An error hands the
    item back to Nuvio's fallback interceptor, which restores the addon's own
    art — the shapes we serve are replaced, the one we don't is left alone.

  * A "{shape}" URL is a landscape URL and a portrait one at once, so it may
    not be filtered down to either. The settings the configurator keeps per
    shape travel twice — the portrait value under the plain name, the
    landscape one as "landscape_<name>", which only a landscape render reads —
    because one value between them would hand one layout the other's look.
    Anything else may only be omitted where both default sets agree.
"""

from pathlib import Path
import re
import unittest

from fastapi import HTTPException

import main
from main import _normalise_shape, build_request_config


class ShapeVocabularyTests(unittest.TestCase):
    """What Nuvio substitutes, mapped onto the two layouts we render."""

    def test_nuvios_word_for_portrait_is_accepted(self):
        self.assertEqual(_normalise_shape("poster"), "portrait")
        self.assertEqual(build_request_config({"shape": "poster"}).shape, "portrait")

    def test_our_own_words_still_work(self):
        for value in ("portrait", "landscape"):
            with self.subTest(shape=value):
                self.assertEqual(_normalise_shape(value), value)
                self.assertEqual(build_request_config({"shape": value}).shape, value)

    def test_case_and_padding_do_not_matter(self):
        self.assertEqual(_normalise_shape("  LANDSCAPE "), "landscape")

    def test_absent_is_portrait(self):
        self.assertEqual(_normalise_shape(None), "portrait")
        self.assertEqual(_normalise_shape(""), "portrait")

    def test_an_unsubstituted_placeholder_is_portrait(self):
        # A build that does not know "{shape}" leaves it verbatim. That is "no
        # shape", not a malformed one — the same reading _normalise_optional_id
        # gives a literal id, and for the same reason: 400ing it would take down
        # every poster served through the template.
        for literal in ("{shape}", "{shape?}"):
            with self.subTest(literal=literal):
                self.assertEqual(_normalise_shape(literal), "portrait")

    def test_an_unknown_shape_stays_lenient(self):
        self.assertEqual(_normalise_shape("banner"), "portrait")

    def test_square_is_refused_rather_than_coerced(self):
        with self.assertRaises(HTTPException) as caught:
            _normalise_shape("square")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("square", caught.exception.detail.lower())

    def test_landscape_still_seeds_its_own_defaults(self):
        cfg = build_request_config({"shape": "landscape"})
        for name, value in main._LANDSCAPE_DEFAULTS.items():
            with self.subTest(param=name):
                self.assertEqual(getattr(cfg, name), value)

    def test_nuvios_portrait_word_does_not_seed_landscape_defaults(self):
        cfg = build_request_config({"shape": "poster"})
        self.assertFalse(cfg.vignette_poster_color_bottom)


class LandscapeSplitParamTests(unittest.TestCase):
    """landscape_<name> carries the landscape value of a per-shape setting."""

    def test_a_dual_url_gives_each_layout_its_own_value(self):
        params = {
            "vignette_poster_color_bottom": "false",
            "landscape_vignette_poster_color_bottom": "true",
            "hide_rating": "true",
            "landscape_hide_rating": "false",
            "sash_mode": "notch",
            "landscape_sash_mode": "hidden",
        }
        portrait = build_request_config({**params, "shape": "poster"})
        landscape = build_request_config({**params, "shape": "landscape"})
        self.assertFalse(portrait.vignette_poster_color_bottom)
        self.assertTrue(portrait.hide_rating)
        self.assertEqual(portrait.sash_mode, "notch")
        self.assertTrue(landscape.vignette_poster_color_bottom)
        self.assertFalse(landscape.hide_rating)
        self.assertEqual(landscape.sash_mode, "hidden")

    def test_portrait_never_reads_the_landscape_value(self):
        cfg = build_request_config({"landscape_vignette_color_saturation": "0.5"})
        self.assertEqual(cfg.vignette_color_saturation,
                         main._render_param_defaults()["vignette_color_saturation"])

    def test_a_landscape_url_from_before_the_split_still_renders_the_same(self):
        # landscape_<name>, then <name>, then the landscape default.
        cfg = build_request_config({"shape": "landscape", "vignette_color_local": "true"})
        self.assertTrue(cfg.vignette_color_local)
        cfg = build_request_config({"shape": "landscape"})
        self.assertFalse(cfg.vignette_color_local)

    def test_the_twin_is_parsed_and_clamped_like_the_plain_name(self):
        cfg = build_request_config({"shape": "landscape",
                                    "landscape_vignette_color_saturation": "99"})
        self.assertEqual(cfg.vignette_color_saturation, 3.0)

    def test_every_split_param_is_a_setting_both_layouts_read(self):
        defaults = main._render_param_defaults()
        for name in main._LANDSCAPE_SPLIT_PARAMS:
            with self.subTest(name=name):
                self.assertIn(name, defaults)
                # The prefixed name must not collide with a real field.
                self.assertNotIn(f"landscape_{name}", defaults)

    def test_the_configurator_maps_exactly_the_split_params(self):
        html = Path("configurator.html").read_text(encoding="utf-8")
        block = re.search(r"const _SHARED_PARAM = \{(.*?)\};", html, re.S).group(1)
        # badge_display_mode is kept per shape by the configurator but is not a
        # split param: landscape reads only landscape_badge_display_mode (opt-in,
        # no fallback to the portrait mode), which the configurator marks — as
        # it does the vignette levels landscape reads from landscape_ alone.
        no_fallback = re.search(r"const _SHARED_NO_FALLBACK = new Set\(\[(.*?)\]\);", html, re.S).group(1)
        self.assertEqual(sorted(re.findall(r"'([a-z_0-9]+)'", no_fallback)),
                         sorted(["badge_display_mode", *main._LANDSCAPE_OWN_PARAMS]))
        self.assertEqual(sorted(re.findall(r":\s*'([a-z_0-9]+)'", block)),
                         sorted([*main._LANDSCAPE_SPLIT_PARAMS, *main._LANDSCAPE_OWN_PARAMS,
                                 "badge_display_mode"]))


class ShapeCacheKeyTests(unittest.TestCase):
    """Four spellings of one render must not be four cache entries."""

    def test_every_portrait_spelling_is_dropped_from_the_render_params(self):
        # The endpoint strips "shape" from raw_params and puts back only the
        # canonical value, so the hash cannot see the difference between
        # "poster", "portrait", a literal "{shape}" and nothing at all.
        source = Path("main.py").read_text(encoding="utf-8")
        block = re.search(r"raw_params = \{(.*?)\n    \}", source, re.S)
        self.assertIsNotNone(block)
        self.assertIn('"shape",', block.group(1))
        self.assertIn('if shape != "portrait":\n        raw_params["shape"] = shape', source)

    def test_the_canonical_value_is_what_the_endpoint_keeps(self):
        # _normalise_shape runs on the endpoint's own parameter before
        # raw_params is built, so the value put back is already canonical.
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("shape = _normalise_shape(shape)", source)


class ConfiguratorDualUrlTests(unittest.TestCase):
    """Copy config's shared "{shape}" entry, and what a dual URL may leave out."""

    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def _template_entry(self, template_id: str) -> str:
        start = self.html.index(f"{{ id: '{template_id}',")
        return self.html[start : self.html.index("}", self.html.index("where:", start)) + 1]

    def test_the_shared_url_sends_the_shape_placeholder(self):
        # AIOMetadata, Nuvio, Bingecat and Xperience all fill "{shape}";
        # Discover+ does not, which is why it is a separate axis from
        # COPY_SHAPE_OPTIMAL.
        self.assertIn("COPY_SHAPE_DUAL", self._template_entry("standard"))
        self.assertNotIn("COPY_SHAPE_DUAL", self._template_entry("discoverplus"))

    def test_the_placeholder_is_only_ever_the_template_url(self):
        # The live preview renders one concrete title, so there is nothing to
        # substitute; a placeholder in it would just be a broken image.
        self.assertIn("const dualShape  = usePlaceholders && !!template.shapePlaceholder;",
                      self.html)

    def test_a_dual_url_is_not_filtered_to_either_shape(self):
        self.assertIn("const emitAll    = full || !landscape || dualShape;", self.html)

    def test_a_dual_url_may_only_omit_what_both_layouts_agree_on(self):
        block = re.search(r"function omitServerDefaults\(params\) \{(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("dual && !split.has(key)             ? [pDefaults, lDefaults]", body)
        # A landscape_ twin is judged against what landscape falls back to.
        # Those it never falls back for are judged against its default alone.
        self.assertIn("params.has(base) && !omit.has(base) && !own.has(base) ? params.get(base) : lDefaults[base]", body)

    def test_a_dual_url_carries_both_shapes_per_shape_values(self):
        self.assertIn("if (emitAll)       emitShared(pShared, '');", self.html)
        self.assertIn("if (emitLandscape) emitShared(lShared, 'landscape_');", self.html)

    def test_the_divergent_defaults_are_the_ones_that_make_that_matter(self):
        # If these two sets ever stop disagreeing the intersection above becomes
        # a no-op — harmless, but the comment explaining it would be a lie. The
        # real point is the opposite direction: a new divergent default must be
        # covered by the intersection automatically, which it is, because the
        # rule is computed rather than listed.
        portrait = main._render_param_defaults("portrait")
        landscape = main._render_param_defaults("landscape")
        divergent = {k for k in portrait if portrait[k] != landscape.get(k)}
        self.assertIn("vignette_poster_color_bottom", divergent)
        # The worst of them: it is the portrait default, so judged against
        # portrait alone it would be omitted, and the landscape render would
        # then tint a band the user had just switched off.
        self.assertFalse(portrait["vignette_poster_color_bottom"])
        self.assertTrue(landscape["vignette_poster_color_bottom"])

    def test_the_landscape_choices_ride_on_a_dual_url_from_either_view(self):
        # The shape a dual URL resolves is the catalogue's, not the preview's,
        # so turning the preview back to portrait must not strip the settings
        # the 16:9 slot will still be served with.
        self.assertIn("const emitLandscape = landscape || dualShape;", self.html)
        block = re.search(r"if \(emitLandscape \|\| full\) \{(.*?)\n  \}", self.html, re.S)
        self.assertIsNotNone(block)
        for param in ("'landscape_art'", "'badge_pos'", "'landscape_badge_scale'",
                      "'landscape_info_scale'"):
            self.assertIn(param, block.group(1))

    def test_importing_a_dual_url_settles_both_shape_stashes(self):
        # Each stash takes the URL's values for its own shape. Leaving the
        # landscape stash alone would let the preview show a landscape
        # configuration the URL will never render.
        self.assertIn("const _dualShape = (parsed.searchParams.get('shape') || '') === '{shape}';",
                      self.html)
        self.assertIn("const _landscapeShared = _dualShape ? _sharedFromParams(p, 'landscape') : null;",
                      self.html)
        self.assertRegex(
            self.html,
            r"if \(_dualShape\) \{\s*_shapeStash\.portrait\s*=\s*_readShared\(\);"
            r"\s*_shapeStash\.landscape\s*=\s*_landscapeShared;",
        )

    def test_saved_settings_never_carry_the_placeholder(self):
        # saveSettings passes no templateId, so it falls back to the neutral
        # shape — a remembered client choice must not land in stored settings.
        self.assertIn("const COPY_TEMPLATE_NEUTRAL = { id: '', name: '', ...COPY_SHAPE_REQUIRED };",
                      self.html)
        self.assertNotIn("shapePlaceholder",
                         re.search(r"const COPY_SHAPE_REQUIRED\s*=\s*\{[^}]*\}", self.html).group(0))


if __name__ == "__main__":
    unittest.main()
