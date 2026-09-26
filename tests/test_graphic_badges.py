"""Graphic badge row (badge_display_mode=7): marks from pinned Commons files,
text boxes, the US certificate, and the row's layout around the chip."""
import asyncio
import hashlib
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
        gb._marks.cache_clear()
        self.addCleanup(gb._marks.cache_clear)

    def test_a_file_that_no_longer_matches_its_hash_is_not_used(self):
        client = _Client({})   # every body is b"nope", matching no pinned SHA-1
        self.assertFalse(asyncio.run(gb.ensure_assets(client)))
        self.assertEqual(os.listdir(self.dir.name), [])
        self.assertEqual(len(client.urls), len(gb._FILES))

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
        self.assertEqual(gb.format_group(gb.parse_group("bl:2:cert:20:0.028")), "bl:2:cert")  # defaults left out
        self.assertEqual(gb.format_group(gb.parse_group("bl:2:cert:20:0.05")), "bl:2:cert:20:0.05")
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

    def test_only_with_graphic_badges_and_the_frosted_notch(self):
        self.assertEqual(self.pos(badge_display_mode="4"), "center")
        self.assertEqual(self.pos(sash_badge_style="gold"), "center")

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

    def ink_runs(self, spread, chip=(22, 22, 200, 62), pos="left", group="chip:2:res,cert"):
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

    def heights(self, chip, group="chip:2:res,cert"):
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

    def test_logos_are_sized_by_area_within_the_row(self):
        row = 30
        # A compact emblem fills the row height.
        self.assertEqual(gb.logo_size((100, 100), row), (30, 30))
        # A long wordmark comes out shorter and no wider than the cap.
        w, h = gb.logo_size((100, 800), row)
        self.assertLess(h, 20)
        self.assertLessEqual(w, round(row * gb._LOGO_MAX_W))
        # Similar area whatever the shape, until a limit applies.
        areas = [w * h for w, h in (gb.logo_size((100, a), row) for a in (250, 350))]
        self.assertAlmostEqual(areas[0] / areas[1], 1, delta=0.08)


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
        self.assertAlmostEqual(height("bl:1:cert:30") / height("bl:1:cert"), 1.5, delta=0.08)
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

    def test_second_group_does_not_overlap_the_first(self):
        ink = self.render(badge_group1="tr:1:cert", badge_group2="tr:1:res", sash_mode="hidden")
        rows = np.flatnonzero(ink.any(axis=1))
        # Two separate bands of rows, one under the other.
        self.assertGreater(int(np.diff(rows).max()), 1)


if __name__ == "__main__":
    unittest.main()
