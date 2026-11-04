import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.sources import capture, stage, unpack


class TestWorkspaceSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        (self.source / "skills/alpha").mkdir(parents=True)
        (self.source / "skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: fixture\n---\nRead ../../helper\n")
        (self.source / "helper").write_text("H1")
        self.binding = {"root": str(self.source), "kind": "local", "include": ["skills", "helper"],
                        "entries": {"alpha": "skills/alpha"}, "platforms": ["codex", "cursor", "claude-code"]}

    def test_capture_layout_reproduce_and_deduplicate(self):
        packet = capture(self.binding)
        one = stage(packet, self.root / "cache")
        inode = one.stat().st_ino
        self.assertEqual(stage(packet, self.root / "cache").stat().st_ino, inode)
        self.assertEqual((one / "helper").read_text(), "H1")
        other = stage(packet, self.root / "other-environment/cache")
        self.assertEqual((other / "helper").read_text(), "H1")
        (self.source / "helper").write_text("H2")
        self.assertNotEqual(capture(self.binding)["inspection"], packet["inspection"])
        self.assertEqual((one / "helper").read_text(), "H1")

    def test_pinned_git_does_not_publish_dirty_worktree(self):
        def git(*args):
            return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(self.source), *args], text=True).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "H1")
        binding = dict(self.binding, kind="git", commit=git("rev-parse", "HEAD"))
        (self.source / "helper").write_text("unapproved H2")
        packet = capture(binding)
        out = stage(packet, self.root / "cache")
        self.assertEqual((out / "helper").read_text(), "H1")
        self.assertEqual(packet["origin"]["commit"], binding["commit"])
        import os
        previous = os.umask(0o077)
        try:
            remote = stage(packet, self.root / "private-target")
        finally:
            os.umask(previous)
        self.assertEqual((remote / "skills").stat().st_mode & 0o777, 0o755)

    def test_missing_dependency_plugin_and_bad_metadata_are_explicit(self):
        for operation, expected in (("dependency", "workspace_dependency_missing"),
                                    ("metadata", "workspace_metadata"), ("plugin", "workspace_plugin_excluded")):
            with self.subTest(operation=operation):
                if operation == "dependency":
                    binding = dict(self.binding, include=["missing"])
                else:
                    binding = self.binding
                    if operation == "metadata":
                        (self.source / "skills/alpha/SKILL.md").write_text("invalid metadata")
                    else:
                        (self.source / "skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: fixture\n---\n")
                        (self.source / "skills/alpha/.codex-plugin").mkdir()
                with self.assertRaises(LifecycleError) as caught:
                    capture(binding)
                self.assertEqual(caught.exception.code, expected)

    def test_untrusted_packet_paths_and_bytes_cannot_escape(self):
        packet = capture(self.binding)
        for mutation in ("escape", "absolute", "duplicate", "bytes", "mode", "symlink", "hash"):
            bad = copy.deepcopy(packet)
            if mutation == "escape": bad["files"][0]["path"] = "../escape"
            if mutation == "absolute": bad["files"][0]["path"] = "/tmp/escape"
            if mutation == "duplicate": bad["files"].append(bad["files"][0])
            if mutation == "bytes": bad["files"][0]["data"] = "!invalid"
            if mutation == "mode": bad["files"][0]["mode"] = True
            if mutation == "symlink":
                bad["files"][0].update(kind="symlink", data="L3RtcC9lc2NhcGU=")
            if mutation == "hash": bad["inspection"]["source_tree_sha256"] = "0" * 64
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp, self.assertRaises(LifecycleError):
                unpack(bad, Path(temp) / "new")

    def test_source_links_must_have_complete_package_targets(self):
        for target in ("/foreign/host/skill", "missing", "loop"):
            link = self.source / "loop"
            link.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(LifecycleError):
                capture(dict(self.binding, include=["."]))
            link.unlink()
