"""Series release status and dates, from the episode data TMDB ships with the
series details.

TMDB's "Returning Series" means "not declared finished", not "on air", and it
used to map straight to "Airing": The Last of Us (last episode May 2025) and
Severance (March 2025) both read Airing in September 2026. The cases below are
the shapes those shows actually had.
"""
import unittest
from datetime import date

import main
from discovery import (
    DiscoveryMeta,
    RELEASE_STATUS_SLOTS,
    _release_status_label,
    release_date_label,
    tv_release_facts,
)
from i18n import load_languages, translate_sash

TODAY = date(2026, 9, 22)


def ep(season, number, air_date):
    return {"season_number": season, "episode_number": number, "air_date": air_date}


def seasons(*pairs):
    return [{"season_number": n, "air_date": d} for n, d in pairs]


def facts(status, **data):
    return tv_release_facts(status, data, today=TODAY)


class ReturningSeriesTests(unittest.TestCase):
    def test_airing_weekly(self):
        # Slow Horses: S6E1 last week, S6E2 tomorrow.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(6, 1, "2026-09-16"),
                  next_episode=ep(6, 2, "2026-09-23"), seasons=seasons((6, "2026-09-16"))),
            ("Airing", None, None))

    def test_the_last_episode_of_a_run_still_airs_for_a_fortnight(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(3, 8, "2026-09-12")),
            ("Airing", None, None))

    def test_dormant_with_nothing_announced_has_no_status(self):
        # The Last of Us: last episode May 2025, no next episode, no S3 entry.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 7, "2025-05-25"),
                  seasons=seasons((1, "2023-01-15"), (2, "2025-04-13"))),
            (None, None, None))

    def test_an_undated_listed_season_is_a_renewal(self):
        # Severance: S3 listed with no air date.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 10, "2025-03-20"),
                  seasons=seasons((1, "2022-02-17"), (2, "2025-01-17"), (3, None))),
            ("Renewed", None, None))

    def test_a_dated_next_season(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 8, "2026-02-03"),
                  next_episode=ep(3, 1, "2027-03-04")),
            ("Renewed", "2027-03-04", "Season 3"))

    def test_a_dated_listed_season_without_a_next_episode(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 8, "2026-02-03"),
                  seasons=seasons((2, "2026-01-01"), (3, "2026-11-05"))),
            ("Renewed", "2026-11-05", "Season 3"))

    def test_a_mid_season_break(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(4, 8, "2026-08-01"),
                  next_episode=ep(4, 9, "2027-01-08")),
            ("Airing", "2027-01-08", "Returns"))

    def test_a_short_break_is_just_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(4, 8, "2026-08-20"),
                  next_episode=ep(4, 9, "2026-10-01")),
            ("Airing", None, None))

    def test_back_to_back_seasons_do_not_stop_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(1, 12, "2026-09-18"),
                  next_episode=ep(2, 1, "2026-09-25")),
            ("Airing", None, None))

    def test_no_episode_data_keeps_the_plain_mapping(self):
        # The anime providers ship a status and nothing to refine it with.
        self.assertEqual(facts("Returning Series"), ("Airing", None, None))

    def test_a_marked_finale_ends_the_run(self):
        # Ted Lasso: S4 finale last week, nothing scheduled, nothing announced.
        finale = dict(ep(4, 10, "2026-09-16"), episode_type="finale")
        self.assertEqual(facts("Returning Series", last_episode=finale,
                               seasons=seasons((4, "2026-07-29"))),
                         (None, None, None))
        # ...and a renewal already listed reads as one.
        self.assertEqual(facts("Returning Series", last_episode=finale,
                               seasons=seasons((4, "2026-07-29"), (5, None))),
                         ("Renewed", None, None))

    def test_a_season_released_all_at_once_is_not_airing(self):
        # Gnomes: all six episodes out on the season's own date.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(1, 6, "2026-09-18"),
                  seasons=[{"season_number": 1, "air_date": "2026-09-18", "episode_count": 6}]),
            (None, None, None))
        # A weekly season whose latest episode is the last one listed still airs.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(2, 3, "2026-09-18"),
                  seasons=[{"season_number": 2, "air_date": "2026-09-04", "episode_count": 3}]),
            ("Airing", None, None))

    def test_an_aired_stale_finale_ends_the_run_too(self):
        # Lanterns cached before its finale aired: "next" was the finale.
        self.assertEqual(
            facts("Returning Series", last_episode=ep(1, 7, "2026-09-13"),
                  next_episode=dict(ep(1, 8, "2026-09-20"), episode_type="finale")),
            (None, None, None))

    def test_a_premiere_cached_before_it_aired_is_airing(self):
        # Elsbeth, cached the day before S4E1: the "next" episode has aired
        # since, and used to leave an undated "Renewed".
        self.assertEqual(
            facts("Returning Series", last_episode=ep(3, 20, "2026-05-21"),
                  next_episode=ep(4, 1, "2026-09-18"),
                  seasons=seasons((3, "2025-10-12"), (4, "2026-09-18"))),
            ("Airing", None, None))

    def test_premiere_day_is_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(3, 20, "2026-05-21"),
                  next_episode=ep(4, 1, TODAY.isoformat())),
            ("Airing", None, None))

    def test_a_listed_season_that_just_premiered_is_airing(self):
        self.assertEqual(
            facts("Returning Series", last_episode=ep(3, 20, "2026-05-21"),
                  seasons=seasons((3, "2025-10-12"), (4, "2026-09-18"))),
            ("Airing", None, None))


