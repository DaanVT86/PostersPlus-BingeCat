"""Operator sash label overrides edited from the dashboard (Sash labels)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import i18n
from i18n import language_text, load_languages, translate_sash


class _LabelsFile(unittest.TestCase):
    """Points i18n at a scratch overrides file and restores it after."""

    @classmethod
    def setUpClass(cls):
        load_languages()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "sash_labels.json")
        self._patch = mock.patch.object(i18n, "_OVERRIDE_PATH", self.path)
        self._patch.start()
        i18n.load_sash_overrides()

    def tearDown(self):
        self._patch.stop()
        i18n.load_sash_overrides()
        self._dir.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        i18n.load_sash_overrides()

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


class OverrideTests(_LabelsFile):
    def test_no_file_means_the_bundled_labels(self):
        self.assertEqual(translate_sash("New Season", "en"), "New Season")
        self.assertEqual(translate_sash("New Season", "fr"), "Nouvelle saison")

    def test_an_override_rewords_its_language_only(self):
        self.write({"fr": {"New Season": "Saison inédite"}})
        self.assertEqual(translate_sash("New Season", "fr"), "Saison inédite")
        self.assertEqual(translate_sash("New Season", "fr-CA"), "Saison inédite")
        self.assertEqual(translate_sash("New Season", "en"), "New Season")
        # Everything else in French keeps the bundled wording.
        self.assertEqual(translate_sash("Cinema", "fr"), "Au cinéma")

    def test_english_overrides_reach_languages_without_a_file(self):
        self.write({"en": {"New Season": "Fresh Season"}})
        self.assertEqual(translate_sash("New Season", "en"), "Fresh Season")
        self.assertEqual(translate_sash("New Season", None), "Fresh Season")
        self.assertEqual(translate_sash("New Season", "zz"), "Fresh Season")
        self.assertEqual(translate_sash("New Season", "fr"), "Nouvelle saison")

    def test_a_regional_override_layers_on_the_base_language(self):
        self.write({"fr-ca": {"New Season": "Saison neuve"}, "fr": {"Cinema": "En salle"}})
        self.assertEqual(translate_sash("New Season", "fr-CA"), "Saison neuve")
        # The rest of French, bundled and overridden, still applies.
        self.assertEqual(translate_sash("Cinema", "fr-CA"), "En salle")
        self.assertEqual(translate_sash("Palme d'Or", "fr-CA"), "Palme d'or")
        self.assertEqual(translate_sash("New Season", "fr"), "Nouvelle saison")

    def test_a_reload_replaces_the_cached_tables(self):
        self.assertEqual(translate_sash("New Season", "en"), "New Season")
        self.write({"en": {"New Season": "Fresh"}})
        self.assertEqual(translate_sash("New Season", "en"), "Fresh")
        self.write({})
        self.assertEqual(translate_sash("New Season", "en"), "New Season")

    def test_templates_and_composites_read_the_overrides(self):
        self.write({"en": {
            "trendingToday": "Top {rank}",
            "releaseDayStreaming": "On streaming {day} {month}",
            "Oscar Nominee": "Oscar Nom",
        }})
        self.assertEqual(translate_sash("#3 Today", "en"), "Top 3")
        self.assertEqual(translate_sash("Oct 16 Streaming", "en"), "On streaming 16 Oct")
        self.assertEqual(translate_sash("Oscar Nominee • Emmy Nominee", "en"), "Oscar Nom • Emmy Nominee")

    def test_font_coverage_sees_the_overrides(self):
        self.write({"fr": {"New Season": "Ωmega"}, "en": {"Cinema": "Ψ"}})
        self.assertIn("Ω", language_text("fr"))
        self.assertNotIn("Ψ", language_text("fr"))
        # A language without a file draws English, overrides included.
        self.assertEqual(language_text("zz").replace("Ψ", ""), "")
        self.assertIn("Ψ", language_text("zz"))

    def test_a_broken_file_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        i18n.load_sash_overrides()
        self.assertEqual(translate_sash("New Season", "en"), "New Season")
        self.write(["not", "an", "object"])
        self.assertEqual(translate_sash("New Season", "en"), "New Season")

    def test_other_workers_reload_when_the_file_changes(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"en": {"New Season": "Again"}}, fh)
        i18n._checked_at = 0.0
        self.assertTrue(i18n.refresh_sash_overrides())
        self.assertEqual(translate_sash("New Season", "en"), "Again")
        i18n._checked_at = 0.0
        self.assertFalse(i18n.refresh_sash_overrides())   # unchanged
        os.remove(self.path)
        i18n._checked_at = 0.0
        self.assertTrue(i18n.refresh_sash_overrides())
        self.assertEqual(translate_sash("New Season", "en"), "New Season")

    def test_the_editor_is_not_shown_stale_entries(self):
        self.write({"fr": {"New Season": "Neuve", "Gone Label": "x"}, "xx": {"New Season": "y"}})
        self.assertEqual(i18n.sash_override_state()["overrides"], {"fr": {"New Season": "Neuve"}})

    def test_a_failed_save_leaves_no_temp_file(self):
        with mock.patch("i18n.os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            i18n.save_sash_overrides({"en": {"New Season": "x"}})
        self.assertEqual(os.listdir(self._dir.name), [])

    def test_save_writes_the_file_and_applies_it(self):
        i18n.save_sash_overrides({"de": {"New Season": "Neu"}})
        self.assertEqual(self.read(), {"de": {"New Season": "Neu"}})
        self.assertEqual(translate_sash("New Season", "de"), "Neu")


class ValidationTests(_LabelsFile):
    def test_blanks_and_copies_of_the_bundled_label_are_dropped(self):
        self.assertEqual(i18n.validate_sash_overrides({
            "fr": {"New Season": "  Saison   inédite ", "Cinema": "Au cinéma", "Streaming": ""},
            "de": {"New Season": None},
            "EN": {"Palme d'Or": "Palme"},
        }), {"fr": {"New Season": "Saison inédite"}, "en": {"Palme d'Or": "Palme"}})

    def test_placeholders(self):
        ok = i18n.validate_sash_overrides({"en": {
            "trendingToday": "No. {rank}",
            "releaseDay": "{month} {day}",                    # window dropped
            "releaseDayStreaming": "{window} {day}/{month}",  # window named
            "seasonWindow": "S{n}",
        }})
        self.assertEqual(len(ok["en"]), 4)
        for key, text in (
            ("trendingToday", "Trending"),           # rank dropped
            ("releaseDay", "{window} {month}"),      # day dropped
            ("trendingToday", "{rank} {day}"),       # day isn't a trending placeholder
            ("seasonWindow", "S{n} {window}"),
        ):
            with self.subTest(key=key, text=text), self.assertRaises(ValueError):
                i18n.validate_sash_overrides({"en": {key: text}})

    def test_malformed(self):
        for bad in (
            None,
            [],
            {"xx": {"New Season": "x"}},
            {"en": ["New Season"]},
            {"en": {"Not A Sash": "x"}},
            {"en": {"New Season": 3}},
            {"en": {"New Season": "y" * (i18n.MAX_OVERRIDE_LENGTH + 1)}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                i18n.validate_sash_overrides(bad)

    def test_length_ignores_placeholders(self):
        text = "x" * (i18n.MAX_OVERRIDE_LENGTH - 1) + " {rank}"
        self.assertEqual(i18n.validate_sash_overrides({"en": {"trendingToday": text}}),
                         {"en": {"trendingToday": text}})


class ApiTests(_LabelsFile):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import main
        cls.main = main
        cls.client = TestClient(main.app)

    def setUp(self):
        super().setUp()
        import admin
        self._key = mock.patch.object(admin, "ADMIN_KEY", "s3cret-long-enough")
        self._key.start()
        self.h = {"X-Admin-Key": "s3cret-long-enough"}

    def tearDown(self):
        self._key.stop()
        super().tearDown()
        self.main._render_assets_signature = self.main._compute_render_assets_signature()

    def test_needs_the_admin_key(self):
        with mock.patch("admin._FAIL_DELAY", 0.0):
            self.assertEqual(self.client.get("/admin/api/sash-labels").status_code, 401)
            self.assertEqual(self.client.put("/admin/api/sash-labels", json={}).status_code, 401)

    def test_get_lists_every_language_and_key(self):
        r = self.client.get("/admin/api/sash-labels", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("New Season", body["keys"])
        self.assertEqual(body["builtin"]["fr"]["New Season"], "Nouvelle saison")
        self.assertEqual(set(body["builtin"]["fr"]), set(body["keys"]))
        self.assertEqual(body["overrides"], {})
        self.assertEqual(body["path"], self.path)
        self.assertTrue(body["writable"])

    def test_save_round_trip_and_signature_changes(self):
        before = self.main._render_assets_signature
        r = self.client.put("/admin/api/sash-labels", headers=self.h, json={
            "overrides": {"en": {"New Season": "Fresh Season", "Cinema": ""}},
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["overrides"], {"en": {"New Season": "Fresh Season"}})
        self.assertEqual(self.read(), {"en": {"New Season": "Fresh Season"}})
        self.assertNotEqual(self.main._render_assets_signature, before)
        self.assertEqual(translate_sash("New Season", "en"), "Fresh Season")
        got = self.client.get("/admin/api/sash-labels", headers=self.h).json()
        self.assertEqual(got["overrides"], {"en": {"New Season": "Fresh Season"}})
        # Saving an empty map puts everything back.
        r = self.client.put("/admin/api/sash-labels", headers=self.h, json={"overrides": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(translate_sash("New Season", "en"), "New Season")

    def test_bad_saves_write_nothing(self):
        for body in (
            {},
            {"overrides": {"xx": {}}},
            {"overrides": {"en": {"trendingToday": "Trending"}}},
        ):
            with self.subTest(body=body):
                r = self.client.put("/admin/api/sash-labels", headers=self.h, json=body)
                self.assertEqual(r.status_code, 400)
        self.assertFalse(os.path.exists(self.path))

    def test_stale_entries_dont_block_a_save(self):
        self.write({"fr": {"New Season": "Neuve", "Gone Label": "x"}, "xx": {"New Season": "y"}})
        shown = self.client.get("/admin/api/sash-labels", headers=self.h).json()["overrides"]
        r = self.client.put("/admin/api/sash-labels", headers=self.h, json={"overrides": shown})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.read(), {"fr": {"New Season": "Neuve"}})


if __name__ == "__main__":
    unittest.main()
