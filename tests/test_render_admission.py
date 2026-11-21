"""Fresh renders are admitted through POSTER_RENDER_CONCURRENCY.

A cold catalog grid asks for 50+ posters in one second. Each render fans out to
several upstream calls, so uncapped that burst asks the shared httpx pool for
several times its connection budget and everything past it fails with
PoolTimeout — while cached posters keep returning fine, because they never
touch the pool. The per-source caps (MDBLIST_CONCURRENCY etc.) limit one
upstream each; nothing limited the number of renders competing for the pool.

These tests drive the real /poster handler with the first upstream call inside
the gated section stubbed, and measure how many stubs are ever inside it at
once.
"""

import asyncio
import unittest
from unittest import mock

import httpx

import main


class _Gate:
    """Stub for the metadata fetch: records peak concurrency, then holds every
    caller until the test releases them so overlap is deterministic."""

    def __init__(self):
        self.active = 0
        self.peak = 0
        self.entered = 0
        self.release = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.active += 1
        self.entered += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        # Ends the pipeline on its ValueError path (404): the handler still
        # runs its finally block, which is what releases the slot.
        raise ValueError("stubbed: no poster")


class RenderAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = {
            "ACCESS_KEY": main._cfg.ACCESS_KEY,
            "SERVER_TMDB_KEY": main._cfg.SERVER_TMDB_KEY,
            "SERVER_MDBLIST_KEYS": main._cfg.SERVER_MDBLIST_KEYS,
            "POSTER_RENDER_CONCURRENCY": main._cfg.POSTER_RENDER_CONCURRENCY,
        }
        self._http_client = main._HTTP_CLIENT
        self._fetch_meta = main._coalesced_fetch_poster_metadata
        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = "test-key"
        main._cfg.SERVER_MDBLIST_KEYS = []
        # The handler only checks that a client exists before it renders.
        main._HTTP_CLIENT = object()
        main._render_semaphore = None
        main._renders_queued = 0
        main._active_poster_renders = 0

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)
        main._HTTP_CLIENT = self._http_client
        main._coalesced_fetch_poster_metadata = self._fetch_meta
        main._render_semaphore = None
        main._renders_queued = 0
        main._active_poster_renders = 0

    async def _burst(self, gate, n, cap):
        main._cfg.POSTER_RENDER_CONCURRENCY = cap
        main._coalesced_fetch_poster_metadata = gate
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            tasks = [
                asyncio.create_task(client.get(
                    "/poster",
                    # Distinct ids so nothing is coalesced or cache-hit; the
                    # cache-buster keeps a stale composite row from any
                    # earlier run out of the way.
                    params={"tmdb_id": str(900000000 + i), "type": "movie", "cb": "admission"},
                ))
                for i in range(n)
            ]
            # Let every request run up to the gate (or the semaphore).
            for _ in range(20):
                await asyncio.sleep(0.01)
                if gate.entered >= min(n, cap) and main._renders_queued == max(0, n - cap):
                    break
            observed = (gate.active, main._renders_queued, main._active_poster_renders)
            gate.release.set()
            responses = await asyncio.gather(*tasks)
        return observed, responses

    async def test_burst_is_capped_and_the_rest_queue(self):
        gate = _Gate()
        (active, queued, holding), responses = await self._burst(gate, n=7, cap=3)

        self.assertEqual(active, 3)
        self.assertEqual(queued, 4)
        self.assertEqual(holding, 3)
        self.assertEqual(gate.peak, 3)
        # Every request still completes once slots free up — nothing is dropped.
        self.assertEqual(gate.entered, 7)
        self.assertEqual({r.status_code for r in responses}, {404})

    async def test_slots_are_returned_after_a_failed_render(self):
        gate = _Gate()
        await self._burst(gate, n=5, cap=2)

        sem = main._get_render_semaphore()
        self.assertFalse(sem.locked())
        self.assertEqual(main._renders_queued, 0)
        self.assertEqual(main._active_poster_renders, 0)
        self.assertEqual(main._render_inflight, {})

    async def test_cap_of_one_serialises(self):
        gate = _Gate()
        (active, queued, _), _ = await self._burst(gate, n=4, cap=1)
        self.assertEqual((active, queued), (1, 3))
        self.assertEqual(gate.peak, 1)


class RenderQueueTimeoutTests(RenderAdmissionTests):
    """A render that can't get a slot within POSTER_RENDER_QUEUE_TIMEOUT is
    answered 503, and so is everyone riding it: unbounded, a burst bigger than
    the host could render queued for good (12k deep on a public instance)."""

    async def test_burst_past_the_queue_timeout_is_turned_away(self):
        gate = _Gate()
        main._cfg.POSTER_RENDER_CONCURRENCY = 2
        main._coalesced_fetch_poster_metadata = gate
        main._render_inflight.clear()
        with mock.patch.object(main._cfg, "POSTER_RENDER_QUEUE_TIMEOUT", 0.05):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                held = [asyncio.create_task(client.get("/poster", params={
                    "tmdb_id": str(910000000 + i), "type": "movie", "cb": "qtimeout"}))
                    for i in range(2)]
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if gate.entered == 2:
                        break
                # One queued request and a rider on it; both outlast the wait.
                params = {"tmdb_id": "910000099", "type": "movie", "cb": "qtimeout"}
                queued = asyncio.create_task(client.get("/poster", params=params))
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if len(main._render_inflight) == 3:
                        break
                rider = asyncio.create_task(client.get("/poster", params=params))
                queued_resp, rider_resp = await asyncio.wait_for(
                    asyncio.gather(queued, rider), 5)
                gate.release.set()
                held_resps = await asyncio.gather(*held)
        self.assertEqual((queued_resp.status_code, rider_resp.status_code), (503, 503))
        self.assertEqual(queued_resp.headers["retry-after"], "30")
        self.assertEqual(queued_resp.headers["cache-control"], "no-store")
        self.assertEqual(gate.entered, 2)         # neither rendered
        self.assertEqual({r.status_code for r in held_resps}, {404})
        self.assertEqual(main._renders_queued, 0)
        self.assertFalse(main._get_render_semaphore().locked())
        self.assertEqual(main._render_inflight, {})

    async def test_zero_waits_indefinitely(self):
        with mock.patch.object(main._cfg, "POSTER_RENDER_QUEUE_TIMEOUT", 0):
            sem = asyncio.Semaphore(0)
            waiter = asyncio.create_task(main._acquire_render_slot(sem))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            sem.release()
            self.assertTrue(await waiter)


class HttpPoolSizingTests(unittest.TestCase):
    """The pool must fit the renders the cap admits, or the cap is theatre."""

    def test_default_cap_fits_the_pool(self):
        self.assertGreaterEqual(main._http_pool_size(8), 8 * main._HTTP_CALLS_PER_RENDER)

    def test_pool_never_shrinks_below_the_old_size(self):
        self.assertEqual(main._http_pool_size(1), main._HTTP_POOL_MIN_CONNECTIONS)

    def test_raised_cap_grows_the_pool(self):
        bigger = main._http_pool_size(32)
        self.assertGreater(bigger, main._HTTP_POOL_MIN_CONNECTIONS)
        self.assertGreaterEqual(bigger, 32 * main._HTTP_CALLS_PER_RENDER)

    def test_config_floor_is_one(self):
        self.assertGreaterEqual(main._cfg.POSTER_RENDER_CONCURRENCY, 1)


if __name__ == "__main__":
    unittest.main()
