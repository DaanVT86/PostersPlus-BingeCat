"""A failed TVDB login isn't retried on every render that wants TVDB art."""
import asyncio
import unittest
from unittest import mock

import httpx

import tvdb


class LoginBackoffTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(tvdb, "_login_failed_at", None),
                  mock.patch.object(tvdb, "_login_rejected", False)):
            p.start()
            self.addCleanup(p.stop)

    def _login(self, status, times):
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(status, json={})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return [await tvdb._login(client) for _ in range(times)]

        return asyncio.run(go()), calls

    def test_a_failure_is_not_retried_straight_away(self):
        results, calls = self._login(503, 3)
        self.assertEqual(results, [None] * 3)
        self.assertEqual(len(calls), 1)

    def test_a_rejected_key_is_not_retried_at_all(self):
        results, calls = self._login(401, 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(tvdb._login_rejected)
        with mock.patch.object(tvdb, "_login_failed_at", -1e9):
            self.assertEqual(self._login(200, 1)[1], [])


if __name__ == "__main__":
    unittest.main()
