import json
import sqlite3
import time
import unittest

import cache


class RatingCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_initialised = cache._initialised
        self.previous_conn = getattr(cache._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE rating_cache (
                imdb_id TEXT PRIMARY KEY,
                ratings_json TEXT,
                genre TEXT,
                cached_at INTEGER,
                release_date TEXT,
                award_wins TEXT,
                award_noms TEXT,
                awards_fetched INTEGER,
                festival_label TEXT,
                age_rating INTEGER,
                is_cult INTEGER,
                is_true_story INTEGER,
                is_metacritic INTEGER,
                rating_min_votes INTEGER
            )
            """
        )
        cache._initialised = True
        cache._local.conn = self.conn

    def tearDown(self):
        self.conn.close()
        cache._initialised = self.previous_initialised
        if self.previous_conn is None:
            try:
                del cache._local.conn
            except AttributeError:
                pass
        else:
            cache._local.conn = self.previous_conn

    def _insert(self, imdb_id, policy, *, cached_at=None, release_date="2020-01-01"):
        self.conn.execute(
            """
            INSERT INTO rating_cache VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                imdb_id,
                json.dumps({"imdb": 75}),
                "Drama",
                int(time.time()) if cached_at is None else cached_at,
                release_date,
                "",
                "",
                1,
                None,
                15,
                0,
                0,
                0,
                policy,
            ),
        )
        self.conn.commit()

    def test_legacy_policy_row_is_reused_and_backfilled(self):
        self._insert("tt0000001", None)

        result = cache.get_cached_rating("tt0000001")

        self.assertIsNotNone(result)
        stored_policy = self.conn.execute(
            "SELECT rating_min_votes FROM rating_cache WHERE imdb_id = ?",
            ("tt0000001",),
        ).fetchone()[0]
        self.assertEqual(stored_policy, cache.RATING_MIN_VOTES)

    def test_explicit_policy_change_still_invalidates(self):
        self._insert("tt0000002", cache.RATING_MIN_VOTES + 1)

        result = cache.get_cached_rating("tt0000002")

        self.assertIsNone(result)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM rating_cache WHERE imdb_id = ?",
            ("tt0000002",),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_old_release_rating_cache_is_reused_for_29_days(self):
        twenty_nine_days_ago = int(time.time() - 29 * 86400)
        self._insert(
            "tt0000003",
            cache.RATING_MIN_VOTES,
            cached_at=twenty_nine_days_ago,
            release_date="2020-01-01",
        )

        result = cache.get_cached_rating("tt0000003")

        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
