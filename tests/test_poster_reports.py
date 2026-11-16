"""Poster reports: filing limits, the mass-report purge, key stripping,
forwarded-address detection, and the endpoints' gates."""
import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import admin
import cache
import config as _cfg
import main
import reports


def _memory_db():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT)")
    for statement in cache.REPORT_SCHEMA:
        db.execute(statement)
    return db


class _DbCase(unittest.TestCase):
    per_ip = 3
    purge_at = 6

    def setUp(self):
        self.db = _memory_db()
        state = {}
        for p in (
            mock.patch.object(reports, "get_db", lambda: self.db),
            mock.patch.object(reports, "get_app_state", state.get),
            mock.patch.object(reports, "set_app_state", state.__setitem__),
            mock.patch.object(_cfg, "REPORTS_PER_IP", self.per_ip),
            mock.patch.object(_cfg, "REPORTS_PURGE_THRESHOLD", self.purge_at),
        ):
            p.start()
            self.addCleanup(p.stop)

    def file(self, reporter="a" * 24, tmdb_id="603", category="wrong_poster", note=""):
        return reports.submit(reporter=reporter, media_type="movie", tmdb_id=tmdb_id, imdb_id="",
                              title="The Matrix", category=category, note=note, params="")

    def count(self, reporter=None):
        if reporter is None:
            return self.db.execute("SELECT COUNT(*) FROM poster_reports").fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM poster_reports WHERE reporter = ?",
                               (reporter,)).fetchone()[0]


