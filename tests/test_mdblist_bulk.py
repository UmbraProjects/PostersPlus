"""MDBList bulk ratings: fetch_ratings_bulk, the live micro-batcher, the warm
cycle's bulk path, and the Wikipedia-derived award lists that replace the
keywords the bulk answer doesn't carry.

The bulk answer fixture (tests/data/mdblist_bulk_movie.json) is a real
``POST /imdb/movie`` answer for The Shawshank Redemption, The Godfather,
Breaking Bad and an unknown id, trimmed to the fields read: MDBList returned
the two films, in its own order, and left the show and the unknown id out.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import httpx

import award_lists
import cache
import festivals
import main
import ratings
import tmdb
from awards import FETCH_FAILED, _RateLimited
from mdblist_batch import RatingBatcher

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_award_lists as builder  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "data" / "mdblist_bulk_movie.json").read_text(encoding="utf-8"))
SHAWSHANK, GODFATHER = "tt0111161", "tt0068646"


def _record(imdb_id: str, mdb_type: str = "movie", **extra) -> dict:
    return {"ids": {"imdb": imdb_id, "tmdb": 1}, "type": mdb_type, "released": "2000-01-01",
            "ratings": [{"source": "imdb", "value": 7.0, "votes": 5000}], **extra}


class _BulkServer:
    """A MockTransport MDBList: answers bulk POSTs from *known* ({type: {id: record}})."""

    def __init__(self, known: dict[str, dict[str, dict]], status: int = 200, body=None, headers=None):
        self.known, self.status, self.body, self.headers = known, status, body, headers or {}
        self.posts: list[tuple[str, list[str]]] = []
        self.gets: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets.append(request.url.path)
            return httpx.Response(404, json={})
        ids = json.loads(request.content)["ids"]
        mdb_type = request.url.path.rsplit("/", 1)[1]
        self.posts.append((request.url.path, ids))
        if self.status != 200 or self.body is not None:
            return httpx.Response(self.status, headers=self.headers, json=self.body if self.body is not None else {})
        found = [self.known.get(mdb_type, {})[i] for i in ids if i in self.known.get(mdb_type, {})]
        return httpx.Response(200, headers=self.headers, json=list(reversed(found)))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class _RefusingServer(_BulkServer):
    """A _BulkServer that answers POST number *refuse_at* (1-based) with a 503."""

    def __init__(self, known, refuse_at: int):
        super().__init__(known)
        self.refuse_at = refuse_at

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and len(self.posts) + 1 == self.refuse_at:
            self.posts.append((request.url.path, json.loads(request.content)["ids"]))
            return httpx.Response(503, json={})
        return super().handler(request)


class _BulkTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ratings._bulk_disabled_until = 0.0
        ratings.MDBLIST_QUOTA.clear()
        # The dates / genre side-caches write to the DB; not under test here.
        for name in ("remember_mdblist_release_dates", "remember_mdblist_tv_horror"):
            patcher = mock.patch.object(ratings, name, lambda *a, **kw: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        ratings._bulk_disabled_until = 0.0
        ratings.MDBLIST_QUOTA.clear()


class FetchRatingsBulkTests(_BulkTestCase):
    async def test_real_answer_is_parsed_like_the_single_call(self):
        server = _BulkServer({}, body=FIXTURE, headers={"x-ratelimit-remaining": "806", "x-ratelimit-limit": "1000"})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", [SHAWSHANK, GODFATHER])
        self.assertEqual(server.posts[0], ("/imdb/movie", [SHAWSHANK, GODFATHER]))
        ratings_dict, _genre, released, keywords, age = out[GODFATHER]
        self.assertEqual(ratings_dict["imdb"], 9.2)
        self.assertEqual(ratings_dict["metacritic"], 100)
        # The bulk value is 9.0, out of 10; the single call's 4.5 is kept.
        self.assertEqual(ratings_dict["letterboxd"], 4.5)
        self.assertEqual(released, "1972-03-14")
        self.assertEqual(age, 16)
        # Metacritic 100 from 16 reviews: Must-See by the score rule.
        self.assertIn({"name": "metacritic-must-see"}, keywords)
        self.assertEqual(out[SHAWSHANK][2], "1994-09-23")
        self.assertEqual(ratings.mdblist_quota_remaining("key"), 806)

    async def test_vote_floor_applies(self):
        rec = _record("tt1", ratings=[{"source": "imdb", "value": 7.0, "votes": 3},
                                      {"source": "tomatoes", "value": 80, "votes": 200}])
        server = _BulkServer({"movie": {"tt1": rec}})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertEqual(out["tt1"][0], {"tomatoes": 80})

    async def test_metacritic_rule_needs_fifteen_reviews(self):
        rec = _record("tt1", ratings=[{"source": "metacritic", "value": 90, "votes": 14}])
        server = _BulkServer({"movie": {"tt1": rec}})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertEqual(out["tt1"][3], [])

    async def test_ids_are_sent_200_to_a_request(self):
        ids = [f"tt{i:07d}" for i in range(450)]
        server = _BulkServer({"movie": {i: _record(i) for i in ids}})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ids)
        self.assertEqual([len(p[1]) for p in server.posts], [200, 200, 50])
        self.assertEqual(set(out), set(ids))

    async def test_missing_imdb_id_is_asked_as_the_other_type_then_not_found(self):
        server = _BulkServer({"movie": {"tt1": _record("tt1")}, "show": {"tt2": _record("tt2", "show")}})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1", "tt2", "tt3"])
        self.assertEqual(server.posts, [("/imdb/movie", ["tt1", "tt2", "tt3"]), ("/imdb/show", ["tt2", "tt3"])])
        self.assertEqual(out["tt2"][0], {"imdb": 7.0})
        self.assertEqual(out["tt3"], ({}, out["tt3"][1], None, [], None))

    async def test_tmdb_ids_are_not_retried_as_the_other_type(self):
        server = _BulkServer({"show": {}})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "tmdb", "show", ["123"])
        self.assertEqual(len(server.posts), 1)
        self.assertEqual(out["123"][0], {})

    async def test_a_later_refusal_keeps_the_answers_already_received(self):
        ids = [f"tt{i:07d}" for i in range(250)]
        server = _RefusingServer({"movie": {i: _record(i) for i in ids}}, refuse_at=2)
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ids)
        self.assertEqual(len(server.posts), 2)
        self.assertTrue(all(isinstance(out[i], tuple) for i in ids[:200]))
        self.assertTrue(all(isinstance(out[i], _RateLimited) for i in ids[200:]))

    async def test_a_refused_reask_leaves_its_ids_unanswered_not_missing(self):
        server = _RefusingServer({"movie": {"tt1": _record("tt1")}}, refuse_at=2)
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1", "tt2"])
        self.assertEqual(out["tt1"][0], {"imdb": 7.0})
        self.assertIsInstance(out["tt2"], _RateLimited)

    async def test_a_refusal_before_any_answer_is_returned_alone(self):
        server = _RefusingServer({"movie": {"tt1": _record("tt1")}}, refuse_at=1)
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertIsInstance(out, _RateLimited)

    async def test_on_request_is_called_for_every_post(self):
        sent = []
        server = _BulkServer({"movie": {"tt1": _record("tt1")}, "show": {"tt2": _record("tt2", "show")}})
        async with server.client() as client:
            await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1", "tt2"],
                                             on_request=lambda: sent.append(1))
        self.assertEqual(len(sent), len(server.posts), 2)

    async def test_429_is_rate_limited_and_keeps_bulk_on(self):
        server = _BulkServer({}, status=429, headers={"retry-after": "10", "x-ratelimit-remaining": "5"})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1", "tt2"])
        self.assertIsInstance(out, _RateLimited)
        self.assertEqual(out.retry_after, 10.0)
        self.assertTrue(ratings.mdblist_bulk_available())

    async def test_quota_429_carries_the_reset(self):
        reset = int(time.time()) + 3600
        server = _BulkServer({}, status=429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1", "tt2"])
        self.assertTrue(out.quota_exhausted)

    async def test_503_is_rate_limited(self):
        server = _BulkServer({}, status=503)
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertIsInstance(out, _RateLimited)

    async def test_error_status_disables_bulk_for_an_hour(self):
        server = _BulkServer({}, status=400)
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
            self.assertIs(out, FETCH_FAILED)
            self.assertFalse(ratings.mdblist_bulk_available())
            self.assertTrue(ratings.mdblist_bulk_available(time.time() + 3601))
            again = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertIs(again, FETCH_FAILED)
        self.assertEqual(len(server.posts), 1)   # no request while disabled

    async def test_unexpected_shape_disables_bulk(self):
        server = _BulkServer({}, body={"error": "nope"})
        async with server.client() as client:
            out = await ratings.fetch_ratings_bulk(client, "key", "imdb", "movie", ["tt1"])
        self.assertIs(out, FETCH_FAILED)
        self.assertFalse(ratings.mdblist_bulk_available())


class RatingBatcherTests(unittest.IsolatedAsyncioTestCase):
    def _batcher(self, *, bulk_result=None, available=lambda: True, max_ids=200):
        self.bulk_calls, self.single_calls, self.gates = [], [], 0

        async def bulk(client, key, provider, mdb_type, ids, *, genre_ids=None):
            self.bulk_calls.append((key, provider, mdb_type, list(ids)))
            if bulk_result is not None:
                return bulk_result(ids)
            return {i: ({"imdb": 7.0}, "Drama", None, [], None) for i in ids}

        async def single(client, key, genre_ids, media_type, *, media_id, provider):
            self.single_calls.append((key, media_id))
            return ({"imdb": 6.0}, "Drama", None, [], None)

        @asynccontextmanager
        async def gate():
            self.gates += 1
            yield

        return RatingBatcher(single=single, bulk=bulk, bulk_available=available, gate=gate,
                             window=0.02, max_ids=max_ids)

    async def test_requests_in_the_window_share_one_bulk_call(self):
        b = self._batcher()
        out = await asyncio.gather(*(b.fetch(None, "server", [], "movie", media_id=f"tt{i}", provider="imdb")
                                     for i in range(5)))
        self.assertEqual(len(self.bulk_calls), 1)
        self.assertEqual(self.bulk_calls[0][3], [f"tt{i}" for i in range(5)])
        self.assertEqual(self.gates, 1)
        self.assertTrue(all(r[0] == {"imdb": 7.0} for r in out))

    async def test_a_client_key_never_batches_with_another_key(self):
        b = self._batcher()
        await asyncio.gather(
            b.fetch(None, "server", [], "movie", media_id="tt1", provider="imdb"),
            b.fetch(None, "server", [], "movie", media_id="tt2", provider="imdb"),
            b.fetch(None, "client-a", [], "movie", media_id="tt3", provider="imdb"),
            b.fetch(None, "client-a", [], "movie", media_id="tt4", provider="imdb"),
            b.fetch(None, "client-b", [], "movie", media_id="tt5", provider="imdb"),
        )
        groups = sorted((c[0], tuple(c[3])) for c in self.bulk_calls)
        self.assertEqual(groups, [("client-a", ("tt3", "tt4")), ("server", ("tt1", "tt2"))])
        self.assertEqual(self.single_calls, [("client-b", "tt5")])   # a batch of one

    async def test_types_and_routes_batch_apart(self):
        b = self._batcher()
        await asyncio.gather(
            b.fetch(None, "k", [], "movie", media_id="tt1", provider="imdb"),
            b.fetch(None, "k", [], "movie", media_id="tt2", provider="imdb"),
            b.fetch(None, "k", [], "series", media_id="tt3", provider="imdb"),
            b.fetch(None, "k", [], "tv", media_id="tt4", provider="imdb"),
            b.fetch(None, "k", [], "movie", media_id="5", provider="tmdb"),
            b.fetch(None, "k", [], "movie", media_id="6", provider="tmdb"),
        )
        self.assertEqual(sorted((c[1], c[2]) for c in self.bulk_calls),
                         [("imdb", "movie"), ("imdb", "show"), ("tmdb", "movie")])

    async def test_same_title_twice_is_asked_once(self):
        b = self._batcher()
        await asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=i, provider="imdb")
                               for i in ("tt1", "tt1", "tt2")))
        self.assertEqual(self.bulk_calls[0][3], ["tt1", "tt2"])

    async def test_full_batch_goes_without_waiting_for_the_window(self):
        b = self._batcher(max_ids=3)
        b.window = 60.0
        await asyncio.wait_for(asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=f"tt{i}", provider="imdb")
                                                for i in range(3))), timeout=1.0)
        self.assertEqual(len(self.bulk_calls), 1)

    async def test_rate_limit_reaches_every_waiter(self):
        limited = _RateLimited(10.0)
        b = self._batcher(bulk_result=lambda ids: limited)
        out = await asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=f"tt{i}", provider="imdb")
                                     for i in range(3)))
        self.assertTrue(all(r is limited for r in out))
        self.assertEqual(self.single_calls, [])

    async def test_bulk_turning_itself_off_falls_back_to_single_calls(self):
        state = {"on": True}

        def fail(ids):
            state["on"] = False
            return FETCH_FAILED
        b = self._batcher(bulk_result=fail, available=lambda: state["on"])
        out = await asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=f"tt{i}", provider="imdb")
                                     for i in range(3)))
        self.assertEqual(sorted(m for _k, m in self.single_calls), ["tt0", "tt1", "tt2"])
        self.assertTrue(all(r[0] == {"imdb": 6.0} for r in out))

    async def test_bulk_turning_off_midway_retries_only_what_it_did_not_answer(self):
        state = {"on": True}

        def partial(ids):
            state["on"] = False
            return {ids[0]: ({"imdb": 7.0}, "Drama", None, [], None),
                    **{i: FETCH_FAILED for i in ids[1:]}}
        b = self._batcher(bulk_result=partial, available=lambda: state["on"])
        out = await asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=f"tt{i}", provider="imdb")
                                     for i in range(3)))
        self.assertEqual(sorted(m for _k, m in self.single_calls), ["tt1", "tt2"])
        self.assertEqual([r[0] for r in out], [{"imdb": 7.0}, {"imdb": 6.0}, {"imdb": 6.0}])

    async def test_network_failure_with_bulk_still_on_is_a_failure_for_all(self):
        b = self._batcher(bulk_result=lambda ids: FETCH_FAILED)
        out = await asyncio.gather(*(b.fetch(None, "k", [], "movie", media_id=f"tt{i}", provider="imdb")
                                     for i in range(2)))
        self.assertTrue(all(r is FETCH_FAILED for r in out))
        self.assertEqual(self.single_calls, [])


class AwardListTests(unittest.TestCase):
    def setUp(self):
        award_lists.reload()
        files = {
            "cult_films.json": {"tt0073629": "The Rocky Horror Picture Show"},
            "festival_winners.json": {"cannes": ["tt0071315"], "venice": ["tt0071315", "tt0042876"]},
            "awards_best_picture.json": {"winners": ["tt0068646"], "nominees": ["tt0071315", "tt0068646"]},
        }

        def read(name):
            return files.get(name)
        patcher = mock.patch.object(award_lists, "_read", read)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(award_lists.reload)

    def test_lookups(self):
        self.assertTrue(award_lists.is_cult("tt0073629"))
        self.assertFalse(award_lists.is_cult("tt0068646"))
        self.assertFalse(award_lists.is_cult(None))
        # Two top prizes: the first festival in prestige order names it.
        self.assertEqual(award_lists.festival_win("tt0071315"), "festival-cannes-winner")
        self.assertEqual(award_lists.festival_win("tt0042876"), "festival-venice-winner")
        self.assertEqual(award_lists.best_picture("tt0068646"), "win")
        self.assertEqual(award_lists.best_picture("tt0071315"), "nom")
        self.assertIsNone(award_lists.best_picture("tt0000001"))

    def test_signature_is_empty_off_the_lists(self):
        self.assertEqual(award_lists.signature("tt0000001"), "")
        self.assertRegex(award_lists.signature(None), r"^\|alv=[0-9a-f]{8}$")
        self.assertEqual(award_lists.signature("tt0071315"), "|al=cannes.bpnom")
        self.assertEqual(award_lists.signature("tt0073629"), "|al=c")

    def test_derive_adds_list_flags_and_keeps_keyword_fallback(self):
        wins, noms, cult, true_story = award_lists.derive(
            "tt0068646", award_wins=["Globe Winner"], award_noms=["Oscar Nominee"],
            keyword_cult=False, keyword_true_story=False, tmdb_true_story=True)
        self.assertEqual(wins, ["Oscar Winner", "Globe Winner"])
        self.assertEqual(noms, [])
        self.assertFalse(cult)
        self.assertTrue(true_story)

        wins, noms, cult, true_story = award_lists.derive(
            "tt0000001", award_wins=[], award_noms=[],
            keyword_cult=True, keyword_true_story=True, tmdb_true_story=None)
        self.assertEqual((wins, noms, cult, true_story), ([], [], True, True))

    def test_derive_nominee(self):
        wins, noms, cult, _ = award_lists.derive(
            "tt0071315", award_wins=[], award_noms=[],
            keyword_cult=False, keyword_true_story=False)
        self.assertEqual(noms, ["Oscar Nominee"])

    def test_listed_winner_gets_the_top_prize_label(self):
        self.assertEqual(festivals.festival_label(None, None, "tt0071315"), "Palme d'Or")
        # Keyword only: the weaker claim, as before.
        self.assertEqual(festivals.festival_label("festival-cannes-winner", None, "tt0000001"), "Cannes Winner")

    def test_metacritic_rule(self):
        self.assertTrue(award_lists.metacritic_must_see(81, 15))
        self.assertFalse(award_lists.metacritic_must_see(80, 40))
        self.assertFalse(award_lists.metacritic_must_see(95, 14))
        self.assertFalse(award_lists.metacritic_must_see(None, 20))

    def test_missing_files_mean_no_list_flags(self):
        award_lists.reload()
        with mock.patch.object(award_lists, "_read", lambda name: None):
            self.assertFalse(award_lists.is_cult("tt0073629"))
            self.assertIsNone(award_lists.festival_win("tt0071315"))


class BundledListTests(unittest.TestCase):
    """The committed data files load and hold the films they obviously should."""

    def setUp(self):
        award_lists.reload()
        self.addCleanup(award_lists.reload)

    def test_bundled_lists(self):
        self.assertTrue(award_lists.is_cult("tt0073629"))      # The Rocky Horror Picture Show
        self.assertTrue(award_lists.is_cult("tt0118715"))      # The Big Lebowski
        self.assertEqual(award_lists.best_picture("tt0068646"), "win")   # The Godfather
        self.assertEqual(award_lists.best_picture("tt0099685"), "nom")   # Goodfellas
        self.assertEqual(award_lists.festival_win("tt0110912"), "festival-cannes-winner")  # Pulp Fiction
        self.assertEqual(award_lists.festival_win("tt0042876"), "festival-venice-winner")  # Rashomon


class TrueStoryKeywordTests(unittest.TestCase):
    def test_movie_and_tv_keyword_blocks(self):
        self.assertTrue(tmdb.tmdb_keywords_true_story(
            {"keywords": {"keywords": [{"id": 9672, "name": "based on true story"}]}}))
        self.assertTrue(tmdb.tmdb_keywords_true_story(
            {"keywords": {"results": [{"id": 1, "name": "Based on a True Story"}]}}))
        self.assertFalse(tmdb.tmdb_keywords_true_story({"keywords": {"keywords": [{"id": 2, "name": "heist"}]}}))
        self.assertIsNone(tmdb.tmdb_keywords_true_story({}))


SORTNAME_PAGE = """{| class="wikitable"
! Film !! Year !! Source
|-
| {{sortname|The|Rocky Horror Picture Show}} || 1975 || <ref>x</ref>
|-
| {{sortname|The|Big Lebowski|dab=film}} || 1998 ||
|-
| ''[[Eraserhead]]'' || 1977 ||
|-
| [[Harold and Maude|''Harold and Maude'']] || 1971 ||
|-
| {{sortname|The|Unlinked|nolink=1}} || 2001 ||
|}"""

WINNERS_PAGE = """== History ==
''[[Not A Winner]]'' is mentioned here.
== Winners ==
=== 1930s ===
{| class="wikitable"
|-
! 1939
| colspan="5" style="background-color:#EFD;" data-sort-value="ω" | '''Cancelled''', though ''[[Goodbye, Mr. Chips (1939 film)|Goodbye, Mr. Chips]]'' was entered.
|-
! [[1946 Cannes Film Festival|1946]]
| colspan="2" | {{sort|Brief Encounter|''[[Brief Encounter]]''}}
| {{sortname|David|Lean}}
|-
| {{sort|Last Chance|''[[The Last Chance (1945 film)|The Last Chance]]''}}
| {{sort|Letzte Chance|''Die Letzte Chance''}}
|}
=== Special Palme d'Or ===
{| class="wikitable"
|-
| ''[[Special Film]]''
|}
== See also ==
''[[Another Film]]''"""

SUNDANCE_ROWS = """{| class="wikitable"
|-
|style="text-align:center;", rowspan=2|1978 || ''[[Girlfriends (1978 film)|Girlfriends]]'' || [[Claudia Weill]]
|-
| ''[[The Whole Shootin' Match]]'' (second place) || [[Eagle Pennell]]
|}"""

BEST_PICTURE_ROWS = """{| class="wikitable"
|- style="background:#FAEB86"
! rowspan="2"| [[1990 in film|1990]]
| '''''[[Dances With Wolves]]'''''
| '''[[Jim Wilson (producer)|Jim Wilson]]'''
|-
| ''[[Ghost (1990 film)|Ghost]]''
| Lisa Weinstein
|}"""


class WikitextParserTests(unittest.TestCase):
    def test_cult_rows_include_the_sortname_form(self):
        self.assertEqual(builder.parse_cult_rows(SORTNAME_PAGE), [
            "The Rocky Horror Picture Show", "The Big Lebowski (film)", "Eraserhead", "Harold and Maude",
        ])

    def test_winners_section_skips_notes_other_sections_and_specials(self):
        rows = builder.parse_winner_rows(builder.section(WINNERS_PAGE, "Winners", ("Special Palme d'Or",)))
        self.assertEqual(rows, ["Brief Encounter", "The Last Chance (1945 film)"])

    def test_runner_up_rows_are_left_out(self):
        self.assertEqual(builder.parse_winner_rows(SUNDANCE_ROWS), ["Girlfriends (1978 film)"])

    def test_best_picture_winner_is_the_bold_title(self):
        self.assertEqual(builder.parse_best_picture_rows(BEST_PICTURE_ROWS),
                         (["Dances With Wolves"], ["Ghost (1990 film)"]))

    def test_first_imdb_claim(self):
        entity = {"claims": {"P345": [{"mainsnak": {"datavalue": {"value": "nm0000001"}}},
                                      {"mainsnak": {"datavalue": {"value": "tt0073629"}}}]}}
        self.assertEqual(builder.first_imdb_claim(entity), "tt0073629")
        self.assertIsNone(builder.first_imdb_claim({"claims": {}}))


class WarmCycleBulkTests(unittest.IsolatedAsyncioTestCase):
    """_run_cache_warm_cycle collects stale titles and rates them in bulk."""

    def _candidates(self, n: int, media_type: str = "movie"):
        return [{"tmdb_id": str(1000 + i), "media_type": media_type} for i in range(n)]

    async def _run(self, candidates, bulk, single, *, mdblist_budget=50, trending=None):
        stored: list[str] = []
        cfg = main._cfg
        meta = {i["tmdb_id"]: {"genre_ids": [18], "imdb_id": f"tt{int(i['tmdb_id']):07d}", "is_textless": False,
                               "logos": [], "poster_path": None, "backdrop_path": None}
                for i in candidates}

        async def none(*a, **kw):
            return []

        async def cands(*a, **kw):
            return candidates

        async def db_call(fn, *a, **kw):
            return fn(*a, **kw)

        async def no_slot():
            return None

        patches = [
            mock.patch.object(cfg, "SERVER_TMDB_KEY", "tmdb"),
            mock.patch.object(cfg, "SERVER_MDBLIST_KEYS", ["mdb"]),
            mock.patch.object(cfg, "CACHE_WARM_TMDB_BUDGET", 1000),
            mock.patch.object(cfg, "CACHE_WARM_MDBLIST_BUDGET", mdblist_budget),
            mock.patch.object(cfg, "CACHE_WARM_MDBLIST_RESERVE", 0),
            mock.patch.object(cfg, "TEXTLESS_TEXT_DETECTION", False),
            mock.patch.object(cfg, "CACHE_WARM_QUALITY_ENABLED", False),
            mock.patch.object(main, "fetch_catalog_candidates", none),
            mock.patch.object(main, "fetch_trending_candidates", trending or cands),
            mock.patch.object(main, "fetch_popular_candidates", none),
            mock.patch.object(main, "fetch_supplemental_candidates", none),
            mock.patch.object(main, "get_cached_tmdb_metadata",
                              lambda key: meta[key.split("_")[1]]),
            mock.patch.object(main, "get_cached_rating", lambda cid: None),
            mock.patch.object(main, "set_cached_rating", lambda cid, *a, **kw: stored.append(cid)),
            mock.patch.object(main, "_db_call", db_call),
            mock.patch.object(main, "_mdblist_wait_for_slot", no_slot),
            mock.patch.object(main, "_resolve_mdblist_key", lambda k: "mdb"),
            mock.patch.object(main, "fetch_ratings_bulk", bulk),
            mock.patch.object(main, "fetch_rating", single),
            mock.patch.object(main.asyncio, "sleep", mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            main._mdblist_key_cooldown.clear()
            await main._run_cache_warm_cycle(None)
        finally:
            for p in reversed(patches):
                p.stop()
            main._mdblist_key_cooldown.clear()
        return stored

    def setUp(self):
        ratings._bulk_disabled_until = 0.0

    def tearDown(self):
        ratings._bulk_disabled_until = 0.0
        main._mdblist_ip_pause_until = 0.0

    async def test_stale_titles_are_rated_in_bulk(self):
        calls = []

        async def bulk(client, key, provider, mdb_type, ids, *, genre_ids=None, on_request=None):
            on_request()
            calls.append((provider, mdb_type, list(ids)))
            return {i: ({"imdb": 7.0}, "Drama", None, [], None) for i in ids}

        single = mock.AsyncMock()
        stored = await self._run(self._candidates(5), bulk, single)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("imdb", "movie"))
        self.assertEqual(len(calls[0][2]), 5)
        self.assertEqual(len(stored), 5)
        single.assert_not_called()

    async def test_bulk_failure_falls_back_to_single_calls(self):
        async def bulk(*a, **kw):
            ratings._bulk_disabled_until = time.time() + 3600
            return FETCH_FAILED

        async def single(client, key, genre_ids, media_type, *, media_id, provider):
            return ({"imdb": 6.0}, "Drama", None, [], None)

        stored = await self._run(self._candidates(3), bulk, single)
        self.assertEqual(len(stored), 3)

    async def test_burst_limit_leaves_titles_uncached(self):
        async def bulk(*a, **kw):
            return _RateLimited(5.0)

        single = mock.AsyncMock()
        stored = await self._run(self._candidates(3), bulk, single)
        self.assertEqual(stored, [])
        single.assert_not_called()

    async def test_every_bulk_post_counts_and_partial_answers_are_kept(self):
        async def bulk(client, key, provider, mdb_type, ids, *, genre_ids=None, on_request=None):
            on_request()
            on_request()                    # the IMDb re-ask under the other type
            return {ids[0]: ({"imdb": 7.0}, "Drama", None, [], None),
                    **{i: _RateLimited(5.0) for i in ids[1:]}}

        single = mock.AsyncMock()
        with self.assertLogs("main", level="INFO") as logs:
            stored = await self._run(self._candidates(3), bulk, single)
        self.assertEqual(len(stored), 1)
        single.assert_not_called()
        self.assertTrue(any("2 MDBList calls (2 bulk, 1 titles rated in bulk)" in m for m in logs.output))

    async def test_candidate_list_is_sized_by_the_tmdb_budget_alone(self):
        asked = []

        async def trending(client, key, *, max_items):
            asked.append(max_items)
            return []

        await self._run([], mock.AsyncMock(), mock.AsyncMock(),
                        mdblist_budget=5000, trending=trending)
        self.assertEqual(asked, [400])   # 40% of the TMDB budget of 1000



class RateLimitMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_bulk_refusal_cools_the_key_without_a_title_backoff(self):
        main._mdblist_key_cooldown.clear()
        main._rating_backoff.clear()
        self.addCleanup(main._mdblist_key_cooldown.clear)
        self.addCleanup(main._rating_backoff.clear)
        main._mark_mdblist_rate_limit(None, "k", _RateLimited(None, reset_at=time.time() + 600))
        self.assertIn("k", main._mdblist_key_cooldown)
        self.assertEqual(main._rating_backoff, {})


class ReleaseDateMemoTests(unittest.TestCase):
    def test_a_bulk_record_keeps_the_remembered_digital_date(self):
        written = {}
        with mock.patch.object(cache, "set_cached_tvdb_json",
                               lambda key, value, ttl: written.update(value=value, ttl=ttl)), \
             mock.patch.object(ratings, "mdblist_release_dates",
                               lambda *a: {"released": "2026-09-01", "released_digital": "2026-10-01"}):
            ratings.remember_mdblist_release_dates("tt1", "movie", {"released": "2026-09-01"})
            self.assertEqual(written["value"]["released_digital"], "2026-10-01")
            # The single call's record says there is none: that is believed.
            ratings.remember_mdblist_release_dates(
                "tt1", "movie", {"released": "2026-09-01", "released_digital": None})
            self.assertIsNone(written["value"]["released_digital"])


class MetadataTrueStoryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = (cache.DB_PATH, cache._initialised, getattr(cache._local, "conn", None))
        cache.DB_PATH = os.path.join(self._dir.name, "cache.db")
        cache._local.conn = None
        cache.init_db()

    def tearDown(self):
        conn = getattr(cache._local, "conn", None)
        if conn is not None:
            conn.close()
        cache.DB_PATH, cache._initialised, cache._local.conn = self._saved
        self._dir.cleanup()

    def _store(self, key, **kw):
        cache.set_cached_tmdb_metadata(key, "T", "2020", [18], False, "/p.jpg", [],
                                       vote_count=10, original_title="T", **kw)

    def test_the_flag_rides_on_the_metadata_row(self):
        self._store("movie_1_en", true_story=True)
        self._store("movie_2_en")
        self.assertTrue(cache.get_cached_tmdb_metadata("movie_1_en")["true_story"])
        self.assertFalse(cache.get_cached_tmdb_metadata("movie_2_en")["true_story"])

    def test_a_row_from_before_the_flag_refreshes_once(self):
        self._store("movie_3_en", true_story=True)
        with cache._db_lock:
            cache.get_db().execute("UPDATE tmdb_metadata_cache SET true_story = NULL")
            cache.get_db().commit()
        with mock.patch.object(cache, "invalidate_final_posters"):
            self.assertIsNone(cache.get_cached_tmdb_metadata("movie_3_en"))


if __name__ == "__main__":
    unittest.main()
