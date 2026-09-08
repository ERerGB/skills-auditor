"""Locks coordinate exact target entries across processes and local stores."""

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.locking import locked_paths


class TestLifecycleLocking(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _holder(self, target):
        script = "from pathlib import Path; from skills_auditor.lifecycle.locking import locked_paths; import sys;\nwith locked_paths([Path(sys.argv[1])]):\n print('locked',flush=True)\n sys.stdin.read()"
        process = subprocess.Popen([sys.executable, "-c", script, str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, process)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        return process

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_sorted_deduplicated_persistent_locks_and_distinct_leaf(self):
        first, second = self.root / "a", self.root / "b"
        with locked_paths([second, first, first]) as paths:
            self.assertEqual(paths, (first, second))
            with locked_paths([self.root / "unrelated"]):
                pass
            self.assertFalse(first.exists())
        files = sorted(self.root.iterdir())
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.is_file() for path in files))
        identities = [path.stat().st_ino for path in files]
        with locked_paths([first, second]):
            pass
        self.assertEqual([path.stat().st_ino for path in files], identities)

    def test_real_process_contention_and_release_after_death(self):
        target = self.root / "skill"
        process = self._holder(target)
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([target], timeout=0.03):
                self.fail("contending lock acquired")
        self.assertEqual(caught.exception.code, "lock_contended")
        process.kill()
        process.communicate(timeout=10)
        with locked_paths([target]):
            pass
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_parent_alias_cannot_bypass_lock_and_leaf_symlink_not_followed(self):
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        entry = real / "skill"
        entry.symlink_to("missing")
        process = self._holder(entry)
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([alias / "skill"]):
                pass
        self.assertEqual(caught.exception.code, "lock_contended")
        with locked_paths([real / "missing"]):
            pass
        self.assertTrue(entry.is_symlink())
        process.stdin.close()
        process.stdin = None
        process.communicate(timeout=10)

    def test_partial_acquisition_failure_releases_earlier_lock(self):
        first, second = self.root / "a", self.root / "z"
        process = self._holder(second)
        with self.assertRaises(LifecycleError):
            with locked_paths([first, second]):
                pass
        with locked_paths([first]):
            pass
        process.kill()
        process.communicate(timeout=10)

    def test_case_and_unicode_aliases_conservatively_share_process_lock(self):
        for first, alias in (("Skill", "skill"), ("caf\u00e9", "cafe\u0301")):
            with self.subTest(first=first, alias=alias):
                process = self._holder(self.root / first)
                with self.assertRaises(LifecycleError) as caught:
                    with locked_paths([self.root / alias]):
                        pass
                self.assertEqual(caught.exception.code, "lock_contended")
                process.kill()
                process.communicate(timeout=10)
                with locked_paths([self.root / first, self.root / alias]):
                    pass

    def test_symlink_and_nonregular_lock_rejected_without_touching_referent(self):
        target = self.root / "skill"
        with locked_paths([target]):
            pass
        lock = next(self.root.iterdir())
        lock.unlink()
        referent = self.root / "private"
        referent.write_text("protected", encoding="utf-8")
        lock.symlink_to(referent)
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([target]):
                pass
        self.assertEqual(caught.exception.code, "unsafe_lock")
        self.assertEqual(referent.read_text(), "protected")
        lock.unlink()
        lock.mkdir()
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([target]):
                pass
        self.assertEqual(caught.exception.code, "unsafe_lock")

    def test_missing_parent_and_bad_timeout_do_not_create_target_directories(self):
        parent = self.root / "missing"
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([parent / "skill"]):
                pass
        self.assertEqual(caught.exception.code, "lock_parent_missing")
        self.assertFalse(parent.exists())
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                with locked_paths([self.root / "skill"], timeout=timeout):
                    pass

    def test_unsupported_backend_and_io_failure_fail_closed(self):
        target = self.root / "skill"
        for location, failure, code in (
            ("fcntl", None, "locking_unsupported"),
            ("os.open", OSError(errno.EACCES, "denied"), "lock_io_error"),
            ("fcntl.flock", OSError(errno.EIO, "I/O failure"), "lock_io_error"),
        ):
            with self.subTest(location=location):
                options = {"new": None} if failure is None else {"side_effect": failure}
                with mock.patch("skills_auditor.lifecycle.locking." + location, **options):
                    with self.assertRaises(LifecycleError) as caught:
                        with locked_paths([target]):
                            pass
                self.assertEqual(caught.exception.code, code)
        with locked_paths([target]):
            pass

    def test_hardlinked_lock_is_rejected(self):
        target = self.root / "skill"
        with locked_paths([target]):
            pass
        lock = next(self.root.iterdir())
        alias = self.root / "hardlink"
        os.link(lock, alias)
        with self.assertRaises(LifecycleError) as caught:
            with locked_paths([target]):
                pass
        self.assertEqual(caught.exception.code, "unsafe_lock")
        self.assertEqual(lock.stat().st_ino, alias.stat().st_ino)

    def test_lock_replacement_during_acquisition_is_rejected(self):
        from skills_auditor.lifecycle.locking import fcntl
        target = self.root / "skill"
        with locked_paths([target]):
            pass
        lock = next(self.root.iterdir())
        real_flock = fcntl.flock

        def replaced(fd, operation):
            real_flock(fd, operation)
            lock.rename(self.root / "displaced-lock")
            lock.touch()

        with mock.patch("skills_auditor.lifecycle.locking.fcntl.flock", side_effect=replaced):
            with self.assertRaises(LifecycleError) as caught:
                with locked_paths([target]):
                    pass
        self.assertEqual(caught.exception.code, "unsafe_lock")
        self.assertTrue((self.root / "displaced-lock").exists())
        with locked_paths([target]):
            pass
