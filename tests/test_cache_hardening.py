import os
import tempfile
import unittest

import cache
import main


def test_vanilla_snapshot_cache_is_persisted_and_pruned(monkeypatch, tmp_path):
    old_connection = getattr(cache._local, "conn", None)
    if old_connection is not None:
        del cache._local.conn
    monkeypatch.setattr(cache, "DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(cache, "TMDB_POSTER_CACHE_DIR", str(tmp_path / "posters"))
    monkeypatch.setattr(cache, "TMDB_LOGO_CACHE_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(cache, "_initialised", False)
    try:
        cache.init_db()
        assert cache.set_cached_vanilla_snapshot("a" * 64, b'{"snapshot":1}') is True
        assert cache.get_cached_vanilla_snapshot("a" * 64) == b'{"snapshot":1}'

        connection = cache.get_db()
        connection.execute(
            "UPDATE vanilla_snapshot_cache SET cached_at = 0 WHERE cache_key = ?",
            ("a" * 64,),
        )
        connection.commit()
        assert cache.get_cached_vanilla_snapshot("a" * 64) is None
        assert connection.execute(
            "SELECT COUNT(*) FROM vanilla_snapshot_cache"
        ).fetchone()[0] == 0
    finally:
        connection = getattr(cache._local, "conn", None)
        if connection is not None:
            connection.close()
            del cache._local.conn
        if old_connection is not None:
            cache._local.conn = old_connection


class CachePathTests(unittest.TestCase):
    def test_safe_cache_path_rejects_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as parent:
            base = os.path.join(parent, "cache")
            os.mkdir(base)
            with self.assertRaises(ValueError):
                cache._safe_cache_path(base, "../cache-other/file")

    def test_safe_cache_path_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(ValueError):
                cache._safe_cache_path(base, "/tmp/elsewhere")

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "poster")
            cache._atomic_write(path, b"first")
            cache._atomic_write(path, b"second")
            with open(path, "rb") as stored:
                self.assertEqual(stored.read(), b"second")
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(base)))


class RenderSignatureTests(unittest.TestCase):
    def test_visual_setting_changes_server_signature(self):
        original = main._cfg.JPEG_QUALITY
        try:
            before = main._server_render_signature()
            main._cfg.JPEG_QUALITY = original - 1
            after = main._server_render_signature()
            self.assertNotEqual(before, after)
        finally:
            main._cfg.JPEG_QUALITY = original


if __name__ == "__main__":
    unittest.main()
