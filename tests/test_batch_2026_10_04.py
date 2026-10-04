"""Fanart landscape art, a hugging badge row that fills its run, the trending
numeral under the notch, genres hidden from the trending lists, and the
landscape poster cut."""

import asyncio
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import config
import fanart
import main
import tmdb
import trending_rank


class FanartLandscapeTests(unittest.TestCase):
    DOC = {
        "showbackground": [{"url": "bg1", "lang": "", "likes": "2"}, {"url": "bg2", "lang": "", "likes": "9"}],
        "tvthumb": [{"url": "th-en", "lang": "en", "likes": "1"}, {"url": "th-de", "lang": "de", "likes": "4"}],
        "tvposter": [{"url": "p", "lang": "00", "likes": "1"}],
    }

    def _url(self, languages=None):
        cache = {}

        async def doc(*a):
            return self.DOC
        with mock.patch.object(fanart, "_fetch_doc", doc), \
                mock.patch.object(fanart, "get_cached_tvdb_json", cache.get), \
                mock.patch.object(fanart, "set_cached_tvdb_json", lambda k, v, t: cache.__setitem__(k, v)), \
                mock.patch.object(fanart._cfg, "FANART_POSTERS", True), \
                mock.patch.object(fanart._cfg, "FANART_API_KEY", "k"):
            return asyncio.run(fanart.fanart_background_url(
                None, media_type="tv", tmdb_id="1399", languages=languages))

    def test_textless_is_the_most_liked_background(self):
        self.assertEqual(self._url(), "bg2")

    def test_original_is_a_thumb_in_the_language_order(self):
        self.assertEqual(self._url(["fr", "en", "de"]), "th-en")
        self.assertIsNone(self._url(["fr"]))

    def test_parsed_only_when_the_operator_offers_fanart(self):
        q = {"shape": "landscape", "landscape_art_source": "fanart_anime"}
        with mock.patch.object(main._cfg, "FANART_POSTERS", False):
            self.assertEqual(main.build_request_config(q).landscape_art_source, "tmdb")
        with mock.patch.object(main._cfg, "FANART_POSTERS", True), \
                mock.patch.object(main._cfg, "FANART_API_KEY", "k"):
            self.assertEqual(main.build_request_config(q).landscape_art_source, "fanart_anime")
            self.assertEqual(main.build_request_config({**q, "shape": "poster"}).landscape_art_source, "tmdb")


class HugFillTests(unittest.TestCase):
    def _items(self, widths):
        return [("", Image.new("RGBA", (w, 20), (255, 255, 255, 255))) for w in widths]

    def _lefts(self, widths, free=300):
        width, margin, gap = 500, 20, 10
        image = Image.new("RGBA", (width, 100), (0, 0, 0, 0))
        cols = np.zeros(width, dtype=bool)
        cols[: width - margin - free] = True          # the chip, on the left
        drawn = []
        with mock.patch.object(main.graphic_badges, "draw_row",
                               lambda img, items, left_x, center_y, gap: drawn.append((left_x, gap))):
            self.assertTrue(main._hug_chip(image, self._items(widths), cols, True, 50, margin, gap))
        return drawn[0], width - margin - free, width - margin

    def test_a_short_row_stays_against_the_chip(self):
        (left, gap), chip_edge, _end = self._lefts([40, 40])
        self.assertEqual((left, gap), (chip_edge + 10, 10))

    def test_a_nearly_full_row_spreads_into_the_slack(self):
        widths = [100, 70, 80]                         # 270 of a 290 run, with gaps
        (left, gap), chip_edge, end = self._lefts(widths)
        self.assertGreater(gap, 10)
        self.assertEqual(left, round(chip_edge + gap))
        self.assertAlmostEqual(left + sum(widths) + gap * 2, end, delta=2)

    def test_the_gaps_grow_only_so_far(self):
        widths = [90, 60, 70]                          # 220: the slack would need 26.7
        (left, gap), chip_edge, end = self._lefts(widths)
        self.assertEqual(gap, 10 * main._HUG_MAX_GAP)
        self.assertLess(left + sum(widths) + gap * 2, end)


