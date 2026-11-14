"""With WORKERS>1 the jobs that write shared state run in one worker: the one
holding the background lock.  Another takes over when it goes."""
import os
import tempfile
import unittest
from unittest import mock

import main


class BackgroundLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(main._cfg, "DB_PATH", os.path.join(self.dir.name, "cache.db"))
        p.start()
        self.addCleanup(p.stop)

    def test_one_holder_at_a_time_and_the_next_takes_over(self):
        first = main._try_background_lock()
        self.assertNotIn(first, (None, True))
        # A second worker (flock is per open file, so this stands in for one).
        self.assertIsNone(main._try_background_lock())
        first.close()   # the holder exits
        second = main._try_background_lock()
        self.assertNotIn(second, (None, True))
        second.close()

    def test_no_lock_file_means_every_worker_runs_them_as_before(self):
        with mock.patch.object(main._cfg, "DB_PATH", "/nonexistent-dir/x/cache.db"):
            self.assertIs(main._try_background_lock(), True)


if __name__ == "__main__":
    unittest.main()