class OtherStatusTests(unittest.TestCase):
    def test_an_unaired_show_is_dated_with_its_premiere(self):
        # Harry Potter (HBO): In Production, first air date Dec 25.
        self.assertEqual(
            facts("In Production", tmdb_release_date="2026-12-25",
                  next_episode=ep(1, 1, "2026-12-25"), seasons=seasons((1, "2026-12-25"))),
            ("Production", "2026-12-25", "Premiere"))

    def test_an_unaired_returning_series_is_a_premiere_not_a_renewal(self):
        # East of Eden: TMDB flipped it to Returning Series before S1E1 aired.
        self.assertEqual(
            facts("Returning Series", tmdb_release_date="2026-10-01",
                  next_episode=ep(1, 1, "2026-10-01"), seasons=seasons((1, "2026-09-30"))),
            ("Production", "2026-09-30", "Premiere"))

    def test_an_unaired_returning_series_without_a_date(self):
        self.assertEqual(facts("Returning Series", seasons=seasons((1, None))),
                         ("Production", None, None))

    def test_an_undated_unaired_show(self):
        self.assertEqual(facts("Planned"), ("Production", None, None))

    def test_finished_shows_pass_through(self):
        self.assertEqual(facts("Ended", last_episode=ep(5, 8, "2026-06-25")), ("Ended", None, None))
        self.assertEqual(facts("Canceled"), ("Cancelled", None, None))


class LabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_labels(self):
        self.assertEqual(release_date_label("2027-03-04", "Season 3", today=TODAY), "Mar 4 Season 3")
        self.assertEqual(release_date_label("2026-12-25", "Premiere", today=TODAY), "Dec 25 Premiere")
        self.assertEqual(release_date_label("2027-11-01", "Season 2", today=TODAY), "Nov 2027 Season 2")

    def test_the_status_label_uses_the_date_for_any_status_that_has_one(self):
        meta = DiscoveryMeta(release_status="Renewed")
        self.assertEqual(_release_status_label(meta), "Renewed")
        meta.upcoming_release_date, meta.upcoming_release_window = "2099-03-04", "Season 3"
        self.assertEqual(_release_status_label(meta), "Mar 2099 Season 3")

    def test_translations_keep_numbers_apart(self):
        # Most locales put the window first; "Temporada 3 4 mar" would run the
        # season into the day.
        self.assertEqual(translate_sash("Mar 4 Season 3", "es-ES"), "T3 4 Mar")
        self.assertEqual(translate_sash("Mar 4 Season 3", "fr-FR"), "S3 4 Mar")
        self.assertEqual(translate_sash("Dec 25 Premiere", "pt-BR"), "Estreia 25 Dez")
        self.assertEqual(translate_sash("Renewed", "it"), "Rinnovata")

    def test_english_reads_the_word_before_the_date(self):
        # Issue 36: "Physical" came after its date while "Streams" came first.
        for label, shown in (("Oct 16 Cinema", "Cinema Oct 16"),
                             ("Nov 3 Physical", "Physical Nov 3"),
                             ("Oct 23 Streaming", "Streams Oct 23"),
                             ("Dec 2027 Cinema", "Cinema Dec 2027"),
                             ("Mar 4 Season 3", "Season 3 Mar 4"),
                             ("Jan 8 Returns", "Returns Jan 8"),
                             ("Dec 25 Premiere", "Premieres Dec 25"),
                             ("Oct 2 Premiered", "Premiered Oct 2")):
            with self.subTest(label):
                self.assertEqual(translate_sash(label, "en"), shown)
                self.assertEqual(translate_sash(label, None), shown)
        self.assertEqual(translate_sash("Oct 16 Cinema", "pl"), "W kinach 16 Paź")
        self.assertEqual(translate_sash("Oct 2 Premiered", "pt-BR"), "Estreou 2 Out")

    def test_the_premiere_sash_says_when(self):
        from discovery import _evaluate_slot, premiered_label
        self.assertEqual(premiered_label("2026-09-14", today=TODAY), "Sep 14 Premiered")
        self.assertIsNone(premiered_label("2026-09-30", today=TODAY))
        meta = DiscoveryMeta(is_premiere=True, premiere_date="2020-09-14")
        self.assertEqual(_evaluate_slot("premiere", meta), "Sep 14 Premiered")
        meta.premiere_date = None   # dates off
        self.assertEqual(_evaluate_slot("premiere", meta), "Premiere")

    def test_the_next_episode_date_on_airing(self):
        # airing_dates: the window is the status itself, read first in English.
        self.assertEqual(release_date_label("2026-09-29", "Airing", today=TODAY), "Sep 29 Airing")
        self.assertEqual(translate_sash("Sep 29 Airing", "en"), "Airing Sep 29")
        self.assertEqual(translate_sash("Sep 29 Airing", None), "Airing Sep 29")
        # An episode is still to come on its own day; a release has arrived.
        self.assertEqual(release_date_label(TODAY.isoformat(), "Airing", today=TODAY), "Sep 22 Airing")
        self.assertIsNone(release_date_label(TODAY.isoformat(), "Cinema", today=TODAY))
        self.assertIsNone(release_date_label("2026-09-21", "Airing", today=TODAY))
        # Other languages use their own word for Airing in their date order.
        self.assertNotIn("Airing", translate_sash("Sep 29 Airing", "fr-FR"))

    def test_airing_dates_is_on_by_default_and_parses(self):
        self.assertTrue(main.RequestConfig().airing_dates)
        self.assertTrue(main.build_request_config({}).airing_dates)
        self.assertFalse(main.build_request_config({"airing_dates": "false"}).airing_dates)


