import unittest
from unittest import mock

import httpx

import main
import quality


class QualityBackoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = main._cfg.QUALITY_SOURCE
        main._quality_source_backoff_until.clear()
        main._quality_source_fail_count.clear()
        main._quality_title_failed.clear()

    def tearDown(self):
        main._quality_title_failed.clear()
        main._cfg.QUALITY_SOURCE = self.source
        main._quality_source_backoff_until.clear()
        main._quality_source_fail_count.clear()

    async def test_failure_creates_short_source_cooldown(self):
        main._cfg.QUALITY_SOURCE = "aiostreams"
        main._record_quality_result(main.FETCH_FAILED)
        self.assertGreater(main._quality_backoff_remaining(), 0)
        self.assertEqual(main._quality_source_fail_count["aiostreams"], 1)

    async def test_concurrent_failures_do_not_skip_escalation_steps(self):
        main._cfg.QUALITY_SOURCE = "aiostreams"
        main._record_quality_result(main.FETCH_FAILED)
        main._record_quality_result(main.FETCH_FAILED)
        self.assertEqual(main._quality_source_fail_count["aiostreams"], 1)

    async def test_success_clears_source_cooldown(self):
        main._cfg.QUALITY_SOURCE = "scraper"
        main._record_quality_result(main.FETCH_FAILED)
        main._record_quality_result([])
        self.assertEqual(main._quality_backoff_remaining(), 0)
        self.assertNotIn("scraper", main._quality_source_fail_count)


    async def test_one_titles_failure_backs_off_only_that_title(self):
        main._cfg.QUALITY_SOURCE = "scraper"
        main._record_quality_result(quality.TITLE_FAILED, "kitsu:1")
        self.assertEqual(main._quality_backoff_remaining(), 0)
        self.assertTrue(main._quality_title_cooling("kitsu:1"))
        self.assertFalse(main._quality_title_cooling("tt1"))

    async def test_a_title_failure_is_not_retried_straight_away(self):
        calls = []

        async def _fetch():
            calls.append(1)
            return quality.TITLE_FAILED

        self.assertIs(await main._with_retry(_fetch), quality.TITLE_FAILED)
        self.assertEqual(len(calls), 1)


class QualityFailureKindTests(unittest.IsolatedAsyncioTestCase):
    """Which answers say the source is in trouble, and which only the title."""

    async def _scraper(self, status):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status)))
        async with client:
            return await quality.fetch_quality_from_scraper(client, "https://addon.example", "tt1", "movie")

    async def test_a_4xx_for_one_id_is_a_title_failure(self):
        for status in (400, 404, 410, 422):
            self.assertIs(await self._scraper(status), quality.TITLE_FAILED, status)

    async def test_auth_throttling_and_5xx_still_fail_the_source(self):
        for status in (401, 403, 429, 500, 502, 503):
            self.assertIs(await self._scraper(status), main.FETCH_FAILED, status)

    async def test_aiostreams_no_results_with_scraper_errors_is_a_title_failure(self):
        body = {"success": True, "data": {"results": [], "errors": {"x": "boom"}}}
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
        with mock.patch.object(quality, "AIOSTREAMS_URL", "https://aio.example"), \
             mock.patch.object(quality, "AIOSTREAMS_AUTH", "x"):
            async with client:
                result = await quality.fetch_quality_from_aiostreams(client, "tt1", "movie")
        self.assertIs(result, quality.TITLE_FAILED)


if __name__ == "__main__":
    unittest.main()
