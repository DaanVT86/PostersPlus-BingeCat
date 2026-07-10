import asyncio
import base64
import unittest
from unittest.mock import patch


from i18n import load_languages, translate_genre, translate_sash
from tmdb import (
    _image_language_keys,
    _image_matches_language,
    _tmdb_include_image_languages,
    fetch_logo,
    image_language_order,
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

    def test_existing_priorities_are_unchanged(self):
        self.assertEqual(
            image_language_order("fr", "ja", "native_original"),
            ["fr", "ja"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "original_native"),
            ["ja", "fr"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "native_text"),
            ["fr"],
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

    def test_region_qualified_language_uses_base_translation_table(self):
        load_languages()
        self.assertEqual(translate_genre("Drama", "fr-FR"), "Drame")
        self.assertEqual(translate_sash("Season Finale", "fr-FR"), "Finale saison")


if __name__ == "__main__":
    unittest.main()
