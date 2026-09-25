"""The watchlist marker: one user's list, held instance-wide, rendered as a
sash on every title in it.

The source payloads are parsed by pure functions, so those are tested on
literal payloads shaped like each API's documented response.  Membership and
diffing are tested on the snapshot directly; the fetches themselves are
exercised against a fake httpx transport so the SIMKL activities gate and
the MDBList pagination can be checked without the network.
"""
import asyncio
import json
import unittest
from unittest import mock

import httpx

import config as _cfg
import watchlist
from discovery import extract_discovery_meta, pick_sash
from watchlist import (
    Snapshot,
    WatchlistEntry,
    diff_snapshots,
    parse_mdblist_items,
    parse_mdblist_watchlist,
    parse_simkl_items,
    parse_trakt_items,
    simkl_activity_fingerprint,
)


class ParserTests(unittest.TestCase):
    def test_mdblist_watchlist_keeps_both_ids_and_the_kind(self):
        payload = {
            "movies": [{"id": 1101383, "imdb_id": "tt27165187", "mediatype": "movie", "rank": 1000}],
            "shows":  [{"id": 1396, "imdb_id": "tt0903747", "mediatype": "show"}],
            "pagination": {"offset": 0, "limit": 500, "total": 2, "has_more": False},
        }
        got = parse_mdblist_watchlist(payload)
        self.assertEqual(got, [
            WatchlistEntry("tt27165187", "1101383", "movie"),
            WatchlistEntry("tt0903747", "1396", "show"),
        ])

    def test_mdblist_list_export_rows_carry_their_own_mediatype(self):
        rows = [
            {"id": 1, "imdb_id": "tt0000001", "mediatype": "movie"},
            {"id": 2, "imdb_id": "tt0000002", "mediatype": "show"},
            {"id": "junk", "imdb_id": None, "mediatype": "movie"},   # nothing usable
            "not a row",
        ]
        got = parse_mdblist_items(rows)
        self.assertEqual([e.kind for e in got], ["movie", "show"])

    def test_trakt_rows_wrap_the_media_object_under_its_type(self):
        rows = [
            {"rank": 1, "type": "movie", "movie": {"ids": {"trakt": 1, "imdb": "tt0111161", "tmdb": 278}}},
            {"rank": 2, "type": "movie", "movie": {"ids": {"trakt": 2}}},           # no usable id
        ]
        self.assertEqual(parse_trakt_items(rows, "movie"), [WatchlistEntry("tt0111161", "278", "movie")])
        # Asking for shows on a movie payload yields nothing rather than mislabelling.
        self.assertEqual(parse_trakt_items(rows, "show"), [])

    def test_simkl_sections_map_to_kinds_and_anime_films_are_movies(self):
        payload = {
            "movies": [{"status": "plantowatch", "movie": {"ids": {"simkl": 1, "imdb": "tt0000001", "tmdb": 11}}}],
            "shows":  [{"status": "plantowatch", "show":  {"ids": {"simkl": 2, "imdb": "tt0000002", "tmdb": 22}}}],
            "anime":  [
                {"status": "plantowatch", "anime_type": "tv",    "show": {"ids": {"simkl": 3, "mal": 5, "tmdb": 33}}},
                {"status": "plantowatch", "anime_type": "movie", "show": {"ids": {"simkl": 4, "imdb": "tt0000004"}}},
            ],
        }
        got = parse_simkl_items(payload)
        self.assertEqual(got, [
            WatchlistEntry("tt0000001", "11", "movie"),
            WatchlistEntry("tt0000002", "22", "show"),
            WatchlistEntry(None, "33", "show"),
            WatchlistEntry("tt0000004", None, "movie"),
        ])

    def test_simkl_empty_result_is_an_empty_object(self):
        self.assertEqual(parse_simkl_items({}), [])

    def test_activity_fingerprint_ignores_ratings_and_history(self):
        base = {
            "all": "2026-09-20T10:00:00Z",
            "movies":   {"all": "x", "plantowatch": "2026-09-01", "completed": "2026-09-20", "removed_from_list": "2026-08-01", "rated_at": "2026-09-20"},
            "tv_shows": {"all": "y", "plantowatch": "2026-09-02", "removed_from_list": None},
            "anime":    {"all": "z", "plantowatch": "2026-09-03"},
        }
        a = simkl_activity_fingerprint(base, ["plantowatch"])
        noisy = json.loads(json.dumps(base))
        noisy["movies"]["completed"] = "2026-09-21"
        noisy["movies"]["rated_at"]  = "2026-09-21"
        noisy["all"] = "2026-09-21T10:00:00Z"
        self.assertEqual(a, simkl_activity_fingerprint(noisy, ["plantowatch"]))

        moved = json.loads(json.dumps(base))
        moved["tv_shows"]["plantowatch"] = "2026-09-21"
        self.assertNotEqual(a, simkl_activity_fingerprint(moved, ["plantowatch"]))

        removed = json.loads(json.dumps(base))
        removed["movies"]["removed_from_list"] = "2026-09-21"
        self.assertNotEqual(a, simkl_activity_fingerprint(removed, ["plantowatch"]))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._saved = watchlist._snapshot
        watchlist._snapshot = Snapshot.from_entries([
            WatchlistEntry("tt0111161", "278", "movie"),
            WatchlistEntry(None, "1396", "show"),
            WatchlistEntry("tt0000009", None, "movie"),
        ], fetched_at=1.0)

    def tearDown(self):
        watchlist._snapshot = self._saved

    def test_membership_by_imdb_or_by_tmdb_and_kind(self):
        self.assertTrue(watchlist.is_listed("tt0111161", None, "movie"))
        self.assertTrue(watchlist.is_listed(None, "278", "movie"))
        self.assertTrue(watchlist.is_listed("tt0000009", "999", "movie"))
        # tv / series / show are one kind; a movie sharing the numeric id is not it.
        self.assertTrue(watchlist.is_listed(None, "1396", "tv"))
        self.assertTrue(watchlist.is_listed(None, "1396", "series"))
        self.assertFalse(watchlist.is_listed(None, "1396", "movie"))
        self.assertFalse(watchlist.is_listed("tt9999999", "1", "movie"))

    def test_diff_is_symmetric_so_removals_regenerate_too(self):
        old = watchlist._snapshot
        new = Snapshot.from_entries([
            WatchlistEntry("tt0111161", "278", "movie"),      # kept
            WatchlistEntry("tt0068646", "238", "movie"),      # added
        ], fetched_at=2.0)
        d = diff_snapshots(old, new)
        self.assertEqual(d.imdb, frozenset({"tt0068646", "tt0000009"}))
        self.assertEqual(d.tmdb, frozenset({("238", "movie"), ("1396", "show")}))
        self.assertTrue(d)
        self.assertFalse(diff_snapshots(new, new))

    def test_sash_slot_fires_only_on_membership(self):
        listed = extract_discovery_meta({}, "movie", [], [], None, is_watchlisted=True)
        unlisted = extract_discovery_meta({}, "movie", [], [], None)
        self.assertEqual(pick_sash(listed, ["watchlist", "wins"]), ("Watchlist", "watchlist"))
        self.assertIsNone(pick_sash(unlisted, ["watchlist"]))

    def test_watchlist_outranks_prestige_in_the_default_order(self):
        # The user put it there; that beats "Oscar Winner" unless they reorder.
        meta = extract_discovery_meta({}, "movie", ["Oscar Winner"], [], None, is_watchlisted=True)
        self.assertEqual(pick_sash(meta, list(_cfg.SASH_PRIORITY))[0], "Watchlist")

    def test_persisted_snapshot_is_only_restored_for_the_same_source(self):
        stored = {
            "source": "mdblist", "fetched_at": 5.0, "count": 1,
            "imdb": ["tt0000001"], "tmdb": [["1", "movie"]],
        }
        with mock.patch.object(watchlist, "get_app_state", return_value=json.dumps(stored)), \
             mock.patch.object(_cfg, "WATCHLIST_SOURCE", "mdblist"):
            watchlist._snapshot = Snapshot.empty()
            snap = watchlist.load_persisted()
            self.assertEqual(snap.count, 1)
            self.assertIn("tt0000001", snap.imdb)
        with mock.patch.object(watchlist, "get_app_state", return_value=json.dumps(stored)), \
             mock.patch.object(_cfg, "WATCHLIST_SOURCE", "trakt"):
            watchlist._snapshot = Snapshot.empty()
            self.assertEqual(watchlist.load_persisted().count, 0)


