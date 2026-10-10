import re
import unittest

import anime
import config
from ratings import calculate_weighted_score


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient recording the last request."""

    def __init__(self, response):
        self.response = response
        self.url: str | None = None
        self.params: dict | None = None
        self.json_body: dict | None = None

    async def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.url = url
        self.params = params
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def post(self, url, json=None, timeout=None, **kwargs):
        self.url = url
        self.json_body = json
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ParseAnimeIdTests(unittest.TestCase):
    def test_accepts_bare_and_prefixed_ids(self):
        self.assertEqual(anime.parse_anime_id("anilist", "16498"), 16498)
        self.assertEqual(anime.parse_anime_id("kitsu", "kitsu:7442"), 7442)
        self.assertEqual(anime.parse_anime_id("kitsu", "  7442 "), 7442)

    def test_rejects_mismatched_namespace(self):
        self.assertIsNone(anime.parse_anime_id("kitsu", "anilist:1"))

    def test_rejects_non_numeric_and_out_of_range(self):
        for bad in ("abc", "", "-5", "0", "1.5", "99999999999", "1;drop"):
            with self.subTest(bad=bad):
                self.assertIsNone(anime.parse_anime_id("anilist", bad))

    def test_rejects_unknown_namespace(self):
        self.assertIsNone(anime.parse_anime_id("anidb", "1"))

    def test_mal_is_parsed_for_translation(self):
        self.assertEqual(anime.parse_anime_id("mal", "mal:1535"), 1535)

    def test_namespaced_id_cannot_collide_with_tmdb_or_imdb(self):
        canonical = anime.namespaced_id("kitsu", 7442)
        self.assertEqual(canonical, "kitsu:7442")
        self.assertFalse(canonical.isdigit())        # not a bare TMDB id
        self.assertFalse(canonical.startswith("tt"))  # not an IMDb id


class GenreMappingTests(unittest.TestCase):
    def test_maps_to_known_tmdb_genre_ids(self):
        for gid in anime._map_genres(["Action", "Mecha", "Slice of Life"]):
            self.assertIn(gid, config.GENRE_MAP)

    def test_animation_is_always_present_as_the_floor(self):
        self.assertIn(16, anime._map_genres([]))
        self.assertIn(16, anime._map_genres(["Nonsense Genre"]))

    def test_every_mapped_id_is_rankable(self):
        # An id absent from GENRE_PRIORITY would never resolve to a label,
        # leaving the title on "Unknown" with no genre background.
        for gid in set(anime._GENRE_IDS.values()) | {16}:
            with self.subTest(gid=gid):
                self.assertIn(gid, config.GENRE_PRIORITY)

    def test_dedupes(self):
        # Mecha and Sci-Fi both map to 878.
        mapped = anime._map_genres(["Mecha", "Sci-Fi"])
        self.assertEqual(len(mapped), len(set(mapped)))


class NormaliseTests(unittest.TestCase):
    def test_anilist_shape(self):
        media = {
            "title": {"english": "Attack on Titan", "native": "進撃の巨人"},
            "startDate": {"year": 2013, "month": 4, "day": 7},
            "seasonYear": 2013,
            "genres": ["Action", "Drama"],
            "averageScore": 85,
            "popularity": 1038684,
            "status": "FINISHED",
            "format": "TV",
            "episodes": 25,
            "isAdult": False,
            "coverImage": {"extraLarge": "https://cdn/x.jpg", "large": "https://cdn/s.jpg"},
            "studios": {"nodes": [{"name": "Wit Studio"}]},
        }
        genre_ids, year, title, poster, td = anime._normalise_anilist(media)

        self.assertEqual(title, "Attack on Titan")
        self.assertEqual(year, "2013")
        self.assertEqual(poster, "https://cdn/x.jpg")     # prefers extraLarge
        self.assertEqual(td["tmdb_release_date"], "2013-04-07")
        self.assertEqual(td["tmdb_status"], "Ended")
        self.assertEqual(td["anime_score"], 85)
        self.assertEqual(td["anime_media_type"], "series")
        self.assertEqual(td["anime_source"], "anilist")
        self.assertEqual(td["production_companies"], [{"name": "Wit Studio"}])

    def test_anilist_movie_format(self):
        _, _, _, _, td = anime._normalise_anilist({"format": "MOVIE", "title": {}})
        self.assertEqual(td["anime_media_type"], "movie")

    def test_anilist_adult_maps_to_age_rating(self):
        _, _, _, _, td = anime._normalise_anilist({"isAdult": True, "title": {}})
        self.assertEqual(td["anime_age_rating"], 18)

    def test_kitsu_shape(self):
        data = {
            "attributes": {
                "titles": {"en": "Attack on Titan", "ja_jp": "進撃の巨人"},
                "canonicalTitle": "Shingeki no Kyojin",
                "startDate": "2013-04-07",
                "averageRating": "84.47",
                "userCount": 612340,
                "status": "finished",
                "subtype": "TV",
                "ageRating": "R",
                "episodeCount": 25,
                "posterImage": {"original": "https://cdn/o.jpg", "large": "https://cdn/l.jpg"},
            },
            "_genres": ["Action", "Fantasy"],
        }
        genre_ids, year, title, poster, td = anime._normalise_kitsu(data)

        self.assertEqual(title, "Attack on Titan")
        self.assertEqual(year, "2013")
        self.assertEqual(poster, "https://cdn/o.jpg")     # prefers original
        self.assertEqual(td["tmdb_release_date"], "2013-04-07")
        self.assertEqual(td["tmdb_status"], "Ended")
        self.assertEqual(td["anime_score"], 84.47)        # string -> float
        self.assertEqual(td["anime_age_rating"], 17)
        self.assertEqual(td["anime_source"], "kitsu")

    def test_kitsu_tolerates_unparseable_score(self):
        _, _, _, _, td = anime._normalise_kitsu(
            {"attributes": {"averageRating": "n/a", "titles": {}}}
        )
        self.assertIsNone(td["anime_score"])

    def test_blank_metadata_covers_every_key_main_reads(self):
        # main.py and the discovery/sash helpers read these straight off the
        # dict; a missing key would raise rather than degrade.
        blank = anime._blank_tmdb_data()
        for key in (
            "credits", "production_companies", "original_language",
            "original_title", "runtime", "number_of_seasons",
            "number_of_episodes", "tmdb_status", "vote_count",
            "text_backdrop_path", "original_poster_path", "poster_langs",
            "imdb_id", "tmdb_release_date", "last_air_date", "next_episode",
            "last_episode", "seasons",
        ):
            with self.subTest(key=key):
                self.assertIn(key, blank)

    def test_empty_metadata_matches_fetch_tuple_shape(self):
        empty = anime.empty_metadata("kitsu")
        self.assertEqual(len(empty), 8)
        genre_ids, is_textless, logos, year, title, poster, backdrop, td = empty
        self.assertEqual(genre_ids, [16])   # Animation — always has a background
        self.assertFalse(is_textless)
        self.assertEqual(logos, [])
        self.assertIsNone(poster)
        self.assertIsNone(backdrop)
        self.assertEqual(td["anime_source"], "kitsu")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    """Network-level behaviour, with the cache stubbed out."""

    def setUp(self):
        self._real_get = anime.get_cached_tvdb_json
        self._real_set = anime.set_cached_tvdb_json
        self.written: list[tuple] = []
        anime.get_cached_tvdb_json = lambda key: None
        anime.set_cached_tvdb_json = lambda k, v, t: self.written.append((k, v, t))

    def tearDown(self):
        anime.get_cached_tvdb_json = self._real_get
        anime.set_cached_tvdb_json = self._real_set

    async def test_anilist_returns_eight_tuple_with_no_logos_or_backdrop(self):
        client = _FakeClient(_FakeResponse(200, {"data": {"Media": {
            "title": {"english": "T"}, "coverImage": {"extraLarge": "https://c/x.jpg"},
            "genres": ["Action"], "averageScore": 70, "status": "RELEASING",
            "format": "TV", "startDate": {},
        }}}))
        result = await anime.fetch_anime_metadata(client, "anilist", 1)

        self.assertEqual(len(result), 8)
        genre_ids, is_textless, logos, _, _, poster, backdrop, td = result
        # Single text-bearing cover: never textless, never a logo, never a backdrop.
        self.assertFalse(is_textless)
        self.assertEqual(logos, [])
        self.assertIsNone(backdrop)
        self.assertEqual(poster, "https://c/x.jpg")
        self.assertEqual(td["tmdb_status"], "Returning Series")

    async def test_kitsu_sideloads_both_vocabularies_inline(self):
        client = _FakeClient(_FakeResponse(200, {
            "data": {"attributes": {"titles": {"en": "T"}, "posterImage": {"original": "u"}}},
            "included": [
                {"type": "categories", "attributes": {"title": "Action"}},
                {"type": "other", "attributes": {"title": "Ignored"}},
            ],
        }))
        result = await anime.fetch_anime_metadata(client, "kitsu", 1)

        # One request, genres sideloaded — no second round-trip.
        self.assertEqual(client.params, {"include": "genres,categories"})
        self.assertIn(28, result[0])          # Action
        self.assertNotIn(None, result[0])

    async def test_missing_title_negative_caches(self):
        client = _FakeClient(_FakeResponse(404))
        self.assertIsNone(await anime.fetch_anime_metadata(client, "kitsu", 999))
        self.assertEqual(len(self.written), 1)
        self.assertTrue(self.written[0][1].get("__miss__"))

    async def test_transient_failure_is_not_negative_cached(self):
        # A network blip must not pin a title to the genre canvas for the whole
        # negative-cache window.
        client = _FakeClient(RuntimeError("connection reset"))
        self.assertIsNone(await anime.fetch_anime_metadata(client, "anilist", 1))
        self.assertEqual(self.written, [])

    async def test_rate_limit_is_not_negative_cached(self):
        client = _FakeClient(_FakeResponse(429, headers={"retry-after": "60"}))
        self.assertIsNone(await anime.fetch_anime_metadata(client, "anilist", 1))
        self.assertEqual(self.written, [])

    async def test_negative_cache_hit_short_circuits(self):
        anime.get_cached_tvdb_json = lambda key: {"__miss__": True}
        client = _FakeClient(_FakeResponse(200, {}))
        self.assertIsNone(await anime.fetch_anime_metadata(client, "kitsu", 1))
        self.assertIsNone(client.url)   # no request made


class UnscoredRecheckTests(unittest.IsolatedAsyncioTestCase):
    """A new show has no score for its first days; its cached metadata is
    asked again once a day rather than keeping "no score" for a week."""

    def setUp(self):
        self._real_get = anime.get_cached_tvdb_json
        self._real_set = anime.set_cached_tvdb_json
        self.store: dict = {}
        anime.get_cached_tvdb_json = lambda key: self.store.get(key)
        anime.set_cached_tvdb_json = lambda k, v, t: self.store.__setitem__(k, v)
        self._quiet = anime._anilist_quiet_until
        anime._anilist_quiet_until = 0.0

    def tearDown(self):
        anime.get_cached_tvdb_json = self._real_get
        anime.set_cached_tvdb_json = self._real_set
        anime._anilist_quiet_until = self._quiet

    def _row(self, score):
        td = anime._blank_tmdb_data()
        td["anime_score"] = score
        return {"genre_ids": [16], "release_year": "2026", "title": "T",
                "poster_path": "u", "tmdb_data": td}

    def _media(self, score):
        return _FakeResponse(200, {"data": {"Media": {
            "id": 1, "idMal": 5, "title": {"english": "T"}, "coverImage": {"extraLarge": "u"},
            "genres": [], "averageScore": score, "format": "TV", "startDate": {}}}})

    async def test_a_cached_unscored_entry_is_asked_again_once(self):
        self.store[anime._cache_key("anilist", 1)] = self._row(None)
        client = _FakeClient(self._media(87))
        result = await anime.fetch_anime_metadata(client, "anilist", 1)
        self.assertEqual(result[7]["anime_score"], 87)
        self.assertEqual(result[7]["anime_mal_id"], 5)
        # Still unscored after asking: the cached row is used until tomorrow.
        self.store[anime._cache_key("anilist", 1)] = self._row(None)
        client = _FakeClient(self._media(None))
        self.store.pop(anime._score_check_key("anilist", 1), None)
        await anime.fetch_anime_metadata(client, "anilist", 1)
        client = _FakeClient(self._media(90))
        result = await anime.fetch_anime_metadata(client, "anilist", 1)
        self.assertIsNone(client.url)
        self.assertIsNone(result[7]["anime_score"])

    async def test_a_failed_recheck_keeps_the_cached_row(self):
        self.store[anime._cache_key("anilist", 1)] = self._row(None)
        result = await anime.fetch_anime_metadata(_FakeClient(RuntimeError("down")), "anilist", 1)
        self.assertEqual(result[4], "T")

    async def test_a_scored_entry_is_not_asked_again(self):
        self.store[anime._cache_key("anilist", 1)] = self._row(70)
        client = _FakeClient(self._media(87))
        self.assertEqual((await anime.fetch_anime_metadata(client, "anilist", 1))[7]["anime_score"], 70)
        self.assertIsNone(client.url)


class JikanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = (anime.get_cached_tvdb_json, anime.set_cached_tvdb_json,
                       anime.JIKAN_API_URL, anime._jikan_quiet_until)
        self.store: dict = {}
        anime.get_cached_tvdb_json = lambda key: self.store.get(key)
        anime.set_cached_tvdb_json = lambda k, v, t: self.store.__setitem__(k, v)
        anime.JIKAN_API_URL = "https://jikan.example/v4"
        anime._jikan_quiet_until = 0.0

    def tearDown(self):
        (anime.get_cached_tvdb_json, anime.set_cached_tvdb_json,
         anime.JIKAN_API_URL, anime._jikan_quiet_until) = self._saved

    async def test_score_is_read_and_cached(self):
        client = _FakeClient(_FakeResponse(200, {"data": {"mal_id": 61967, "score": 8.83}}))
        self.assertEqual(await anime.fetch_mal_score(client, 61967), (8.83, True))
        self.assertEqual(client.url, "https://jikan.example/v4/anime/61967")
        client = _FakeClient(_FakeResponse(200, {"data": {"score": 1.0}}))
        self.assertEqual(await anime.fetch_mal_score(client, 61967), (8.83, True))
        self.assertIsNone(client.url)

    async def test_unscored_and_missing_are_answers(self):
        client = _FakeClient(_FakeResponse(200, {"data": {"score": None}}))
        self.assertEqual(await anime.fetch_mal_score(client, 1), (None, True))
        self.assertEqual(await anime.fetch_mal_score(_FakeClient(_FakeResponse(404)), 2), (None, True))

    async def test_an_outage_pauses_jikan(self):
        import httpx
        self.assertEqual(await anime.fetch_mal_score(_FakeClient(httpx.ConnectError("x")), 3), (None, False))
        client = _FakeClient(_FakeResponse(200, {"data": {"score": 7.0}}))
        self.assertEqual(await anime.fetch_mal_score(client, 3), (None, False))
        self.assertIsNone(client.url)

    async def test_unset_url_asks_nothing(self):
        anime.JIKAN_API_URL = ""
        client = _FakeClient(_FakeResponse(200, {"data": {"score": 7.0}}))
        self.assertEqual(await anime.fetch_mal_score(client, 4), (None, False))
        self.assertIsNone(client.url)


class RatingIntegrationTests(unittest.TestCase):
    """The provider score has to behave like any other weighted source."""

    def test_sources_are_registered_with_identity_normalisers(self):
        for source in ("anilist", "kitsu"):
            with self.subTest(source=source):
                self.assertIn(source, config.SCORE_NORMALISERS)
                self.assertIn(source, config.MOVIE_WEIGHTS)
                self.assertIn(source, config.TV_WEIGHTS)
                # Both are already percentages.
                self.assertEqual(config.SCORE_NORMALISERS[source](84.47), 84.47)

    def test_default_weight_is_zero(self):
        for source in ("anilist", "kitsu"):
            with self.subTest(source=source):
                self.assertEqual(config.MOVIE_WEIGHTS[source], 0)
                self.assertEqual(config.TV_WEIGHTS[source], 0)

    def test_absent_source_leaves_other_titles_untouched(self):
        # An operator weighting anilist must not change a live-action score:
        # weights renormalise over the sources actually present.
        weights = dict(config.TV_WEIGHTS, trakt=0.8, tomatoes=0.2, anilist=0.5)
        without = calculate_weighted_score({"trakt": 80, "tomatoes": 60}, weights)
        self.assertEqual(without, round(0.8 * 80 + 0.2 * 60))

    def test_anime_score_is_used_when_present(self):
        weights = dict(config.TV_WEIGHTS, trakt=0, tomatoes=0, anilist=1.0)
        self.assertEqual(calculate_weighted_score({"anilist": 85}, weights), 85)

    def test_kitsu_and_anilist_blend(self):
        weights = dict(config.TV_WEIGHTS, trakt=0, tomatoes=0, anilist=0.5, kitsu=0.5)
        self.assertEqual(
            calculate_weighted_score({"anilist": 80, "kitsu": 90}, weights), 85
        )


if __name__ == "__main__":
    unittest.main()


def _shape_of(html: str, template_id: str) -> str:
    """Which COPY_SHAPE_* constant one client's COPY_TEMPLATES entry spreads."""
    entry = html[html.index(f"{{ id: '{template_id}',") :]
    return re.search(r"\.\.\.(COPY_SHAPE_\w+)", entry).group(1)