class SlotTests(unittest.TestCase):
    def test_renewed_is_a_release_status_slot(self):
        self.assertIn("renewed", RELEASE_STATUS_SLOTS)
        self.assertIn("renewed", main.ALL_PRIORITY_SLOTS)

    def test_it_is_last_in_the_default_so_saved_moves_keep_their_positions(self):
        self.assertEqual(list(main._cfg.SASH_PRIORITY)[-1], "renewed")

    def test_an_explicit_list_with_airing_gains_renewed_behind_it(self):
        self.assertEqual(main._parse_sash_priority("wins,airing,ended"),
                         ["wins", "airing", "renewed", "ended"])
        self.assertEqual(main._parse_sash_priority("wins,airing,-renewed"), ["wins", "airing"])
        self.assertEqual(main._parse_sash_priority("wins,ended"), ["wins", "ended"])

    def test_release_status_expands_to_include_it(self):
        self.assertIn("renewed", main._parse_sash_priority("release_status"))


if __name__ == "__main__":
    unittest.main()


class NextEpisodeExpiryTests(unittest.TestCase):
    """A series row is refreshed once its scheduled next episode has aired."""

    def setUp(self):
        import os, tempfile
        import cache
        self.cache = cache
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()

    def tearDown(self):
        conn = getattr(self.cache._local, "conn", None)
        if conn is not None:
            conn.close()
        self.cache.DB_PATH, self.cache._initialised, self.cache._local.conn = self._saved
        self._dir.cleanup()

    def _store(self, key, next_air, cached_ago=3 * 86400):
        import time
        self.cache.set_cached_tmdb_metadata(
            key, "T", "2020", [18], True, "/p.jpg", [], original_title="T", vote_count=10,
            tmdb_release_date="2020-01-01", next_episode=ep(4, 1, next_air.isoformat()),
            metadata_version=6,
        )
        # Cached three days ago.
        self.cache.get_db().execute(
            "UPDATE tmdb_metadata_cache SET cached_at = ? WHERE cache_key = ?",
            (time.time() - cached_ago, key))
        self.cache.get_db().commit()

    def test_rows_expire_when_the_next_episode_airs(self):
        from datetime import timedelta
        from unittest import mock
        today = date.today()
        self._store("tv_1_en_x", today - timedelta(days=1))
        self._store("tv_2_en_x", today + timedelta(days=2))
        with mock.patch.object(self.cache, "invalidate_final_posters") as inv:
            self.assertIsNone(self.cache.get_cached_tmdb_metadata("tv_1_en_x"))
            inv.assert_called_once_with("1", "tv")
        self.assertIsNotNone(self.cache.get_cached_tmdb_metadata("tv_2_en_x"))

    def test_a_row_tmdb_sent_with_an_aired_next_is_retried_after_hours(self):
        # Slow Horses, fetched the morning after S6E4 with S6E4 still "next".
        from datetime import datetime, timedelta, timezone
        import time
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        # Cached since midnight UTC: just after midnight, "60 s ago" was still
        # the air day, which rightly expires the row.
        self._store("tv_1_en_x", yesterday, cached_ago=min(60, (time.time() % 86400) / 2))
        self._store("tv_2_en_x", yesterday, cached_ago=self.cache.STALE_NEXT_EPISODE_RETRY + 60)
        self.assertIsNotNone(self.cache.get_cached_tmdb_metadata("tv_1_en_x"))
        self.assertIsNone(self.cache.get_cached_tmdb_metadata("tv_2_en_x"))

    def test_a_row_cached_on_the_air_day_expires_the_day_after(self):
        # Elsbeth was cached at 13:32 on its premiere day, with that premiere
        # still TMDB's "next" episode.
        from datetime import datetime, timedelta, timezone
        import time
        today = datetime.now(timezone.utc).date()
        since_midnight = time.time() % 86400
        self._store("tv_1_en_x", today - timedelta(days=1), cached_ago=since_midnight + 3600)
        self._store("tv_2_en_x", today, cached_ago=min(60, since_midnight / 2))
        self.assertIsNone(self.cache.get_cached_tmdb_metadata("tv_1_en_x"))
        self.assertIsNotNone(self.cache.get_cached_tmdb_metadata("tv_2_en_x"))


