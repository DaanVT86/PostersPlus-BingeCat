"""Streaming Top 10: FlixPatrol's per-country charts (fp-crawler's copies)
as trending sources, "fp_<service>_<country>"."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import cache
import config
import main
import tmdb
import top10_charts
import trending_lists
from tests.test_trending_lists import _Menu
from tests.test_trending_snapshot_turnover import _TempDb

INDEX = {
    "united-states": {"hulu": {"movies": [], "series": []}, "shudder": {"overall": []},
                      "netflix": {"movies": [], "series": []}, "crunchyroll": {"overall": []}},
    "united-kingdom": {"netflix": {"movies": [], "series": []}},
}
US = {"charts": [
    {"catalog_id": "hulu.movies", "entries": [
        {"rank": 2, "title": "B", "tmdb": {"id": 20, "media_type": "movie", "release_date": "2025-01-01"}},
        {"rank": 1, "title": "A", "tmdb": {"id": 10, "media_type": "movie"}},
        {"rank": 3, "title": "No id"}]},
    {"catalog_id": "shudder.overall", "entries": [
        {"rank": 1, "title": "Show", "tmdb": {"id": 7, "media_type": "tv"}},
        {"rank": 2, "title": "Film", "tmdb": {"id": 8, "media_type": "movie"}}]},
    {"catalog_id": "crunchyroll.overall", "entries": [
        {"rank": 1, "title": "Anime", "tmdb": {"id": 9, "media_type": "tv"}}]},
]}


class _Top10(_TempDb):
    def setUp(self):
        super().setUp()
        _Menu(self, TRENDING_LIST_SOURCES="top10,flixpatrol")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for name, value in (("_store_path", lambda: os.path.join(self._tmp.name, "index.json")),):
            patcher = mock.patch.object(top10_charts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._reset()
        self.addCleanup(self._reset)
        top10_charts._set_availability(INDEX)

    @staticmethod
    def _reset():
        top10_charts._availability = None
        top10_charts._availability_at = top10_charts._stored_mtime = top10_charts._stored_checked = 0.0
        top10_charts._charts.clear()
        top10_charts._failed_at.clear()


class IdTests(_Top10):
    def test_ids_name_a_service_and_a_country(self):
        self.assertEqual(top10_charts.parse_id("fp_hulu_us"), ("hulu", "united-states"))
        self.assertEqual(top10_charts.parse_id("fp_sony-pictures-core_tt"), ("sony-pictures-core", "trinidad-and-tobago"))
        self.assertIsNone(top10_charts.parse_id("fp_hulu_xx"))
        self.assertIsNone(top10_charts.parse_id("hulu"))
        self.assertEqual(top10_charts.name("fp_hulu_us"), "Hulu Top 10 (US)")
        self.assertEqual(top10_charts.name("fp_netflix_ww"), "Netflix Top 10 (Worldwide)")

    def test_offered_where_the_index_has_the_chart(self):
        offered = top10_charts.offered
        self.assertTrue(offered("movie", "fp_hulu_us"))
        self.assertTrue(offered("tv", "fp_shudder_us"))  # its overall chart
        self.assertFalse(offered("movie", "fp_hulu_gb"))
        # Crunchyroll for the anime lists, and nothing else for them.
        self.assertTrue(offered("anime", "fp_crunchyroll_us"))
        self.assertFalse(offered("tv", "fp_crunchyroll_us"))
        self.assertFalse(offered("anime", "fp_hulu_us"))
        with mock.patch.object(config, "TRENDING_LIST_SOURCES", "top10"):
            self.assertFalse(offered("movie", "fp_hulu_us"))

    def test_the_picker_leaves_out_the_built_in_us_top_10s(self):
        caps = top10_charts.caps()
        self.assertEqual(sorted(caps["regions"]["us"]["services"]), ["crunchyroll", "hulu", "shudder"])
        self.assertEqual(caps["regions"]["us"]["services"]["shudder"], ["movie", "tv"])
        self.assertIn("netflix", caps["regions"]["gb"]["services"])
        with mock.patch.object(config, "TRENDING_LIST_SOURCES", "flixpatrol"):
            self.assertIn("netflix", top10_charts.caps()["regions"]["us"]["services"])


class ListTests(_Top10):
    def test_a_pick_is_a_source_like_any_other(self):
        lists = trending_lists.parse("m:fp_hulu_us.tmdb.fp_hulu_gb,t:fp_shudder_us")
        self.assertEqual(lists.catalogs("movie"), ("fp_hulu_us", "tmdb"))
        self.assertEqual(lists.url("tv"), "top10:shudder:united-states")
        key = lists.key("movie")
        self.assertEqual(trending_lists.from_key(key)[1].key("movie"), key)
        self.assertEqual(main._resolve_trending_catalog("pp.trending.movie.fp_hulu_us", "movie"),
                         ("movie", "fp_hulu_us"))
        self.assertIsNone(main._resolve_trending_catalog("pp.trending.movie.fp_hulu_gb", "movie"))
        names = [n for *_x, n in main._trending_catalog_specs(lists)]
        self.assertIn("Trending Movies · Hulu Top 10 (US)", names)

    def test_a_pick_made_before_the_index_is_read_counts_once_it_is(self):
        self._reset()
        top10_charts._stored_checked = time.monotonic()  # no stored copy to read
        self.assertEqual(trending_lists.parse("m:fp_hulu_us").token(), "")
        top10_charts._set_availability(INDEX)
        self.assertEqual(trending_lists.parse("m:fp_hulu_us").token(), "m:fp_hulu_us")


class FetchTests(_Top10):
    def _client(self, payloads):
        calls = []

        class _Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                if isinstance(self.data, Exception):
                    raise self.data

            def json(self):
                return self.data

        async def _get(url, timeout=None):
            calls.append(url)
            return _Resp(payloads[url.rsplit("/", 1)[1]])

        return calls, mock.Mock(get=_get)

    def test_a_chart_in_rank_order_of_its_lists_kind(self):
        calls, client = self._client({"united-states.json": US})
        details = {}
        ids = asyncio.run(top10_charts.fetch_ids(client, "movie", "top10:hulu:united-states", details))
        self.assertEqual(ids, ["10", "20"])
        self.assertEqual(details["20"], {"name": "B", "year": "2025"})
        # An overall chart split by kind; one download for the country.
        self.assertEqual(asyncio.run(top10_charts.fetch_ids(client, "tv", "top10:shudder:united-states")), ["7"])
        self.assertEqual(asyncio.run(top10_charts.fetch_ids(client, "movie", "top10:shudder:united-states")), ["8"])
        self.assertEqual(asyncio.run(top10_charts.fetch_ids(client, "anime", "top10:crunchyroll:united-states")), ["9"])
        self.assertEqual(len(calls), 1)

    def test_an_unreadable_chart_serves_its_last_list(self):
        lists = trending_lists.parse("m:fp_hulu_us")
        key = lists.key("movie")
        self._store(key, {"10": 1, "20": 2}, time.time() - 3 * 86400,
                    sig=tmdb.trending_source_signature("movie", lists))
        _calls, client = self._client({"united-states.json": RuntimeError("gone")})
        entry = asyncio.run(tmdb.ensure_trending_snapshot(client, "k", "movie", lists, note_request=False))
        self.assertEqual(entry[0], {"10": 1, "20": 2})
        self.assertGreater(entry[1], time.time())

    def test_the_index_keeps_its_last_good_copy(self):
        _calls, client = self._client({"availability.json": {"regions": INDEX}})
        asyncio.run(top10_charts.refresh_availability(client, force=True))
        with open(top10_charts._store_path()) as fh:
            self.assertEqual(json.load(fh), INDEX)
        _calls, broken = self._client({"availability.json": RuntimeError("404")})
        asyncio.run(top10_charts.refresh_availability(broken, force=True))
        self.assertTrue(top10_charts.offered("movie", "fp_hulu_us"))
        # Another worker reads the stored copy.
        self._reset()
        self.assertTrue(top10_charts.offered("movie", "fp_hulu_us"))