class ConfiguratorAnimeIdTests(unittest.TestCase):
    """The copy template is the only way these ids reach the server, so the
    placeholder wiring is worth pinning down."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_anime_ids_follow_the_client_not_a_toggle(self):
        # There is no Anime IDs switch any more: whether a client can resolve
        # kitsu:/anilist: ids is a fact about the client, so it rides on the
        # copy template instead of on something the user has to reason about.
        self.assertNotIn('id="tog-anime-ids"', self.html)
        self.assertRegex(
            self.html,
            r"if \(template\.animeIds\)\s*params\.set\('stremio_id'",
        )

    def test_the_clients_that_reject_optional_syntax_send_no_anime_ids(self):
        # Discover+ serves nothing for a URL containing "{name?}", so its
        # template is tmdb_id alone — no anime ids, no optional
        # imdb_id.
        for client in ("discoverplus",):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_REQUIRED")
        self.assertIn("const COPY_SHAPE_REQUIRED = { tmdbOptional: false, "
                      "imdbOptional: false, animeIds: false };", self.html)

    def test_the_clients_that_resolve_them_send_them(self):
        # AIOMetadata, Nuvio, Bingecat and Xperience share one URL.
        for client in ("standard",):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_OPTIMAL")
        self.assertIn("const COPY_SHAPE_OPTIMAL  = { tmdbOptional: true,  "
                      "imdbOptional: true,  animeIds: true  };", self.html)

    def test_placeholders_are_template_only(self):
        # Emitted under usePlaceholders, so the live preview (which renders one
        # concrete title picked from search) never carries an unsubstitutable
        # placeholder.
        self.assertRegex(
            self.html,
            r"if \(usePlaceholders\) \{\s*"
            r"if \(template\.imdbOptional\) params\.set\('imdb_id',\s*'\{imdb_id\?\}'\);\s*"
            r"if \(template\.animeIds\)\s+params\.set\('stremio_id', '\{id\}'\);",
        )

    def test_the_core_id_is_optional_only_where_the_client_takes_it(self):
        # The optional form is now right for tmdb_id too: either id renders on
        # its own, and a REQUIRED placeholder the client can't fill nulls the
        # whole URL. Older builds that leave "{tmdb_id?}" verbatim are read as
        # "no TMDB id" server-side (main._normalise_optional_id), not as a
        # malformed one — which is what made the required form load-bearing
        # before. A client that rejects "{name?}" still gets the required form.
        self.assertIn("template.tmdbOptional ? '{tmdb_id?}' : '{tmdb_id}'", self.html)
        for client in ("discoverplus",):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_REQUIRED")
        for client in ("standard",):
            with self.subTest(client=client):
                self.assertEqual(_shape_of(self.html, client), "COPY_SHAPE_OPTIMAL")

    def test_anime_ids_ride_on_the_raw_stremio_id(self):
        # "{id}" is plain, present in every build, and always populated, so it
        # can never null the URL and never needs the optional form.
        self.assertIn("params.set('stremio_id', '{id}')", self.html)

    def test_per_namespace_anime_placeholders_are_never_emitted(self):
        # They are empty for every live-action title, so they would have to be
        # optional — and "{id}" already carries them with no such cost.
        for bad in ("{kitsu_id?}", "{anilist_id?}", "{kitsu_id}", "{anilist_id}"):
            with self.subTest(placeholder=bad):
                self.assertNotIn(f"'{bad}'", self.html)

    def test_question_mark_survives_url_encoding(self):
        # URLSearchParams encodes "?" as %3F; the pattern only matches if it
        # reaches the client literally. Anchored to the closing brace so real
        # values containing "?" aren't corrupted.
        self.assertIn(r".replace(/%3F\}/g, '?}')", self.html)

    def test_unsupported_namespaces_are_not_emitted(self):
        # MAL needs auth and AniDB's API is heavily restricted, so neither is
        # wired as a source. Emitting their placeholders would imply otherwise.
        self.assertNotIn("'{mal_id}'", self.html)
        self.assertNotIn("'{anidb_id}'", self.html)

    def test_new_rating_sources_are_selectable(self):
        for source in ("'anilist'", "'kitsu'"):
            with self.subTest(source=source):
                self.assertIn(source, self.html)
        self.assertRegex(self.html, r"anilist:'AniList'")
        self.assertRegex(self.html, r"kitsu:'Kitsu'")
        # Default weights must stay 0 in both tables.
        self.assertRegex(self.html, r"DEFAULT_MOVIE_W = \{[^}]*anilist:0[^}]*kitsu:0")
        self.assertRegex(self.html, r"DEFAULT_TV_W\s*= \{[^}]*anilist:0[^}]*kitsu:0")


class ResolveAnimeRequestTests(unittest.TestCase):
    """Guards the /poster entry point's id dispatch."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.resolve = staticmethod(main._resolve_anime_request)
        cls.HTTPException = __import__("fastapi").HTTPException

    def test_no_ids_takes_the_tmdb_path(self):
        self.assertEqual(self.resolve("", ""), (None, None))

    def test_unsubstituted_placeholder_is_treated_as_absent(self):
        # A provider that has no anime id for a title may leave the template
        # placeholder literal. Rejecting it would 400 every live-action poster
        # served through the same URL template.
        self.assertEqual(self.resolve("{anilist_id}", "{kitsu_id}"), (None, None))
        self.assertEqual(self.resolve("{anilist_id}", "7442"), ("kitsu", 7442))

    def test_empty_values_take_the_tmdb_path(self):
        self.assertEqual(self.resolve("", "   "), (None, None))

    def test_anilist_wins_when_both_supplied(self):
        # Deterministic, so a client sending both always hits one cache entry.
        self.assertEqual(self.resolve("16498", "7442"), ("anilist", 16498))

    def test_malformed_id_is_rejected_rather_than_silently_ignored(self):
        with self.assertRaises(self.HTTPException) as ctx:
            self.resolve("", "not-an-id")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid kitsu_id")


