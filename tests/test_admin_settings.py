"""The settings registry and the admin dashboard's API.

config.py declares every operator setting through settings.env(); the file
the dashboard writes wins over the environment, which wins over the default.
The API is gated by ADMIN_KEY alone and validates a whole save before
writing any of it.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import settings as _settings
from settings import Setting, normalise


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "settings.json")
        self._saved_path = _settings.SETTINGS_PATH
        _settings._reset_for_tests(self.path)

    def tearDown(self):
        _settings._reset_for_tests(self._saved_path)
        self._dir.cleanup()

    def test_file_beats_env_beats_default(self):
        with mock.patch.dict(os.environ, {"PP_TEST_X": "from-env"}, clear=False):
            self.assertEqual(_settings.resolve("PP_TEST_X", "dflt"), "from-env")
            self.assertEqual(_settings.source_of("PP_TEST_X"), "env")
            _settings.save({"PP_TEST_X": "from-file"})
            self.assertEqual(_settings.resolve("PP_TEST_X", "dflt"), "from-file")
            self.assertEqual(_settings.source_of("PP_TEST_X"), "file")
            _settings.save({"PP_TEST_X": None})
            self.assertEqual(_settings.resolve("PP_TEST_X", "dflt"), "from-env")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PP_TEST_X", None)
            self.assertEqual(_settings.resolve("PP_TEST_X", "dflt"), "dflt")
            self.assertEqual(_settings.source_of("PP_TEST_X"), "default")

    def test_save_is_atomic_and_readable_back(self):
        _settings.save({"A": "1", "B": "two"})
        _settings.save({"A": None, "C": ""})
        with open(self.path) as fh:
            self.assertEqual(json.load(fh), {"B": "two", "C": ""})
        self.assertFalse(any(n.startswith(".settings-") for n in os.listdir(self._dir.name)))

    def test_a_corrupt_file_is_ignored_not_fatal(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        _settings._reset_for_tests(self.path)
        self.assertEqual(_settings.resolve("ANY", "d"), "d")

    def test_pending_restart_reports_saved_but_unapplied_keys(self):
        # env() records what the process started with; a later save differs.
        saved_groups = list(_settings.GROUPS)
        with mock.patch.dict(_settings.REGISTRY, {}, clear=True), \
             mock.patch.dict(_settings._running, {}, clear=True):
            self.addCleanup(lambda: _settings.GROUPS.__init__(saved_groups))
            self.assertEqual(_settings.env("PP_TEST_Y", "1", group="t", kind="int"), "1")
            self.assertEqual(_settings.pending_restart(), [])
            _settings.save({"PP_TEST_Y": "2"})
            self.assertEqual(_settings.pending_restart(), ["PP_TEST_Y"])
            _settings.save({"PP_TEST_Y": None})
            self.assertEqual(_settings.pending_restart(), [])


class NormaliseTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(normalise(Setting("K", "", "g", "bool"), "Yes"), "true")
        self.assertEqual(normalise(Setting("K", "", "g", "bool"), False), "false")
        with self.assertRaises(ValueError):
            normalise(Setting("K", "", "g", "bool"), "maybe")
        self.assertEqual(normalise(Setting("K", "", "g", "int", min=1, max=5), " 3 "), "3")
        with self.assertRaises(ValueError):
            normalise(Setting("K", "", "g", "int", min=1, max=5), "9")
        with self.assertRaises(ValueError):
            normalise(Setting("K", "", "g", "int"), "3.5")
        self.assertEqual(normalise(Setting("K", "", "g", "float", min=0, max=1), "0.7"), "0.7")
        self.assertEqual(normalise(Setting("K", "", "g", "choice", choices=("a", "b")), "B"), "b")
        with self.assertRaises(ValueError):
            normalise(Setting("K", "", "g", "choice", choices=("a", "b")), "c")
        with self.assertRaises(ValueError):
            normalise(Setting("K", "", "g", "url"), "example.com")
        self.assertEqual(normalise(Setting("K", "", "g", "list"), " a, b ,,c "), "a,b,c")

    def test_blank_means_no_override_for_parsed_kinds_but_is_a_value_for_text(self):
        self.assertIsNone(normalise(Setting("K", "3", "g", "int"), ""))
        self.assertIsNone(normalise(Setting("K", "true", "g", "bool"), ""))
        self.assertIsNone(normalise(Setting("K", "a", "g", "choice", choices=("a",)), ""))
        self.assertEqual(normalise(Setting("K", "", "g", "text"), ""), "")
        self.assertEqual(normalise(Setting("K", "", "g", "secret"), ""), "")


class RegistryTests(unittest.TestCase):
    def test_config_declares_every_env_setting_once_with_metadata(self):
        import config  # noqa: F401 — populates the registry
        self.assertGreater(len(_settings.REGISTRY), 80)
        for key, s in _settings.REGISTRY.items():
            with self.subTest(key=key):
                self.assertTrue(s.help, "every setting needs help text")
                self.assertTrue(s.label and s.label != key, "every setting needs a label")
                self.assertIn(s.kind, _settings.KINDS)
                if s.kind == "choice":
                    self.assertIn(s.default.lower(), s.choices)
                if s.kind == "int":
                    int(s.default)
                if s.kind == "bool":
                    self.assertIn(s.default, ("true", "false"))
        for group in _settings.GROUPS:
            self.assertIn(group, _settings.GROUP_ORDER, f"{group!r} has no place in the sidebar order")

    def test_show_if_points_at_an_earlier_setting_in_the_same_group_with_a_valid_value(self):
        import config  # noqa: F401
        for key, s in _settings.REGISTRY.items():
            if not s.show_if:
                continue
            with self.subTest(key=key):
                dep_key, values = s.show_if
                ctrl = _settings.REGISTRY.get(dep_key)
                self.assertIsNotNone(ctrl, f"{key} depends on unknown {dep_key}")
                # The switch must render above what it controls.
                self.assertEqual(ctrl.group, s.group)
                self.assertLess(ctrl.order, s.order, f"{dep_key} must be declared before {key}")
                self.assertFalse(ctrl.advanced and not s.advanced, "an everyday field cannot hang off an advanced switch")
                if ctrl.kind == "choice":
                    self.assertTrue(set(values) <= set(ctrl.choices), f"{key}: {values} not in {ctrl.choices}")
                elif ctrl.kind == "bool":
                    self.assertTrue(set(values) <= {"true", "false"})


class AdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.client = TestClient(main.app)

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved_path = _settings.SETTINGS_PATH
        _settings._reset_for_tests(os.path.join(self._dir.name, "settings.json"))
        import admin
        self._admin = admin
        self._saved_key = admin.ADMIN_KEY
        admin.ADMIN_KEY = "s3cret-long-enough"
        admin._failures.clear()
        admin._lockouts.clear()
        # Every failed attempt is deliberately slow; the tests below fail on
        # purpose, so take the wait out.
        self._delay = mock.patch.object(admin, "_FAIL_DELAY", 0.0)
        self._delay.start()

    def tearDown(self):
        self._delay.stop()
        self._admin._failures.clear()
        self._admin._lockouts.clear()
        self._admin.ADMIN_KEY = self._saved_key
        _settings._reset_for_tests(self._saved_path)
        self._dir.cleanup()

    def _h(self, key="s3cret-long-enough"):
        return {"X-Admin-Key": key}

    def test_short_keys_do_not_enable_the_dashboard(self):
        self.assertEqual(self._admin._validated_key("abc"), "")
        self.assertEqual(self._admin._validated_key("  "), "")
        self.assertEqual(self._admin._validated_key("twelve-chars"), "twelve-chars")

    def test_key_is_header_only(self):
        # A query-string key would land in access logs and could be fired
        # from an <img> on another site.
        self.assertEqual(self.client.get("/admin/api/settings?admin_key=s3cret-long-enough").status_code, 401)
        self.assertEqual(self.client.get("/admin/api/settings", headers=self._h()).status_code, 200)

    def test_non_ascii_key_is_a_401_not_a_500(self):
        # Header bytes arrive latin-1 decoded, so a non-ASCII byte becomes a
        # non-ASCII str — which hmac.compare_digest on str would choke on.
        headers = {"X-Admin-Key": "pässwörd-long".encode("latin-1")}
        self.assertEqual(self.client.get("/admin/api/settings", headers=headers).status_code, 401)

    def test_repeated_bad_keys_lock_the_client_out(self):
        for _ in range(self._admin._FAIL_LIMIT):
            self.assertEqual(self.client.get("/admin/api/settings", headers=self._h("wrong-key-value")).status_code, 401)
        # Locked: even the right key is refused, and the session probe too.
        r = self.client.get("/admin/api/settings", headers=self._h())
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.client.get("/admin/api/session", headers=self._h()).status_code, 429)
        # A lockout that has expired clears itself.
        for ip in list(self._admin._lockouts):
            self._admin._lockouts[ip] = 0.0
        self.assertEqual(self.client.get("/admin/api/settings", headers=self._h()).status_code, 200)

    def test_session_probe_counts_wrong_keys(self):
        for _ in range(self._admin._FAIL_LIMIT):
            self.assertEqual(self.client.get("/admin/api/session", headers=self._h("wrong-key-value")).json()["ok"], False)
        self.assertEqual(self.client.get("/admin/api/session", headers=self._h()).status_code, 429)

    def test_responses_are_not_cacheable_and_page_cannot_be_framed(self):
        r = self.client.get("/admin/api/settings", headers=self._h())
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        page = self.client.get("/admin")
        self.assertEqual(page.headers.get("x-frame-options"), "DENY")
        self.assertEqual(page.headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(page.headers.get("cache-control"), "no-store")

    def test_oversized_and_non_string_values_are_rejected(self):
        r = self.client.put("/admin/api/settings", headers=self._h(),
                            json={"changes": {"TRENDING_FETCH_TIMEZONE": "x" * 5000, "TRENDING_FETCH_TIME": ["a"]}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()["errors"]), {"TRENDING_FETCH_TIMEZONE", "TRENDING_FETCH_TIME"})

    def test_disabled_without_admin_key(self):
        self._admin.ADMIN_KEY = ""
        self.assertEqual(self.client.get("/admin/api/session").json(), {"enabled": False, "ok": False})
        self.assertEqual(self.client.get("/admin/api/settings", headers=self._h()).status_code, 403)
        # The page itself still serves, so it can explain how to enable it.
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_wrong_or_missing_key_is_rejected(self):
        self.assertEqual(self.client.get("/admin/api/settings").status_code, 401)
        self.assertEqual(self.client.get("/admin/api/settings", headers=self._h("nope")).status_code, 401)
        self.assertEqual(self.client.get("/admin/api/session", headers=self._h("nope")).json(), {"enabled": True, "ok": False})
        self.assertEqual(self.client.get("/admin/api/session", headers=self._h()).json(), {"enabled": True, "ok": True})

    def test_access_key_does_not_open_the_dashboard(self):
        import config as _cfg
        with mock.patch.object(_cfg, "ACCESS_KEY", "client-key"):
            self.assertEqual(self.client.get("/admin/api/settings?access_key=client-key").status_code, 401)

    def test_listing_masks_secrets_and_groups_in_sidebar_order(self):
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "abcdefgh1234"}):
            body = self.client.get("/admin/api/settings", headers=self._h()).json()
        names = [g["name"] for g in body["groups"]]
        self.assertEqual(names[:2], ["API keys", "Access & serving"])
        tmdb = next(s for g in body["groups"] for s in g["settings"] if s["key"] == "TMDB_API_KEY")
        self.assertEqual(tmdb["kind"], "secret")
        self.assertEqual(tmdb["value"], "")
        self.assertTrue(tmdb["has_value"])
        self.assertEqual(tmdb["hint"], "…1234")
        self.assertNotIn("abcdefgh", json.dumps(body))

    def test_save_validates_everything_before_writing_anything(self):
        r = self.client.put("/admin/api/settings", headers=self._h(),
                            json={"changes": {"JPEG_QUALITY": "99", "IMAGE_FORMAT": "jpeg", "NOPE": "x"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()["errors"]), {"JPEG_QUALITY", "NOPE"})
        self.assertEqual(_settings.file_values(), {})

    def test_save_writes_reports_pending_and_reset_removes(self):
        r = self.client.put("/admin/api/settings", headers=self._h(),
                            json={"changes": {"JPEG_QUALITY": "90", "CACHE_WARM_ENABLED": True, "TRENDING_FETCH_TIME": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["saved"], ["CACHE_WARM_ENABLED", "JPEG_QUALITY", "TRENDING_FETCH_TIME"])
        self.assertEqual(_settings.file_values(), {"CACHE_WARM_ENABLED": "true", "JPEG_QUALITY": "90", "TRENDING_FETCH_TIME": ""})
        jq = next(s for g in body["groups"] for s in g["settings"] if s["key"] == "JPEG_QUALITY")
        self.assertEqual(jq["value"], "90")
        self.assertEqual(jq["source"], "file")
        self.assertTrue(jq["pending"])
        self.assertIn("JPEG_QUALITY", body["pending_restart"])

        r = self.client.put("/admin/api/settings", headers=self._h(), json={"changes": {"JPEG_QUALITY": None}})
        self.assertNotIn("JPEG_QUALITY", _settings.file_values())
        jq = next(s for g in r.json()["groups"] for s in g["settings"] if s["key"] == "JPEG_QUALITY")
        self.assertEqual(jq["source"], "default")

    def test_restart_signals_the_server_after_answering(self):
        import time as _time
        signalled = []
        import main
        with mock.patch.object(self._admin, "_terminate", side_effect=lambda pid: signalled.append(pid)), \
             mock.patch.object(self._admin, "_server_pid", return_value=4242), \
             TestClient(main.app) as client:   # context-managed: the loop outlives the request
            self.assertEqual(client.post("/admin/api/restart").status_code, 401)
            r = client.post("/admin/api/restart", headers=self._h())
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["pid"], 4242)
            # The signal is deferred half a second so the response leaves first.
            deadline = _time.time() + 3
            while not signalled and _time.time() < deadline:
                _time.sleep(0.1)
        self.assertEqual(signalled, [4242])

    def test_status_carries_stats_and_restart_state(self):
        body = self.client.get("/admin/api/status", headers=self._h()).json()
        for key in ("cache", "runtime", "watchlist", "uptime_secs", "pending_restart", "settings_file", "version"):
            self.assertIn(key, body)

    # SIMKL linking is an operator action: the pending code lets whoever
    # approves it point the instance at their own watchlist, so it is behind
    # the admin key and off every access-key endpoint.
    def test_simkl_link_state_is_admin_only(self):
        import config as _cfg
        import watchlist
        pending = {"user_code": "ABCD", "link": "https://simkl.com/pin", "expires_at": 9e12, "flow": "v1"}
        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(_cfg, "SIMKL_ACCESS_TOKEN", ""), \
             mock.patch.object(watchlist, "_simkl_load_tokens", return_value=None), \
             mock.patch.object(watchlist, "_simkl_pending", pending):
            self.assertEqual(self.client.get("/admin/api/watchlist").status_code, 401)
            body = self.client.get("/admin/api/watchlist", headers=self._h()).json()
            self.assertEqual(body["simkl"], {"linked": False, "pending": pending})
            self.assertEqual(self.client.get("/admin/api/status", headers=self._h()).json()["watchlist"]["simkl"]["pending"], pending)
            # The public views carry the snapshot only.
            for path in ("/server-caps", "/stats"):
                public = self.client.get(path).json()["watchlist"]
                self.assertNotIn("simkl", public)
                self.assertEqual(public["source"], "simkl")
            self.assertEqual(self.client.get("/watchlist/status").status_code, 404)

    def test_simkl_link_nudges_the_loop_only_when_nothing_is_pending(self):
        import config as _cfg
        import watchlist
        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "mdblist"):
            r = self.client.post("/admin/api/watchlist/simkl/link", headers=self._h())
            self.assertEqual(r.status_code, 400)
        pending = {"user_code": "ABCD", "link": "https://simkl.com/pin", "expires_at": 9e12, "flow": "v1"}
        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(_cfg, "SIMKL_ACCESS_TOKEN", ""), \
             mock.patch.object(watchlist, "_simkl_load_tokens", return_value=None), \
             mock.patch.object(watchlist, "request_refresh") as refresh:
            self.assertEqual(self.client.post("/admin/api/watchlist/simkl/link").status_code, 401)
            with mock.patch.object(watchlist, "_simkl_pending", None):
                r = self.client.post("/admin/api/watchlist/simkl/link", headers=self._h())
                self.assertEqual(r.status_code, 200)
                refresh.assert_called_once()
            with mock.patch.object(watchlist, "_simkl_pending", pending):
                r = self.client.post("/admin/api/watchlist/simkl/link", headers=self._h())
                self.assertEqual(r.json()["simkl"]["pending"], pending)
                refresh.assert_called_once()   # the outstanding code is kept

    def test_simkl_unlink_runs_the_registered_hook(self):
        import config as _cfg
        calls = []

        async def unlinker():
            calls.append(1)
            return {"unlinked": True, "revoked": True, "had_token": True, "flow": "v2"}

        with mock.patch.object(_cfg, "WATCHLIST_SOURCE", "simkl"), \
             mock.patch.object(self._admin, "_simkl_unlinker", unlinker):
            self.assertEqual(self.client.post("/admin/api/watchlist/simkl/unlink").status_code, 401)
            r = self.client.post("/admin/api/watchlist/simkl/unlink", headers=self._h())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [1])
        self.assertTrue(r.json()["revoked"])
        self.assertEqual(r.json()["status"]["source"], "simkl")


if __name__ == "__main__":
    unittest.main()


class MultiWorkerSaveTests(unittest.TestCase):
    """With WORKERS>1 each process holds its own copy of the file."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "_settings.json")
        self._old = _settings.SETTINGS_PATH
        _settings._reset_for_tests(self.path)
        self.addCleanup(_settings._reset_for_tests, self._old)

    def _other_worker_saves(self, values):
        # Another process writes the file; this one's cached copy is stale.
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(values, fh)
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_a_save_keeps_what_another_worker_saved(self):
        _settings.save({"A": "1"})
        self._other_worker_saves({"A": "1", "B": "2"})
        self.assertEqual(_settings.save({"C": "3"}), {"A": "1", "B": "2", "C": "3"})

    def test_reads_see_another_workers_save(self):
        _settings.save({"A": "1"})
        self._other_worker_saves({"A": "9"})
        self.assertEqual(_settings.file_values(), {"A": "9"})
