"""Every slider's number opens for typing, the rebuilt weight rows included.

The weight rows are rebuilt with innerHTML on restore, import, preset and
reset, so per-span listeners bound at startup were lost with them. The editor
is delegated from the document instead, and commits by firing the slider's own
input event so the weights total and URL update as a drag would.
"""

from pathlib import Path
import unittest


class RangeValEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")
        start = cls.html.index("function _initRangeValEditing()")
        cls.body = cls.html[start : cls.html.index("function openFallbackGallery()")]

    def test_editor_is_delegated_not_bound_per_span(self):
        self.assertNotIn("querySelectorAll('.range-val')", self.body)
        for event in ("click", "keydown", "paste", "focusout"):
            with self.subTest(event=event):
                self.assertIn(f"document.addEventListener('{event}'", self.body)

    def test_commit_runs_the_slider_s_own_handler(self):
        self.assertIn("slider.dispatchEvent(new Event('input', { bubbles: true }));", self.body)

    def test_touch_wrapped_slider_is_still_found(self):
        self.assertIn("prev.querySelector('input[type=\"range\"]')", self.html)


if __name__ == "__main__":
    unittest.main()
