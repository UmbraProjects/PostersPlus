import asyncio
import time
import unittest

import httpx

import main
import ratings


class MDBListBackoffTests(unittest.TestCase):
    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        self.active_key_idx = main._mdblist_active_key_idx
        main._rating_backoff.clear()
        main._rating_fail_count.clear()
        main._mdblist_key_cooldown.clear()

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._mdblist_active_key_idx = self.active_key_idx
        main._rating_backoff.clear()
        main._rating_fail_count.clear()
        main._mdblist_key_cooldown.clear()

    def test_replacement_key_is_not_blocked_by_title_backoff(self):
        title = "tt11347692"
        first_key = main._rating_retry_key(title, "exhausted-key")
        replacement_key = main._rating_retry_key(title, "healthy-key")

        main._rating_backoff[first_key] = 3600.0

        self.assertIn(first_key, main._rating_backoff)
        self.assertNotIn(replacement_key, main._rating_backoff)

    def test_failure_escalation_is_independent_per_key(self):
        title = "tt11347692"
        first_key = main._rating_retry_key(title, "key-1")
        second_key = main._rating_retry_key(title, "key-2")

        main._rating_fail_count[first_key] = 3

        self.assertEqual(main._rating_fail_count[first_key], 3)
        self.assertEqual(main._rating_fail_count.get(second_key, 0), 0)

    def test_rotation_selects_next_healthy_server_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown["key-1"] = 100.0

        selected = main._next_mdblist_server_key("key-1", now=10.0)

        self.assertEqual(selected, "key-2")
        self.assertEqual(main._mdblist_active_key_idx, 1)

    def test_rotation_can_fall_back_to_primary_after_secondary_limit(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown["key-2"] = 100.0

        selected = main._next_mdblist_server_key("key-2", now=10.0)

        self.assertEqual(selected, "key-1")
        self.assertEqual(main._mdblist_active_key_idx, 0)
        self.assertEqual(main._mdblist_server_key_label(selected), "configured key #1")

    def test_spent_query_supplied_key_hands_over_to_the_active_server_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_active_key_idx = 1
        self.assertEqual(main._next_mdblist_server_key("user-key", now=10.0), "key-2")

    def test_query_supplied_key_skips_cooling_server_keys(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_active_key_idx = 0
        main._mdblist_key_cooldown["key-1"] = 100.0
        self.assertEqual(main._next_mdblist_server_key("user-key", now=10.0), "key-2")
        self.assertEqual(main._mdblist_active_key_idx, 1)

    def test_single_server_key_can_replace_a_query_supplied_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1"]
        main._mdblist_active_key_idx = 0
        self.assertEqual(main._next_mdblist_server_key("user-key", now=10.0), "key-1")

    def test_no_replacement_without_a_healthy_server_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = []
        self.assertIsNone(main._next_mdblist_server_key("user-key", now=10.0))
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1"]
        main._mdblist_key_cooldown["key-1"] = 100.0
        self.assertIsNone(main._next_mdblist_server_key("user-key", now=10.0))

    def test_same_key_and_title_share_retry_state(self):
        self.assertEqual(
            main._rating_retry_key("tt11347692", "key-2"),
            main._rating_retry_key("tt11347692", "key-2"),
        )


class MDBListRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        main._cfg.SERVER_MDBLIST_KEYS = ["server-key-1", "server-key-2"]
        main._rating_backoff.clear()
        main._mdblist_key_cooldown.clear()
        main._mdblist_ip_pause_until = 0.0

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._rating_backoff.clear()
        main._mdblist_key_cooldown.clear()
        main._mdblist_ip_pause_until = 0.0

    async def test_query_key_quota_limit_falls_back_to_a_server_key(self):
        result = main._RateLimited(retry_after=60, reset_at=time.time() + 3600)

        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "request-key", result
        )

        self.assertEqual(delay, 60)
        self.assertIn(fallback, main._cfg.SERVER_MDBLIST_KEYS)
        self.assertIn("request-key", main._mdblist_key_cooldown)

    async def test_query_key_burst_limit_does_not_switch_keys(self):
        # Per-IP: the server's keys would be refused for the same window.
        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "request-key", main._RateLimited(retry_after=10)
        )
        self.assertIsNone(fallback)
        self.assertNotIn("request-key", main._mdblist_key_cooldown)

    # -- burst (per-IP) limit -------------------------------------------------

    async def test_burst_429_pauses_process_not_key(self):
        """Retry-After with quota left is MDBList's per-IP burst limit: every key
        on the address is refused, so rotating is pointless and cooling the key
        down for an hour is wrong."""
        result = main._RateLimited(retry_after=10)

        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "server-key-1", result
        )

        self.assertEqual(delay, 10)
        self.assertIsNone(fallback)
        self.assertNotIn("server-key-1", main._mdblist_key_cooldown)
        self.assertEqual(main._rating_backoff, {})
        self.assertAlmostEqual(main._mdblist_ip_pause_remaining(), 10, delta=0.5)

    async def test_burst_429_without_retry_after_pauses_briefly(self):
        """The contributor's case: a 429 with no Retry-After and 24k calls left
        used to park the key for 3600 s."""
        result = main._RateLimited(retry_after=None, reset_at=None)

        delay, _ = main._mark_mdblist_rate_limit("tt11347692", "server-key-1", result)

        self.assertEqual(delay, main._MDBLIST_BURST_PAUSE_DEFAULT)
        self.assertNotIn("server-key-1", main._mdblist_key_cooldown)

    async def test_burst_pause_is_capped_and_only_extends(self):
        main._mark_mdblist_rate_limit("tt1", "server-key-1", main._RateLimited(retry_after=9999))
        self.assertAlmostEqual(main._mdblist_ip_pause_remaining(), main._MDBLIST_BURST_PAUSE_MAX, delta=0.5)

        main._mark_mdblist_rate_limit("tt2", "server-key-2", main._RateLimited(retry_after=5))
        self.assertAlmostEqual(main._mdblist_ip_pause_remaining(), main._MDBLIST_BURST_PAUSE_MAX, delta=0.5)

    async def test_burst_pause_leaves_sibling_keys_selectable_afterwards(self):
        main._mark_mdblist_rate_limit("tt1", "server-key-1", main._RateLimited(retry_after=10))
        main._mdblist_ip_pause_until = 0.0  # pause over

        self.assertEqual(main._next_mdblist_server_key("server-key-1"), "server-key-2")
        self.assertEqual(
            main._warm_mdblist_key_with_quota("server-key-1", asyncio.get_running_loop().time(), 0),
            "server-key-1",
        )

    # -- quota (per-key) limit ------------------------------------------------

    async def test_quota_429_without_retry_after_sleeps_until_reset(self):
        reset_at = time.time() + 5 * 3600
        result = main._RateLimited(retry_after=None, reset_at=reset_at)

        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "server-key-1", result
        )

        self.assertAlmostEqual(delay, 5 * 3600, delta=5)
        self.assertEqual(fallback, "server-key-2")

    async def test_retry_after_still_wins_over_reset(self):
        result = main._RateLimited(retry_after=60, reset_at=time.time() + 5 * 3600)

        delay, _ = main._mark_mdblist_rate_limit("tt11347692", "server-key-1", result)

        self.assertEqual(delay, 60)

    async def test_reset_in_the_past_still_cools_briefly(self):
        result = main._RateLimited(retry_after=None, reset_at=time.time() - 10)

        delay, _ = main._mark_mdblist_rate_limit("tt11347692", "server-key-1", result)

        self.assertEqual(delay, 60.0)


