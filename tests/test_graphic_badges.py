"""Graphic badge row (badge_display_mode=7): marks from pinned Commons files,
text boxes, the US certificate, and the row's layout around the chip."""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import graphic_badges as gb
from discovery import DiscoveryMeta
import main
import tmdb


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return json.loads(self.content)


class _Client:
    def __init__(self, bodies):
        self.bodies, self.urls = bodies, []

    async def get(self, url, **_kw):
        self.urls.append(url)
        return _Resp(self.bodies.get(url.rsplit("/", 1)[1], b"nope"))


class AssetFetchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(gb, "ASSET_DIR", self.dir.name)
        p.start()
        self.addCleanup(p.stop)
        for p in (mock.patch.dict(gb._failed_at, clear=True), mock.patch.object(gb, "_gone", set())):
            p.start()
            self.addCleanup(p.stop)
        gb._marks.cache_clear()
        self.addCleanup(gb._marks.cache_clear)

    def test_a_file_that_no_longer_matches_its_hash_is_not_used(self):
        # Every body is b"nope": no pinned SHA-1, and the history lookup fails
        # too, so each file is backed off rather than asked for per render.
        client = _Client({})
        self.assertFalse(asyncio.run(gb.ensure_assets(client)))
        self.assertEqual(os.listdir(self.dir.name), [])
        self.assertEqual(len(client.urls), 2 * len(gb._FILES))
        self.assertFalse(asyncio.run(gb.ensure_assets(client)))
        self.assertEqual(len(client.urls), 2 * len(gb._FILES))

    def _history(self, sha1_for):
        """Commons' API answer: each file's one revision, hashed by sha1_for."""
        return {"api.php": json.dumps({"query": {"pages": [{"imageinfo": [
            {"sha1": sha1_for, "url": "https://upload.wikimedia.org/archive/old.svg"}]}]}}).encode(),
            "old.svg": b"old"}

    def test_a_reuploaded_file_is_fetched_from_its_pinned_revision(self):
        sha = hashlib.sha1(b"old").hexdigest()
        with mock.patch.dict(gb._FILES, {k: gb._CommonsFile(f.title, sha) for k, f in gb._FILES.items()}):
            client = _Client(self._history(sha))
            self.assertTrue(asyncio.run(gb.ensure_assets(client)))
            self.assertTrue(all(os.path.exists(gb._asset_path(k)) for k in gb._FILES))
            self.assertIn("https://upload.wikimedia.org/archive/old.svg", client.urls)

    def test_a_file_with_no_matching_revision_is_given_up_on(self):
        client = _Client(self._history("0" * 40))
        self.assertTrue(asyncio.run(gb.ensure_assets(client)))   # nothing left worth waiting for
        self.assertEqual(os.listdir(self.dir.name), [])
        n = len(client.urls)
        self.assertTrue(asyncio.run(gb.ensure_assets(client)))
        self.assertEqual(len(client.urls), n)

    def test_matching_files_are_kept_and_not_fetched_again(self):
        bodies = {}
        with mock.patch.dict(gb._FILES, {k: gb._CommonsFile(f.title, hashlib.sha1(k.encode()).hexdigest())
                                         for k, f in gb._FILES.items()}):
            for k, f in gb._FILES.items():
                bodies[f.title.replace(" ", "_")] = k.encode()
            client = _Client(bodies)
            self.assertTrue(asyncio.run(gb.ensure_assets(client)))
            self.assertTrue(asyncio.run(gb.ensure_assets(client)))
            self.assertEqual(len(client.urls), len(gb._FILES))


class RowItemTests(unittest.TestCase):
    def setUp(self):
        # No Commons files: the marks are absent and only boxes are drawn.
        p = mock.patch.object(gb, "_marks", lambda: {})
        p.start()
        self.addCleanup(p.stop)
        gb._mark.cache_clear()
        self.addCleanup(gb._mark.cache_clear)

    def slots(self, *a, **kw):
        return [slot for slot, _ in gb.row_items(*a, **kw)]

    def test_boxes_and_certificate_in_group_order(self):
        self.assertEqual(self.slots(["4K", "HDR10+"], "PG-13", None, 30), ["video", "res", "cert"])
        self.assertEqual(self.slots(["4K", "HDR10+"], "PG-13", None, 30, ("cert", "res")), ["cert", "res"])
        self.assertEqual(self.slots(["1080P"], "tv-ma", None, 30), ["res", "cert"])

    def test_certificate_falls_back_to_the_age_and_ignores_unknowns(self):
        self.assertEqual(self.slots([], None, 14, 30), ["cert"])
        self.assertEqual(self.slots([], "Unrated", None, 30), [])

    def test_quality_gate_keeps_the_certificate(self):
        self.assertEqual(self.slots(["4K", "HDR10"], "R", None, 30, show_quality=False), ["cert"])

    def test_missing_marks_are_left_out(self):
        self.assertEqual(self.slots(["4K", "DV", "ATMOS"], "R", None, 30), ["res", "cert"])


