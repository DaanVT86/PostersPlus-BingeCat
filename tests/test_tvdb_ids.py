"""Requests carrying only a tvdb: id, TVDB as the last metadata spine, and the
blank-image guard on TMDB downloads.

- ``tvdb:<id>`` stremio ids parse, and nothing else passes for one.
- A series TVDB id maps to TMDB through /find; a movie one never does (TMDB
  holds no TVDB movie ids, and TVDB numbers movies and series apart, so the
  same number would name some series).
- With no TMDB record the id itself carries the title to TVDB, or the request
  is refused when there is no TVDB key.
- A TVDB record comes out shaped like the TMDB metadata tuple.
- A flat single-colour image is never returned (or cached) as art.
"""

import asyncio
import io
import unittest
from unittest import mock

from fastapi import HTTPException
from PIL import Image

import main
import tmdb
import tvdb
from tests.test_either_id import _FakeClient, _FakeResponse, _MemoryJsonCache


def _run(coro):
    return asyncio.run(coro)


def _find_payload(tv=(), movie=()):
    return {"tv_results": [{"id": i} for i in tv], "movie_results": [{"id": i} for i in movie]}


class ParseTvdbStremioIdTests(unittest.TestCase):
    def test_accepts_tvdb_ids(self):
        self.assertEqual(main._parse_tvdb_stremio_id("tvdb:81189"), 81189)
        self.assertEqual(main._parse_tvdb_stremio_id(" TVDB:81189 "), 81189)
        self.assertEqual(main._parse_tvdb_stremio_id("tvdb:81189:2:3"), 81189)

    def test_rejects_everything_else(self):
        for raw in ("", None, "tt0903747", "tmdb:1396", "tvdb:", "tvdb:abc",
                    "tvdb:0", "tvdb:-5", "tvdb:12345678901", "tvdb:١٢٣"):
            with self.subTest(raw=raw):
                self.assertIsNone(main._parse_tvdb_stremio_id(raw))


class ResolveTvdbToTmdbTests(unittest.TestCase):
    def test_series_maps_through_find_and_is_cached(self):
        client = _FakeClient(_FakeResponse(200, _find_payload(tv=[1396])))
        with _MemoryJsonCache(tmdb) as cache:
            first = _run(tmdb.resolve_tvdb_to_tmdb(client, 81189, "series", "k"))
            second = _run(tmdb.resolve_tvdb_to_tmdb(client, 81189, "tv", "k"))
        self.assertEqual(first, {"tmdb_id": "1396", "media_type": "tv"})
        self.assertEqual(second, first)
        self.assertEqual(len(client.calls), 1)
        url, params = client.calls[0]
        self.assertTrue(url.endswith("/find/81189"))
        self.assertEqual(params["external_source"], "tvdb_id")
        self.assertEqual(list(cache.store), ["idmap:v1:tvdb:series:81189"])

    def test_movie_never_asks_find(self):
        # /find would answer 81189 with Breaking Bad whatever the TVDB movie is.
        client = _FakeClient(_FakeResponse(200, _find_payload(tv=[1396])))
        with _MemoryJsonCache(tmdb) as cache:
            self.assertIsNone(_run(tmdb.resolve_tvdb_to_tmdb(client, 81189, "movie", "k")))
        self.assertEqual(client.calls, [])
        self.assertEqual(cache.store, {})

    def test_series_ignores_a_movie_only_answer(self):
        client = _FakeClient(_FakeResponse(200, _find_payload(movie=[550])))
        with _MemoryJsonCache(tmdb) as cache:
            self.assertIsNone(_run(tmdb.resolve_tvdb_to_tmdb(client, 5, "series", "k")))
        self.assertTrue(cache.store["idmap:v1:tvdb:series:5"].get("__miss__"))

    def test_failed_lookup_raises_and_is_not_cached(self):
        client = _FakeClient(_FakeResponse(503))
        with _MemoryJsonCache(tmdb) as cache:
            with self.assertRaises(tmdb.IdResolveError):
                _run(tmdb.resolve_tvdb_to_tmdb(client, 81189, "series", "k"))
        self.assertEqual(cache.store, {})


class ResolveTvdbIdentityTests(unittest.TestCase):
    def setUp(self):
        self._client = main._HTTP_CLIENT
        main._HTTP_CLIENT = object()

    def tearDown(self):
        main._HTTP_CLIENT = self._client

    def _resolve(self, found, tvdb_on, media_type="series"):
        async def fake_resolve(client, tvdb_id, media_type, key):
            return found
        with mock.patch.object(main, "resolve_tvdb_to_tmdb", fake_resolve), \
                mock.patch.object(tvdb, "tvdb_enabled", lambda: tvdb_on):
            return _run(main._resolve_tvdb_identity(81189, media_type, "k"))

    def test_tmdb_record_takes_the_title(self):
        found = {"tmdb_id": "1396", "media_type": "tv"}
        self.assertEqual(self._resolve(found, tvdb_on=True), ("1396", "tv", False))

    def test_no_tmdb_record_renders_from_tvdb(self):
        self.assertEqual(self._resolve(None, tvdb_on=True, media_type="movie"),
                         ("tvdb:81189", "movie", True))

    def test_no_tmdb_record_and_no_tvdb_key_is_a_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self._resolve(None, tvdb_on=False)
        self.assertEqual(ctx.exception.status_code, 404)


