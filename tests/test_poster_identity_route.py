import asyncio
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import cache
import main
from bingecat_resolver import ResolvedIdentity


def _close_thread_cache_connection() -> None:
    conn = getattr(cache._local, "conn", None)
    if conn is not None:
        conn.close()
        delattr(cache._local, "conn")


class PosterIdentityRouteTests(unittest.TestCase):
    def test_poster_route_accepts_imdb_only_when_identity_resolves(self):
        async def fake_digital_loop(client):
            await asyncio.Event().wait()

        async def fake_resolve_identity(**kwargs):
            self.assertEqual(kwargs["imdb_id"], "tt1234567")
            self.assertIn(kwargs["tmdb_id"], ("", None))
            return ResolvedIdentity(
                imdb_id="tt1234567",
                tmdb_id="42",
                media_type="movie",
                source="test",
            )

        with tempfile.TemporaryDirectory() as tmp:
            _close_thread_cache_connection()
            patches = [
                patch.object(cache, "_initialised", False),
                patch.object(cache, "DB_PATH", f"{tmp}/cache.db"),
                patch.object(cache, "TMDB_POSTER_CACHE_DIR", f"{tmp}/tmdb_posters"),
                patch.object(cache, "TMDB_LOGO_CACHE_DIR", f"{tmp}/tmdb_logos"),
                patch.object(main._cfg, "ACCESS_KEY", None),
                patch.object(main._cfg, "SERVER_TMDB_KEY", "server-tmdb-key"),
                patch.object(main._cfg, "BINGECAT_DATABASE_URL", ""),
                patch.object(main._cfg, "TEXTLESS_TEXT_DETECTION", False),
                patch.object(main, "digital_release_poll_loop", fake_digital_loop),
                patch.object(main, "resolve_poster_identity", fake_resolve_identity),
                patch.object(main, "get_cached_final_poster", lambda cache_key: b"jpeg"),
            ]
            for active_patch in patches:
                active_patch.start()
            try:
                with TestClient(main.app) as client:
                    response = client.get("/poster", params={"imdb_id": "tt1234567"})
            finally:
                for active_patch in reversed(patches):
                    active_patch.stop()
                _close_thread_cache_connection()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg")
        self.assertEqual(response.headers["x-postersplus-imdb-id"], "tt1234567")
        self.assertEqual(response.headers["x-postersplus-tmdb-id"], "42")
        self.assertEqual(response.headers["x-postersplus-type"], "movie")


if __name__ == "__main__":
    unittest.main()
