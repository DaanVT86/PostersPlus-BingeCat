"""AIOMetadata's Up Next, Resume and Unwatched rows reach /poster as synthetic
meta ids that wrap the show's own id ("mdblist_upnext_tmdb:2190_S1E1",
"upnext_tt0121955_trakt123", "unwatched_tt0121955"). Nuvio's {id} is all a
client sends for them, so /poster unwraps the id and renders the show.

- Every row format AIOMetadata emits unwraps to the inner id.
- Any other id we don't recognise ("had:episode:tt13491734:6:6") gives up the
  one IMDb id that is a whole segment of it. No tt… segment, two different
  ones, or a namespace with its own parser (kitsu:…:1:1) leaves it as it was.
- The handler takes the unwrapped id as if a plain catalogue row had sent it:
  a tt… id as imdb_id, tmdb:… as tmdb_id, tvdb:… through the TVDB route.
"""

import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


class UnwrapStremioIdTests(unittest.TestCase):
    def test_row_ids_unwrap_to_the_show(self):
        cases = {
            "mdblist_upnext_tmdb:2190_S1E1": "tmdb:2190",
            "mdblist_upnext_tt0121955_S12E3": "tt0121955",
            "upnext_tt0121955_trakt123456": "tt0121955",
            "upnext_tvdb:75897_S01E01": "tvdb:75897",
            "upnext_tmdb:2190_unknown": "tmdb:2190",
            "simkl_upnext_tt0121955_S1E1": "tt0121955",
            "simkl_upnext_kitsu:7442_S1E1": "kitsu:7442",
            "pmdb_resume_tmdb:2190_S1E1": "tmdb:2190",
            "pmdb_resume_tmdb:550_": "tmdb:550",
            "unwatched_tt0121955": "tt0121955",
            " unwatched_tmdb:2190 ": "tmdb:2190",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(main._unwrap_stremio_id(raw), expected)

    def test_everything_else_is_left_alone(self):
        for raw in ("", "tt0121955", "tmdb:2190", "kitsu:7442", "trakt:2104",
                    "upnext_trakt:2104_S1E1", "upnext__S1E1", "aiom.error.x",
                    # embedded ids: none, two different, or only part of a segment
                    "had:episode:123:6:6", "foo:tt0121955:tt0903747",
                    "xtt0121955:1", "foo-tt0121955abc", "had:tt12345678901",
                    # namespaces with their own parser keep their suffixes
                    "kitsu:7442:1:1", "anilist:21:tt0121955", "mal:1535:tt0121955",
                    "tmdb:2190:tt0121955", "tvdb:75897:1:1"):
            with self.subTest(raw=raw):
                self.assertEqual(main._unwrap_stremio_id(raw), raw)
        self.assertIsNone(main._unwrap_stremio_id(None))

    def test_unknown_ids_give_up_their_imdb_id(self):
        cases = {
            "had:episode:tt13491734:6:6": "tt13491734",
            "tt0121955:1:2": "tt0121955",
            "foo/tt0121955": "tt0121955",
            "foo:tt0121955:tt0121955": "tt0121955",
            # AIOMetadata-shaped ids that miss its exact row format
            "upnext_tt0121955": "tt0121955",
            "unwatched_tt0121955_S1E1": "tt0121955",
            "upnext_tt0121955_S1E1_extra": "tt0121955",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(main._unwrap_stremio_id(raw), expected)


class _Captured(Exception):
    pass


class PosterIdentityTests(unittest.TestCase):
    """Drive /poster up to identity resolution and record what reaches it."""

    def setUp(self):
        self._saved_key = main._cfg.ACCESS_KEY
        main._cfg.ACCESS_KEY = ""
        self._settle = main._settle_title_identity
        self._tvdb = main._resolve_tvdb_identity
        self.seen = []

        async def _settle(tmdb_id, imdb_id, media_type, tmdb_key):
            self.seen.append(("settle", tmdb_id, imdb_id, media_type))
            raise HTTPException(status_code=418, detail="captured")

        async def _tvdb(tvdb_id, media_type, tmdb_key):
            self.seen.append(("tvdb", tvdb_id, media_type))
            raise HTTPException(status_code=418, detail="captured")

        main._settle_title_identity = _settle
        main._resolve_tvdb_identity = _tvdb
        self.client = TestClient(main.app)

    def tearDown(self):
        main._cfg.ACCESS_KEY = self._saved_key
        main._settle_title_identity = self._settle
        main._resolve_tvdb_identity = self._tvdb

    def _poster(self, stremio_id, **params):
        resp = self.client.get("/poster", params={
            "stremio_id": stremio_id, "type": "series", "shape": "landscape", **params})
        self.assertEqual(resp.status_code, 418, resp.text)
        return self.seen[-1]

    def test_wrapped_imdb_id_is_the_imdb_id(self):
        self.assertEqual(self._poster("upnext_tt0121955_trakt123"),
                         ("settle", "", "tt0121955", "series"))

    def test_wrapped_tmdb_id_is_the_tmdb_id(self):
        self.assertEqual(self._poster("mdblist_upnext_tmdb:2190_S1E1"),
                         ("settle", "2190", "", "series"))

    def test_wrapped_tvdb_id_takes_the_tvdb_route(self):
        self.assertEqual(self._poster("upnext_tvdb:75897_S1E1"), ("tvdb", 75897, "series"))

    def test_plain_tmdb_stremio_id_fills_a_missing_tmdb_id(self):
        self.assertEqual(self._poster("tmdb:2190"), ("settle", "2190", "", "series"))

    def test_ids_the_client_sent_win(self):
        self.assertEqual(self._poster("mdblist_upnext_tmdb:2190_S1E1", tmdb_id="1"),
                         ("settle", "1", "", "series"))
        self.assertEqual(self._poster("upnext_tt0121955_S1E1", imdb_id="tt1"),
                         ("settle", "", "tt1", "series"))

    def test_unknown_scheme_with_an_imdb_id_renders_it(self):
        self.assertEqual(self._poster("had:episode:tt13491734:6:6"),
                         ("settle", "", "tt13491734", "series"))

    def test_unknown_wrapper_still_needs_an_id(self):
        resp = self.client.get("/poster", params={"stremio_id": "upnext_trakt:1_S1E1", "type": "series"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Missing required parameter", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
