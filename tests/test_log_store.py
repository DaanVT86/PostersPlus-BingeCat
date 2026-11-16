"""The dashboard Logs view's store: what it writes, how it filters, rotation."""
import logging
import os
import tempfile
import time
import unittest

import log_store


class LogStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.handler = log_store._JsonFileHandler(
            os.path.join(self.dir.name, "postersplus.log"), 64 * 1024,
            lambda s: s.replace("sekrit", "***"))
        self.prev = log_store._handler
        log_store._handler = self.handler
        self.addCleanup(setattr, log_store, "_handler", self.prev)
        self.log = logging.getLogger("test_log_store")
        self.log.propagate = False
        self.log.setLevel(logging.INFO)
        self.log.addHandler(self.handler)
        self.addCleanup(self.log.removeHandler, self.handler)

    def emit(self, rid, level, msg, *args, **kw):
        token = log_store.request_id.set(rid)
        try:
            self.log.log(level, msg, *args, **kw)
        finally:
            log_store.request_id.reset(token)

    def emit_access(self, rid, path, status):
        token = log_store.request_id.set(rid)
        try:
            rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0,
                                    '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", path, "1.1", status), None)
            self.handler.handle(rec)
        finally:
            log_store.request_id.reset(token)

    def messages(self, **kw):
        expand = kw.pop("expand", False)
        return [e["m"] for e in log_store.search(log_store.Query(**kw), expand=expand)["entries"]]

    def test_redacts_and_keeps_full_access_path(self):
        path = "/poster?tmdb_id=550&access_key=sekrit&" + "x" * 200
        self.emit_access("r1", path, 200)
        self.emit("r1", logging.INFO, "token sekrit here")
        entries = log_store.search(log_store.Query())["entries"]
        self.assertEqual(entries[0]["m"], "token *** here")
        self.assertEqual(entries[1]["m"], "GET " + path.replace("sekrit", "***"))
        self.assertEqual(entries[1]["st"], 200)
        self.assertEqual(entries[1]["rid"], "r1")

    def test_addon_settings_blob_is_dropped(self):
        # The cfg- segment is base64 of the user's poster query, keys and all.
        self.emit_access("r1", "/trending/cfg-dG1kYl9rZXk9c2VrcmV0MTIzJnNhc2g9MQ/catalog/movie/t.json", 200)
        self.emit("r1", logging.WARNING, "Bad addon config /trending/cfg-dG1kYl9rZXk9c2Vr== for x")
        got = self.messages()
        self.assertEqual(got, ["Bad addon config /trending/cfg-… for x",
                               "GET /trending/cfg-…/catalog/movie/t.json"])

    def test_a_kinded_tmdb_id_leaves_out_the_other_kind(self):
        # Movie 550 and series 550 are different titles.
        self.emit("m", logging.INFO, "TMDB metadata cache expired for movie_550_en_p3d1")
        self.emit("m", logging.INFO, "Poster cache hit for 550")
        self.emit_access("m", "/poster?tmdb_id=550&type=movie", 200)
        self.emit("t", logging.INFO, "TMDB metadata cache expired for tv_550_en_p3d1")
        self.emit("t", logging.INFO, "Poster cache hit for 550")
        self.emit_access("t", "/poster?type=series&tmdb_id=550", 200)
        self.emit(None, logging.INFO, "Final poster cached for tmdb:550:550:tv:abc")
        self.emit(None, logging.INFO, "Backdrop face-aware crop for 550: left=3")
        movie = log_store.search(log_store.Query(ids="movie:550"))["entries"]
        self.assertEqual([(e.get("rid"), e["m"]) for e in movie], [
            (None, "Backdrop face-aware crop for 550: left=3"),
            ("m", "GET /poster?tmdb_id=550&type=movie"),
            ("m", "Poster cache hit for 550"),
            ("m", "TMDB metadata cache expired for movie_550_en_p3d1"),
        ])
        tv = log_store.search(log_store.Query(ids="tv:550"), expand=True)["entries"]
        self.assertEqual({e.get("rid") for e in tv}, {"t", None})
        self.assertEqual(len(tv), 5)
        self.assertEqual(len(self.messages(ids="550")), 8)

    def test_tmdb_kind_in(self):
        k = log_store.tmdb_kind_in
        self.assertEqual(k("expired for movie_550_en", "550"), "movie")
        self.assertEqual(k("cached for tmdb:1399:1399:tv:c6", "1399"), "tv")
        self.assertEqual(k("cached for tt0137523:550:movie:abc", "550"), "movie")
        self.assertEqual(k("-> TMDB tv/1399 via /find", "1399"), "tv")
        self.assertEqual(k("GET /poster?type=series&tmdb_id=1399", "1399"), "tv")
        self.assertIsNone(k("Poster cache hit for 1399", "1399"))
        self.assertIsNone(k("GET /poster?tmdb_id=12&type=movie", "1399"))

    def test_tmdb_id_matches_only_where_an_id_is_logged(self):
        self.emit(None, logging.INFO, "Poster cache hit for 550")
        self.emit(None, logging.INFO, "Invalidated final poster cache for tmdb_id=550")
        self.emit(None, logging.INFO, "TMDB metadata cache expired for movie_550_en_p3d1")
        self.emit(None, logging.INFO, "Backdrop crop for 12: left=550 of w=550")
        self.emit(None, logging.INFO, "Poster cache hit for 5501")
        self.emit(None, logging.INFO, "TVDB logo rescue for tvdb_id=550")
        self.emit(None, logging.INFO, "Quality for tt0137523: tokens=[]")
        got = self.messages(ids="550")
        self.assertEqual(len(got), 3)
        self.assertNotIn("TVDB logo rescue for tvdb_id=550", got)
        self.assertEqual(self.messages(ids="tvdb:550"), ["TVDB logo rescue for tvdb_id=550"])
        self.assertEqual(self.messages(ids="tt0137523"), ["Quality for tt0137523: tokens=[]"])

    def test_whole_requests_bring_their_other_lines(self):
        self.emit("a", logging.INFO, "External API Call: Requested meta from TMDB for 42")
        self.emit("b", logging.INFO, "unrelated")
        self.emit("a", logging.ERROR, "render failed")
        self.emit_access("a", "/health", 200)
        self.emit_access("a", "/poster?tmdb_id=42", 500)
        got = log_store.search(log_store.Query(levels="ERROR"), expand=True)["entries"]
        self.assertEqual([(e["m"], bool(e.get("ctx"))) for e in got], [
            ("GET /poster?tmdb_id=42", True),
            ("render failed", False),
            ("External API Call: Requested meta from TMDB for 42", True),
        ])

    def test_text_terms_exclusions_regex_and_problems(self):
        self.emit(None, logging.INFO, "TMDB logo cache hit")
        self.emit(None, logging.INFO, "TMDB backdrop cache hit for 7")
        self.emit(None, logging.WARNING, "MDBList quota exhausted")
        self.emit_access(None, "/poster?imdb_id=tt1", 404)
        self.assertEqual(self.messages(q="cache -logo"), ["TMDB backdrop cache hit for 7"])
        self.assertEqual(self.messages(q='"logo cache"'), ["TMDB logo cache hit"])
        self.assertEqual(self.messages(q="/backdrop.*\\d$/"), ["TMDB backdrop cache hit for 7"])
        self.assertEqual(self.messages(problems=True), ["GET /poster?imdb_id=tt1", "MDBList quota exhausted"])
        with self.assertRaises(ValueError):
            log_store.Query(q="/(/")

    def test_paging_and_tail(self):
        for i in range(10):
            self.emit(None, logging.INFO, f"line {i}")
        first = log_store.search(log_store.Query(), limit=4)
        self.assertEqual([e["m"] for e in first["entries"]], ["line 9", "line 8", "line 7", "line 6"])
        second = log_store.search(log_store.Query(), before=first["older"], limit=4)
        self.assertEqual([e["m"] for e in second["entries"]], ["line 5", "line 4", "line 3", "line 2"])
        self.emit(None, logging.INFO, "line 10")
        self.assertEqual([e["m"] for e in log_store.tail(log_store.Query(), first["tail"])["entries"]], ["line 10"])

    def test_tail_reads_only_new_bytes(self):
        for i in range(50):
            self.emit(None, logging.INFO, f"line {i}")
        cursor = log_store.search(log_store.Query())["tail"]
        self.emit(None, logging.INFO, "fresh")
        segs = log_store._segments(log_store._parse_cursor(cursor))
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][1].count(b"\n"), 1)
        got = log_store.tail(log_store.Query(), cursor)
        self.assertEqual([e["m"] for e in got["entries"]], ["fresh"])
        self.assertEqual(log_store.tail(log_store.Query(), got["tail"])["entries"], [])

    def test_live_whole_requests_bring_earlier_lines(self):
        self.emit("a", logging.INFO, "External API Call: Requested meta from TMDB for 42")
        cursor = log_store.search(log_store.Query())["tail"]
        self.emit("a", logging.ERROR, "render failed")
        self.emit("b", logging.INFO, "unrelated")
        got = log_store.tail(log_store.Query(levels="ERROR"), cursor, expand=True)["entries"]
        self.assertEqual([(e["m"], bool(e.get("ctx"))) for e in got], [
            ("External API Call: Requested meta from TMDB for 42", True),
            ("render failed", False),
        ])
        plain = log_store.tail(log_store.Query(levels="ERROR"), cursor)["entries"]
        self.assertEqual([e["m"] for e in plain], ["render failed"])

    def test_rotation_keeps_both_halves_and_cursors(self):
        cursor = log_store.search(log_store.Query())["tail"]
        for i in range(1500):
            self.emit(None, logging.INFO, f"filler {i:05d} " + "x" * 40)
        self.handler._checked = 0
        self.emit(None, logging.INFO, "last")
        self.assertTrue(os.path.exists(self.handler.path + ".1"))
        total = sum(os.path.getsize(p) for p in (self.handler.path, self.handler.path + ".1"))
        self.assertLess(total, 64 * 1024 * 1.2)
        self.assertEqual(self.messages(q="last"), ["last"])
        # A tail cursor from before the rotation still finds the newest lines.
        self.assertEqual(log_store.tail(log_store.Query(), cursor)["entries"][-1]["m"], "last")

    def test_since_stops_at_old_lines(self):
        self.emit(None, logging.INFO, "old")
        self.emit(None, logging.INFO, "new")
        self.assertEqual(self.messages(since=time.time() + 10), [])
        self.assertEqual(self.messages(since=time.time() - 10), ["new", "old"])


if __name__ == "__main__":
    unittest.main()