def _fake_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = watchlist._snapshot
        watchlist._snapshot = Snapshot.empty()
        self._state: dict[str, str] = {}
        self._patches = [
            mock.patch.object(watchlist, "get_app_state", side_effect=self._state.get),
            mock.patch.object(watchlist, "set_app_state", side_effect=self._state.__setitem__),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        watchlist._snapshot = self._saved

    async def test_mdblist_follows_pagination_and_persists_the_snapshot(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.url.params))
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                body = {"movies": [{"id": 1, "imdb_id": "tt0000001", "mediatype": "movie"}],
                        "shows": [], "pagination": {"offset": 0, "limit": 500, "total": 2, "has_more": True}}
            else:
                body = {"movies": [], "shows": [{"id": 2, "imdb_id": "tt0000002", "mediatype": "show"}],
                        "pagination": {"offset": 500, "limit": 500, "total": 2, "has_more": False}}
            return httpx.Response(200, json=body, headers={"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "900"})

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "mdblist"), \
             mock.patch.object(_cfg, "SERVER_MDBLIST_KEY", "k"):
            async with _fake_client(handler) as client:
                changed = await watchlist.refresh(client)

        self.assertEqual([c["offset"] for c in calls], ["0", "500"])
        self.assertEqual(changed.imdb, frozenset({"tt0000001", "tt0000002"}))
        self.assertTrue(watchlist.is_listed("tt0000002", None, "series"))
        self.assertEqual(json.loads(self._state["watchlist_snapshot"])["count"], 2)

    async def test_a_failed_fetch_keeps_the_previous_snapshot(self):
        watchlist._snapshot = Snapshot.from_entries([WatchlistEntry("tt0000001", "1", "movie")], 1.0)

        def handler(request):
            return httpx.Response(500, text="boom")

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "mdblist"), \
             mock.patch.object(_cfg, "SERVER_MDBLIST_KEY", "k"):
            async with _fake_client(handler) as client:
                self.assertIsNone(await watchlist.refresh(client))
        self.assertTrue(watchlist.is_listed("tt0000001", None, "movie"))
        # Status and host, never httpx's message: that carries the request
        # URL with ?apikey=, and status() is served on public endpoints.
        self.assertEqual(
            watchlist.status()["last_error"], "HTTP 500 from https://api.mdblist.com/watchlist/items"
        )

    async def test_simkl_reads_the_lists_only_when_activities_moved(self):
        seen: list[str] = []
        activities = {"movies": {"plantowatch": "2026-09-01"}, "tv_shows": {}, "anime": {}}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            # Every call carries SIMKL's required identification.
            self.assertEqual(request.url.params.get("app-name"), "postersplus")
            self.assertIn("client_id", request.url.params)
            self.assertTrue(request.headers["User-Agent"].startswith("postersplus/"))
            self.assertEqual(request.headers["Authorization"], "Bearer tok")
            if request.url.path == "/sync/activities":
                return httpx.Response(200, json=activities)
            self.assertEqual(request.url.params.get("extended"), "ids_only")
            if request.url.path == "/sync/all-items/movies/plantowatch":
                return httpx.Response(200, json={"movies": [{"movie": {"ids": {"imdb": "tt0000001", "tmdb": 1}}}]})
            return httpx.Response(200, json={})

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(_cfg, "SIMKL_CLIENT_ID", "cid"), \
             mock.patch.object(_cfg, "SIMKL_ACCESS_TOKEN", "tok"), \
             mock.patch.object(_cfg, "WATCHLIST_SIMKL_STATUSES", ["plantowatch"]):
            async with _fake_client(handler) as client:
                first = await watchlist.refresh(client)
                # Movies have no watching/hold list; plantowatch is asked of all three types, in order.
                self.assertEqual(seen, [
                    "/sync/activities",
                    "/sync/all-items/movies/plantowatch",
                    "/sync/all-items/shows/plantowatch",
                    "/sync/all-items/anime/plantowatch",
                ])
                self.assertEqual(first.imdb, frozenset({"tt0000001"}))

                seen.clear()
                self.assertIsNone(await watchlist.refresh(client))
                self.assertEqual(seen, ["/sync/activities"])

                activities["movies"]["plantowatch"] = "2026-09-21"
                seen.clear()
                self.assertIsNotNone(await watchlist.refresh(client))
                self.assertEqual(len(seen), 4)

    async def test_simkl_v1_app_falls_back_to_the_pin_flow(self):
        # The developer page still hands out AUTH V1 apps, which the V2 device
        # endpoint refuses with invalid_client.  Those go through /oauth/pin.
        polls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/oauth2/device":
                return httpx.Response(401, json={"error": "invalid_client",
                                                 "error_description": "This client_id is not enabled for OAuth 2.0"})
            if path == "/oauth/pin":
                return httpx.Response(200, json={"result": "OK", "device_code": "DEVICE_CODE", "user_code": "B52B8",
                                                 "verification_uri": "https://simkl.com/pin", "expires_in": 900, "interval": 0})
            if path == "/oauth/pin/B52B8":
                polls["n"] += 1
                if polls["n"] < 2:
                    return httpx.Response(200, json={"result": "KO", "message": "Authorization pending"})
                return httpx.Response(200, json={"result": "OK", "access_token": "v1tok"})
            if path == "/sync/activities":
                self.assertEqual(request.headers["Authorization"], "Bearer v1tok")
                return httpx.Response(200, json={"movies": {"plantowatch": "2026-09-01"}})
            if path.startswith("/sync/all-items/"):
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(_cfg, "SIMKL_CLIENT_ID", "cid"), \
             mock.patch.object(_cfg, "SIMKL_ACCESS_TOKEN", ""), \
             mock.patch.object(watchlist, "_simkl_next_device_prompt", 0.0), \
             mock.patch.object(asyncio, "sleep", mock.AsyncMock()):
            async with _fake_client(handler) as client:
                changed = await watchlist.refresh(client)
        self.assertIsNotNone(changed)
        saved = json.loads(self._state["simkl_tokens"])
        self.assertEqual(saved["access_token"], "v1tok")
        self.assertIsNone(saved["refresh_token"])
        # A V1 token has no refresh; it must not be treated as about to expire.
        self.assertGreater(saved["expires_at"], watchlist.time.time() + 365 * 86400)

    async def test_unlink_revokes_a_v2_grant_clears_state_and_reports_removals(self):
        watchlist._snapshot = Snapshot.from_entries([WatchlistEntry("tt0000001", "1", "movie")], 1.0)
        self._state["simkl_tokens"] = json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": 9e12})
        self._state["simkl_activities"] = "movies.plantowatch=x"
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, dict(request.headers).get("content-type")))
            body = request.content.decode()
            self.assertIn("token=rt", body)
            self.assertIn("client_id=cid", body)
            return httpx.Response(200, json={})

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(_cfg, "SIMKL_CLIENT_ID", "cid"), \
             mock.patch.object(_cfg, "SIMKL_CLIENT_SECRET", ""):
            async with _fake_client(handler) as client:
                result = await watchlist.simkl_unlink(client)
            self.assertFalse(watchlist.link_status()["simkl"]["linked"])
        self.assertEqual([c[0] for c in calls], ["/oauth2/revoke"])
        self.assertTrue(result["revoked"])
        self.assertEqual(result["flow"], "v2")
        # The title that had the marker is reported so its poster gets re-rendered.
        self.assertEqual(result["changed"].imdb, frozenset({"tt0000001"}))
        self.assertEqual(self._state["simkl_tokens"], "")
        self.assertEqual(self._state["simkl_activities"], "")
        self.assertEqual(watchlist.status()["titles"], 0)

    async def test_unlink_of_a_v1_token_does_not_call_revoke(self):
        self._state["simkl_tokens"] = json.dumps({"access_token": "v1", "refresh_token": None, "expires_at": 9e12})

        def handler(request):
            self.fail("V1 has no revoke endpoint; nothing should be called")

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), mock.patch.object(_cfg, "SIMKL_CLIENT_ID", "cid"):
            async with _fake_client(handler) as client:
                result = await watchlist.simkl_unlink(client)
        self.assertFalse(result["revoked"])
        self.assertEqual(result["flow"], "v1")
        self.assertEqual(self._state["simkl_tokens"], "")

    async def test_trakt_reads_the_public_profile_with_only_a_client_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["trakt-api-key"], "cid")
            self.assertEqual(request.headers["trakt-api-version"], "2")
            self.assertNotIn("Authorization", request.headers)
            if request.url.path == "/users/someone/watchlist/movies":
                return httpx.Response(200, json=[{"type": "movie", "movie": {"ids": {"imdb": "tt0000001", "tmdb": 1}}}])
            if request.url.path == "/users/someone/watchlist/shows":
                return httpx.Response(200, json=[{"type": "show", "show": {"ids": {"imdb": "tt0000002", "tmdb": 2}}}])
            return httpx.Response(404)

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "trakt"), \
             mock.patch.object(_cfg, "TRAKT_CLIENT_ID", "cid"), \
             mock.patch.object(_cfg, "TRAKT_USERNAME", "someone"), \
             mock.patch.object(_cfg, "TRAKT_ACCESS_TOKEN", ""):
            async with _fake_client(handler) as client:
                changed = await watchlist.refresh(client)
        self.assertEqual(changed.tmdb, frozenset({("1", "movie"), ("2", "show")}))


if __name__ == "__main__":
    unittest.main()