class CinemaBadgeTests(unittest.TestCase):
    def test_the_disc_shows_the_home_date_else_an_icon(self):
        import datetime
        dated = gb.CinemaRun("Cinema", datetime.date(2026, 10, 16))
        self.assertEqual(gb.cinema_ink("auto", dated), "disc|auto|OCT 16")
        # Dated wins even in production.
        self.assertEqual(gb.cinema_ink("auto", gb.CinemaRun("Production", datetime.date(2027, 1, 2))),
                         "disc|auto|JAN 2")
        self.assertEqual(gb.cinema_ink("auto", gb.CinemaRun("Cinema")), "disc|auto|popcorn")
        self.assertEqual(gb.cinema_ink("auto", gb.CinemaRun("Production")), "disc|auto|clapper")
        self.assertEqual(gb.cinema_ink("frosted", dated, (10.4, 20, 30)), "disc|rgb:10,20,30|OCT 16")
        # Frosted before the tint is sampled: laid out as auto (same size).
        self.assertEqual(gb.cinema_ink("frosted", dated), "disc|auto|OCT 16")
        self.assertIsNone(gb.cinema_ink("auto", None))

    def test_every_face_holds_the_row_height_and_its_slot(self):
        for face in ("OCT 16", "popcorn", "clapper"):
            with self.subTest(face=face):
                mark = gb._cinema_mark(f"disc|auto|{face}", 33)
                self.assertEqual(mark.size, (33, 33))
                # The face is drawn: more than a bare disc.
                face_px = np.asarray(mark.convert("L"), dtype=np.int16)
                self.assertGreater(int((face_px > 150).sum()), 20)
        slots = [s for s, _ in gb.row_items([], "R", None, 30, ("cinema", "cert"), cinema="disc|auto|popcorn")]
        self.assertEqual(slots, ["cinema", "cert"])
        self.assertEqual([s for s, _ in gb.row_items([], "R", None, 30, ("cinema", "cert"))], ["cert"])

    def test_the_disc_takes_its_tone_from_the_art(self):
        def drawn(bg, key="disc|auto|OCT 16"):
            poster = Image.new("RGBA", (200, 100), (*bg, 255))
            gb.draw_row(poster, [("cinema", gb._cinema_mark(key, 40))], left_x=80, center_y=50, gap=0)
            return np.asarray(poster.convert("L"), dtype=np.float32)[40:60, 85:115].mean()
        self.assertGreater(drawn((10, 10, 12)), 150)     # light disc on dark art
        self.assertLess(drawn((240, 240, 240)), 100)     # dark disc on light art
        # Pale glass over dark blue art (bg luma ~55), under a solid dark glyph.
        self.assertGreater(drawn((20, 60, 160), "disc|rgb:200,210,240|popcorn"), 90)
        # No border: the rim is the body's tone, not a white keyline.
        rim = gb._gradient_disc("OCT 16", 40, False)
        self.assertLess(np.asarray(rim.convert("RGB"))[20, 1].max(), 120)

    def test_the_slot_parses_and_the_style_is_read(self):
        self.assertEqual(gb.parse_group("tr:2:cinema,cert").slots, ("cinema", "cert"))
        read = lambda v: main.build_request_config({"badge_cinema_style": v}).badge_cinema_style
        self.assertEqual(read("Frosted"), "frosted")
        # The popcorn's old colours still parse, as the disc's auto look.
        for old in ("timing", "red", "black", "white"):
            self.assertEqual(read(old), "auto")
        self.assertEqual(read("pink"), "auto")


class MarkTests(unittest.TestCase):
    """Stand-in lockups, so the Dolby choices run without the Commons files."""

    def slots(self, slots):
        a = np.zeros((60, 200), dtype=np.uint8)
        a[5:25, 10:190] = 255
        with mock.patch.object(gb, "_marks", lambda: {"DV": a, "ATMOS": a, "DV+ATMOS": a}):
            gb._mark.cache_clear()
            try:
                return [s for s, _ in gb.row_items(["4K", "DV", "ATMOS"], "R", None, 30, slots)]
            finally:
                gb._mark.cache_clear()

    def test_combined_lockup_when_video_and_audio_share_a_group(self):
        self.assertEqual(self.slots(("video", "audio", "res", "cert")), ["video", "res", "cert"])

    def test_separate_marks_when_they_do_not(self):
        self.assertEqual(self.slots(("audio", "cert")), ["audio", "cert"])
        self.assertEqual(self.slots(("video",)), ["video"])

    def test_set_line_handles_bullet_spaces(self):
        g = np.full((10, 4), 255, dtype=np.uint8)
        line = gb._set_line([g, None, g, None, g], 2)
        self.assertEqual(line.shape, (10, 3 * 4 + 2 * 2 + 2 * int(2 * 0.6)))


class SameHeightTests(unittest.TestCase):
    def test_every_badge_inks_the_full_row_height(self):
        a = np.zeros((60, 200), dtype=np.uint8)
        a[:, 10:190] = 255
        with mock.patch.object(gb, "_marks", lambda: {"DV": a, "ATMOS": a, "DTSX": a}):
            gb._mark.cache_clear()
            try:
                items = (gb.row_items(["4K", "DV", "DTSX"], "PG-13", None, 30)
                         + gb.row_items(["1080P", "HDR10+", "ATMOS"], None, 16, 30))
            finally:
                gb._mark.cache_clear()
        self.assertEqual(len(items), 8)
        for slot, im in items:
            rows = np.flatnonzero(np.asarray(im)[..., 3].max(axis=1) > 40)
            self.assertEqual((rows.min(), rows.max()), (0, 29), slot)


