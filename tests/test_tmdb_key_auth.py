"""TMDB credentials: a v4 Read Access Token goes as a Bearer header (#47)."""
import unittest

import httpx

import tmdb

V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzY29wZXMiOlsiYXBpX3JlYWQiXX0.c2ln"


class BearerAuthTests(unittest.IsolatedAsyncioTestCase):
    async def _sent(self, url: str, params: dict) -> httpx.Request:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks={"request": [tmdb.tmdb_bearer_auth]},
        ) as client:
            await client.get(url, params=params)
        return seen[0]

    async def test_a_read_access_token_moves_to_the_bearer_header(self):
        req = await self._sent("https://api.themoviedb.org/3/movie/550",
                               {"api_key": V4_TOKEN, "language": "hu"})
        self.assertEqual(req.headers["Authorization"], f"Bearer {V4_TOKEN}")
        self.assertNotIn("api_key", req.url.params)
        self.assertEqual(req.url.params["language"], "hu")

    async def test_a_v3_key_stays_a_query_param(self):
        req = await self._sent("https://api.themoviedb.org/3/movie/550", {"api_key": "abc123"})
        self.assertEqual(req.url.params["api_key"], "abc123")
        self.assertNotIn("Authorization", req.headers)

    async def test_other_hosts_are_left_alone(self):
        req = await self._sent("https://webservice.fanart.tv/v3/movies/550", {"api_key": V4_TOKEN})
        self.assertEqual(req.url.params["api_key"], V4_TOKEN)
        self.assertNotIn("Authorization", req.headers)


class KeyRejectedTests(unittest.TestCase):
    def _status_error(self, status: int, host: str = "api.themoviedb.org") -> httpx.HTTPStatusError:
        request = httpx.Request("GET", f"https://{host}/3/movie/550")
        return httpx.HTTPStatusError(
            str(status), request=request, response=httpx.Response(status, request=request))

    def test_a_tmdb_401_is_a_rejected_key(self):
        self.assertTrue(tmdb.tmdb_key_rejected(self._status_error(401)))

    def test_a_wrapped_401_is_found_through_the_cause(self):
        err = tmdb.IdResolveError("TMDB find failed")
        err.__cause__ = self._status_error(401)
        self.assertTrue(tmdb.tmdb_key_rejected(err))

    def test_other_failures_are_not(self):
        self.assertFalse(tmdb.tmdb_key_rejected(self._status_error(500)))
        self.assertFalse(tmdb.tmdb_key_rejected(self._status_error(401, "api.mdblist.com")))
        self.assertFalse(tmdb.tmdb_key_rejected(tmdb.IdResolveError("timeout")))
        self.assertFalse(tmdb.tmdb_key_rejected(None))


class FailureMessageTests(unittest.IsolatedAsyncioTestCase):
    async def _find_error(self, handler) -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(tmdb.IdResolveError) as ctx:
                await tmdb.tmdb_find_by_imdb(client, "tt0111161", "secret-v3-key")
        return str(ctx.exception)

    async def test_a_status_error_names_the_status_not_the_keyed_url(self):
        # The message can reach a client in a 502 detail.
        msg = await self._find_error(lambda request: httpx.Response(503))
        self.assertIn("HTTP 503", msg)
        self.assertNotIn("secret-v3-key", msg)

    async def test_a_timeout_names_its_type(self):
        def handler(request):
            raise httpx.ReadTimeout("", request=request)
        self.assertIn("ReadTimeout", await self._find_error(handler))


if __name__ == "__main__":
    unittest.main()
