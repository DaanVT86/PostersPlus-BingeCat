"""The shared rating row holds MDBList's answer only.  A request's own
extras — the anime provider's score and age rating, the IMDb dataset, TMDB's
own average — are merged per request and must not be written back, or a
title's scores depend on which request fetched it first."""
from pathlib import Path
import re
import unittest

from main import _mdblist_row_ratings


class RowRatingsTests(unittest.TestCase):
    def test_anime_provider_scores_are_dropped(self):
        row = {"imdb": 7.5, "myanimelist": 7.2, "kitsu": 73.6, "anilist": 70}
        self.assertEqual(_mdblist_row_ratings(row), {"imdb": 7.5, "myanimelist": 7.2})

    def test_non_dicts_pass_through(self):
        self.assertIsNone(_mdblist_row_ratings(None))
        self.assertEqual(_mdblist_row_ratings("N/A"), "N/A")


class WriteBackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Path("main.py").read_text(encoding="utf-8")

    def test_row_is_captured_before_the_per_request_merges(self):
        start = self.src.index("ratings_dict, genre, rel, keywords, age_rating = rating_result")
        capture = self.src.index("_row_ratings, _row_age_rating = ratings_dict, age_rating", start)
        for merge in ('tmdb_data.get("anime_score")', 'tmdb_data.get("anime_age_rating")',
                      "_merge_imdb_dataset_rating(ratings_dict", "_merge_direct_tmdb_rating(ratings_dict"):
            self.assertGreater(self.src.index(merge, start), capture, merge)

    def test_the_request_path_writes_the_captured_row(self):
        start = self.src.index("# Write rating + awards to cache (only on a fresh fetch).")
        call = self.src[start:self.src.index(")", self.src.index("set_cached_rating,", start) + 300)]
        self.assertRegex(call, r"_row_ratings if isinstance\(_row_ratings, dict\)")
        self.assertIn("age_rating=_row_age_rating", call)


if __name__ == "__main__":
    unittest.main()
