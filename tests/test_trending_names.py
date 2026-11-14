"""Operator-set names for the trending addon's catalogs (trending_names.py):
set in the dashboard, served in the manifest, never carried in the URL."""

import unittest
from unittest import mock

import trending_lists
import trending_names
from tests.test_trending_catalogs_addon import _AddonTest


class TrendingNamesTest(_AddonTest):
    def setUp(self):
        super().setUp()
        import admin
        self._key = mock.patch.object(admin, "ADMIN_KEY", "s3cret-long-enough")
        self._key.start()
        self.h = {"X-Admin-Key": "s3cret-long-enough"}

    def tearDown(self):
        self._key.stop()
        super().tearDown()

    def _manifest_names(self, path="/trending/sekrit/manifest.json"):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return {c["id"]: c["name"] for c in r.json()["catalogs"]}

    def test_no_names_keeps_the_generated_ones(self):
        self.assertEqual(self._manifest_names()["pp.trending.movie"], "Trending Movies")

    def test_a_saved_name_replaces_the_generated_one_whole(self):
        r = self.client.put("/admin/api/trending-names", headers=self.h, json={"names": {
            "movie": {trending_lists.default_source("movie"): "  Hot   Movies "},
            "tv": {"imdb": "IMDb Shows"},
        }})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["names"]["movie"], {trending_lists.default_source("movie"): "Hot Movies"})
        # The movie list's plain catalog, with the language note it would carry.
        names = self._manifest_names("/trending/sekrit/tl-m:tmdb,t:tmdb.imdb,l:ko/manifest.json")
        self.assertEqual(names["pp.trending.movie"], "Hot Movies")
        self.assertEqual(names["pp.trending.series.imdb"], "IMDb Shows")
        self.assertTrue(names["pp.trending.series"].startswith("Trending Series · "))

    def test_a_blank_name_goes_back_to_the_generated_one(self):
        self.client.put("/admin/api/trending-names", headers=self.h,
                        json={"names": {"movie": {"tmdb": "Hot"}}})
        r = self.client.put("/admin/api/trending-names", headers=self.h,
                            json={"names": {"movie": {"tmdb": "  "}}})
        self.assertEqual(r.json()["names"], {})
        self.assertEqual(self._manifest_names()["pp.trending.movie"], "Trending Movies")

    def test_the_editor_lists_each_offered_catalog_with_its_id(self):
        body = self.client.get("/admin/api/trending-names", headers=self.h).json()
        rows = {(c["list"], c["source"]): c for c in body["catalogs"]}
        self.assertEqual(rows[("movie", "tmdb")]["id"], "pp.trending.movie")
        self.assertEqual(rows[("movie", "tmdb")]["name"], "Trending Movies")
        self.assertEqual(rows[("anime_movie", "anilist")]["id"], "pp.trending.anime.movie")
        self.assertEqual(body["lists"]["tv"], {"id": "pp.trending.series", "name": "Trending Series"})

    def test_bad_saves_store_nothing(self):
        for body in (
            {"names": {"films": {"tmdb": "x"}}},
            {"names": {"movie": {"tmdb": "x" * 61}}},
            {"names": {"movie": {"bad id!": "x"}}},
            {"names": {"movie": {"tmdb": 5}}},
            {"names": []},
            {},
        ):
            with self.subTest(body=body):
                r = self.client.put("/admin/api/trending-names", headers=self.h, json=body)
                self.assertEqual(r.status_code, 400)
        self.assertEqual(trending_names.load(), {})

    def test_needs_the_admin_key(self):
        with mock.patch("admin._FAIL_DELAY", 0.0):
            self.assertEqual(self.client.get("/admin/api/trending-names").status_code, 401)
            self.assertEqual(self.client.put("/admin/api/trending-names", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