class AnimeGenrePriorityTests(unittest.TestCase):
    """The Western priority order surfaces the least representative label for
    anime, because the two catalogues invert which genres are discriminating."""

    def test_covers_every_id_the_mapper_can_emit(self):
        # A mapped id missing from the anime order would fall through to
        # "Unknown" and lose its genre background.
        emitted = set(anime._GENRE_IDS.values()) | {16}
        for gid in emitted:
            with self.subTest(gid=gid):
                self.assertIn(gid, config.ANIME_GENRE_PRIORITY)

    def test_animation_is_last_so_it_only_wins_as_a_floor(self):
        self.assertEqual(config.ANIME_GENRE_PRIORITY[-1], 16)

    def test_action_and_adventure_outrank_mystery(self):
        order = config.ANIME_GENRE_PRIORITY
        self.assertLess(order.index(12), order.index(9648))   # Adventure
        self.assertLess(order.index(28), order.index(9648))   # Action

    def test_no_duplicates(self):
        self.assertEqual(
            len(config.ANIME_GENRE_PRIORITY), len(set(config.ANIME_GENRE_PRIORITY))
        )

    def _pick(self, genres, order):
        ids = set(anime._map_genres(genres))
        for gid in order:
            if gid in ids and config.GENRE_MAP.get(gid):
                return config.GENRE_MAP[gid]
        return "Unknown"

    def test_representative_labels_for_known_titles(self):
        # Real AniList genre lists. Each expectation is what a viewer would
        # actually call the show; the Western order gets most of these wrong.
        cases = {
            "Attack on Titan":  (["Action", "Drama", "Fantasy", "Mystery"], "Action"),
            "Death Note":       (["Mystery", "Psychological", "Supernatural", "Thriller"], "Thriller"),
            "One Piece":        (["Action", "Adventure", "Comedy", "Drama", "Fantasy"], "Adventure"),
            "Evangelion":       (["Action", "Drama", "Mecha", "Mystery", "Psychological", "Sci-Fi"], "Sci-Fi"),
            "Steins;Gate":      (["Drama", "Psychological", "Sci-Fi", "Thriller"], "Sci-Fi"),
            "Your Name":        (["Drama", "Romance", "Supernatural"], "Romance"),
            "Jujutsu Kaisen":   (["Action", "Drama", "Supernatural"], "Action"),
            "Cowboy Bebop":     (["Action", "Adventure", "Drama", "Sci-Fi"], "Sci-Fi"),
            "Perfect Blue":     (["Drama", "Horror", "Psychological", "Thriller"], "Horror"),
        }
        for title, (genres, expected) in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    self._pick(genres, config.ANIME_GENRE_PRIORITY), expected
                )


