"""A deferred OCR scan waits for an idle worker, but not forever: a worker
that always has a render in flight would otherwise never run it, and the
renders waiting on it would stay provisional."""
import asyncio
import unittest
from unittest import mock

import main


class IdleWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_gives_up_waiting_at_the_deadline(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(main, "_active_poster_renders", 1):
            start = loop.time()
            await main._wait_for_idle_detection(start + 0.3)
            self.assertGreaterEqual(loop.time() - start, 0.25)
            self.assertLess(loop.time() - start, 2.0)

    async def test_an_idle_worker_does_not_wait(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(main, "_active_poster_renders", 0), \
             mock.patch.object(main, "_foreground_detection_count", 0):
            start = loop.time()
            await main._wait_for_idle_detection(start + 30)
            self.assertLess(loop.time() - start, 0.05)


if __name__ == "__main__":
    unittest.main()
