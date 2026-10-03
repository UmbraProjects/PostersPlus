"""fanart.tv poster source and random top-5 poster pick."""

import asyncio
import unittest
from unittest import mock

import fanart
import main
import textless_report
import tmdb


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
        return cfg.poster_source, cfg.poster_pick

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
        self.assertEqual(self._parsed(True, False, poster_source="fanart_anime"), ("fanart_anime", "top"))
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


if __name__ == "__main__":
    unittest.main()


class CinemetaPosterSourceParsingTests(unittest.TestCase):
    """poster_source=cinemeta is offered whenever Cinemeta is enabled."""

    def _source(self, enabled, **params):
        with mock.patch.object(main._cfg, "CINEMETA_ENABLED", enabled):
            return main.build_request_config(
                {"poster_source": "cinemeta", **params}).poster_source

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
