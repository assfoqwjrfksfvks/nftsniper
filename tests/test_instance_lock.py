import tempfile
import unittest
from pathlib import Path

from instance_lock import InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_blocked_then_release_allows_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.lock"
            with InstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(path):
                        self.fail("Second instance acquired an active lock")
            self.assertTrue(path.exists())
            with InstanceLock(path):
                self.assertTrue(path.exists())

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.lock"
            with self.assertRaises(ValueError):
                with InstanceLock(path):
                    raise ValueError("test")
            with InstanceLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