class AnimeScoreFallbackTests(unittest.TestCase):
    """The provider score is the only rating an anime-native title has."""

    def test_falls_back_when_no_anime_source_is_weighted(self):
        # Every bundled preset and existing user URL carries a weights string
        # naming none of the anime sources, so without the fallback the score
        # would always be N/A on exactly the titles this path exists for.
        weights = dict(config.TV_WEIGHTS, trakt=0.8, tomatoes=0.2)
        self.assertEqual(
            calculate_weighted_score({"kitsu": 84.47}, weights), "N/A"
        )
        self.assertEqual(
            calculate_weighted_score(
                {"kitsu": 84.47}, weights, fallback_source="kitsu"
            ),
            84,
        )

    def test_an_explicit_weight_still_wins(self):
        weights = dict(config.TV_WEIGHTS, trakt=0, tomatoes=0, kitsu=1.0)
        self.assertEqual(
            calculate_weighted_score(
                {"kitsu": 84.47}, weights, fallback_source="kitsu"
            ),
            84,
        )

    def test_fallback_does_not_fire_when_a_weighted_source_is_present(self):
        weights = dict(config.TV_WEIGHTS, trakt=1.0, tomatoes=0)
        self.assertEqual(
            calculate_weighted_score(
                {"trakt": 70, "kitsu": 90}, weights, fallback_source="kitsu"
            ),
            70,
        )

    def test_missing_provider_score_still_yields_na(self):
        weights = dict(config.TV_WEIGHTS, trakt=0.8)
        self.assertEqual(
            calculate_weighted_score({}, weights, fallback_source="kitsu"), "N/A"
        )