class CentredRankTests(unittest.TestCase):
    def _notch_box(self, pos):
        """What a notch at *pos* draws on a flat poster, as build_poster measures it."""
        art = Image.new("RGBA", (500, 750), (90, 60, 30, 255))
        before = np.asarray(art.convert("RGB"))
        cfg = main.build_request_config({"sash_mode": "notch", "sash_badge_pos": pos})
        drawn = main.draw_award_badge(art, "Premiere", sash_type="status", position=pos,
                                      size_ratio_w=cfg.sash_badge_size_w, size_ratio_h=cfg.sash_badge_size_h,
                                      notch_style=cfg.sash_badge_style, notch_inset=cfg.sash_badge_inset,
                                      notch_pad_ratio=cfg.sash_badge_pad,
                                      font_size_ratio=cfg.sash_badge_font_ratio)
        return main._changed_box(before, np.asarray(drawn.convert("RGB")))

    def _rank_body(self, notch_box):
        cfg = main.build_request_config({"trending_style": "number", "trending_side": "center",
                                         "sash_mode": "notch"})
        image = Image.new("RGBA", (500, 750), (20, 20, 20, 255))
        _, (body, _extent) = main._draw_trending_rank(image, cfg, 2, None, notch_box=notch_box)
        return body

    def test_numeral_hangs_under_the_notch_wherever_it_is(self):
        shade = round(trending_rank._SHADE * 500)
        for pos in ("center", "left", "right"):
            with self.subTest(pos=pos):
                box = self._notch_box(pos)
                self.assertIsNotNone(box)
                body = self._rank_body(box)
                self.assertGreater(body[1] + shade, box[3])
                self.assertAlmostEqual((body[0] + body[2]) / 2, (box[0] + box[2]) / 2, delta=4)
        self.assertLess(self._notch_box("left")[2], 250)

    def test_without_a_notch_it_sits_at_the_top_centre(self):
        body = self._rank_body(None)
        corner, _ = trending_rank.number_footprint(500, 2)
        self.assertEqual(body[1], corner[1])
        self.assertAlmostEqual((body[0] + body[2]) / 2, 250, delta=3)

    def test_it_stays_inside_the_corner_inset(self):
        body = self._rank_body((0, 0, 20, 40))
        corner, _ = trending_rank.number_footprint(500, 2)
        self.assertEqual(body[0], corner[0])

    def test_the_ribbon_reads_under_notch_as_left(self):
        cfg = main.build_request_config({"trending_style": "ribbon", "trending_side": "center"})
        self.assertEqual(cfg.trending_side, "left")
        cfg = main.build_request_config({"trending_style": "number", "trending_side": "center"})
        self.assertEqual(cfg.trending_side, "center")

    def test_the_sash_is_left_where_it_is(self):
        cfg = main.build_request_config({"trending_style": "number", "trending_side": "center",
                                         "trending_sash": "opposite", "sash_mode": "notch"})
        self.assertEqual(main._sash_beside_rank(cfg).sash_badge_pos, cfg.sash_badge_pos)


class TrendingGenreFilterTests(unittest.TestCase):
    """TRENDING_HIDE_GENRES leaves titles off the list itself, before ranks are
    numbered, so the catalogs and every poster's rank still agree."""

    def test_mixed_titles(self):
        romcom = [35, 10749]
        self.assertTrue(config.genre_hidden(romcom, {"Romance"}, mixed=True))
        self.assertFalse(config.genre_hidden(romcom, {"Romance"}, mixed=False))
        self.assertTrue(config.genre_hidden([10749], {"Romance"}, mixed=False))
        self.assertTrue(config.genre_hidden(romcom, {"Romance", "Comedy"}, mixed=False))
        self.assertTrue(config.genre_hidden(romcom, {"Rom-Com"}, mixed=False))
        # TV's merged genres count as both halves.
        self.assertTrue(config.genre_hidden([10765], {"Fantasy"}, mixed=True))

    def test_choices_are_the_genre_names(self):
        self.assertEqual(set(config._TRENDING_GENRE_CHOICES), set(config.GENRE_MAP.values()))

    def _filter(self, ids, details, hidden, mixed=True, metadata=None):
        async def fetch(client, tmdb_id, key, kind, *a, **k):
            if metadata is None or tmdb_id not in metadata:
                raise RuntimeError("no metadata")
            return (metadata[tmdb_id],) + (None,) * 7
        with mock.patch.object(tmdb, "TRENDING_HIDE_GENRES", hidden), \
                mock.patch.object(tmdb, "TRENDING_HIDE_MIXED_GENRES", mixed), \
                mock.patch.object(tmdb, "fetch_poster_metadata", fetch):
            return asyncio.run(tmdb._without_hidden_genres(None, "k", "movie", ids, details))

    def test_rows_are_dropped_by_their_own_genres(self):
        details = {"1": {"genres": [10749]}, "2": {"genres": [35, 10749]}, "3": {"genres": [28]}}
        self.assertEqual(self._filter(["1", "2", "3"], details, ["Romance"]), ["3"])
        self.assertEqual(self._filter(["1", "2", "3"], details, ["Romance"], mixed=False), ["2", "3"])
        self.assertEqual(self._filter(["1", "2", "3"], details, []), ["1", "2", "3"])

    def test_rows_without_genres_are_looked_up_or_kept(self):
        details = {}
        out = self._filter(["1", "2", "anilist:5"], details, ["Horror"], metadata={"1": [27]})
        self.assertEqual(out, ["2", "anilist:5"])          # 2 failed, the AniList row has none
        self.assertEqual(details["1"]["genres"], [27])     # stored with the snapshot

    def test_ranks_are_numbered_after_the_filter(self):
        rows = [{"id": 1, "genre_ids": [10749]}, {"id": 2, "genre_ids": [28]},
                {"id": 3, "genre_ids": [27]}]

        async def tmdb_ids(client, key, endpoint, details_out=None):
            for row in rows:
                details_out[str(row["id"])] = tmdb._trending_item_details(row)
            return [str(r["id"]) for r in rows]
        stored = {}
        with mock.patch.object(tmdb, "TRENDING_HIDE_GENRES", ["Romance"]), \
                mock.patch.object(tmdb, "_fetch_tmdb_trending_ids", tmdb_ids), \
                mock.patch.object(tmdb, "fetch_trending_source_ids", mock.AsyncMock(return_value=None)), \
                mock.patch.object(tmdb, "anime_split", lambda: False), \
                mock.patch.object(tmdb, "get_cached_trending_snapshot_entry", lambda *a, **k: None), \
                mock.patch.object(tmdb, "set_cached_trending_snapshot",
                                  lambda ep, rankings, sig, details: stored.update(rankings=rankings, sig=sig)):
            asyncio.run(tmdb.ensure_trending_snapshot(None, "k", "movie"))
        self.assertEqual(stored["rankings"], {"2": 1, "3": 2})
        self.assertIn("-genres:Romance", stored["sig"])


