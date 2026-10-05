from pathlib import Path
import re
import unittest


# Switching quality display mode used to overwrite "Minimum Quality to Display"
# with that mode's default, throwing away a choice the user had already made.
# The modes have since become Graphic Badges alone (the old ones are the Legacy
# badge's styles), so nothing seeds it any more; these keep the restore honest.
#
# These are source-shape assertions, not behavioural ones: the repo has no JS
# runtime, so they pin the four parts that have to agree rather than driving the
# widget.  Anything that reworks this logic should expect to rewrite them.
class BadgeMinScoreStickinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def _fn(self, name: str) -> str:
        match = re.search(rf"function {name}\(\)\s*\{{(.*?)\n\}}", self.html, re.S)
        self.assertIsNotNone(match, f"{name}() not found")
        return match.group(1)

    def test_repaint_does_not_seed(self):
        # updateBadgeModeHint also runs after the startup restore and after a
        # preset. Seeding there overwrote the restored Badge Size on every load.
        body = self._fn("updateBadgeModeHint")
        self.assertNotIn("cfg-badge-h", body)
        self.assertNotIn("cfg-badge-min-score').value", body)

    def test_saved_settings_keep_defaults(self):
        # The startup restore runs before /server-caps answers, so it can't
        # fill an omitted parameter back in from the server defaults. The
        # minimum quality's form default (5) is not the server's (2), so an
        # omitted "HD Web" came back as 5 on every reload.
        self.assertIn("const _emitted = full ? params : omitServerDefaults(params);", self.html)

    def test_choosing_a_minimum_marks_it_user_set(self):
        select = re.search(
            r"<select id=\"cfg-badge-min-score\"[^>]*onchange=\"([^\"]*)\"", self.html
        )
        self.assertIsNotNone(select, "cfg-badge-min-score select not found")
        self.assertIn("dataset.userSet", select.group(1))

    def test_imported_minimum_is_marked_user_set(self):
        # An imported URL (and so the localStorage settings restore) is as
        # deliberate as a click, and must survive a later mode change.
        self.assertRegex(
            self.html,
            r"badge_min_score'\)\)\s*\{[^}]*cfg-badge-min-score'\)\.dataset\.userSet\s*=",
        )

    def test_both_reset_paths_clear_the_mark(self):
        # Reset to Defaults must re-arm the per-mode seeding; there are two
        # separate reset loops in this file and both have to forget the mark.
        self.assertEqual(
            len(re.findall(r"delete el\.dataset\.userSet;", self.html)), 2,
            "both form-reset loops must clear dataset.userSet",
        )


if __name__ == "__main__":
    unittest.main()