RECORD = {
    "id": 424242, "name": "Petite Série", "image": "/banners/posters/424242.jpg",
    "year": "2019", "firstAired": "2019-03-04", "originalLanguage": "fra",
    "genres": [{"slug": "science-fiction", "name": "Science Fiction"},
               {"slug": "documentary", "name": "Documentary"}],
    "remoteIds": [{"sourceName": "IMDB", "id": "tt9999999"},
                  {"sourceName": "TheMovieDB.com", "id": "777"}],
    "status": {"name": "Ended"}, "averageRuntime": 22,
    "translations": {"nameTranslations": [{"language": "eng", "name": "Little Series"}]},
}

ARTWORKS = {"logos": [], "posters": [], "backgrounds": [
    {"url": "https://artworks.thetvdb.com/bg-fra.jpg", "language": "fra", "score": 9},
    {"url": "https://artworks.thetvdb.com/bg-null.jpg", "language": None, "score": 5},
]}


class FetchTvdbMetadataTests(unittest.TestCase):
    def _fetch(self, record=RECORD, language="en"):
        async def resolve(client, **kw):
            return int(kw["tvdb_id_hint"])

        async def fetch_record(client, tvdb_id, want):
            return record

        async def artworks(client, tvdb_id, media_type):
            return ARTWORKS

        with mock.patch.object(tvdb, "tvdb_enabled", lambda: True), \
                mock.patch.object(tvdb, "resolve_tvdb_id", resolve), \
                mock.patch.object(tvdb, "_fetch_record", fetch_record), \
                mock.patch.object(tvdb, "fetch_tvdb_artworks", artworks):
            return _run(tvdb.fetch_tvdb_metadata(
                None, media_type="series", tvdb_id_hint=424242, language=language))

    def test_shape_matches_the_tmdb_metadata_tuple(self):
        result = self._fetch()
        self.assertEqual(len(result), 8)
        genre_ids, is_textless, logos, year, title, poster, backdrop, td = result
        self.assertEqual(title, "Little Series")
        self.assertEqual(year, "2019")
        self.assertIn(878, genre_ids)   # science-fiction -> Sci-Fi
        self.assertIn(99, genre_ids)
        self.assertFalse(is_textless)
        self.assertEqual(logos, [])
        self.assertEqual(poster, "https://artworks.thetvdb.com/banners/posters/424242.jpg")
        self.assertEqual(backdrop, "https://artworks.thetvdb.com/bg-null.jpg")
        self.assertEqual(td["tvdb_id"], 424242)
        self.assertEqual(td["cinemeta_source"], "tvdb")
        self.assertEqual(td["original_title"], "Petite Série")
        self.assertEqual(td["original_language"], "fr")
        self.assertEqual(td["tmdb_release_date"], "2019-03-04")
        self.assertEqual(td["tmdb_status"], "Ended")
        self.assertEqual(td["runtime"], 22)
        self.assertEqual(td["imdb_id"], "tt9999999")
        self.assertEqual(td["cinemeta_tmdb_id"], "777")
        self.assertEqual(td["original_poster_path"], poster)

    def test_title_falls_back_to_the_original_name(self):
        record = dict(RECORD, translations={})
        self.assertEqual(self._fetch(record)[4], "Petite Série")

    def test_off_without_a_key(self):
        with mock.patch.object(tvdb, "tvdb_enabled", lambda: False):
            self.assertIsNone(_run(tvdb.fetch_tvdb_metadata(
                None, media_type="series", tvdb_id_hint=1)))


def _jpeg(colour=None) -> bytes:
    image = Image.new("RGB", (300, 450), colour or (0, 0, 0))
    if colour is None:
        for x in range(0, 300, 10):
            image.putpixel((x, 200), (255, 255, 255))
        image = image.resize((300, 450))
        image.paste((200, 30, 30), (0, 0, 150, 450))
    buf = io.BytesIO()
    image.save(buf, "JPEG")
    return buf.getvalue()


class _ArtResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _ArtClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        return _ArtResponse(self.bodies[len(self.urls) - 1])


class BlankArtTests(unittest.TestCase):
    def test_flat_image_is_blank(self):
        self.assertTrue(tmdb._is_blank_art(_jpeg((0, 0, 0))))
        self.assertTrue(tmdb._is_blank_art(_jpeg((40, 40, 40))))
        self.assertFalse(tmdb._is_blank_art(_jpeg()))
        self.assertFalse(tmdb._is_blank_art(b"not an image"))

    def test_blank_rendition_is_retried_at_another_size(self):
        client = _ArtClient([_jpeg((0, 0, 0)), _jpeg()])
        content = _run(tmdb._get_art(client, "https://image.tmdb.org/t/p/w780/abc.jpg"))
        self.assertEqual(content, client.bodies[1])
        self.assertEqual(client.urls, ["https://image.tmdb.org/t/p/w780/abc.jpg",
                                       "https://image.tmdb.org/t/p/original/abc.jpg"])

    def test_all_blank_raises(self):
        client = _ArtClient([_jpeg((0, 0, 0))] * 3)
        with self.assertRaises(tmdb.BlankArtError):
            _run(tmdb._get_art(client, "https://image.tmdb.org/t/p/w500/abc.jpg"))
        self.assertEqual(len(client.urls), 3)

    def test_other_hosts_are_fetched_once(self):
        client = _ArtClient([_jpeg((0, 0, 0))])
        with self.assertRaises(tmdb.BlankArtError):
            _run(tmdb._get_art(client, "https://artworks.thetvdb.com/x.jpg"))
        self.assertEqual(len(client.urls), 1)


if __name__ == "__main__":
    unittest.main()