class DtsCutTests(unittest.TestCase):
    def test_keeps_the_letters_and_drops_the_overlapping_x(self):
        a = np.zeros((40, 100), dtype=np.uint8)
        a[5:35, 0:50] = 255          # "dts": one joined shape
        a[0:40, 45:100] = 0
        a[2:38, 46:100] = 200        # the X, starting under the letters' right edge
        a[5:35, 45] = 0              # ...but not touching them
        out = gb._dts_letters(a)
        self.assertEqual(out.shape, (30, 45))
        self.assertEqual(int(out[:, 44].max()), 255)


class GroupParseTests(unittest.TestCase):
    def test_round_trip_and_clean_up(self):
        self.assertEqual(gb.format_group(gb.parse_group("BL:9:cert,Res,cert,bogus")), "bl:4:cert,res")
        self.assertIsNone(gb.parse_group("middle:2:cert"))
        self.assertIsNone(gb.parse_group("tr:2:"))
        self.assertIsNone(gb.parse_group(""))

    def test_custom_position(self):
        g = gb.parse_group("0.0500,0.9,r:2:cert")
        self.assertEqual((g.anchor, g.xy, g.align), ("custom", (0.05, 0.9), "r"))
        self.assertEqual(gb.format_group(g), "0.05,0.9,r:2:cert")
        # Without an align, the nearer edge decides.
        self.assertEqual([gb.parse_group(f"{x},0.5:1:cert").align for x in (0.2, 0.5, 0.8)], ["l", "c", "r"])
        for bad in ("1.2,0.5:2:cert", "nan,0.5:2:cert", "0.5:2:cert", "a,b:2:cert",
                    "0.1,0.2,0.3:2:cert", "0.1,0.2,x:2:cert"):
            self.assertIsNone(gb.parse_group(bad), bad)

    def test_size_and_spacing_fields(self):
        g = gb.parse_group("bl:2:cert:28:0.01")
        self.assertEqual((g.size, g.spacing), (28, 0.01))
        self.assertEqual(gb.format_group(g), "bl:2:cert:28:0.01")
        self.assertEqual(gb.format_group(gb.parse_group("bl:2:cert:28")), "bl:2:cert:28")
        self.assertEqual(gb.format_group(gb.parse_group("bl:2:cert:22:0.028")), "bl:2:cert")  # defaults left out
        self.assertEqual(gb.format_group(gb.parse_group("bl:2:cert:22:0.05")), "bl:2:cert:22:0.05")
        self.assertEqual(gb.parse_group("bl:2:cert:999:1").size, 60)
        self.assertEqual(gb.parse_group("bl:2:cert:999:1").spacing, 0.08)
        self.assertEqual(gb.parse_group("0.1,0.2,r:2:cert:30").size, 30)
        for bad in ("bl:2:cert:big", "bl:2:cert:20:nan", "bl:2:cert:20:0.02:x", "bl:2:cert:inf"):
            self.assertIsNone(gb.parse_group(bad), bad)

    def test_a_badge_stays_in_the_first_group(self):
        groups = gb.resolve_groups("chip:4:video,cert", "bl:2:cert,res")
        self.assertEqual([g.slots for g in groups], [("video", "cert"), ("res",)])
        self.assertEqual([g.slots for g in gb.resolve_groups("tr:1:cert", "bl:1:cert")], [("cert",)])


class LayoutTests(unittest.TestCase):
    def box(self, w):
        return ("x", Image.new("RGBA", (w, 20), (255, 255, 255, 255)))

    def test_fit_keeps_the_longest_prefix(self):
        items = [self.box(100), self.box(100), self.box(40)]
        self.assertEqual(len(gb.fit(items, 210, 10)), 2)
        self.assertEqual(len(gb.fit(items, 99, 10)), 0)

    def test_free_run_from_either_edge(self):
        cols = np.zeros(100, dtype=bool)
        cols[60:70] = True
        self.assertEqual(gb.free_run(cols, right=False, margin=5), 55)
        self.assertEqual(gb.free_run(cols, right=True, margin=5), 25)
        self.assertEqual(gb.free_run(np.zeros(100, dtype=bool), right=True, margin=5), 95)


class CertificationParseTests(unittest.TestCase):
    def test_movie_prefers_the_theatrical_certificate(self):
        body = {"results": [
            {"iso_3166_1": "GB", "release_dates": [{"type": 3, "certification": "15"}]},
            {"iso_3166_1": "US", "release_dates": [
                {"type": 1, "certification": ""},
                {"type": 4, "certification": "NR"},
                {"type": 3, "certification": "PG-13"},
            ]},
        ]}
        self.assertEqual(tmdb.us_certification_from_release_dates(body), "PG-13")
        self.assertEqual(tmdb.us_certification_from_release_dates({"results": []}), "")

    def test_tv(self):
        body = {"results": [{"iso_3166_1": "DE", "rating": "16"}, {"iso_3166_1": "US", "rating": "TV-MA"}]}
        self.assertEqual(tmdb.us_certification_from_content_ratings(body), "TV-MA")


