"""fanart.tv poster source and random top-5 poster pick."""

import asyncio
import unittest
from unittest import mock

import fanart
import main
import textless_report
import tmdb


def _source(cfg):
    """The one source a config's three share, else all three."""
    per_type = (cfg.poster_source_movie, cfg.poster_source_tv, cfg.poster_source_anime)
    return per_type[0] if len(set(per_type)) == 1 else per_type


class PosterSourceParsingTests(unittest.TestCase):
    """Both settings parse to the defaults unless the operator allows them."""

    def _cfg(self, **params):
        return main.build_request_config(
            {"poster_source": "fanart", "poster_pick": "random", **params}
        )

    def _parsed(self, fanart_on, random_on, key="k", **params):
        with mock.patch.multiple(
            main._cfg, FANART_POSTERS=fanart_on, FANART_API_KEY=key,
            RANDOM_POSTERS=random_on,
        ):
            cfg = self._cfg(**params)
        return _source(cfg), cfg.poster_pick

    def test_operator_switches_gate_each_setting(self):
        self.assertEqual(self._parsed(True, True), ("fanart", "random"))
        self.assertEqual(self._parsed(True, False), ("fanart", "top"))
        self.assertEqual(self._parsed(False, True), ("tmdb", "random"))
        self.assertEqual(self._parsed(True, True, key=""), ("tmdb", "random"))

    def test_disabled_settings_share_the_default_composite(self):
        with mock.patch.multiple(
            main._cfg, FANART_POSTERS=False, FANART_API_KEY="k", RANDOM_POSTERS=False,
        ):
            self.assertEqual(
                main._render_config_signature(self._cfg()),
                main._render_config_signature(main.build_request_config({})),
            )

    def test_fanart_for_anime_is_gated_like_fanart(self):
        self.assertEqual(self._parsed(True, False, poster_source="fanart_anime"), (("tmdb", "tmdb", "fanart"), "top"))
        self.assertEqual(self._parsed(False, False, poster_source="fanart_anime"), ("tmdb", "top"))

    def test_landscape_ignores_both(self):
        self.assertEqual(self._parsed(True, True, shape="landscape"), ("tmdb", "top"))


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        return self.resp


def _poster(lang, likes, n):
    return {"lang": lang, "likes": str(likes), "url": f"https://assets.fanart.tv/{n}.jpg"}


class FanartPoolTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        patches = [
            mock.patch.multiple(fanart._cfg, FANART_POSTERS=True, FANART_API_KEY="k"),
            mock.patch.object(fanart, "get_cached_tvdb_json", self.store.get),
            mock.patch.object(
                fanart, "set_cached_tvdb_json",
                lambda key, value, ttl: self.store.__setitem__(key, value),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _url(self, client, **kw):
        return asyncio.run(fanart.fanart_poster_url(
            client, media_type="movie", tmdb_id="550", **kw))

    def test_pools_rank_tagged_textless_before_untagged_and_skip_xx(self):
        client = _Client(_Resp(200, {"movieposter": [
            _poster("", 50, "untagged"), _poster("00", 1, "low"),
            _poster("00", 9, "high"), _poster("xx", 99, "xx"),
            _poster("en", 3, "en"), _poster("cz", 2, "cz"),
        ]}))
        self.assertTrue(self._url(client).endswith("/high.jpg"))
        pools = next(iter(self.store.values()))
        self.assertEqual(
            [u.rsplit("/", 1)[1] for u in pools["textless"]],
            ["high.jpg", "low.jpg", "untagged.jpg"],
        )
        self.assertEqual(sorted(pools["langs"]), ["cs", "en"])
        self.assertTrue(self._url(client, languages=["cs"]).endswith("/cz.jpg"))
        self.assertEqual(client.calls, 1)  # second lookup served from cache

    def test_random_stays_within_the_top_five(self):
        client = _Client(_Resp(200, {"movieposter": [
            _poster("00", 10 - n, f"p{n}") for n in range(8)
        ]}))
        picks = {self._url(client, random_top=True) for _ in range(60)}
        self.assertEqual(picks, {f"https://assets.fanart.tv/p{n}.jpg" for n in range(5)})

    def test_transient_errors_are_not_cached(self):
        self.assertIsNone(self._url(_Client(_Resp(503))))
        self.assertEqual(self.store, {})


class TmdbTextlessRankTests(unittest.TestCase):
    def test_rank_starts_with_the_default_pick(self):
        posters = [
            {"file_path": f"/{n}.jpg", "vote_average": rating, "vote_count": votes}
            for n, (rating, votes) in enumerate(
                [(5.5, 1), (5.4, 40), (5.3, 40), (3.0, 90), (5.2, 2)]
            )
        ]
        ranked = tmdb._rank_textless_posters(posters)
        self.assertIs(ranked[0], tmdb._select_textless_poster(posters))
        self.assertEqual(ranked[-1]["file_path"], "/3.jpg")  # not competitive
        self.assertEqual(tmdb._rank_textless_posters([]), [])


class FakeTextlessReportTests(unittest.TestCase):
    def test_absolute_urls_are_not_reported(self):
        with mock.patch.object(textless_report._cfg, "TEXTLESS_FAKE_REPORT", True), \
                mock.patch.object(textless_report, "Path") as path:
            textless_report.report_fake_textless_poster(
                media_type="movie", tmdb_id="550",
                image_path="https://assets.fanart.tv/fanart/x.jpg", vote_count=10,
            )
        path.assert_not_called()


class CinemetaPosterSourceParsingTests(unittest.TestCase):
    """poster_source=cinemeta is offered whenever Cinemeta is enabled."""

    def _source(self, enabled, **params):
        with mock.patch.object(main._cfg, "CINEMETA_ENABLED", enabled):
            return main.build_request_config(
                {"poster_source": "cinemeta", **params}).poster_source_movie

    def test_gated_on_cinemeta_enabled(self):
        self.assertEqual(self._source(True), "cinemeta")
        self.assertEqual(self._source(False), "tmdb")

    def test_landscape_ignores_it(self):
        self.assertEqual(self._source(True, shape="landscape"), "tmdb")

    def test_gets_its_own_composite(self):
        with mock.patch.object(main._cfg, "CINEMETA_ENABLED", True):
            self.assertNotEqual(
                main._render_config_signature(
                    main.build_request_config({"poster_source": "cinemeta"})),
                main._render_config_signature(main.build_request_config({})),
            )


class PerTypePosterSourceTests(unittest.TestCase):
    """poster_source_movie / _tv / _anime, and the legacy poster_source."""

    def _cfg(self, **params):
        with mock.patch.multiple(main._cfg, FANART_POSTERS=True, FANART_API_KEY="k",
                                 CINEMETA_ENABLED=True),                 mock.patch.object(main.tvdb, "poster_source_enabled", lambda: True):
            return main.build_request_config(params)

    def test_each_type_parses_on_its_own(self):
        cfg = self._cfg(poster_source_movie="cinemeta", poster_source_tv="tvdb",
                        poster_source_anime="fanart")
        self.assertEqual(_source(cfg), ("cinemeta", "tvdb", "fanart"))

    def test_a_per_type_param_beats_the_legacy_one(self):
        self.assertEqual(_source(self._cfg(poster_source="tvdb", poster_source_anime="fanart")),
                         ("tvdb", "tvdb", "fanart"))
        self.assertEqual(_source(self._cfg(poster_source="fanart_anime", poster_source_movie="cinemeta")),
                         ("cinemeta", "tmdb", "fanart"))

    def test_unknown_or_unoffered_sources_parse_as_tmdb(self):
        self.assertEqual(_source(self._cfg(poster_source_movie="bogus")), "tmdb")
        with mock.patch.object(main._cfg, "CINEMETA_ENABLED", False):
            self.assertEqual(main.build_request_config(
                {"poster_source_tv": "cinemeta"}).poster_source_tv, "tmdb")

    def test_landscape_resets_all_three(self):
        self.assertEqual(_source(self._cfg(poster_source_movie="fanart", poster_source_tv="tvdb",
                                           poster_source_anime="cinemeta", shape="landscape")),
                         "tmdb")

    def test_source_for_a_title(self):
        cfg = self._cfg(poster_source_movie="cinemeta", poster_source_tv="tvdb",
                        poster_source_anime="fanart")
        self.assertEqual(main._poster_source_for(cfg, "movie", False), "cinemeta")
        self.assertEqual(main._poster_source_for(cfg, "tv", False), "tvdb")
        self.assertEqual(main._poster_source_for(cfg, "series", False), "tvdb")
        self.assertEqual(main._poster_source_for(cfg, "movie", True), "fanart")
        self.assertEqual(main._poster_source_for(cfg, "tv", True), "fanart")

    def test_splits_the_old_field_could_express_keep_their_cache_key(self):
        import json
        sig = lambda **p: json.loads(main._render_config_signature(self._cfg(**p)))
        self.assertEqual(sig()["poster_source"], "tmdb")
        self.assertEqual(sig(poster_source="tvdb")["poster_source"], "tvdb")
        self.assertEqual(sig(poster_source_movie="tvdb", poster_source_tv="tvdb",
                             poster_source_anime="tvdb")["poster_source"], "tvdb")
        self.assertEqual(sig(poster_source="fanart_anime")["poster_source"], "fanart_anime")
        self.assertEqual(sig(poster_source_anime="fanart")["poster_source"], "fanart_anime")
        self.assertNotIn("poster_source_movie", sig())
        self.assertNotEqual(sig(poster_source_movie="cinemeta"), sig(poster_source_tv="cinemeta"))


if __name__ == "__main__":
    unittest.main()