class SubmitTests(_DbCase):
    def test_files_a_report(self):
        self.file()
        self.assertEqual(self.count(), 1)
        self.assertEqual(reports.counts()["open"], 1)

    def test_validation(self):
        for kw, status in (({"category": "nope"}, 400), ({"tmdb_id": "abc"}, 400),
                           ({"category": "other"}, 400)):
            with self.subTest(kw=kw), self.assertRaises(reports.ReportError) as err:
                self.file(**kw)
            self.assertEqual(err.exception.status, status)
        self.file(category="other", note="The logo is upside down")

    def test_each_id_is_checked_on_its_own(self):
        # A valid IMDb id used to let any tmdb_id through, and the dashboard
        # puts tmdb_id in an onclick handler.
        for tmdb_id, imdb_id in (("'+alert()+'", "tt0133093"), ("603", "tt'+x+'"),
                                 ("６０３", ""), ("²", ""), ("", "tt６０"), ("12345678901", "")):
            with self.subTest(tmdb_id=tmdb_id, imdb_id=imdb_id), \
                    self.assertRaises(reports.ReportError) as err:
                reports.submit(reporter="a" * 24, media_type="movie", tmdb_id=tmdb_id,
                               imdb_id=imdb_id, title="", category="wrong_poster", note="", params="")
            self.assertEqual(err.exception.status, 400)
        self.assertEqual(self.count(), 0)
        reports.submit(reporter="a" * 24, media_type="movie", tmdb_id="", imdb_id="tt0133093",
                       title="", category="wrong_poster", note="", params="")

    def test_note_and_title_lose_control_and_bidi_characters(self):
        self.file(category="other", note="fine‮txt.exe\x00\r\n\n\n\nnext​line")
        note, = self.db.execute("SELECT note FROM poster_reports").fetchone()
        self.assertEqual(note, "finetxt.exe\n\nnextline")
        self.assertEqual(reports.clean_text("a\nb⁦c", 200), "a bc")

    def test_duplicate_is_refused(self):
        self.file()
        with self.assertRaises(reports.ReportError) as err:
            self.file()
        self.assertEqual(err.exception.status, 409)
        self.file(category="wrong_logo")

    def test_daily_limit_is_per_reporter(self):
        for i in range(self.per_ip):
            self.file(tmdb_id=str(100 + i))
        with self.assertRaises(reports.ReportError) as err:
            self.file(tmdb_id="999")
        self.assertEqual(err.exception.status, 429)
        self.file(reporter="b" * 24, tmdb_id="999")

    def test_mass_reporting_deletes_everything_and_blocks(self):
        flood, bystander = "c" * 24, "d" * 24
        self.file(reporter=bystander)
        statuses = []
        for i in range(self.purge_at + 1):
            try:
                self.file(reporter=flood, tmdb_id=str(200 + i))
                statuses.append(200)
            except reports.ReportError as exc:
                statuses.append(exc.status)
        self.assertEqual(statuses[-1], 403)
        self.assertEqual(self.count(flood), 0)
        self.assertEqual(self.count(bystander), 1)
        self.assertEqual([b["reporter"] for b in reports.blocks()], [flood])
        with self.assertRaises(reports.ReportError) as err:
            self.file(reporter=flood, tmdb_id="1")
        self.assertEqual(err.exception.status, 403)
        # Unblocking forgets the attempts too, so the next report counts afresh.
        self.assertTrue(reports.unblock(flood))
        self.file(reporter=flood, tmdb_id="1")

    def _ids(self, reporter):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM poster_reports WHERE reporter = ? ORDER BY id", (reporter,))]

    def test_resolved_reports_hand_the_daily_quota_back(self):
        who = "f" * 24
        for i in range(self.per_ip):
            self.file(reporter=who, tmdb_id=str(300 + i))
        with self.assertRaises(reports.ReportError):
            self.file(reporter=who, tmdb_id="399")
        reports.set_status(self._ids(who)[:1], "resolved")
        self.file(reporter=who, tmdb_id="399")
        with self.assertRaises(reports.ReportError) as err:
            self.file(reporter=who, tmdb_id="398")
        self.assertEqual(err.exception.status, 429)

    def test_dismissed_and_deleted_reports_keep_counting(self):
        who = "0" * 24
        for i in range(self.per_ip):
            self.file(reporter=who, tmdb_id=str(400 + i))
        ids = self._ids(who)
        reports.set_status(ids[:1], "dismissed")
        reports.delete(ids[1:2])
        with self.assertRaises(reports.ReportError) as err:
            self.file(reporter=who, tmdb_id="499")
        self.assertEqual(err.exception.status, 429)

    def test_reopening_takes_the_quota_back(self):
        who = "1" * 24
        for i in range(self.per_ip):
            self.file(reporter=who, tmdb_id=str(500 + i))
        first = self._ids(who)[:1]
        reports.set_status(first, "resolved")
        reports.set_status(first, "open")
        with self.assertRaises(reports.ReportError):
            self.file(reporter=who, tmdb_id="599")

    def test_resolved_reports_dont_count_towards_the_purge_and_survive_it(self):
        who = "2" * 24
        with mock.patch.object(_cfg, "REPORTS_PER_IP", 1000):
            for i in range(self.purge_at):
                self.file(reporter=who, tmdb_id=str(600 + i))
            reports.set_status(self._ids(who), "resolved")
            # Well past the threshold in total, but the resolved ones are credited.
            for i in range(self.purge_at):
                self.file(reporter=who, tmdb_id=str(700 + i))
            with self.assertRaises(reports.ReportError) as err:
                self.file(reporter=who, tmdb_id="799")
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(self.count(who), self.purge_at)   # the resolved ones are kept
        self.assertEqual(reports.counts()["open"], 0)

    def test_block_from_dashboard(self):
        self.file(reporter="e" * 24)
        self.file(reporter="e" * 24, tmdb_id="604")
        self.assertEqual(reports.block("e" * 24), 2)
        self.assertEqual(self.count(), 0)

    def test_status_and_delete(self):
        self.file()
        rid = reports.list_reports()[0]["id"]
        reports.set_status([rid], "resolved")
        self.assertEqual(reports.list_reports("open"), [])
        self.assertEqual(reports.list_reports("resolved")[0]["id"], rid)
        with self.assertRaises(reports.ReportError):
            reports.set_status([rid], "bogus")
        self.assertEqual(reports.delete([rid]), 1)

    def test_reporter_id_hides_the_address_and_groups_ipv6_by_64(self):
        a = reports.reporter_id("203.0.113.7")
        self.assertNotIn("203", a)
        self.assertEqual(len(a), 24)
        self.assertNotEqual(a, reports.reporter_id("203.0.113.8"))
        self.assertEqual(reports.reporter_id("2001:db8::1"), reports.reporter_id("2001:db8::ffff"))
        self.assertNotEqual(reports.reporter_id("2001:db8::1"), reports.reporter_id("2001:db8:0:1::1"))