class AutoNotchTests(unittest.TestCase):
    """sash_badge_pos=auto: beside the top badges when there are any, centred
    when there are none."""

    def pos(self, tokens=("4K",), cert="R", **params):
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "notch",
                                         "sash_badge_pos": "auto", "badge_min_score": "0", **params})
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            try:
                return main._auto_notch_pos(cfg, list(tokens), cert, None)
            finally:
                gb._mark.cache_clear()

    def test_parses(self):
        self.assertEqual(main.build_request_config({"sash_badge_pos": "Auto"}).sash_badge_pos, "auto")

    def test_beside_the_badges(self):
        self.assertEqual(self.pos(), "left")                                  # chip group goes right
        self.assertEqual(self.pos(badge_group1="tr:2:res,cert"), "left")
        self.assertEqual(self.pos(badge_group1="tl:2:res,cert"), "right")
        self.assertEqual(self.pos(badge_group1="0.95,0.05,r:2:res,cert"), "left")

    def test_centred_when_the_top_has_nothing(self):
        self.assertEqual(self.pos(tokens=(), cert=None), "center")            # nothing to show
        self.assertEqual(self.pos(badge_group1="bl:2:res,cert"), "center")    # only at the bottom
        self.assertEqual(self.pos(badge_group1="0.9,0.9,r:2:res,cert"), "center")
        self.assertEqual(self.pos(badge_group1="tl:1:res", badge_group2="tr:1:cert"), "center")

    def test_only_with_graphic_badges(self):
        self.assertEqual(self.pos(badge_display_mode="4"), "center")

    def test_every_notch_style_moves_alike(self):
        frosted = self.pos()
        for style in ("black", "silver", "gold"):
            with self.subTest(style=style):
                self.assertEqual(self.pos(sash_badge_style=style), frosted)

    def test_dark_styles_draw_a_side_chip(self):
        img = Image.new("RGBA", (500, 750), (120, 140, 160, 255))
        for style in ("black", "silver", "gold"):
            with self.subTest(style=style):
                def draw(mode):
                    cfg = main.build_request_config({"sash_mode": mode, "sash_badge_style": style,
                                                     "sash_badge_pos": "left", "rating_display_mode": "0"})
                    return np.asarray(main.build_poster(
                        img.copy(), 80, "Drama", cfg,
                        discovery_meta=DiscoveryMeta(award_wins=["Oscar Winner"])))[:120, :, :3].astype(int)
                # Against the same poster without it, so the gradients drop out.
                changed = np.abs(draw("notch") - draw("hidden")).sum(2) > 30
                cols = changed.any(0)
                self.assertTrue(cols[25:60].all())      # a chip in from the left edge
                self.assertFalse(cols[:15].any())       # floated in by the margin
                self.assertFalse(cols[245:256].any())   # nothing left at the centre

    def test_the_render_draws_the_resolved_position(self):
        img = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "notch",
                                         "sash_badge_pos": "auto", "badge_min_score": "0",
                                         "rating_display_mode": "0"})
        # The notch is drawn at the position auto resolved to.
        seen = {}
        real = main.draw_award_badge
        def spy(image, label, **kw):
            seen["position"] = kw.get("position")
            return real(image, label, **kw)
        with mock.patch.object(main, "draw_award_badge", spy), \
             mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            try:
                main.build_poster(img, 80, "Drama", cfg, quality_tokens=["4K"], certification="R",
                                  discovery_meta=DiscoveryMeta(award_wins=["Oscar Winner"]))
            finally:
                gb._mark.cache_clear()
        self.assertEqual(seen.get("position"), "left")


class SpreadBesideChipTests(unittest.TestCase):
    """An auto notch that became a side chip spreads its chip group evenly."""

    def ink_runs(self, spread, chip=(22, 22, 200, 62), pos="left", group="chip:2:res,cert:20"):
        img = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        before = np.array(img)
        img.paste((200, 50, 50, 255), chip)            # stand-in chip
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "notch",
                                         "sash_badge_pos": pos, "badge_group1": group})
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            main._draw_graphic_badges(img, cfg, ["4K"], "PG-13", None, before,
                                      spread_beside_chip="spread" if spread else None)
            gb._mark.cache_clear()
        a = np.asarray(img)
        cols = ((a[..., 1] > 180) & (a[..., 2] > 180))[:120].any(axis=0)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], cols.astype(np.int8), [0]))))
        return list(zip(edges[::2], edges[1::2]))      # (start, end) of each badge

    def test_equal_gaps_from_the_chip_and_last_on_the_margin(self):
        (a0, a1), (b0, b1) = self.ink_runs(True)
        self.assertAlmostEqual(a0 - 200, b0 - a1, delta=2)    # chip->first == first->second
        self.assertAlmostEqual(b1, 478, delta=2)              # on the right margin (500 - 22)

    def test_mirrored_for_a_right_chip(self):
        (a0, a1), (b0, b1) = self.ink_runs(True, chip=(300, 22, 478, 62), pos="right")
        self.assertAlmostEqual(a0, 22, delta=2)
        self.assertAlmostEqual(b0 - a1, 300 - b1, delta=2)

    def heights(self, chip, group="chip:2:res,cert:20"):
        img = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        before = np.array(img)
        img.paste((200, 50, 50, 255), chip)
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "notch",
                                         "sash_badge_pos": "left", "badge_group1": group})
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            main._draw_graphic_badges(img, cfg, ["4K"], "PG-13", None, before, spread_beside_chip="spread")
            gb._mark.cache_clear()
        a = np.asarray(img)
        ink = ((a[..., 1] > 180) & (a[..., 2] > 180))[:150]
        rows = np.flatnonzero(ink.any(axis=1))
        return rows.max() - rows.min() + 1, int(ink.any(axis=0).sum())

    def test_badges_grow_into_room_up_to_1_2x(self):
        h, _ = self.heights((22, 22, 120, 62))
        self.assertGreater(h, 30)                 # bigger than the set size (20 -> 30 px)
        self.assertLessEqual(h, 36)               # but at most 1.2x

    def test_badges_shrink_before_any_is_dropped(self):
        # Room for both only below the set size: both drawn, smaller.
        h, _ = self.heights((22, 22, 360, 62))
        self.assertLess(h, 30)
        self.assertGreaterEqual(h, 21)            # not below 70 %
        runs = self.ink_runs(True, chip=(22, 22, 360, 62))
        self.assertEqual(len(runs), 2)

    def test_only_when_asked(self):
        (_, a1), (b0, _) = self.ink_runs(False)
        self.assertAlmostEqual(b0 - a1, 14, delta=2)          # the group's own spacing

    def test_no_chip_on_the_poster_falls_back(self):
        runs = self.ink_runs(True, chip=(0, 0, 1, 1))
        self.assertAlmostEqual(runs[1][0] - runs[0][1], 14, delta=2)


