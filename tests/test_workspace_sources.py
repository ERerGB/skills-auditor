import base64
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

    def test_nested_plugin_marker_cannot_be_omitted_from_a_local_selection(self):
        nested = self.source / "bundle"
        nested.mkdir()
        (nested / ".codex-plugin").mkdir()
        import shutil
        shutil.copytree(self.source / "skills", nested / "skills")
        with self.assertRaises(LifecycleError) as caught:
            capture(dict(self.binding, include=["bundle/skills"], entries={"alpha": "bundle/skills/alpha"}))
        self.assertEqual(caught.exception.code, "workspace_plugin_excluded")

    def test_git_plugin_ownership_uses_the_pinned_tree_not_dirty_markers(self):
        def git(*args):
            return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-C", str(self.source), *args], text=True).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "standalone")
        standalone = git("rev-parse", "HEAD")
        marker = self.source / ".codex-plugin"
        marker.mkdir()
        (marker / "plugin.json").write_text("{}")
        git("add", ".codex-plugin")
        git("-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "plugin")
        plugin = git("rev-parse", "HEAD")
        # An unselected marker from a later checkout does not alter pinned bytes.
        packet = capture(dict(self.binding, kind="git", commit=standalone))
        self.assertEqual(packet["origin"]["commit"], standalone)
        (marker / "plugin.json").unlink()
        marker.rmdir()
        with self.assertRaises(LifecycleError) as caught:
            capture(dict(self.binding, kind="git", commit=plugin))
        self.assertEqual(caught.exception.code, "workspace_plugin_excluded")

    def test_cyclic_source_root_is_a_contract_error(self):
        alias = self.root / "source-loop"
        alias.symlink_to(alias)
        with self.assertRaises(LifecycleError) as caught:
            capture(dict(self.binding, root=str(alias)))
        self.assertEqual(caught.exception.code, "workspace_source")

    def test_source_alias_cannot_hide_plugin_ownership(self):
        alias = self.root / "source-alias"
        alias.symlink_to(self.source / "skills/alpha", target_is_directory=True)
        for marker in (".codex-plugin", ".claude-plugin", ".cursor-plugin", ".grok-plugin"):
            plugin = self.source / marker
            plugin.mkdir()
            try:
                with self.subTest(marker=marker):
                    with self.assertRaises(LifecycleError) as caught:
                        capture(dict(self.binding, root=str(alias), include=["SKILL.md"], entries={"alpha": "."}))
                    self.assertEqual(caught.exception.code, "workspace_plugin_excluded")
            finally:
                plugin.rmdir()

    def test_portable_ancestor_aliases_are_rejected_before_any_staging_write(self):
        def record(path, kind, data):
            return {"path": path, "mode": 0o777 if kind == "symlink" else 0o644,
                    "kind": kind, "data": base64.b64encode(data).decode()}
        for parent, alias in (("a", "A"), ("caf\u00e9", "cafe\u0301")):
            with self.subTest(parent=parent), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                outside = root / "outside"
                outside.mkdir()
                packet = {"origin": {"kind": "local", "uncommitted_snapshot": True},
                    "entries": {"alpha": "skill"}, "platforms": ["codex"], "files": [
                        record(parent, "symlink", b"../outside"),
                        record(alias + "/pwn", "file", b"owned-portable-boundary-test"),
                        record("skill/SKILL.md", "file", b"---\nname: alpha\ndescription: fixture\n---\n")]}
                destination = root / "destination"
                with self.assertRaises(LifecycleError) as caught:
                    unpack(packet, destination)
                self.assertEqual(caught.exception.code, "workspace_packet")
                self.assertFalse(destination.exists())
                self.assertEqual(list(outside.iterdir()), [])

    def test_implicit_directory_spellings_share_one_portable_namespace(self):
        packet = capture(self.binding)
        packet.pop("inspection")
        for name in ("folder/one", "FOLDER/two"):
            packet["files"].append({"path": name, "mode": 0o644,
                "kind": "file", "data": base64.b64encode(b"owned").decode()})
        destination = self.root / "invalid-implicit-alias"
        with self.assertRaises(LifecycleError) as caught:
            unpack(packet, destination)
        self.assertEqual(caught.exception.code, "workspace_packet")
        self.assertFalse(destination.exists())

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