class MDBListQuotaTrackingTests(unittest.IsolatedAsyncioTestCase):
    """fetch_rating records X-RateLimit-* headers and the warmer honours them."""

    def setUp(self):
        ratings.MDBLIST_QUOTA.clear()

    def tearDown(self):
        ratings.MDBLIST_QUOTA.clear()

    @staticmethod
    def _client(status: int, headers: dict, body=None):
        async def handler(request):
            return httpx.Response(status, headers=headers, json=body if body is not None else {})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_success_response_records_quota(self):
        reset = int(time.time()) + 3600
        async with self._client(200, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "647",
            "x-ratelimit-reset": str(reset),
        }, {"ratings": [], "keywords": []}) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertNotIsInstance(result, main._RateLimited)
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 647)
        self.assertEqual(ratings.MDBLIST_QUOTA["key-a"].limit, 1000)
        self.assertEqual(ratings.MDBLIST_QUOTA["key-a"].reset_at, float(reset))

    async def test_quota_429_carries_reset_and_zero_remaining(self):
        reset = int(time.time()) + 3600
        async with self._client(429, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(reset),
        }) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertIsNone(result.retry_after)
        self.assertEqual(result.reset_at, float(reset))
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 0)

    async def test_429_with_quota_left_does_not_carry_reset(self):
        async with self._client(429, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "412",
            "x-ratelimit-reset": str(int(time.time()) + 3600),
        }) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertIsNone(result.reset_at)
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 412)

    async def test_503_is_a_burst_signal_not_a_network_failure(self):
        """A run of 503s is how the per-IP burst limit shows itself first; it
        must not walk the 30s/2m/8m/1h network-failure ladder."""
        async with self._client(503, {"retry-after": "10"}) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertEqual(result.retry_after, 10)
        self.assertFalse(result.quota_exhausted)

    async def test_503_never_counts_as_quota_exhaustion(self):
        async with self._client(503, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(time.time()) + 3600),
        }) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertFalse(result.quota_exhausted)

    async def test_stale_snapshot_is_unknown_after_reset(self):
        async with self._client(200, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "3",
            "x-ratelimit-reset": str(int(time.time()) - 1),
        }, {"ratings": [], "keywords": []}) as client:
            await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsNone(ratings.mdblist_quota_remaining("key-a"))

    async def test_missing_headers_leave_quota_unknown(self):
        async with self._client(200, {}, {"ratings": [], "keywords": []}) as client:
            await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertNotIn("key-a", ratings.MDBLIST_QUOTA)
        self.assertIsNone(ratings.mdblist_quota_remaining("key-a"))


