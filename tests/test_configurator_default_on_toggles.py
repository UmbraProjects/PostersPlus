from pathlib import Path
import re
import unittest


# Almost every configurator switch is off by default on both sides, so build()
# can omit its param when unchecked.  These are the exception: the renderer
# defaults them to *on*, so an omitted param means "on" and a switch that only
# writes itself when checked can never turn anything off.
DEFAULT_ON_PARAMS = ("vignette_color_ramp", "vignette_color_local", "release_status_dates",
                     "airing_dates", "anime_foreign_sash")


class DefaultOnToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")
        cls.main = Path("main.py").read_text(encoding="utf-8")

    def test_renderer_still_defaults_these_on(self):
        # If one of these ever flips to False in RequestConfig the pairing below
        # stops being required — this test is what says so out loud.
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                self.assertRegex(
                    self.main,
                    re.compile(rf"^\s*{param}:\s*bool\s*=\s*True\b", re.M),
                )

    def test_configurator_writes_both_states(self):
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                # set() is buildBaseParams' per-shape writer (params.set under a
                # shape's prefix), so either spelling counts.
                match = re.search(rf"\bset\(\s*'{param}',([^\n]*)", self.html)
                self.assertIsNotNone(match, f"{param} is never written by build()")
                self.assertIn("'false'", match.group(1),
                              f"{param} must be written as false when its switch is off")

    def test_configurator_reads_both_states_back(self):
        # The importer is what makes an off switch survive a paste or a reload.
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                self.assertRegex(self.html, rf"p\.has\('{param}'\)")


if __name__ == "__main__":
    unittest.main()


class SectionResetTests(unittest.TestCase):
    """Right-click / press-and-hold a tab or group heading to reset just it."""

    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_wiring(self):
        self.assertIn('<div class="menu" id="reset-menu" role="menu"', self.html)
        self.assertIn("if (!ev.target.closest?.('.tab-btn, .section .group-title')) return;", self.html)
        # The full reset and the section reset put a control back the same way.
        self.assertIn("if (!preserve.has(el.id)) _resetControl(el);", self.html)
        self.assertIn("    _resetControl(el);\n    touched.push(el.id);", self.html)
        # Keys and the selected title survive a section reset, as they do Reset config.
        self.assertIn("'cfg-tmdb-key', 'cfg-mdblist-key', 'cfg-access-key'", self.html)
        # The other shape's controls are left alone.
        self.assertIn("const otherShape = landscape ? '.portrait-only' : '.landscape-only';", self.html)