class AnimeSashAndLogoTests(unittest.TestCase):
    def test_foreign_is_demoted_to_last_for_anime(self):
        priority = ["foreign", "trending", "new_season"]
        demoted = [s for s in priority if s != "foreign"] + ["foreign"]
        self.assertEqual(demoted, ["trending", "new_season", "foreign"])
        # Demoted, not dropped: a title with nothing else to say still gets one.
        self.assertIn("foreign", demoted)

    def test_foreign_demotion_preserves_relative_order(self):
        priority = ["wins", "foreign", "trending", "cult"]
        demoted = [s for s in priority if s != "foreign"] + ["foreign"]
        self.assertEqual(demoted, ["wins", "trending", "cult", "foreign"])

    def test_disabled_foreign_stays_disabled(self):
        # A user who removed the slot entirely must not have it reinstated.
        priority = ["wins", "trending"]
        demoted = (
            [s for s in priority if s != "foreign"] + ["foreign"]
            if "foreign" in priority else priority
        )
        self.assertEqual(demoted, ["wins", "trending"])

    def test_logo_compositing_is_configurable_and_on_by_default(self):
        self.assertTrue(hasattr(config, "ANIME_COMPOSITE_LOGO"))
        self.assertIs(config.ANIME_COMPOSITE_LOGO, True)