class CacheWarmKeyFallbackTests(unittest.TestCase):
    """The warmer moves to key #2 when key #1 is limited or at its reserve."""

    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        self.active_key_idx = main._mdblist_active_key_idx
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown.clear()
        ratings.MDBLIST_QUOTA.clear()

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._mdblist_active_key_idx = self.active_key_idx
        main._mdblist_key_cooldown.clear()
        ratings.MDBLIST_QUOTA.clear()

    @staticmethod
    def _quota(key, remaining):
        ratings.MDBLIST_QUOTA[key] = ratings.MDBListQuota(1000, remaining, time.time() + 3600, time.time())

    def test_keeps_current_key_when_quota_unknown(self):
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-1")

    def test_keeps_current_key_above_reserve(self):
        self._quota("key-1", 301)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-1")

    def test_moves_to_sibling_at_reserve_without_changing_live_key(self):
        self._quota("key-1", 300)
        main._mdblist_active_key_idx = 0

        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-2")
        self.assertEqual(main._mdblist_active_key_idx, 0)

    def test_moves_to_sibling_when_current_is_cooling_down(self):
        main._mdblist_key_cooldown["key-1"] = 100.0
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-2")

    def test_stops_when_every_key_is_spent_or_cooling(self):
        self._quota("key-1", 0)
        main._mdblist_key_cooldown["key-2"] = 100.0
        self.assertIsNone(main._warm_mdblist_key_with_quota("key-1", 10.0, 300))

    def test_reserve_zero_spends_down_to_the_last_request(self):
        self._quota("key-1", 1)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 0), "key-1")
        self._quota("key-1", 0)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 0), "key-2")

    def test_rate_limit_then_reserve_chains_to_second_key(self):
        """Key #1 429s (rotates), later reaches reserve on key #2 -> stop."""
        async def run():
            result = main._RateLimited(retry_after=None, reset_at=time.time() + 3600)
            _, replacement = main._mark_mdblist_rate_limit("tt0111161", "key-1", result)
            self.assertEqual(replacement, "key-2")
            now = main.asyncio.get_running_loop().time()
            self.assertEqual(main._warm_mdblist_key_with_quota("key-2", now, 300), "key-2")
            self._quota("key-2", 250)
            self.assertIsNone(main._warm_mdblist_key_with_quota("key-2", now, 300))
        main.asyncio.run(run())


if __name__ == "__main__":
    unittest.main()


class MDBListPacingTests(unittest.IsolatedAsyncioTestCase):
    """_mdblist_wait_for_slot spaces request starts and sits out a burst pause."""

    def setUp(self):
        self.interval = main._cfg.MDBLIST_MIN_INTERVAL
        main._mdblist_next_slot = 0.0
        main._mdblist_ip_pause_until = 0.0

    def tearDown(self):
        main._cfg.MDBLIST_MIN_INTERVAL = self.interval
        main._mdblist_next_slot = 0.0
        main._mdblist_ip_pause_until = 0.0

    async def test_concurrent_callers_are_spaced_by_the_interval(self):
        main._cfg.MDBLIST_MIN_INTERVAL = 0.05
        loop = asyncio.get_running_loop()
        starts: list[float] = []

        async def caller():
            await main._mdblist_wait_for_slot()
            starts.append(loop.time())

        await asyncio.gather(*(caller() for _ in range(4)))

        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(gaps), 3)
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.05 - 0.005)

    async def test_zero_interval_is_a_no_op(self):
        main._cfg.MDBLIST_MIN_INTERVAL = 0.0
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(5):
            await main._mdblist_wait_for_slot()
        self.assertLess(loop.time() - t0, 0.02)

    async def test_burst_pause_is_waited_out_before_the_slot(self):
        main._cfg.MDBLIST_MIN_INTERVAL = 0.0
        loop = asyncio.get_running_loop()
        main._mdblist_ip_pause_until = loop.time() + 0.1
        t0 = loop.time()
        await main._mdblist_wait_for_slot()
        self.assertGreaterEqual(loop.time() - t0, 0.1 - 0.005)
        self.assertEqual(main._mdblist_ip_pause_remaining(), 0.0)