class PosterCutTests(unittest.TestCase):
    def test_a_portrait_cut_fills_the_canvas_from_the_upper_part(self):
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        art.paste((255, 0, 0, 255), (0, 0, 500, 250))       # top third red
        with mock.patch("face_detect.detect_face_boxes", lambda im: []):
            out = tmdb._landscape_from_portrait(art)
        self.assertEqual(out.size, (tmdb.LANDSCAPE_WIDTH, tmdb.LANDSCAPE_HEIGHT))
        self.assertEqual(out.getpixel((10, 5))[:3], (255, 0, 0))

    def test_faces_pull_the_cut_to_them(self):
        art = Image.new("RGBA", (500, 750), (0, 0, 0, 255))
        with mock.patch("face_detect.detect_face_boxes", lambda im: [(400, 1200, 200, 200, 0.9)]):
            out = tmdb._landscape_from_portrait(art)
        self.assertEqual(out.size, (tmdb.LANDSCAPE_WIDTH, tmdb.LANDSCAPE_HEIGHT))

    def test_parsed_for_landscape_only(self):
        q = {"landscape_poster_crop": "true"}
        self.assertTrue(main.build_request_config({**q, "shape": "landscape"}).landscape_poster_crop)
        self.assertFalse(main.build_request_config(q).landscape_poster_crop)


if __name__ == "__main__":
    unittest.main()


class AnimeRequestWiringTests(unittest.TestCase):
    """An anime-id request agrees with the TMDB-id one for the same show: the
    same TMDB entry, Metahub's background when TMDB has no backdrop, and the
    same anime rank.  Read off main.py, like test_anime_id_map's wiring test."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = Path("main.py").read_text(encoding="utf-8")

    def test_a_mapped_tmdb_id_tmdb_deleted_is_resolved_past(self):
        block = self.src[self.src.index("    if is_anime:\n        # A client that resolves its own pattern"):]
        block = block[:block.index("has_tmdb_id = bool(tmdb_id)")]
        self.assertIn("tmdb_id_gone(_mapped.tmdb_id, type)", block)
        self.assertIn("resolve_imdb_to_tmdb(", block)
        # Before the name search, which then runs when the IMDb id finds nothing.
        self.assertLess(block.index("tmdb_id_gone("), block.index("anime_resolve.resolve("))

    def test_a_404_on_the_mapped_id_marks_it_gone(self):
        block = self.src[self.src.index("# A failed logo lookup is never fatal"):]
        block = block[:block.index("using_anime_art = ")]
        self.assertIn("_is_tmdb_title_404(_logo_meta", block)
        self.assertIn("mark_tmdb_id_gone(tmdb_id, type, imdb_id or None)", block)

    def test_tmdbs_imdb_id_rides_along(self):
        self.assertIn('if not tmdb_data.get("imdb_id") and _logo_meta[7].get("imdb_id"):', self.src)

    def test_metahub_backs_anime_landscape_only(self):
        self.assertIn("(is_anime and not _is_landscape) or not effective_imdb_id", self.src)

    def test_rank_keys_use_the_imdb_id_tmdb_gave(self):
        block = self.src[self.src.index("_anime_rank_keys = _anime_trending_keys("):]
        self.assertIn("imdb_id or effective_imdb_id", block[:200])