class KitsuVocabularyTests(unittest.IsolatedAsyncioTestCase):
    """Kitsu exposes two genre vocabularies of very different quality."""

    def setUp(self):
        self._real_get = anime.get_cached_tvdb_json
        self._real_set = anime.set_cached_tvdb_json
        anime.get_cached_tvdb_json = lambda key: None
        anime.set_cached_tvdb_json = lambda k, v, t: None

    def tearDown(self):
        anime.get_cached_tvdb_json = self._real_get
        anime.set_cached_tvdb_json = self._real_set

    def _payload(self, genres, categories):
        return {
            "data": {"attributes": {"titles": {"en": "T"},
                                    "posterImage": {"original": "u"}}},
            "included": (
                [{"type": "genres", "attributes": {"name": g}} for g in genres]
                + [{"type": "categories", "attributes": {"title": c}} for c in categories]
            ),
        }

    async def test_prefers_genres_over_the_category_tag_cloud(self):
        # Real Attack on Titan data: `categories` carries a Horror tag the show
        # doesn't warrant, and it outranks Action in the priority order.
        client = _FakeClient(_FakeResponse(200, self._payload(
            genres=["Action", "Drama", "Mystery", "Fantasy", "Super Power", "Military"],
            categories=["Post Apocalypse", "Violence", "Action", "Adventure",
                        "Fantasy", "Angst", "Horror", "Drama", "Military", "Shounen"],
        )))
        genre_ids = (await anime.fetch_anime_metadata(client, "kitsu", 7442))[0]
        self.assertNotIn(27, genre_ids)   # Horror — categories-only, dropped
        self.assertIn(28, genre_ids)      # Action

    async def test_falls_back_to_categories_when_genres_is_empty(self):
        # Not every title has the older relationship populated (e.g. Demon
        # Slayer); without this fallback those drop to bare Animation.
        client = _FakeClient(_FakeResponse(200, self._payload(
            genres=[], categories=["Action", "Adventure", "Fantasy"],
        )))
        genre_ids = (await anime.fetch_anime_metadata(client, "kitsu", 41370))[0]
        self.assertIn(12, genre_ids)      # Adventure, from categories
        self.assertNotEqual(genre_ids, [16])

    async def test_both_vocabularies_are_sideloaded_in_one_request(self):
        client = _FakeClient(_FakeResponse(200, self._payload(["Action"], ["Horror"])))
        await anime.fetch_anime_metadata(client, "kitsu", 1)
        self.assertEqual(client.params, {"include": "genres,categories"})


