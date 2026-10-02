import asyncio
import base64
import unittest
from unittest.mock import patch


from i18n import load_languages, translate_genre, translate_sash
from tmdb import (
    LOGO_PRIORITY_PRESETS,
    _image_language_keys,
    _image_matches_language,
    _tmdb_include_image_languages,
    fetch_logo,
    image_language_order,
    logo_language_steps,
    logo_priority_draws_text,
    logo_priority_falls_back_to_art,
    logo_priority_uses_custom,
    parse_logo_priority,
    split_logo_priority_at_art,
)


class _FakeImageResponse:
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return _FakeImageResponse()


class LogoPriorityListTests(unittest.TestCase):
    def test_presets_parse_as_themselves(self):
        for name in LOGO_PRIORITY_PRESETS:
            with self.subTest(name=name):
                self.assertEqual(parse_logo_priority(name), name)

    def test_list_matching_a_preset_is_stored_under_its_name(self):
        # Keeps the composite cache key of a configurator URL that spells the
        # default order out the same as the one that leaves it to the preset.
        for name, sources in LOGO_PRIORITY_PRESETS.items():
            with self.subTest(name=name):
                self.assertEqual(parse_logo_priority(",".join(sources)), name)

    def test_custom_list_is_normalised(self):
        self.assertEqual(
            parse_logo_priority(" Original, bogus,native,original,text,neutral"),
            "original,native,text",
        )

    def test_invalid_or_empty_values_are_rejected(self):
        for value in (None, "", "bogus", " , "):
            with self.subTest(value=value):
                self.assertIsNone(parse_logo_priority(value))

    def test_steps_follow_the_list(self):
        self.assertEqual(
            logo_language_steps("fr", "ja", "english,neutral,native,text"),
            ["en", "metahub", "null", "fr"],
        )

    def test_steps_skip_sources_with_no_language(self):
        self.assertEqual(
            logo_language_steps("fr", None, "native,custom,original,text"),
            ["fr"],
        )

    def test_native_if_original_only_applies_to_native_content(self):
        self.assertEqual(
            logo_language_steps("fr", "fr", "native_if_original,english"),
            ["fr", "en", "metahub"],
        )
        self.assertEqual(
            logo_language_steps("fr", "ja", "native_if_original,english"),
            ["en", "metahub"],
        )

    def test_art_is_a_source_anywhere_above_text(self):
        self.assertEqual(parse_logo_priority("native,art,english,text"),
                         "native,art,english,text")
        self.assertEqual(parse_logo_priority("native,english,art"), "native,english,art")
        # Nothing follows text, art included.
        self.assertEqual(parse_logo_priority("native,text,art"), "native,text")

    def test_art_contributes_no_logo_step(self):
        self.assertEqual(
            logo_language_steps("fr", "ja", "native,art,english,text"),
            ["fr", "en", "metahub"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "native,art,original,text"),
            ["fr", "ja"],
        )

    def test_art_flag_and_split(self):
        self.assertTrue(logo_priority_falls_back_to_art("native,art,text"))
        self.assertFalse(logo_priority_falls_back_to_art("native_original"))
        self.assertEqual(split_logo_priority_at_art("native,english,art,text"),
                         ("native,english", "text"))
        # A side that matches a preset comes back under its name.
        self.assertEqual(
            split_logo_priority_at_art("native,english,neutral,art,text"),
            ("native,english,neutral", "text"),
        )
        self.assertEqual(split_logo_priority_at_art("art,native,english,neutral,text"),
                         (None, "native_text"))
        self.assertEqual(split_logo_priority_at_art("native,art"), ("native", None))
        self.assertEqual(split_logo_priority_at_art("native_original"),
                         ("native_original", None))

    def test_text_and_custom_flags(self):
        self.assertTrue(logo_priority_draws_text("native_original"))
        self.assertFalse(logo_priority_draws_text("native,original"))
        self.assertTrue(logo_priority_uses_custom("native_custom_text"))
        self.assertFalse(logo_priority_uses_custom("native_original"))

    def test_disabled_neutral_is_never_used(self):
        async def run_case():
            logos = [{"file_path": "/neutral.png", "iso_639_1": None, "vote_average": 9}]
            return await fetch_logo(
                _FakeClient(), logos, logo_language="fr", original_language="ja",
                logo_priority="native,original,text", use_metahub=False,
            )

        self.assertIsNone(asyncio.run(run_case()))


