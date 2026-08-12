import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pauk.storage.atomic import GroupLock, _pid_alive


class PidAliveTest(unittest.TestCase):
    def test_running_process_is_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))

    def test_exited_process_is_not_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(_pid_alive(proc.pid))


class GroupLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.lock_path = self.data_dir / ".locks" / "g.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_lock_of_dead_process_is_broken(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text(f"pid={proc.pid}\n", encoding="utf-8")
        with GroupLock(self.data_dir, "g", timeout=5.0):
            self.assertEqual(
                self.lock_path.read_text(encoding="utf-8").strip(),
                f"pid={os.getpid()}",
            )
        self.assertFalse(self.lock_path.exists())

    def test_lock_of_live_process_times_out(self):
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
        with self.assertRaises(TimeoutError):
            GroupLock(self.data_dir, "g", timeout=0.3).__enter__()