class LiteralPlaceholderToleranceTests(unittest.TestCase):
    """An AIOMetadata build that doesn't understand "{name?}" sends it verbatim."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.resolve = staticmethod(main._resolve_anime_request)

    def test_literal_optional_placeholder_is_treated_as_absent(self):
        self.assertEqual(self.resolve("{anilist_id?}", "{kitsu_id?}"), (None, None))

    def test_literal_required_placeholder_is_treated_as_absent(self):
        self.assertEqual(self.resolve("{anilist_id}", "{kitsu_id}"), (None, None))

    def test_a_real_id_still_wins_beside_a_literal(self):
        self.assertEqual(self.resolve("{anilist_id?}", "7442"), ("kitsu", 7442))


class StremioIdTests(unittest.TestCase):
    """The raw Stremio meta id is the preferred carrier for an anime id."""

    def test_recognises_supported_anime_namespaces(self):
        self.assertEqual(anime.parse_stremio_id("kitsu:7442"), ("kitsu", 7442))
        self.assertEqual(anime.parse_stremio_id("anilist:16498"), ("anilist", 16498))

    def test_strips_season_and_episode_suffix(self):
        # Stremio appends these for series playback ids.
        self.assertEqual(anime.parse_stremio_id("kitsu:7442:1:2"), ("kitsu", 7442))

    def test_non_anime_ids_yield_the_tmdb_path(self):
        # Never an error — an unrecognised namespace just isn't anime.
        for raw in ("tt0903747", "tmdb:1396", "tvdb:81189", "", "   ", "garbage"):
            with self.subTest(raw=raw):
                self.assertEqual(anime.parse_stremio_id(raw), (None, None))

    def test_unsupported_anime_namespaces_yield_the_tmdb_path(self):
        # AniDB's API is restricted, so it is not a source.
        self.assertEqual(anime.parse_stremio_id("anidb:99"), (None, None))

    def test_mal_is_recognised_for_the_caller_to_translate(self):
        self.assertEqual(anime.parse_stremio_id("mal:1535:1:2"), ("mal", 1535))

    def test_malformed_id_in_a_known_namespace_is_not_fatal(self):
        self.assertEqual(anime.parse_stremio_id("kitsu:notanid"), (None, None))


class StremioIdDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.resolve = staticmethod(main._resolve_anime_request)

    def test_stremio_id_drives_the_anime_path(self):
        self.assertEqual(self.resolve("", "", "kitsu:7442"), ("kitsu", 7442))

    def test_live_action_stremio_id_takes_the_tmdb_path(self):
        self.assertEqual(self.resolve("", "", "tt0903747"), (None, None))

    def test_legacy_per_namespace_params_still_work(self):
        # URLs generated before stremio_id existed must keep working.
        self.assertEqual(self.resolve("", "7442", ""), ("kitsu", 7442))
        self.assertEqual(self.resolve("16498", "", ""), ("anilist", 16498))

    def test_stremio_id_wins_over_the_legacy_params(self):
        self.assertEqual(self.resolve("", "1", "kitsu:7442"), ("kitsu", 7442))


class MalIdDispatchTests(unittest.TestCase):
    """A MAL id renders as the Kitsu/AniList id the community mapping gives it."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main
        cls.HTTPException = __import__("fastapi").HTTPException

    def setUp(self):
        from unittest import mock
        table = {1535: ("kitsu", 1376), 16498: ("anilist", 16498)}
        patcher = mock.patch.object(self.main.anime_ids, "mal_to_provider",
                                    side_effect=lambda mal_id: table.get(mal_id))
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, *args):
        return self.main._resolve_anime_request(*args)

    def test_mal_stremio_id_renders_as_its_provider_id(self):
        self.assertEqual(self.resolve("", "", "mal:1535"), ("kitsu", 1376))
        self.assertEqual(self.resolve("", "", "mal:16498:1:1"), ("anilist", 16498))

    def test_mal_id_param_renders_as_its_provider_id(self):
        self.assertEqual(self.resolve("", "", "", "1535"), ("kitsu", 1376))
        self.assertEqual(self.resolve("", "", "", "mal:1535"), ("kitsu", 1376))

    def test_an_unmapped_mal_id_takes_the_tmdb_path(self):
        self.assertEqual(self.resolve("", "", "mal:999"), (None, None))
        self.assertEqual(self.resolve("", "", "", "999"), (None, None))

    def test_a_provider_id_sent_alongside_wins(self):
        self.assertEqual(self.resolve("", "7442", "", "1535"), ("kitsu", 7442))
        self.assertEqual(self.resolve("", "7442", "mal:1535"), ("kitsu", 7442))

    def test_an_unmapped_mal_stremio_id_still_lets_the_params_through(self):
        # As before MAL support, when "mal:" was an unknown namespace.
        self.assertEqual(self.resolve("", "7442", "mal:999"), ("kitsu", 7442))
        self.assertEqual(self.resolve("", "", "mal:999", "1535"), ("kitsu", 1376))

    def test_the_stremio_mal_id_is_tried_ahead_of_mal_id(self):
        self.assertEqual(self.resolve("", "", "mal:16498", "1535"), ("anilist", 16498))

    def test_placeholder_is_absent_and_malformed_is_rejected(self):
        self.assertEqual(self.resolve("", "", "", "{mal_id}"), (None, None))
        with self.assertRaises(self.HTTPException) as ctx:
            self.resolve("", "", "", "abc")
        self.assertEqual(ctx.exception.detail, "Invalid mal_id")


