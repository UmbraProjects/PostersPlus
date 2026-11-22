import asyncio
import unittest
from unittest import mock

import httpx

import box_office
import main
from discovery import ALL_PRIORITY_SLOTS, extract_discovery_meta, pick_sash


def _transport(results: list[int], revenues: dict[int, int], calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/discover/movie"):
            assert request.url.params["sort_by"] == "revenue.desc"
            return httpx.Response(200, json={"results": [{"id": i} for i in results]})
        tmdb_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": tmdb_id, "revenue": revenues.get(tmdb_id, 0)})
    return httpx.MockTransport(handler)


class TopGrossingTests(unittest.TestCase):
    def setUp(self):
        box_office._memo.clear()
        box_office._failed_at.clear()
        box_office._locks.clear()
        self.state: dict[str, str] = {}
        for name, fn in (("get_app_state", self.state.get),
                         ("set_app_state", self.state.__setitem__)):
            patcher = mock.patch.object(box_office, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _ask(self, transport, tmdb_id, date="1997-12-19", top_n=10, floor=100_000_000):
        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                return await box_office.is_blockbuster(client, tmdb_id, date, "key")
        with mock.patch.object(box_office._cfg, "BLOCKBUSTER_TOP_N", top_n), \
             mock.patch.object(box_office._cfg, "BLOCKBUSTER_MIN_REVENUE", floor):
            return asyncio.run(run())

    def test_top_n_of_the_year_qualifies_and_the_rest_do_not(self):
        calls: list[str] = []
        ids = list(range(1, 21))
        t = _transport(ids, {i: 2_000_000_000 - i for i in ids}, calls)
        self.assertTrue(self._ask(t, "10"))
        self.assertFalse(self._ask(t, "11"))
        # Eleventh film is never looked up; the year is fetched once.
        self.assertEqual(sum(p.endswith("/discover/movie") for p in calls), 1)
        self.assertEqual(len(calls), 11)

    def test_floor_is_inflation_adjusted_and_unknown_revenue_excludes(self):
        # $100M today is about $16.7M in 1975 dollars.
        self.assertEqual(box_office.floor_for_year(1975, 100_000_000),
                         round(100_000_000 * 53.8 / box_office._CPI[box_office._CPI_LAST]))
        self.assertEqual(box_office.floor_for_year(2999, 100_000_000), 100_000_000)
        self.assertEqual(box_office.floor_for_year(1900, 100_000_000),
                         box_office.floor_for_year(1913, 100_000_000))
        calls: list[str] = []
        t = _transport([1, 2, 3], {1: 20_000_000, 2: 15_000_000}, calls)
        self.assertTrue(self._ask(t, "1", date="1975-01-01"))
        self.assertFalse(self._ask(t, "2", date="1975-01-01"))
        self.assertFalse(self._ask(t, "3", date="1975-01-01"))
        t = _transport([1, 2, 3], {1: 20_000_000, 2: 15_000_000}, calls)
        # Floor 0 still won't count TMDB's "unknown" revenue of 0.
        box_office._memo.clear(); self.state.clear()
        self.assertTrue(self._ask(t, "2", date="1975-01-01", floor=0))
        self.assertFalse(self._ask(t, "3", date="1975-01-01", floor=0))

    def test_stored_year_is_reused_across_workers(self):
        calls: list[str] = []
        t = _transport([7], {7: 900_000_000}, calls)
        self.assertTrue(self._ask(t, "7"))
        box_office._memo.clear()   # a different worker: only the shared row survives
        self.assertTrue(self._ask(t, "7"))
        self.assertEqual(len(calls), 2)

    def test_failure_is_no_sash_and_backs_off(self):
        calls: list[str] = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(500)
        t = httpx.MockTransport(handler)
        # Unknown, not "no": the render is kept provisional on it.
        self.assertIsNone(self._ask(t, "1"))
        self.assertIsNone(self._ask(t, "1"))
        self.assertEqual(len(calls), 1)

    def test_future_year_and_missing_date_make_no_call(self):
        calls: list[str] = []
        t = _transport([1], {1: 900_000_000}, calls)
        self.assertIs(self._ask(t, "1", date="2999-01-01"), False)
        self.assertIs(self._ask(t, "1", date=None), False)
        self.assertEqual(calls, [])


class BlockbusterSashTests(unittest.TestCase):
    def test_slot_label_and_movies_only(self):
        self.assertIn("blockbuster", ALL_PRIORITY_SLOTS)
        movie = extract_discovery_meta({}, "movie", [], [], None, is_blockbuster=True)
        self.assertEqual(pick_sash(movie, ["blockbuster"])[0], "Blockbuster")
        show = extract_discovery_meta({}, "series", [], [], None, is_blockbuster=True)
        self.assertIsNone(pick_sash(show, ["blockbuster"]))

    def test_default_order_puts_it_just_above_cult(self):
        order = main._parse_sash_priority(None)
        self.assertEqual(order.index("blockbuster") + 1, order.index("cult"))


if __name__ == "__main__":
    unittest.main()


class PrewarmTests(unittest.TestCase):
    """The background fill builds what renders would otherwise build inline."""
    setUp = TopGrossingTests.setUp
    _ask = TopGrossingTests._ask

    def _prewarm(self, transport, first_year):
        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                with mock.patch.object(box_office, "_PREWARM_PAUSE", 0):
                    return await box_office.prewarm_years(client, "key", first_year=first_year)
        return asyncio.run(run())

    def test_builds_missing_years_then_renders_make_no_calls(self):
        calls: list[str] = []
        t = _transport([1, 2], {1: 2_000_000_000, 2: 1_500_000_000}, calls)
        this_year = box_office.date.today().year
        self.assertEqual(self._prewarm(t, this_year - 2), 3)
        self.assertEqual(sum(p.endswith("/discover/movie") for p in calls), 3)
        calls.clear()
        self.assertTrue(self._ask(t, "1", date=f"{this_year - 1}-06-01"))
        self.assertEqual(calls, [])
        # A second run finds them all fresh.
        self.assertEqual(self._prewarm(t, this_year - 2), 0)
        self.assertEqual(calls, [])

    def test_a_failure_keeps_the_stored_list_and_ends_the_run(self):
        this_year = box_office.date.today().year
        key = box_office._state_key(this_year)
        # Built two days ago: past the current year's one-day TTL.
        self.state[key] = box_office.json.dumps({"at": box_office.time.time() - 2 * 86400, "ids": ["7"]})
        calls: list[str] = []

        def failing(request):
            calls.append(request.url.path)
            return httpx.Response(503)
        self.assertEqual(self._prewarm(httpx.MockTransport(failing), this_year - 5), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(box_office.json.loads(self.state[key])["ids"], ["7"])


class ReleasePrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_lookups_start_early_and_series_need_none(self):
        cfg = main.RequestConfig(sash_priority=["blockbuster", "release_status"])
        data = {"tmdb_release_date": "1997-12-19", "tmdb_status": "Released", "vote_count": 10}
        with mock.patch.object(main, "fetch_release_status", mock.AsyncMock(return_value="Streaming")) as rs, \
             mock.patch.object(box_office, "top_grossing_ids", mock.AsyncMock(return_value=frozenset())) as tg:
            status, block = main._start_release_prefetch(None, cfg, "movie", "597", data, True, "key")
            await main._settle_prefetch(status)
            await main._settle_prefetch(block)
            rs.assert_awaited_once()
            tg.assert_awaited_once_with(None, 1997, "key")
            self.assertEqual(main._start_release_prefetch(None, cfg, "tv", "1", data, True, "key"), (None, None))
            self.assertEqual(main._start_release_prefetch(None, cfg, "movie", "1", data, True, ""), (None, None))

    async def test_a_failed_lookup_is_left_for_the_real_call(self):
        cfg = main.RequestConfig(sash_priority=["release_status"])
        with mock.patch.object(main, "fetch_release_status", mock.AsyncMock(side_effect=httpx.ConnectError("x"))):
            status, block = main._start_release_prefetch(None, cfg, "movie", "1", {}, True, "key")
            await main._settle_prefetch(status)   # does not raise
            self.assertIsNone(block)
