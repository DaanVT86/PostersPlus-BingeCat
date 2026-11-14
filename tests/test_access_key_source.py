"""The configurator's access key comes from its own URL, never from storage.

When the server has an ACCESS_KEY the page can't load without the right one in
its URL, so a remembered copy is never needed.  It was kept in localStorage,
outlived the server dropping or changing its key, and went into every copied
URL.  /server-caps says whether a key is required, so one left in an old
bookmark is dropped too.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import main

HTML = (Path(__file__).resolve().parent.parent / "configurator.html").read_text(encoding="utf-8")


class ServerCapsReportsAccessKey(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_open_instance_says_no_key_is_required(self):
        with mock.patch.object(main._cfg, "ACCESS_KEY", None):
            caps = self.client.get("/server-caps").json()
        self.assertIs(caps["access_key_required"], False)

    def test_keyed_instance_says_a_key_is_required(self):
        with mock.patch.object(main._cfg, "ACCESS_KEY", "sekrit"):
            caps = self.client.get("/server-caps?access_key=sekrit").json()
        self.assertIs(caps["access_key_required"], True)


class ConfiguratorReadsTheUrlOnly(unittest.TestCase):
    def test_access_key_is_never_read_from_or_written_to_storage(self):
        self.assertIsNone(re.search(r"localStorage\.(getItem|setItem)\('postersplus_access_key'", HTML))
        self.assertIn("localStorage.removeItem('postersplus_access_key')", HTML)

    def test_key_is_cleared_when_the_server_requires_none(self):
        self.assertIn("serverCaps.access_key_required === false", HTML)


class ConfiguratorProtectedExternally(unittest.TestCase):
    """CONFIGURATOR_EXTERNAL_AUTH: the configurator sits behind the operator's
    own login, so it stops asking for the key and is handed it instead; the
    poster API keeps requiring it."""

    def setUp(self):
        self.client = TestClient(main.app)
        patches = [mock.patch.object(main._cfg, "ACCESS_KEY", "sekrit"),
                   mock.patch.object(main._cfg, "CONFIGURATOR_EXTERNAL_AUTH", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_configurator_and_its_endpoints_open_without_the_key(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        caps = self.client.get("/server-caps")
        self.assertEqual(caps.status_code, 200)
        self.assertEqual(caps.json()["access_key"], "sekrit")

    def test_poster_and_logo_still_require_the_key(self):
        for path in ("/poster?tmdb_id=1&type=movie", "/logo?tmdb_id=1&type=movie", "/stats"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_off_by_default_the_configurator_needs_the_key_and_never_sees_it(self):
        with mock.patch.object(main._cfg, "CONFIGURATOR_EXTERNAL_AUTH", False):
            self.assertEqual(self.client.get("/").status_code, 403)
            self.assertEqual(self.client.get("/server-caps").status_code, 403)
            caps = self.client.get("/server-caps?access_key=sekrit").json()
        self.assertNotIn("access_key", caps)

    def test_page_takes_the_key_it_is_handed(self):
        self.assertIn("if (serverCaps.access_key && vEl('cfg-access-key') !== serverCaps.access_key)", HTML)


if __name__ == "__main__":
    unittest.main()