class LogoAnchorTests(unittest.TestCase):
    def render(self, anchor, logo_size=(240, 90), extra=None, logo=True):
        """A real build_poster with a plain white logo (and, optionally, a
        stand-in obstacle drawn after it)."""
        img = Image.new("RGBA", (500, 750), (40, 60, 90, 255))
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "hidden",
                                         "rating_display_mode": "0", "badge_min_score": "0",
                                         "top_gradient": "none", "bottom_gradient": "none",
                                         "badge_group1": f"{anchor}:2:res,cert"})
        logo_im = Image.new("RGBA", logo_size, (255, 255, 255, 255)) if logo else None
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            try:
                out = main.build_poster(img, 80, "Drama", cfg, logo=logo_im,
                                        quality_tokens=["4K"], certification="R")
            finally:
                gb._mark.cache_clear()
        a = np.asarray(out).astype(int)
        white = (a[..., :3] > 200).all(axis=2)   # badges are ~92% opaque
        rows = np.flatnonzero(white.any(axis=1))
        return white, rows

    def logo_rows(self, white):
        # The logo is the widest white run; badges are much narrower.
        widths = white.sum(axis=1)
        return np.flatnonzero(widths > 150)

    def test_above_and_below_follow_the_logo(self):
        for size in ((240, 90), (240, 40)):
            with self.subTest(size=size):
                white, rows = self.render("above_logo", size)
                logo = self.logo_rows(white)
                badges = rows[rows < logo.min()]
                self.assertTrue(badges.size)
                self.assertLess(logo.min() - badges.max(), 30)       # just above it
                white, rows = self.render("below_logo", size)
                logo = self.logo_rows(white)
                badges = rows[rows > logo.max()]
                self.assertTrue(badges.size)
                self.assertLess(badges.min() - logo.max(), 30)       # just below it

    def test_centred_on_the_logo(self):
        white, rows = self.render("above_logo")
        logo = self.logo_rows(white)
        cols = np.flatnonzero(white[:logo.min()].any(axis=0))
        logo_cols = np.flatnonzero(white[logo].any(axis=0))
        self.assertAlmostEqual((cols.min() + cols.max()) / 2, (logo_cols.min() + logo_cols.max()) / 2, delta=3)

    def test_no_logo_sits_bottom_centre(self):
        white, rows = self.render("below_logo", logo=False)
        self.assertGreater(rows.min(), 650)
        cols = np.flatnonzero(white.any(axis=0))
        self.assertAlmostEqual((cols.min() + cols.max()) / 2, 250, delta=3)