class ImageLanguageOrderTests(unittest.TestCase):
    def test_native_content_keeps_native_language_first(self):
        self.assertEqual(
            image_language_order("fr", "fr", "native_if_original_english"),
            ["fr", "en"],
        )

    def test_foreign_content_prefers_english_then_original(self):
        for original_language in ("ko", "ja", "ru", "zh"):
            with self.subTest(original_language=original_language):
                self.assertEqual(
                    image_language_order(
                        "fr", original_language, "native_if_original_english"
                    ),
                    ["en", original_language],
                )

    def test_presets_order_languages_as_their_lists_do(self):
        # English is part of every preset's list, at the end of the language
        # tags; the language-neutral and Metahub steps are not languages.
        self.assertEqual(
            image_language_order("fr", "ja", "native_original"),
            ["fr", "ja", "en"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "original_native"),
            ["ja", "fr", "en"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "native_text"),
            ["fr", "en"],
        )

    def test_duplicate_languages_are_only_tried_once(self):
        self.assertEqual(
            image_language_order("en", "en", "native_if_original_english"),
            ["en"],
        )

    def test_region_qualified_french_does_not_fall_back_to_bare_french_art(self):
        self.assertEqual(
            image_language_order("fr-fr", "en", "native_original"),
            ["fr-fr", "en"],
        )
        self.assertNotIn(
            "fr",
            image_language_order("fr-fr", "en", "native_original"),
        )

    def test_tmdb_language_region_images_match_locale_requests(self):
        france = {"iso_639_1": "fr", "iso_3166_1": "FR"}
        canada = {"iso_639_1": "fr", "iso_3166_1": "CA"}
        generic = {"iso_639_1": "fr", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(france), ["fr-fr", "fr"])
        self.assertTrue(_image_matches_language(france, "fr-fr"))
        self.assertFalse(_image_matches_language(canada, "fr-fr"))
        self.assertFalse(_image_matches_language(generic, "fr-fr"))
        self.assertTrue(_image_matches_language(canada, "fr"))

    def test_region_qualified_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("fr-fr"),
            ["fr-fr", "fr", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("fr"),
            ["fr", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("en"),
            ["en", "null"],
        )

    def test_native_text_uses_english_before_neutral_logo(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-native-text-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
                {
                    "file_path": "/english-native-text-test.png",
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "vote_average": 1,
                },
            ]
            with patch("tmdb.get_cached_tmdb_logo", return_value=None), patch(
                "tmdb.set_cached_tmdb_logo"
            ):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=False,
                )
            return client.urls[0]

        self.assertIn("/english-native-text-test.png", asyncio.run(run_case()))

    def test_other_priorities_keep_neutral_before_english_fallback(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-default-test.png",
                    "iso_639_1": None,
                    "vote_average": 1,
                },
                {
                    "file_path": "/english-default-test.png",
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "vote_average": 99,
                },
            ]
            with patch("tmdb.get_cached_tmdb_logo", return_value=None), patch(
                "tmdb.set_cached_tmdb_logo"
            ):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    original_language="ja",
                    logo_priority="native_original",
                    use_metahub=False,
                )
            return client.urls[0]

        self.assertIn("/neutral-default-test.png", asyncio.run(run_case()))

    def test_native_text_uses_metahub_before_neutral_logo(self):
        async def run_case():
            logos = [
                {
                    "file_path": "/neutral-native-text-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
            ]
            with patch("tmdb._fetch_metahub_logo", return_value="metahub") as metahub:
                result = await fetch_logo(
                    _FakeClient(),
                    logos,
                    logo_language="fr",
                    imdb_id="tt1234567",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=True,
                )
            return result, metahub.called

        result, metahub_called = asyncio.run(run_case())
        self.assertEqual(result, "metahub")
        self.assertTrue(metahub_called)

    def test_native_text_uses_neutral_logo_after_metahub_miss(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-after-metahub-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
            ]
            with patch("tmdb._fetch_metahub_logo", return_value=None), patch(
                "tmdb.get_cached_tmdb_logo", return_value=None
            ), patch("tmdb.set_cached_tmdb_logo"):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    imdb_id="tt1234567",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=True,
                )
            return client.urls[0]

        self.assertIn("/neutral-after-metahub-test.png", asyncio.run(run_case()))

    def test_region_qualified_spanish_does_not_cross_between_spain_and_mexico(self):
        spain = {"iso_639_1": "es", "iso_3166_1": "ES"}
        mexico = {"iso_639_1": "es", "iso_3166_1": "MX"}
        generic = {"iso_639_1": "es", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(spain), ["es-es", "es"])
        self.assertEqual(_image_language_keys(mexico), ["es-mx", "es"])

        self.assertTrue(_image_matches_language(spain, "es-es"))
        self.assertFalse(_image_matches_language(mexico, "es-es"))
        self.assertTrue(_image_matches_language(mexico, "es-mx"))
        self.assertFalse(_image_matches_language(spain, "es-mx"))

        # Untagged Spanish art is not claimed by either region, but both are
        # still Spanish for a bare "es" request.
        self.assertFalse(_image_matches_language(generic, "es-es"))
        self.assertFalse(_image_matches_language(generic, "es-mx"))
        self.assertTrue(_image_matches_language(spain, "es"))
        self.assertTrue(_image_matches_language(mexico, "es"))

    def test_region_qualified_spanish_does_not_fall_back_to_bare_spanish_art(self):
        for locale in ("es-es", "es-mx"):
            with self.subTest(locale=locale):
                order = image_language_order(locale, "en", "native_original")
                self.assertEqual(order, [locale, "en"])
                self.assertNotIn("es", order)

    def test_region_qualified_spanish_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("es-es"),
            ["es-es", "es", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("es-mx"),
            ["es-mx", "es", "en", "null"],
        )

    def test_brazilian_portuguese_does_not_cross_with_european_portuguese(self):
        brazil = {"iso_639_1": "pt", "iso_3166_1": "BR"}
        portugal = {"iso_639_1": "pt", "iso_3166_1": "PT"}
        generic = {"iso_639_1": "pt", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(brazil), ["pt-br", "pt"])

        self.assertTrue(_image_matches_language(brazil, "pt-br"))
        self.assertFalse(_image_matches_language(portugal, "pt-br"))
        self.assertFalse(_image_matches_language(generic, "pt-br"))
        self.assertTrue(_image_matches_language(brazil, "pt"))

    def test_brazilian_portuguese_does_not_fall_back_to_bare_portuguese_art(self):
        order = image_language_order("pt-br", "en", "native_original")
        self.assertEqual(order, ["pt-br", "en"])
        self.assertNotIn("pt", order)

    def test_brazilian_portuguese_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("pt-br"),
            ["pt-br", "pt", "en", "null"],
        )

    def test_brazilian_portuguese_renders_translated_poster_text(self):
        # Resolves against languages/pt-br.json when present, otherwise the
        # bare pt.json — either way the poster must not fall back to English.
        load_languages()
        self.assertEqual(translate_genre("Horror", "pt-BR"), "Terror")
        self.assertEqual(translate_genre("Comedy", "pt-BR"), "Comédia")
        self.assertNotEqual(translate_sash("Season Finale", "pt-BR"), "Season Finale")

    def test_region_qualified_language_uses_base_translation_table(self):
        load_languages()
        self.assertEqual(translate_genre("Drama", "fr-FR"), "Drame")
        self.assertEqual(translate_sash("Season Finale", "fr-FR"), "Finale saison")

    def test_region_qualified_spanish_uses_base_translation_table(self):
        load_languages()
        for locale in ("es-ES", "es-MX"):
            with self.subTest(locale=locale):
                self.assertEqual(
                    translate_genre("Drama", locale),
                    translate_genre("Drama", "es"),
                )
                self.assertEqual(
                    translate_sash("Season Finale", locale),
                    translate_sash("Season Finale", "es"),
                )


if __name__ == "__main__":
    unittest.main()
