"""A render's coalescing future is always resolved, and riding it can neither
hang a request nor break the render it rides.

Requests for the same uncached poster share one render: the first publishes a
future in _render_inflight and the rest await it.  A future left unresolved
hung every later request for that poster; a rider awaiting it bare, if
cancelled, cancelled the owner's render out from under it.
"""

import asyncio
import unittest
from unittest import mock

import httpx

import main
from tests.test_render_admission import _Gate


class CoalescingTests(unittest.IsolatedAsyncioTestCase):
    PARAMS = {"tmdb_id": "912345678", "type": "movie"}

    def setUp(self):
        self._saved = {
            "ACCESS_KEY": main._cfg.ACCESS_KEY,
            "SERVER_TMDB_KEY": main._cfg.SERVER_TMDB_KEY,
            "SERVER_MDBLIST_KEYS": main._cfg.SERVER_MDBLIST_KEYS,
            "DISABLE_COMPOSITE_CACHE": main._cfg.DISABLE_COMPOSITE_CACHE,
        }
        self._http_client = main._HTTP_CLIENT
        self._fetch_meta = main._coalesced_fetch_poster_metadata
        main._cfg.ACCESS_KEY = ""
        main._cfg.SERVER_TMDB_KEY = "test-key"
        main._cfg.SERVER_MDBLIST_KEYS = []
        main._cfg.DISABLE_COMPOSITE_CACHE = False
        main._HTTP_CLIENT = object()
        main._render_semaphore = None
        main._render_inflight.clear()
        self.gate = _Gate()
        main._coalesced_fetch_poster_metadata = self.gate

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(main._cfg, name, value)
        main._HTTP_CLIENT = self._http_client
        main._coalesced_fetch_poster_metadata = self._fetch_meta
        main._render_semaphore = None
        main._render_inflight.clear()

    async def _until(self, condition):
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition never became true")

    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t")

    async def test_a_second_request_rides_the_first(self):
        async with await self._client() as client:
            owner = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await self._until(lambda: self.gate.entered == 1)
            rider = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await asyncio.sleep(0.05)
            self.assertEqual(self.gate.entered, 1)
            self.gate.release.set()
            owner_resp, rider_resp = await asyncio.gather(owner, rider)
        self.assertEqual(self.gate.entered, 2)   # the failed render: the rider tried itself
        self.assertEqual((owner_resp.status_code, rider_resp.status_code), (404, 404))
        self.assertEqual(main._render_inflight, {})

    async def test_cancelled_owner_releases_its_riders(self):
        # The owner's handlers caught Exception only, so a cancellation left
        # the future unresolved and the rider waiting on it for good.
        async with await self._client() as client:
            owner = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await self._until(lambda: self.gate.entered == 1)
            rider = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await asyncio.sleep(0.05)
            owner.cancel()
            # The rider falls through and renders the poster itself.
            await self._until(lambda: self.gate.entered == 2)
            self.gate.release.set()
            resp = await asyncio.wait_for(rider, 5)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(main._render_inflight, {})

    async def test_cancelled_rider_leaves_the_owners_future_alone(self):
        async with await self._client() as client:
            owner = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await self._until(lambda: self.gate.entered == 1)
            (fut,) = main._render_inflight.values()
            rider = asyncio.create_task(client.get("/poster", params=self.PARAMS))
            await asyncio.sleep(0.05)
            rider.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(fut.cancelled())
            self.gate.release.set()
            self.assertEqual((await owner).status_code, 404)

    async def test_no_http_client_publishes_nothing(self):
        main._HTTP_CLIENT = None
        async with await self._client() as client:
            resp = await client.get("/poster", params=self.PARAMS)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(main._render_inflight, {})


class RideTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        main._render_inflight.clear()

    async def test_an_unresolved_render_times_out_instead_of_hanging(self):
        fut = asyncio.get_running_loop().create_future()
        main._render_inflight["k"] = fut
        with mock.patch.object(main, "_RENDER_COALESCE_TIMEOUT", 0.01):
            self.assertIsNone(await main._ride_inflight_render(None, "k"))
        self.assertFalse(fut.done())   # shielded: the timeout did not cancel it

    async def test_unpublish_only_removes_its_own_future(self):
        loop = asyncio.get_running_loop()
        mine, theirs = loop.create_future(), loop.create_future()
        main._render_inflight["k"] = theirs
        main._unpublish_render("k", mine)
        self.assertIs(main._render_inflight["k"], theirs)
        self.assertIsInstance(mine.exception(), main._RenderAbandoned)
        main._unpublish_render("k", theirs)
        self.assertNotIn("k", main._render_inflight)


if __name__ == "__main__":
    unittest.main()
