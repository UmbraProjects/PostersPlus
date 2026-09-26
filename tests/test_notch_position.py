"""sash_badge_pos moves the frosted notch off centre: left/right float a chip in
from that top corner."""
import unittest

import numpy as np
from PIL import Image

import awards
import main


def _poster():
    # Opaque and flat, so any change is the badge (or its shadow).
    return Image.new("RGBA", (500, 750), (40, 90, 140, 255))


def _changed(out, box):
    a = np.asarray(out.crop(box), dtype=np.int16)
    b = np.asarray(_poster().crop(box), dtype=np.int16)
    return int(np.abs(a - b).sum())


class NotchPositionRenderingTests(unittest.TestCase):
    def test_center_is_unchanged_by_default(self):
        default = awards.draw_award_badge(_poster(), "Oscar Winner")
        centre = awards.draw_award_badge(_poster(), "Oscar Winner", position="center")
        np.testing.assert_array_equal(np.asarray(default), np.asarray(centre))
        self.assertGreater(_changed(default, (240, 0, 260, 20)), 0)

    def test_sides_leave_the_centre_and_far_corner_alone(self):
        for pos, near, far in (("left", (0, 0, 60, 80), (440, 0, 500, 80)),
                               ("right", (440, 0, 500, 80), (0, 0, 60, 80))):
            with self.subTest(pos=pos):
                out = awards.draw_award_badge(_poster(), "#12 Today", position=pos)
                self.assertGreater(_changed(out, near), 0)
                self.assertEqual(_changed(out, far), 0)
                self.assertEqual(_changed(out, (245, 0, 255, 120)), 0)

    def test_chip_floats_in_from_the_corner(self):
        chip = awards.draw_award_badge(_poster(), "#12 Today", position="left")
        self.assertEqual(_changed(chip, (0, 0, 8, 8)), 0)

    def test_right_mirrors_left_with_a_blank_label(self):
        a = awards.draw_award_badge(_poster(), " ", position="left")
        b = awards.draw_award_badge(_poster(), " ", position="right")
        diff = np.abs(np.asarray(a.transpose(Image.Transpose.FLIP_LEFT_RIGHT), dtype=np.int16)
                      - np.asarray(b, dtype=np.int16))
        # Integer placement can shift the mirror by a pixel at the edge.
        self.assertLess((diff.max(axis=2) > 8).mean(), 0.002)

    def test_other_styles_stay_centred(self):
        out = awards.draw_award_badge(_poster(), "#12 Today", notch_style="black", position="left")
        self.assertGreater(_changed(out, (245, 0, 255, 20)), 0)
        self.assertEqual(_changed(out, (0, 0, 60, 80)), 0)

    def test_negative_inset_does_not_raise(self):
        for pos in ("left", "right"):
            with self.subTest(pos=pos):
                awards.draw_award_badge(_poster(), "#12 Today", position=pos, notch_inset=-0.02)


class NotchPositionConfigTests(unittest.TestCase):
    def test_parses_positions_and_ignores_junk(self):
        self.assertEqual(main.build_request_config({}).sash_badge_pos, "center")
        self.assertEqual(main.build_request_config({"sash_badge_pos": "Right"}).sash_badge_pos, "right")
        self.assertEqual(main.build_request_config({"sash_badge_pos": "corner_left"}).sash_badge_pos, "center")
        self.assertEqual(main.build_request_config({"sash_badge_pos": "auto"}).sash_badge_pos, "auto")
        self.assertEqual(main.build_request_config({"sash_badge_pos": "middle"}).sash_badge_pos, "center")

    def test_bookmark_steps_right_for_a_left_notch(self):
        cfg = lambda **p: main.build_request_config({"sash_mode": "notch", **p})
        self.assertTrue(main._sash_holds_left(cfg(sash_badge_pos="left")))
        self.assertFalse(main._sash_holds_left(cfg(sash_badge_pos="right")))
        self.assertFalse(main._sash_holds_left(cfg(sash_badge_pos="left", sash_badge_style="gold")))
        self.assertTrue(main._sash_holds_left(main.build_request_config(
            {"sash_mode": "sash", "sash_side": "left"})))


if __name__ == "__main__":
    unittest.main()
