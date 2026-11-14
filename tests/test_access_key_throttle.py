"""A short ACCESS_KEY can be guessed online, so a run of wrong guesses from
one address locks it out; a long one isn't throttled (a stale key in many
clients behind one proxy address would otherwise lock everyone out)."""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import admin
import main


class AccessKeyThrottleTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.dict(admin._failures, clear=True),
                  mock.patch.dict(admin._lockouts, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def _probe(self, key, n):
        with mock.patch.object(main._cfg, "ACCESS_KEY", key), TestClient(main.app) as client:
            return [client.get("/logo", params={"tmdb_id": "1", "access_key": f"guess{i}"}).status_code
                    for i in range(n)]

    def test_a_short_key_locks_out_a_guessing_address(self):
        codes = self._probe("short", admin._FAIL_LIMIT + 1)
        self.assertEqual(codes[:admin._FAIL_LIMIT], [403] * admin._FAIL_LIMIT)
        self.assertEqual(codes[-1], 429)

    def test_a_long_key_is_not_throttled(self):
        codes = self._probe("a-long-enough-access-key", admin._FAIL_LIMIT + 1)
        self.assertEqual(set(codes), {403})


if __name__ == "__main__":
    unittest.main()
