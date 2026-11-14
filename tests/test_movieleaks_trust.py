"""An r/movieleaks post may not outrank a digital date TMDB schedules far off.

The digital-release cache takes every IMDb id in every r/movieleaks post at
face value. The Odyssey (2026) picked one up from a knock-off posted under
Nolan's IMDb id in August, while TMDB listed its digital release for November,
and the override turned a film still in cinemas "Streaming".
"""
import re
import unittest
from pathlib import Path


class MovieleaksTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Path("main.py").read_text(encoding="utf-8")

    def _helper(self) -> str:
        m = re.search(r"def _leak_confirmed\(\) -> bool:(.*?)\n\n", self.src, re.S)
        self.assertIsNotNone(m, "_leak_confirmed() is gone")
        return m.group(1)

    def test_a_distant_tmdb_digital_date_outranks_the_leak(self):
        body = self._helper()
        self.assertIn('get_cached_movie_release_info(f"movie_{tmdb_id}")', body)
        self.assertIn('.get("digital_date")', body)
        self.assertIn("(_scheduled_digital - datetime.now().date()).days <= _LEAK_LEAD_DAYS", body)

    def test_a_leak_may_still_beat_the_published_date_by_days(self):
        # Catching releases ahead of TMDB's date is what the feed is for, so
        # the window must stay short but not vanish.
        import main
        self.assertTrue(7 <= main._LEAK_LEAD_DAYS <= 30)

    def test_both_uses_of_the_leak_go_through_the_check(self):
        # The status override and the "New" sash's digital flag must agree.
        self.assertIn('if _release_status in ("Cinema", "Production") and _leak_confirmed():', self.src)
        self.assertIn("is_digital_release_override=_leak_confirmed(),", self.src)
        self.assertEqual(self.src.count("is_digital_release(effective_imdb_id)"), 1)

    def test_it_is_read_after_the_release_row_is_fetched(self):
        # Called before fetch_release_status, a title's first render would find
        # no cached row and trust the leak.
        override = self.src.index('and _leak_confirmed():')
        fetch = self.src.index("_release_status = await fetch_release_status(")
        self.assertLess(fetch, override)


if __name__ == "__main__":
    unittest.main()
