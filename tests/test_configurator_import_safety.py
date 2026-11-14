"""Import URL: a pasted link can't swap the user's API keys, and only a
well-formed title id moves the selection (keeping the card, links and Report
button on the title the preview shows).  Checked in headless Chromium when
written; these pin the rules in the page source."""
from pathlib import Path
import re
import unittest


class ImportSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = Path("configurator.html").read_text(encoding="utf-8")
        start = html.index("function importUrl(")
        cls.body = html[start:html.index("\n}\n", start)]
        cls.html = html

    def test_keys_are_never_imported(self):
        # BingeCat fork: an imported URL's keys are ignored, not just kept out
        # of filled fields (see test_access_key_source for storage).
        self.assertIsNone(re.search(r"cfg-(tmdb|mdblist)-key", self.body))
        self.assertIn("if (p.has('tmdb_key') || p.has('mdblist_key')) clearPersistedApiKeys();", self.body)

    def test_domain_and_access_key_are_never_imported(self):
        self.assertNotIn("cfg-domain", self.body.split("if (!settingsOnly)")[1].split("// Generated URLs")[0])
        self.assertIsNone(re.search(r"_setEl\('cfg-(domain|access-key)'", self.body))

    def test_title_ids_are_validated(self):
        self.assertIn("/^[0-9]{1,10}$/.test(rawTmdb)", self.body)
        self.assertIn("/^tt[0-9]{1,10}$/.test(rawImdb)", self.body)
        self.assertNotIn("resolvedTmdbId = rawTmdb", self.body)

    def test_report_sends_the_previewed_title(self):
        send = self.html[self.html.index("async function sendReport("):]
        self.assertIn("tmdb_id: resolvedTmdbId", send[:3000])


if __name__ == "__main__":
    unittest.main()