class HelperTests(unittest.TestCase):
    def test_clean_params_strips_every_key(self):
        q = reports.clean_params("https://x.example/poster?tmdb_id=603&type=movie&access_key=s"
                                 "&tmdb_key=t&mdblist_key=m&fanart_key=f&rating_badges=imdb")
        self.assertEqual(q, "tmdb_id=603&type=movie&rating_badges=imdb")

    def test_forwarding_state(self):
        fs = reports.forwarding_state
        self.assertEqual(fs("198.51.100.4", {"X-Forwarded-For": "198.51.100.4"}), "forwarded")
        self.assertEqual(fs("198.51.100.4", {"x-forwarded-for": "10.0.0.9, 198.51.100.4"}), "forwarded")
        self.assertEqual(fs("172.18.0.2", {"X-Forwarded-For": "198.51.100.4"}), "untrusted")
        self.assertEqual(fs("172.18.0.2", {}), "private")
        self.assertEqual(fs("127.0.0.1", {}), "private")
        self.assertEqual(fs("8.8.8.8", {}), "direct")


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def _patches(self, enabled=True, key="a-long-admin-key"):
        return [mock.patch.object(_cfg, "REPORTS_ENABLED", enabled),
                mock.patch.object(_cfg, "ACCESS_KEY", None),
                mock.patch.object(admin, "ADMIN_KEY", key),
                mock.patch.object(reports, "note_forwarding", lambda s: None)]

    def _with(self, patches, fn):
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in patches:
                p.stop()

    def test_caps_off_by_default(self):
        self.assertIs(_cfg.REPORTS_ENABLED, False)
        caps = self._with(self._patches(enabled=False), lambda: self.client.get("/server-caps").json())
        self.assertFalse(caps["reports"])

    def test_caps_need_the_dashboard(self):
        caps = self._with(self._patches(key=""), lambda: self.client.get("/server-caps").json())
        self.assertFalse(caps["reports"])

    def test_caps_hidden_behind_an_untrusted_proxy(self):
        caps = self._with(self._patches(), lambda: self.client.get(
            "/server-caps", headers={"X-Forwarded-For": "198.51.100.4"}).json())
        self.assertFalse(caps["reports"])
        caps = self._with(self._patches(), lambda: self.client.get("/server-caps").json())
        self.assertTrue(caps["reports"])

    def test_report_endpoint_gates(self):
        body = {"media_type": "movie", "tmdb_id": "603", "category": "wrong_poster"}
        r = self._with(self._patches(enabled=False), lambda: self.client.post("/report", json=body))
        self.assertEqual(r.status_code, 404)
        r = self._with(self._patches(), lambda: self.client.post(
            "/report", json=body, headers={"X-Forwarded-For": "198.51.100.4"}))
        self.assertEqual(r.status_code, 503)

    def test_report_endpoint_files(self):
        db = _memory_db()
        patches = self._patches() + [
            mock.patch.object(reports, "get_db", lambda: db),
            mock.patch.object(reports, "get_app_state", {}.get),
        ]
        url = "http://h/poster?tmdb_id=603&type=movie&access_key=secret&tmdb_key=k"
        r = self._with(patches, lambda: self.client.post("/report", json={
            "media_type": "movie", "tmdb_id": "603", "category": "fake_textless", "url": url}))
        self.assertEqual(r.status_code, 200, r.text)
        params = db.execute("SELECT params FROM poster_reports").fetchone()[0]
        self.assertEqual(params, "tmdb_id=603&type=movie")

    def _open_patches(self):
        db = _memory_db()
        return db, self._patches() + [mock.patch.object(reports, "get_db", lambda: db),
                                      mock.patch.object(reports, "get_app_state", {}.get)]

    def test_cross_site_simple_posts_are_refused(self):
        # text/plain and form bodies need no CORS preflight, so another site
        # could send them from a visitor's browser.
        db, patches = self._open_patches()
        body = '{"media_type":"movie","tmdb_id":"603","category":"wrong_poster"}'
        for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
            with self.subTest(ctype=ctype):
                r = self._with(patches, lambda: self.client.post(
                    "/report", content=body, headers={"Content-Type": ctype} if ctype else {}))
                self.assertEqual(r.status_code, 415)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM poster_reports").fetchone()[0], 0)

    def test_chunked_body_is_capped(self):
        db, patches = self._open_patches()

        def chunks():
            yield b'{"media_type":"movie","tmdb_id":"603","category":"other","note":"'
            for _ in range(50):
                yield b"A" * 10_000
            yield b'"}'
        r = self._with(patches, lambda: self.client.post(
            "/report", content=chunks(), headers={"Content-Type": "application/json"}))
        self.assertEqual(r.status_code, 413)

    def test_admin_api_needs_the_key(self):
        r = self._with(self._patches(), lambda: self.client.get("/admin/api/reports"))
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