class KitsuSignedUrlTests(unittest.IsolatedAsyncioTestCase):
    """Kitsu's image urls are presigned for 15 minutes; the bucket is public,
    so the bare url is the one kept (Apothecary Diaries 502'd on a cached
    signed url four days old)."""

    SIGNED = ("https://kitsu-production-media.s3.us-west-002.backblazeb2.com/anime/50021/"
              "poster_image/abc.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900"
              "&X-Amz-Signature=f00")
    BARE = "https://kitsu-production-media.s3.us-west-002.backblazeb2.com/anime/50021/poster_image/abc.jpg"

    def setUp(self):
        self._real_get = anime.get_cached_tvdb_json
        self.store: dict = {}
        anime.get_cached_tvdb_json = lambda key: self.store.get(key)

    def tearDown(self):
        anime.get_cached_tvdb_json = self._real_get

    def test_url(self):
        self.assertEqual(anime.kitsu_image_url(self.SIGNED), self.BARE)
        unsigned = "https://media.kitsu.app/anime/poster_images/1/original.jpg"
        self.assertEqual(anime.kitsu_image_url(unsigned), unsigned)
        self.assertIsNone(anime.kitsu_image_url(None))

    def test_provider_image_hosts(self):
        self.assertTrue(anime.is_provider_image(self.SIGNED))
        self.assertTrue(anime.is_provider_image("https://media.kitsu.app/anime/poster_images/1/original.jpg"))
        self.assertTrue(anime.is_provider_image("https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/x.jpg"))
        self.assertFalse(anime.is_provider_image("https://assets.fanart.tv/fanart/tv/1/x.jpg"))
        self.assertFalse(anime.is_provider_image("https://image.tmdb.org/t/p/original/x.jpg"))

    def test_normalised_poster_and_cover_are_bare(self):
        _, _, _, poster, td = anime._normalise_kitsu({"attributes": {
            "titles": {}, "posterImage": {"original": self.SIGNED},
            "coverImage": {"original": self.SIGNED}}})
        self.assertEqual(poster, self.BARE)
        self.assertEqual(td["anime_banner"], self.BARE)

    async def test_a_row_cached_signed_reads_bare(self):
        td = anime._blank_tmdb_data()
        td.update(anime_score=80.0, anime_banner=self.SIGNED)
        self.store[anime._cache_key("kitsu", 50021)] = {
            "genre_ids": [16], "release_year": "2023", "title": "T",
            "poster_path": self.SIGNED, "tmdb_data": td}
        result = await anime.fetch_anime_metadata(_FakeClient(AssertionError("no fetch")), "kitsu", 50021)
        self.assertEqual(result[5], self.BARE)
        self.assertEqual(result[7]["anime_banner"], self.BARE)
