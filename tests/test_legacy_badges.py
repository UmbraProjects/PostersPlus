"""The "legacy" graphic badge slot: the old display modes' badges, placed by
the groups (badge_legacy_style)."""
import types
import unittest

import numpy as np
from PIL import Image

import age_badge
import graphic_badges
import main

TOKENS = ["4K", "REMUX", "DV", "ATMOS"]


def _cfg(style="notch", **kw):
    cfg = main.RequestConfig()
    cfg.badge_display_mode = 7
    cfg.badge_legacy_style = style
    cfg.badge_min_score = 2
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class ParseTests(unittest.TestCase):
    def test_slot_and_style_parse(self):
        self.assertEqual(graphic_badges.parse_group("tl:2:legacy,cert").slots, ("legacy", "cert"))
        self.assertEqual(graphic_badges.legacy_style("Bookmark"), "bookmark")
        self.assertIsNone(graphic_badges.legacy_style("tier"))
        cfg = main.build_request_config({"badge_legacy_style": "row"})
        self.assertEqual(cfg.badge_legacy_style, "row")

    def test_quality_use_follows_the_style(self):
        self.assertTrue(graphic_badges.groups_use_quality(_cfg("notch", badge_group1="tl:1:legacy")))
        self.assertFalse(graphic_badges.groups_use_quality(_cfg("age", badge_group1="tl:1:legacy")))


class ItemTests(unittest.TestCase):
    def test_each_row_style_draws_an_item_without_the_row_shadow(self):
        for style in ("notch", "row", "combined", "quality_age", "age"):
            make = main._legacy_badge(_cfg(style), TOKENS, 14)
            items = graphic_badges.row_items(TOKENS, None, 14, 33, ("legacy",), True,
                                             None, None, None, make)
            self.assertEqual(len(items), 1, style)
            self.assertIn("legacy", items[0][1].info, style)

    def test_default_size_draws_at_the_old_default_height(self):
        self.assertEqual(main._legacy_height("notch", 33), 20)
        self.assertEqual(main._legacy_height("bookmark", 66), 60)

    def test_gates_match_the_old_modes(self):
        low = ["1080P"]   # under a minimum of 5
        self.assertIsNone(main._legacy_badge(_cfg("notch", badge_min_score=5), low, 14))
        self.assertIsNotNone(main._legacy_badge(_cfg("notch"), [], 14))       # silver, as before
        self.assertIsNone(main._legacy_badge(_cfg("row"), [], 14))
        self.assertIsNone(main._legacy_badge(_cfg("age"), TOKENS, None)(33))  # no age rating
        self.assertIsNone(main._legacy_badge(_cfg("bookmark"), TOKENS, 14))   # not a row item


class BookmarkTests(unittest.TestCase):
    def test_corner_follows_the_group(self):
        G = graphic_badges.parse_group
        self.assertEqual(main._legacy_bookmark_corner(G("br:1:legacy"), False), ("right", True))
        self.assertEqual(main._legacy_bookmark_corner(G("tl:1:legacy"), True), ("left", False))
        self.assertEqual(main._legacy_bookmark_corner(G("chip:1:legacy"), True), ("right", False))
        self.assertEqual(main._legacy_bookmark_corner(G("0.9,0.9:1:legacy"), False), ("right", True))

    def test_drawn_in_the_corner_and_taken_out_of_the_group(self):
        im = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        cfg = _cfg("bookmark")
        groups = [graphic_badges.parse_group("bl:2:legacy,cert"), graphic_badges.parse_group("tr:1:res")]
        left = main._draw_legacy_bookmark(im, cfg, groups, TOKENS, lambda g: 33, chip_right=False)
        self.assertEqual([g.slots for g in left], [("cert",), ("res",)])
        a = np.asarray(im)[..., :3].sum(axis=2)
        self.assertGreater(a[745, 2], 0)    # bottom-left corner painted
        self.assertEqual(a[2, 2], 0)

    def test_bottom_flips_the_fold(self):
        im = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
        age_badge.draw_quality_corner_bookmark(im, TOKENS, bookmark_size=30, side="right", bottom=True)
        box = im.getbbox()
        self.assertEqual((box[2], box[3]), (200, 300))


if __name__ == "__main__":
    unittest.main()