class NetworkStudioTests(unittest.TestCase):
    def test_pick_logos(self):
        tv = {"networks": [{"id": 1, "logo_path": None}, {"id": 213, "logo_path": "/n.png"}],
              "companies": [{"id": 999, "logo_path": "/x.png"}, {"id": 3, "logo_path": "/pixar.png"}]}
        net, studio, streamer = gb.pick_logos(tv, "tv")
        self.assertEqual((net.id, studio.id, streamer), (213, 3, None))
        # A film: no network of its own; a streamer studio maps to one, and
        # studios off the curated list are passed over.
        film = {"networks": [], "companies": [{"id": 178464, "logo_path": "/nf.png"},
                                               {"id": 88152, "logo_path": "/cre.png"}]}
        self.assertEqual(gb.pick_logos(film, "movie"), (None, None, 213))
        self.assertEqual(gb.pick_logos(None, "movie"), (None, None, None))

    def plate(self, fg, bg, box=(10, 10, 90, 40), text=(30, 18, 70, 32), text_rgb=(255, 255, 255)):
        im = Image.new("RGBA", (100, 50), (*bg, 0))
        im.paste((*fg, 255), box)
        if text:
            im.paste((*text_rgb, 255), text)
        return im

    def test_a_solid_block_keeps_its_lettering(self):
        # Red box, white lettering (Marvel Studios): the lettering is cut out.
        a = gb.logo_alpha(self.plate((200, 20, 30), (0, 0, 0)))
        self.assertEqual(a.shape, (30, 80))
        self.assertGreater(int(a[2, 2]), 200)          # the box
        self.assertLess(int(a[15, 40]), 20)            # the lettering, cut out

    def test_light_lettering_is_not_cut_out(self):
        # A logo that is itself light lettering stays whole.
        a = gb.logo_alpha(self.plate((240, 240, 240), (0, 0, 0), text=None))
        self.assertGreater(int(a.min()), 200)

    def test_an_opaque_plate_takes_its_shape_from_contrast(self):
        im = Image.new("RGBA", (100, 50), (255, 255, 255, 255))
        im.paste((20, 20, 20, 255), (20, 10, 80, 40))
        a = gb.logo_alpha(im)
        self.assertEqual(a.shape, (30, 60))

    def test_row_items_include_the_logos(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gb, "LOGO_DIR", d), \
             mock.patch.object(gb, "_marks", lambda: {}):
            gb._logo_mark.cache_clear(); gb._mark.cache_clear()
            net = gb.Logo("network", 213, "/n.png")
            logo = Image.new("RGBA", (140, 60), (0, 0, 0, 0))
            logo.paste((229, 9, 20, 255), (10, 10, 130, 50))
            logo.save(os.path.join(d, "network_213_n.png"))
            items = gb.row_items([], "R", None, 30, ("network", "studio", "cert"), True,
                                 network=net, studio=gb.Logo("company", 3, "/missing.png"))
            gb._logo_mark.cache_clear(); gb._mark.cache_clear()
        self.assertEqual([s for s, _ in items], ["network", "cert"])   # studio file absent: left out
        self.assertLessEqual(items[0][1].height, 30)

    def test_logos_are_sized_by_area_around_the_row(self):
        row = 30
        # A compact emblem stands taller than the row, up to the cap.
        self.assertEqual(gb.logo_size((100, 100), row), (40, 40))
        self.assertLessEqual(gb.logo_size((100, 100), row, fill=0.05)[1], round(row * gb._LOGO_MAX_H))
        # A long wordmark comes out shorter and no wider than the cap.
        w, h = gb.logo_size((100, 800), row)
        self.assertLess(h, 20)
        self.assertLessEqual(w, round(row * gb._LOGO_MAX_W))
        # Similar area whatever the shape, until a limit applies.
        areas = [w * h for w, h in (gb.logo_size((100, a), row) for a in (150, 250, 350))]
        for a in areas[1:]:
            self.assertAlmostEqual(areas[0] / a, 1, delta=0.08)

    def test_logos_are_weighed_by_their_ink(self):
        row = 30
        typical = gb.logo_size((100, 250), row)
        solid = gb.logo_size((100, 250), row, fill=0.9)
        thin = gb.logo_size((100, 250), row, fill=0.2)
        self.assertLess(solid[0], typical[0])
        self.assertGreater(thin[0], typical[0])
        # Half way to equal ink, so a solid block still carries more ink.
        self.assertGreater(solid[0] * solid[1] * 0.9, thin[0] * thin[1] * 0.2)
        # Within limits: a hairline logo isn't blown up without bound.
        self.assertEqual(gb.logo_size((100, 250), row, fill=0.01),
                         gb.logo_size((100, 250), row, fill=0.45 / gb._LOGO_FILL_LIMITS[1] ** 2))

    def test_logo_scale_multiplies_the_size(self):
        for shape in ((100, 100), (100, 250), (100, 800)):
            w, h = gb.logo_size(shape, 30)
            w2, h2 = gb.logo_size(shape, 30, scale=1.5)
            self.assertAlmostEqual(w2 / w, 1.5, delta=0.06)
            self.assertAlmostEqual(h2 / h, 1.5, delta=0.1)

    def test_logo_scale_reaches_the_row(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gb, "LOGO_DIR", d):
            gb._logo_mark.cache_clear()
            net = gb.Logo("network", 213, "/n.png")
            logo = Image.new("RGBA", (140, 60), (0, 0, 0, 0))
            logo.paste((255, 255, 255, 255), (10, 10, 130, 50))
            logo.save(os.path.join(d, "network_213_n.png"))
            base = gb.row_items([], None, None, 30, ("network",), True, network=net)[0][1]
            big = gb.row_items([], None, None, 30, ("network",), True, network=net,
                               logo_scale=2.0)[0][1]
            gb._logo_mark.cache_clear()
        self.assertAlmostEqual(big.width / base.width, 2.0, delta=0.05)

    def test_logo_scale_param_is_clamped(self):
        self.assertEqual(main.build_request_config({}).badge_logo_scale, 1.0)
        self.assertEqual(main.build_request_config({"badge_logo_scale": "1.3"}).badge_logo_scale, 1.3)
        self.assertEqual(main.build_request_config({"badge_logo_scale": "9"}).badge_logo_scale,
                         gb.LOGO_SCALE_RANGE[1])
        self.assertEqual(main.build_request_config({"badge_logo_scale": "x"}).badge_logo_scale, 1.0)


class QualityNeedTests(unittest.TestCase):
    """Only layouts that show quality should fetch, wait for or hold a render
    back over it."""

    def uses(self, **params):
        return main._uses_quality(main.build_request_config(params))

    def test_quality_modes(self):
        for mode in ("1", "2", "4", "5", "6"):
            self.assertTrue(self.uses(badge_display_mode=mode), mode)
        for mode in ("0", "3"):
            self.assertFalse(self.uses(badge_display_mode=mode), mode)

    def test_graphic_badges_only_when_a_group_shows_quality(self):
        self.assertTrue(self.uses(badge_display_mode="7"))                    # default group has video
        self.assertFalse(self.uses(badge_display_mode="7", badge_group1="chip:3:cert,network,studio"))
        self.assertTrue(self.uses(badge_display_mode="7", badge_group1="chip:3:cert,network",
                                  badge_group3="bl:1:res"))

    def test_no_greyscale_for_missing_quality_nothing_shows(self):
        img = Image.new("RGBA", (500, 750), (200, 40, 40, 255))
        cfg = main.build_request_config({"badge_display_mode": "7", "badge_group1": "chip:2:cert,network",
                                         "wait_for_quality": "true", "greyscale_no_quality": "true",
                                         "rating_display_mode": "0", "sash_mode": "hidden"})
        with mock.patch.object(gb, "_marks", lambda: {}):
            out = main.build_poster(img, 80, "Drama", cfg, quality_tokens=[])
        r, g, b = out.convert("RGB").getpixel((250, 400))
        self.assertGreater(r - g, 50)                                         # still in colour


