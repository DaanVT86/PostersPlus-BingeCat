"""Notable studio / director / cast lists edited from the dashboard (Sash lists)."""

import json
import os
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import discovery


class _ListsFile(unittest.TestCase):
    """Points discovery at a scratch overrides file and restores it after."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "discovery_overrides.json")
        self._patch = mock.patch.object(discovery, "_OVERRIDE_PATH", self.path)
        self._patch.start()
        discovery._load_discovery_overrides()

    def tearDown(self):
        self._patch.stop()
        discovery._load_discovery_overrides()
        self._dir.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


class ListsTests(_ListsFile):
    def test_no_file_means_the_built_in_lists(self):
        lists = discovery.current_lists()
        self.assertFalse(lists["cast"]["custom"])
        self.assertEqual(
            {e["name"]: e["label"] for e in lists["cast"]["entries"]},
            discovery.BUILTIN_LISTS["cast"],
        )

    def test_saving_a_section_replaces_it_and_leaves_the_others_built_in(self):
        discovery.save_sections({"cast": {"Tilda Swinton": "Tilda"}})
        self.assertEqual(discovery.NOTABLE_CAST, {"Tilda Swinton": "Tilda"})
        self.assertEqual(discovery.NOTABLE_STUDIOS, discovery.BUILTIN_LISTS["studios"])
        self.assertEqual(self.read(), {"mode": "replace", "cast": {"Tilda Swinton": "Tilda"}})
        lists = discovery.current_lists()
        self.assertTrue(lists["cast"]["custom"])
        self.assertFalse(lists["studios"]["custom"])

    def test_restoring_drops_the_section(self):
        discovery.save_sections({"cast": {"A": "A"}, "studios": {"B": "B"}})
        discovery.save_sections({"cast": None})
        self.assertEqual(self.read(), {"mode": "replace", "studios": {"B": "B"}})
        self.assertEqual(discovery.NOTABLE_CAST, discovery.BUILTIN_LISTS["cast"])

    def test_a_merge_file_keeps_what_it_produced_for_untouched_sections(self):
        self.write({"mode": "merge", "studios": {"Mubi": "MUBI"}, "cast": {"X": "X"}})
        discovery._load_discovery_overrides()
        self.assertIn("A24", discovery.NOTABLE_STUDIOS)
        self.assertIn("Mubi", discovery.NOTABLE_STUDIOS)
        discovery.save_sections({"cast": {"Y": "Y"}})
        data = self.read()
        self.assertEqual(data["mode"], "replace")
        self.assertEqual(data["cast"], {"Y": "Y"})
        self.assertEqual(data["studios"], {**discovery.BUILTIN_LISTS["studios"], "Mubi": "MUBI"})
        self.assertNotIn("directors", data)
        self.assertEqual(discovery.NOTABLE_STUDIOS, data["studios"])

    def test_saved_sections_are_written_a_to_z(self):
        discovery.save_sections({"directors": {
            "zhao": "Z", "Alfonso Cuarón": "A. Cuarón", "Alfonso Cuaron": "x", "Bong Joon Ho": "B",
        }})
        self.assertEqual(list(self.read()["directors"]),
                         ["Alfonso Cuaron", "Alfonso Cuarón", "Bong Joon Ho", "zhao"])

    def test_an_empty_list_turns_the_sash_off(self):
        discovery.save_sections({"directors": {}})
        meta = discovery.extract_discovery_meta(
            {"credits": {"crew": [{"job": "Director", "name": "Christopher Nolan"}]}},
            "movie", [], [], None,
        )
        self.assertEqual(meta.matched_directors, [])

    def test_matching_uses_the_saved_list(self):
        discovery.save_sections({"studios": {"Mubi": "MUBI Original"}})
        meta = discovery.extract_discovery_meta(
            {"production_companies": [{"name": "A24"}, {"name": "Mubi"}]},
            "movie", [], [], None,
        )
        self.assertEqual(meta.matched_studios, ["MUBI Original"])

    def test_other_workers_reload_when_the_file_changes(self):
        self.write({"cast": {"Z": "Z"}})
        discovery._checked_at = 0.0
        self.assertTrue(discovery.refresh_overrides())
        self.assertEqual(discovery.NOTABLE_CAST, {"Z": "Z"})
        discovery._checked_at = 0.0
        self.assertFalse(discovery.refresh_overrides())   # unchanged
        os.remove(self.path)
        discovery._checked_at = 0.0
        self.assertTrue(discovery.refresh_overrides())
        self.assertEqual(discovery.NOTABLE_CAST, discovery.BUILTIN_LISTS["cast"])

    def test_validation(self):
        self.assertEqual(
            discovery.validate_entries([{"name": " A24 ", "label": ""}]), {"A24": "A24"})
        for bad in (
            {"name": "x"},
            [{"label": "no name"}],
            ["str"],
            [{"name": "x", "label": "y" * (discovery.MAX_LABEL_LENGTH + 1)}],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                discovery.validate_entries(bad)


class ApiTests(_ListsFile):
    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main
        cls.client = TestClient(main.app)

    def setUp(self):
        super().setUp()
        import admin
        self._key = mock.patch.object(admin, "ADMIN_KEY", "s3cret-long-enough")
        self._key.start()
        self.h = {"X-Admin-Key": "s3cret-long-enough"}
        self.main._sash_lookup_cache.clear()

    def tearDown(self):
        self._key.stop()
        super().tearDown()

    def test_needs_the_admin_key(self):
        with mock.patch("admin._FAIL_DELAY", 0.0):
            self.assertEqual(self.client.get("/admin/api/sash-lists").status_code, 401)
            self.assertEqual(self.client.put("/admin/api/sash-lists", json={}).status_code, 401)

    def test_save_round_trip_and_signature_changes(self):
        before = self.main._render_assets_signature
        r = self.client.put("/admin/api/sash-lists", headers=self.h, json={
            "changes": {"cast": [{"name": "Tilda Swinton", "label": "Tilda"}], "studios": None},
        })
        self.assertEqual(r.status_code, 200, r.text)
        cast = r.json()["sections"]["cast"]
        self.assertTrue(cast["custom"])
        self.assertEqual(cast["entries"], [{"name": "Tilda Swinton", "label": "Tilda"}])
        self.assertNotEqual(self.main._render_assets_signature, before)
        got = self.client.get("/admin/api/sash-lists", headers=self.h).json()
        self.assertEqual(got["sections"]["cast"]["entries"], cast["entries"])

    def test_bad_saves_write_nothing(self):
        for body in (
            {"changes": {"actors": []}},
            {"changes": {"cast": [{"name": ""}]}},
            {"changes": {}},
        ):
            with self.subTest(body=body):
                r = self.client.put("/admin/api/sash-lists", headers=self.h, json=body)
                self.assertEqual(r.status_code, 400)
        self.assertFalse(os.path.exists(self.path))

    def test_search_and_lookup_flag_names_tmdb_lacks(self):
        people = {"results": [
            {"id": 1, "name": "Neon Actor", "known_for_department": "Acting", "known_for": []},
            {"id": 2, "name": "Bong Joon-ho", "known_for_department": "Directing",
             "profile_path": "/b.jpg", "known_for": [{"title": "Parasite"}]},
        ]}

        async def fake_get(url, params):
            return httpx.Response(200, json=people)

        with mock.patch.object(self.main, "_proxy_tmdb_get", fake_get), \
             mock.patch.object(self.main, "_art_client_and_key", return_value=(None, "k")):
            r = self.client.get("/admin/api/sash-lists/search?section=directors&q=bong", headers=self.h)
            self.assertEqual(r.status_code, 200, r.text)
            first = r.json()["results"][0]
            self.assertEqual(first["name"], "Bong Joon-ho")   # directors first for the director list
            self.assertEqual(first["known_for"], ["Parasite"])
            r = self.client.post("/admin/api/sash-lists/lookup", headers=self.h,
                                 json={"section": "directors", "names": ["Bong Joon-ho", "Nobody"]})
        found = r.json()["found"]
        self.assertTrue(found["Bong Joon-ho"]["exact"])
        self.assertFalse(found["Nobody"]["exact"])


if __name__ == "__main__":
    unittest.main()
