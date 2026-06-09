import unittest

import httpx

from bingecat_resolver import (
    IdentityResolutionError,
    normalise_media_type,
    normalise_optional,
    resolve_poster_identity,
)


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    def acquire(self):
        return _FakeAcquire(self)

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.rows.pop(0) if self.rows else None


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.themoviedb.org/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def get(self, url, params=None, **kwargs):
        self.requests.append((url, params or {}))
        return self.responses.pop(0)


class BingeCatResolverTests(unittest.IsolatedAsyncioTestCase):
    def test_normalises_empty_placeholders_and_type_aliases(self):
        self.assertIsNone(normalise_optional(""))
        self.assertIsNone(normalise_optional("{imdb_id}"))
        self.assertIsNone(normalise_optional("undefined"))
        self.assertEqual(normalise_media_type("series"), "tv")
        self.assertEqual(normalise_media_type("show"), "tv")

    async def test_imdb_uses_media_data_before_tmdb(self):
        pool = _FakePool([{"imdb_id": "tt1234567", "tmdb_id": 42, "content_type": "series"}])
        client = _FakeClient([])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            imdb_id="tt1234567",
        )

        self.assertEqual(result.tmdb_id, "42")
        self.assertEqual(result.media_type, "tv")
        self.assertEqual(result.source, "bingecat_media_data_imdb")
        self.assertEqual(client.requests, [])

    async def test_imdb_uses_stremio_resolution_cache_after_media_miss(self):
        pool = _FakePool([
            None,
            {"imdb_id": "tt1234567", "tmdb_id": 43, "content_type": "movie"},
        ])
        client = _FakeClient([])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            imdb_id="tt1234567",
        )

        self.assertEqual(result.tmdb_id, "43")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.source, "bingecat_stremio_id_cache")

    async def test_imdb_falls_back_to_tmdb_find(self):
        pool = _FakePool([None, None])
        client = _FakeClient([_FakeResponse({"tv_results": [{"id": 44}]})])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            imdb_id="tt1234567",
        )

        self.assertEqual(result.tmdb_id, "44")
        self.assertEqual(result.media_type, "tv")
        self.assertEqual(result.source, "tmdb_find_imdb")

    async def test_tmdb_type_uses_media_data_to_fill_imdb(self):
        pool = _FakePool([{"imdb_id": "tt7654321", "tmdb_id": 55, "content_type": "movie"}])
        client = _FakeClient([])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            tmdb_id="55",
            media_type="movie",
        )

        self.assertEqual(result.imdb_id, "tt7654321")
        self.assertEqual(result.tmdb_id, "55")
        self.assertEqual(result.source, "bingecat_media_data_tmdb")

    async def test_tmdb_type_falls_back_to_external_ids(self):
        pool = _FakePool([None])
        client = _FakeClient([_FakeResponse({"imdb_id": "tt7654321"})])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            tmdb_id="55",
            media_type="movie",
        )

        self.assertEqual(result.imdb_id, "tt7654321")
        self.assertEqual(result.source, "tmdb_external_ids")

    async def test_tmdb_media_data_without_imdb_falls_back_to_external_ids(self):
        pool = _FakePool([{"imdb_id": None, "tmdb_id": 55, "content_type": "movie"}])
        client = _FakeClient([_FakeResponse({"imdb_id": "tt7654321"})])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            tmdb_id="55",
            media_type="movie",
        )

        self.assertEqual(result.imdb_id, "tt7654321")
        self.assertEqual(result.source, "tmdb_external_ids")

    async def test_imdb_has_priority_over_conflicting_tmdb(self):
        pool = _FakePool([{"imdb_id": "tt1234567", "tmdb_id": 42, "content_type": "movie"}])
        client = _FakeClient([])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            imdb_id="tt1234567",
            tmdb_id="999",
            media_type="tv",
        )

        self.assertEqual(result.tmdb_id, "42")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(len(pool.queries), 1)

    async def test_imdb_ignores_invalid_conflicting_tmdb_and_type(self):
        pool = _FakePool([{"imdb_id": "tt1234567", "tmdb_id": 42, "content_type": "movie"}])
        client = _FakeClient([])

        result = await resolve_poster_identity(
            pool=pool,
            client=client,
            tmdb_key="key",
            imdb_id="tt1234567",
            tmdb_id="definitely-not-tmdb",
            media_type="banana",
        )

        self.assertEqual(result.tmdb_id, "42")
        self.assertEqual(result.media_type, "movie")

    async def test_missing_identity_is_bad_request(self):
        with self.assertRaises(IdentityResolutionError) as ctx:
            await resolve_poster_identity(
                pool=None,
                client=_FakeClient([]),
                tmdb_key="key",
                imdb_id="{imdb_id}",
                tmdb_id="{tmdb_id}",
                media_type="{type}",
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_unresolved_imdb_is_not_found(self):
        pool = _FakePool([None, None])
        client = _FakeClient([_FakeResponse({"movie_results": [], "tv_results": []})])

        with self.assertRaises(IdentityResolutionError) as ctx:
            await resolve_poster_identity(
                pool=pool,
                client=client,
                tmdb_key="key",
                imdb_id="tt1234567",
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