class LogoShrinkTests(unittest.TestCase):
    """A long logo that doesn't fit beside what shares its line (Minimalist's
    genre and year) is shrunk to stay on that line, not moved off it."""

    def draw(self, text_left):
        img = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        before = np.array(img)
        img.paste((200, 50, 50, 255), (text_left, 700, 480, 730))   # the "genre | year"
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "hidden",
                                         "badge_group1": "bl:1:network"})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gb, "LOGO_DIR", d):
            gb._logo_mark.cache_clear()
            net = gb.Logo("network", 1, "/mx.png")
            logo = Image.new("RGBA", (820, 120), (0, 0, 0, 0))
            logo.paste((255, 255, 255, 255), (10, 10, 810, 110))  # TOKYO MX: 8:1
            logo.save(os.path.join(d, "network_1_mx.png"))
            full = gb.row_items([], None, None, 33, ("network",), True, network=net)[0][1].width
            main._draw_graphic_badges(img, cfg, [], None, None, before, logos=(net, None))
            gb._logo_mark.cache_clear()
        white = (np.asarray(img)[..., :3] > 200).all(axis=2)
        rows, cols = np.flatnonzero(white.any(axis=1)), np.flatnonzero(white.any(axis=0))
        return full, rows, cols

    def test_shrinks_to_stay_on_its_line(self):
        full, rows, cols = self.draw(text_left=140)
        self.assertGreater(rows.min(), 650)                 # still down by the text
        self.assertLess(cols.max(), 140)                    # clear of it
        self.assertLess(cols.max() - cols.min() + 1, full)  # by being drawn smaller

    def test_full_size_where_there_is_room(self):
        full, rows, cols = self.draw(text_left=400)
        self.assertAlmostEqual(cols.max() - cols.min() + 1, full, delta=2)


class HugChipTests(unittest.TestCase):
    def test_starts_against_the_chip_and_leaves_the_corner(self):
        img = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        before = np.array(img)
        img.paste((200, 50, 50, 255), (22, 22, 200, 62))
        cfg = main.build_request_config({"badge_display_mode": "7", "sash_mode": "notch",
                                         "sash_badge_pos": "left", "badge_group1": "chip:1:cert"})
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            main._draw_graphic_badges(img, cfg, [], "PG-13", None, before, spread_beside_chip="hug")
            gb._mark.cache_clear()
        a = np.asarray(img)
        cols = np.flatnonzero(((a[..., 1] > 180) & (a[..., 2] > 180))[:120].any(axis=0))
        self.assertAlmostEqual(cols.min(), 200 + 14, delta=2)      # one spacing off the chip
        self.assertLess(cols.max(), 330)                            # the right corner stays clear

    def test_auto_hug_parses_and_resolves_like_auto(self):
        self.assertEqual(main.build_request_config({"sash_badge_pos": "auto_hug"}).sash_badge_pos, "auto_hug")