class FollowingEpisodeTests(unittest.TestCase):
    """TMDB still lists an aired episode as next: the season list has the one after."""

    def setUp(self):
        import os, tempfile
        import cache
        self.cache = cache
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()

    def tearDown(self):
        conn = getattr(self.cache._local, "conn", None)
        if conn is not None:
            conn.close()
        self.cache.DB_PATH, self.cache._initialised, self.cache._local.conn = self._saved
        self._dir.cleanup()

    def test_the_episode_after_the_stale_one(self):
        import asyncio
        from datetime import timedelta
        import httpx
        import tmdb
        today = date.today()
        days = [today - timedelta(days=8), today - timedelta(days=1), today + timedelta(days=6)]
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"episodes": [{"air_date": d.isoformat()} for d in days]})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                stale = ep(2, 2, days[1].isoformat())
                first = await tmdb.fetch_following_episode_date(client, "290856", "k", stale)
                again = await tmdb.fetch_following_episode_date(client, "290856", "k", stale)
                return first, again

        first, again = asyncio.run(run())
        self.assertEqual(first, days[2].isoformat())
        self.assertEqual(again, first)
        self.assertEqual(calls, ["/3/tv/290856/season/2"])   # cached


class SashOrderSeedTests(unittest.TestCase):
    """Premiere moved under Airing; diffs saved against the old order keep it."""

    def test_premiere_sits_under_airing(self):
        order = main._parse_sash_priority(None)
        self.assertEqual(order.index("premiere"), order.index("airing") + 1)

    def test_an_old_seed_counts_positions_in_the_old_order(self):
        old = main._parse_sash_priority("default,cast@0")
        self.assertEqual(old[0], "cast")
        self.assertLess(old.index("premiere"), old.index("airing"))
        self.assertEqual(old[1:], [s for s in main._SASH_PRIORITY_V1 if s != "cast"])
        new = main._parse_sash_priority("default2,cast@0")
        self.assertGreater(new.index("premiere"), new.index("airing"))

    def test_the_configurator_is_told_every_seed(self):
        import re
        html = open("configurator.html", encoding="utf-8").read()
        self.assertIn("serverCaps.sash_priority_diff_bases", html)
        slots = re.findall(r"\{ id:'(\w+)',", html.split("const SASH_SLOTS", 1)[1].split("];", 1)[0])
        self.assertEqual(slots, [s for s in main._cfg.SASH_PRIORITY if s in slots])

    def test_the_new_default_keeps_the_old_defaults_cache_keys(self):
        import json
        default_key = main._render_config_signature(main.RequestConfig())
        self.assertEqual(json.loads(default_key)["sash_priority"], list(main._SASH_PRIORITY_V1))
        explicit_old = main.RequestConfig(sash_priority=list(main._SASH_PRIORITY_V1))
        self.assertNotEqual(main._render_config_signature(explicit_old), default_key)