class ConfigAndLayoutTests(unittest.TestCase):
    def test_mode_and_groups_parse(self):
        cfg = main.build_request_config({"badge_display_mode": "7", "badge_group2": "BR:2:cert"})
        self.assertEqual(cfg.badge_display_mode, 7)
        self.assertEqual(cfg.badge_group1, gb.DEFAULT_GROUP1)
        self.assertEqual(cfg.badge_group2, "br:2:cert")
        self.assertEqual(main.build_request_config({"badge_group1": "nope"}).badge_group1, "")

    def render(self, occupy=None, **params):
        """Group placement on a flat poster; ``occupy`` is a box the other
        overlays 'drew', so the groups have to avoid it."""
        img = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        before = np.array(img)
        if occupy:
            img.paste((200, 50, 50, 255), occupy)
        cfg = main.build_request_config({"badge_display_mode": "7", **params})
        with mock.patch.object(gb, "_marks", lambda: {}):
            gb._mark.cache_clear()
            main._draw_graphic_badges(img, cfg, ["4K"], "R", None, before)
            gb._mark.cache_clear()
        # The badges are near-white; the stand-in obstacle is red.
        a = np.asarray(img)
        return (a[..., 1] > 180) & (a[..., 2] > 180)

    def test_beside_chip_takes_the_free_corner(self):
        ink = self.render(badge_group1="chip:4:video,audio,res,cert", sash_mode="notch", sash_badge_pos="right")
        self.assertTrue(ink[:120, :250].any())
        self.assertFalse(ink[:, 250:].any())

    def test_bottom_group_sits_on_the_bottom_margin(self):
        ink = self.render(badge_group1="bl:2:res,cert", sash_mode="hidden")
        ys, xs = np.nonzero(ink)
        self.assertGreater(ys.min(), 650)
        self.assertLess(xs.max(), 250)

    def test_a_taken_corner_pushes_the_group_up(self):
        clear = self.render(badge_group1="bl:2:res,cert", sash_mode="hidden")
        blocked = self.render((0, 690, 500, 750), badge_group1="bl:2:res,cert", sash_mode="hidden")
        self.assertLess(np.nonzero(blocked)[0].max(), 690)
        self.assertLess(np.nonzero(blocked)[0].min(), np.nonzero(clear)[0].min())

    def test_custom_left_half_starts_at_x_right_half_ends_at_x(self):
        ink = self.render(badge_group1="0.1,0.5:2:res,cert", sash_mode="hidden")
        ys, xs = np.nonzero(ink)
        self.assertAlmostEqual(xs.min(), 50, delta=2)
        self.assertAlmostEqual((ys.min() + ys.max()) / 2, 375, delta=3)
        ink = self.render(badge_group1="0.9,0.5:2:res,cert", sash_mode="hidden")
        self.assertAlmostEqual(np.nonzero(ink)[1].max(), 449, delta=2)

    def test_align_picks_the_edge_at_x_on_either_half(self):
        xs = np.nonzero(self.render(badge_group1="0.2,0.5,r:2:res,cert", sash_mode="hidden"))[1]
        self.assertAlmostEqual(xs.max(), 99, delta=2)          # right edge at x, grows left
        xs = np.nonzero(self.render(badge_group1="0.8,0.5,l:2:res,cert", sash_mode="hidden"))[1]
        self.assertAlmostEqual(xs.min(), 400, delta=2)         # left edge at x, grows right
        xs = np.nonzero(self.render(badge_group1="0.3,0.5,c:2:res,cert", sash_mode="hidden"))[1]
        self.assertAlmostEqual((xs.min() + xs.max()) / 2, 150, delta=3)

    def test_custom_ignores_whatever_is_underneath(self):
        # Placed by hand on purpose, so it doesn't slide off an obstacle.
        ink = self.render((0, 350, 500, 400), badge_group1="0.1,0.5:1:cert", sash_mode="hidden")
        self.assertTrue(ink[350:400].any())

    def test_custom_row_stays_on_the_poster(self):
        ink = self.render(badge_group1="0.99,0.999:2:res,cert", sash_mode="hidden")
        ys, xs = np.nonzero(ink)
        self.assertLess(ys.max(), 750)
        self.assertGreater(xs.min(), 300)

    def test_spacing_sets_the_gap_between_badges(self):
        def gaps(spacing):
            ink = self.render(badge_group1=f"bl:2:res,cert:20:{spacing}", sash_mode="hidden")
            cols = ink.any(axis=0)
            runs = np.flatnonzero(np.diff(cols.astype(np.int8)))
            return runs[2] - runs[1]     # dark run between the two badges
        self.assertAlmostEqual(gaps("0.05") - gaps("0.01"), 20, delta=2)   # 0.04 x 500 px

    def test_each_group_has_its_own_size(self):
        def height(spec):
            ink = self.render(badge_group1=spec, sash_mode="hidden")
            rows = np.flatnonzero(ink.any(axis=1))
            return rows.max() - rows.min() + 1
        self.assertAlmostEqual(height("bl:1:cert:30") / height("bl:1:cert:20"), 1.5, delta=0.08)
        ink = self.render(badge_group1="bl:1:cert:30", sash_mode="hidden")
        self.assertGreater(np.nonzero(ink)[0].max(), 700)    # still on the bottom margin

    def test_three_groups_each_take_their_corner(self):
        cfg = main.build_request_config({"badge_group3": "TL:1:cert"})
        self.assertEqual(cfg.badge_group3, "tl:1:cert")
        ink = self.render(badge_group1="tr:1:video", badge_group2="bl:1:res",
                          badge_group3="tl:1:cert", sash_mode="hidden")
        self.assertTrue(ink[:100, :250].any())      # tl: the certificate
        self.assertTrue(ink[650:, :250].any())      # bl: resolution
        # No video mark without the Commons files, so top right stays empty.
        self.assertFalse(ink[:100, 250:].any())

    def test_fourth_group_is_drawn(self):
        cfg = main.build_request_config({"badge_group4": "BR:1:res",
                                         "landscape_badge_group4": "tl:1:res", "shape": "landscape"})
        self.assertEqual(cfg.badge_group4, "tl:1:res")
        self.assertEqual(main.build_request_config({"badge_group4": "BR:1:res"}).badge_group4, "br:1:res")
        ink = self.render(badge_group1="tl:1:cert", badge_group4="br:1:res", sash_mode="hidden")
        self.assertTrue(ink[:100, :250].any())      # group 1, tl: the certificate
        self.assertTrue(ink[650:, 250:].any())      # group 4, br: resolution
        self.assertFalse(ink[650:, :250].any())
        # A slot already in an earlier group stays there.
        self.assertEqual([g.slots for g in gb.resolve_groups("tl:1:cert", "", "", "br:2:cert,res")],
                         [("cert",), ("res",)])

    def test_second_group_does_not_overlap_the_first(self):
        ink = self.render(badge_group1="tr:1:cert", badge_group2="tr:1:res", sash_mode="hidden")
        rows = np.flatnonzero(ink.any(axis=1))
        # Two separate bands of rows, one under the other.
        self.assertGreater(int(np.diff(rows).max()), 1)


if __name__ == "__main__":
    unittest.main()
