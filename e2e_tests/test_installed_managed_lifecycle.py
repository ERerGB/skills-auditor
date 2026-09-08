"""Managed governance through the installed console script, outside source."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
import unicodedata
from types import SimpleNamespace

from lifecycle_model import ModelOracle, normalized_tree, tree_state


CLI = Path(os.environ["SKILLS_AUDITOR_CLI"])


class TestInstalledManagedModelContext(unittest.TestCase):
    """S05/S06 run the installed package, with the same independent oracle."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-installed-model-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "owner's project with spaces"
        self.project.mkdir()
        self.outside = self.root / "unrelated cwd"
        self.outside.mkdir()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Installed model fixture\n")
        (self.source / "payload").write_text("H1")
        self.candidate_before = tree_state(self.source)
        self.target = self.project / "host entry"
        self.oracle = ModelOracle(self, SimpleNamespace(project_root=self.project))
        self.oracle.watch_pointer(self.target, None)
        self.model_trees = {}
        self.snapshot_inodes = {}
        self.pointer_inodes = {}

    def after_action(self):
        self.assertEqual(tree_state(self.source), self.candidate_before)
        self.assertFalse((self.outside / ".skills-auditor-local").exists())
        if self.oracle.database.exists():
            self.oracle.check()
            for path, inode in list(self.snapshot_inodes.items()):
                if path.exists():
                    self.assertEqual(path.stat().st_ino, inode, "An existing immutable snapshot tree was rebuilt")
            for path, (kind, expected) in self.oracle.paths.items():
                if kind == "tree" and expected is not None and path.exists():
                    self.snapshot_inodes.setdefault(path, path.stat().st_ino)
                elif kind == "pointer":
                    actual = (os.readlink(path), path.lstat().st_ino) if path.is_symlink() else None
                    if path in self.pointer_inodes and self.pointer_inodes[path][0] == expected:
                        self.assertEqual(actual, self.pointer_inodes[path][1], "An unchanged selected pointer was rebuilt")
                    self.pointer_inodes[path] = (expected, actual)

    def remember_plan(self, plan, *, source=None):
        if "version" in plan:
            version = plan["version"]
            if source is not None:
                self.model_trees[version["version_id"]] = normalized_tree(tree_state(source))
            self.assertIn(version["version_id"], self.model_trees)

    def expect_effect(self, plan):
        if "children" in plan:
            for child in plan["children"]:
                self.expect_effect(child["plan"])
            return
        version = plan["version"]
        snapshot = version["snapshot"]["path"]
        self.oracle.watch_tree(snapshot, self.model_trees[version["version_id"]])
        self.oracle.watch_pointer(plan["after"]["target"], snapshot if plan["after"]["state"] == "active" else None)

    def cli(self, *arguments, expected=0, text=False, project=None):
        result = subprocess.run([str(CLI), "lifecycle", "--project-root", str(project or self.project),
                                 "--format", "text" if text else "json", *map(str, arguments)],
                                cwd=self.outside, capture_output=True, text=True, timeout=30)
        self.after_action()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout if text else json.loads(result.stdout)

    def emitted(self, command, *, expected=0):
        arguments = shlex.split(command)
        self.assertEqual(arguments[:2], ["skills-audit", "lifecycle"])
        self.assertIn("--project-root", arguments)
        self.assertEqual(arguments[arguments.index("--project-root") + 1], str(self.project))
        result = subprocess.run([str(CLI), "lifecycle", "--format", "json", *arguments[2:]], cwd=self.outside,
                                capture_output=True, text=True, timeout=30)
        self.after_action()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_installed_s05_exported_status_investigation_and_unknown_context_from_other_cwd(self):
        path = self.project / "install.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.remember_plan(plan, source=self.source)
        self.expect_effect(plan)
        receipt = self.cli("apply", path, "--approve-plan-id", plan["plan_id"])
        identifier = receipt["installation_id"]
        self.cli("verify", identifier)
        for command in (("status", identifier), ("preflight", identifier, "--cached"),
                        ("invocation", "select", identifier, "--cached")):
            value = self.cli(*command)
            status = value.get("status", value)
            self.assertEqual(status.get("project_root"), str(self.project))
            self.assertIs(status.get("context_verified"), True)
            self.assertEqual(self.emitted(status["recommended_next_action"]["command"])["installation_id"], identifier)
            text = self.cli(*command, text=True)
            suggested = next(line[6:] for line in text.splitlines() if line.startswith("Next: "))
            self.assertEqual(self.emitted(suggested)["installation_id"], identifier)
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        self.after_action()
        failed = self.cli("verify", identifier, expected=3)
        incident = self.cli("incidents")["incidents"][0]["incident_id"]
        self.cli("append-note", incident, "--text", "Evidence belongs to the original project", "--actor", "installed-reviewer",
                 "--tool", "installed-model", "--evidence-ref", "verification:" + failed["verification_id"])
        first = self.cli("investigate", incident, "--limit", "1")
        self.assertEqual(first.get("project_root"), str(self.project))
        following = self.emitted(first["next_action"])
        self.assertEqual(following["incident"]["incident_id"], incident)
        self.assertEqual(following["events"][0]["event_type"], "note")
        self.assertGreater(following["events"][0]["sequence"], first["events"][-1]["sequence"])
        text = self.cli("status", identifier, expected=3, text=True)
        for line in text.splitlines():
            if line.startswith("Investigate: "):
                self.assertEqual(self.emitted(line[len("Investigate: "):])["incident_id"], incident)
        self.assertEqual(self.cli("inspect", "receipt", receipt["receipt_id"]), receipt)
        for project in (self.project, self.root / "missing owner's project"):
            project.mkdir(exist_ok=True)
            value = self.cli("status", "unknown", project=project, expected=3)
            self.assertEqual(value.get("project_root"), str(project))
            self.assertIs(value.get("context_verified"), project == self.project)
            self.assertIn(str(project), self.cli("status", "unknown", project=project, expected=3, text=True))
        self.assertFalse((self.root / "missing owner's project" / ".skills-auditor-local").exists())

    def test_installed_s06_discover_generated_core_batch_and_retention_ids_after_process_death(self):
        owner = self.project
        for kind, boundary in (("transaction", "transaction:prepared"),
                               ("batch", "batch:child:0:transaction:prepared"),
                               ("retention-transaction", "retention:prepared")):
            with self.subTest(kind=kind):
                self.project = owner / kind
                self.project.mkdir()
                self.target = self.project / "host entry"
                self.oracle = ModelOracle(self, SimpleNamespace(project_root=self.project))
                self.oracle.watch_pointer(self.target, None)
                path = self.project / "plan.json"
                if kind == "retention-transaction":
                    prepare = """
import json, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.snapshots import inspect_source, materialize
m = Manager(sys.argv[1])
d = inspect_source(sys.argv[2])
materialize(sys.argv[2], m.store_root, d['source_tree_sha256'], d['snapshot_tree_sha256'])
print(json.dumps({'path': str(m.store_root / d['snapshot_tree_sha256'] / 'tree')}))
"""
                    prepared = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", prepare, str(self.project), str(self.source)],
                                              cwd=self.outside, capture_output=True, text=True, timeout=30)
                    self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
                    self.oracle.watch_tree(json.loads(prepared.stdout)["path"], normalized_tree(self.candidate_before))
                    self.after_action()
                    plan = self.cli("retention", "plan", "collect", "--plan-out", path)
                    self.oracle.watch_tree(Path(plan["objects"][0]["quarantine_path"]) / "tree", None)
                else:
                    child = self.project / "child.json"
                    plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", child)
                    self.remember_plan(plan, source=self.source)
                    self.oracle.watch_tree(plan["version"]["snapshot"]["path"], None)
                    if kind == "batch":
                        plan = self.cli("batch", "plan", child, "--plan-out", path)
                    else:
                        path = child
                die = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.retention import apply_retention
m = Manager(sys.argv[1], create=False)
with open(sys.argv[3]) as handle:
    p = json.load(handle)
def checkpoint(name, value):
    if name == sys.argv[4]: os._exit(73)
if sys.argv[2] == 'transaction': m.apply(p, approve_plan_id=p['plan_id'], checkpoint=checkpoint)
elif sys.argv[2] == 'batch': BatchManager(m).apply(p, approve_plan_id=p['plan_id'], checkpoint=checkpoint)
else: apply_retention(m, p, approve_plan_id=p['plan_id'], checkpoint=checkpoint)
"""
                stopped = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", die, str(self.project), kind, str(path), boundary],
                                         cwd=self.outside, capture_output=True, text=True, timeout=30)
                self.after_action()
                self.assertEqual(stopped.returncode, 73, stopped.stdout + stopped.stderr)
                self.assertFalse(self.target.is_symlink())
                before = self.oracle.check()
                listing = self.cli("list", "--pending")
                self.assertEqual(listing["schema_version"], "skills-auditor-lifecycle-pending-list/v1")
                self.assertEqual(listing["project_root"], str(self.project))
                entry = next(item for item in listing["pending"] if item["kind"] == kind)
                inspected = self.emitted(entry["inspection"]["command"])
                self.assertEqual(inspected["plan"]["plan_id"], plan["plan_id"])
                self.assertEqual(self.oracle.check(), before)
                prefix = ("batch",) if kind == "batch" else ("retention",) if kind == "retention-transaction" else ()
                reference_key = "batch_id" if kind == "batch" else "transaction_id"
                option = "--batch-id" if kind == "batch" else "--transaction-id"
                for suffix in ((), (option, "different-default-id")):
                    failed = self.cli(*prefix, "apply", path, "--approve-plan-id", plan["plan_id"], *suffix, expected=3)
                    self.assertEqual(failed["details"][reference_key], entry["id"])
                if kind == "retention-transaction":
                    failed = self.cli("retention", "plan", "collect", expected=3)
                    self.assertEqual(failed["details"][reference_key], entry["id"])
                    text = self.cli("retention", "plan", "collect", expected=3, text=True)
                    hint = next(line[len("Next (read-only): "):] for line in text.splitlines()
                                if line.startswith("Next (read-only): "))
                    self.assertEqual(self.emitted(hint)["transaction_id"], entry["id"])
                self.assertEqual(self.oracle.check(), before, "Conflicting new IDs must not create substitute intents or receipts")
                if kind == "batch":
                    self.expect_effect(plan)
                    self.cli("batch", "resume", entry["id"], "--approve-plan-id", plan["plan_id"])
                else:
                    prefix = ("retention",) if kind == "retention-transaction" else ()
                    if kind == "retention-transaction":
                        obj = plan["objects"][0]
                        self.oracle.watch_tree(self.project / ".skills-auditor-local/lifecycle/store/sha256" / obj["name"] / "tree", None)
                        self.oracle.watch_tree(Path(obj["quarantine_path"]) / "tree", normalized_tree(self.candidate_before))
                    else:
                        self.expect_effect(plan)
                    self.cli(*prefix, "recover", entry["id"], "--mode", "resume", "--approve-plan-id", plan["plan_id"])
                self.assertEqual(self.cli("list", "--pending")["pending"], [])
                if kind == "retention-transaction":
                    self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).is_dir())
                    obj = plan["objects"][0]
                    self.assertEqual(Path(obj["quarantine_path"]).stat().st_ino, obj["identity"][1])
                    original_tree = self.project / ".skills-auditor-local/lifecycle/store/sha256" / obj["name"] / "tree"
                    self.assertEqual((Path(obj["quarantine_path"]) / "tree").stat().st_ino, self.snapshot_inodes[original_tree])
                else:
                    self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_installed_s02_s03_shared_skill_rollout_inverse_and_inverse_of_inverse_keep_history(self):
        paths = []

        def planned(operation, *arguments, source=None):
            path = self.project / ("core-{}.json".format(len(paths)))
            paths.append(path)
            plan = self.cli("plan", operation, *arguments, "--plan-out", path)
            self.remember_plan(plan, source=source)
            return path, plan

        def apply_core(path, plan):
            self.cli("apply", path, expected=3)
            self.expect_effect(plan)
            return self.cli("apply", path, "--approve-plan-id", plan["plan_id"])

        path, plan = planned("install", "--source", self.source, "--target", self.target, source=self.source)
        first = apply_core(path, plan)
        second_target = self.project / "second host"
        self.oracle.watch_pointer(second_target, None)
        path, plan = planned("install-retained", "--version-id", first["version_id"], "--target", second_target)
        second = apply_core(path, plan)
        self.assertEqual(first["skill_id"], second["skill_id"])
        self.assertEqual(first["version_id"], second["version_id"])
        # Candidate edits must not mutate either selected H1 snapshot.
        (self.source / "payload").write_text("H2")
        self.candidate_before = tree_state(self.source)
        self.after_action()
        saved = []
        for operation, receipt in (("update", first), ("edit", second)):
            path, plan = planned(operation, "--installation-id", receipt["installation_id"],
                                 "--source", self.source, source=self.source)
            saved.append((path, plan))
        self.assertEqual(saved[0][1]["version"]["version_id"], saved[1][1]["version"]["version_id"])
        parent_path = self.project / "rollout.json"
        parent = self.cli("batch", "plan", *[path for path, _ in saved], "--plan-out", parent_path)
        self.cli("batch", "apply", parent_path, expected=3)
        self.expect_effect(parent)
        forward = self.cli("batch", "apply", parent_path, "--approve-plan-id", parent["plan_id"])
        ancestors = [forward["batch_id"]]

        def history():
            before = self.oracle.check()
            for identifier in ancestors:
                self.assertIsNotNone(self.cli("batch", "inspect", identifier)["receipt_id"])
            self.assertEqual(self.cli("retention", "plan", "collect")["objects"], [])
            after = self.oracle.check()
            self.assertEqual(after["records"].get("receipt"), before["records"].get("receipt"))
            self.assertEqual(after["records"].get("batch-receipt"), before["records"].get("batch-receipt"))

        history()
        for generation, expected_version in ((1, first["version_id"]), (2, saved[0][1]["version"]["version_id"])):
            path = self.project / ("inverse-{}.json".format(generation))
            inverse = self.cli("batch", "compensate-plan", ancestors[-1], "--plan-out", path)
            history()
            self.cli("batch", "apply", path, expected=3)
            history()
            self.expect_effect(inverse)
            completed = self.cli("batch", "apply", path, "--approve-plan-id", inverse["plan_id"])
            ancestors.append(completed["batch_id"])
            for receipt in (first, second):
                self.oracle.expect_installation(receipt["installation_id"], state="active", authorization="valid",
                                                version_id=expected_version)
            history()
        self.assertEqual(self.cli("inspect", "receipt", first["receipt_id"]), first)
        self.assertEqual(self.cli("inspect", "receipt", second["receipt_id"]), second)


class TestInstalledManagedLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-installed-managed-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Managed fixture\n")
        (self.source / "payload").write_text("H1")
        self.target = self.project / "installed"
        self.sequence = 0

    def cli(self, *arguments, expected=0):
        result = subprocess.run([str(CLI), "lifecycle", "--project-root", str(self.project), "--format", "json", *map(str, arguments)], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def approved(self, operation, installation_id=None, **options):
        self.sequence += 1
        path = self.project / ("plan-{}.json".format(self.sequence))
        args = ["plan", operation, "--plan-out", path]
        if installation_id:
            args.extend(["--installation-id", installation_id])
        for key, value in options.items():
            args.extend(["--" + key.replace("_", "-"), value])
        plan = self.cli(*args)
        return self.cli("apply", path, "--approve-plan-id", plan["plan_id"])

    def test_installed_incident_journal_pagination_and_explicit_resolution(self):
        first = self.approved("install", source=self.source, target=self.target)
        identifier = first["installation_id"]
        raw = os.readlink(self.target)
        self.target.unlink()
        failed = self.cli("verify", identifier, expected=3)
        incident = self.cli("incidents", "--installation-id", identifier, "--state", "open")["incidents"][0]["incident_id"]
        note = self.cli("append-note", incident, "--text", "Missing pointer under investigation", "--actor", "local-reviewer", "--tool", "installed-fixture", "--event-id", "installed-note", "--evidence-ref", "verification:" + failed["verification_id"])
        self.assertEqual(note["actor"], "local-reviewer")
        first_page = self.cli("investigate", incident, "--limit", "1")
        self.assertTrue(first_page["has_more"])
        page = self.cli("investigate", incident, "--limit", "1", "--after-sequence", first_page["continuation"]["after_sequence"])
        self.assertEqual(page["events"][0]["event_type"], "note")
        self.cli("resolve", incident, "--verification-id", failed["verification_id"], expected=3)
        self.target.symlink_to(raw)
        self.cli("verify", identifier, expected=3)
        self.approved("renew", identifier)
        clean = self.cli("verify", identifier)
        self.assertEqual(self.cli("resolve", incident, "--verification-id", clean["verification_id"])["state"], "resolved")
        self.assertEqual(self.cli("investigate", incident)["incident"]["resolution"]["kind"], "remediated")
        self.assertEqual(self.cli("inspect", "receipt", first["receipt_id"]), first)

    def test_installed_invocation_stale_override_and_revocation_never_switch_candidate(self):
        first = self.approved("install", source=self.source, target=self.target)
        identifier = first["installation_id"]
        (self.source / "payload").write_text("H2 candidate")
        result = self.cli("invocation", "select", identifier, "--policy", "last-known-good")
        self.assertEqual((Path(result["snapshot_path"]) / "payload").read_text(), "H1")
        time.sleep(1.05)
        self.cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", expected=3)
        self.cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--policy", "last-known-good", expected=4)
        path = self.project / "override.json"
        plan = self.cli("invocation", "override-plan", identifier, "--reason", "Temporary stale adapter cache", "--max-age-seconds", "1", "--ttl-seconds", "300", "--plan-out", path)
        self.cli("invocation", "override-apply", path, expected=3)
        override = self.cli("invocation", "override-apply", path, "--approve-plan-id", plan["plan_id"])
        self.assertEqual(self.cli("invocation", "override-get", override["override_id"]), override)
        selected = self.cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override["override_id"])
        self.assertTrue(selected["override"]["used"])
        self.assertEqual(selected["status"]["freshness"]["state"], "stale")
        self.assertTrue(selected["audit"]["recorded"])
        self.assertEqual(selected["version_id"], first["version_id"])
        self.cli("invocation", "override-revoke", override["override_id"], "--reason", "Exception ended")
        blocked = self.cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override["override_id"], expected=3)
        self.assertIsNone(blocked["snapshot_path"])
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_installed_retention_preserves_history_and_requires_second_purge_gate(self):
        first = self.approved("install", source=self.source, target=self.target)
        last = self.approved("uninstall", first["installation_id"])
        path = self.project / "retention.json"

        def prepare(operation, *options):
            return self.cli("retention", "plan", operation, *options, "--plan-out", path)

        def apply(plan, *options):
            return self.cli("retention", "apply", path, "--approve-plan-id", plan["plan_id"], *options)

        policy = prepare("policy", "--keep-recent", "0", "--clear-pins")
        self.cli("retention", "apply", path, expected=3)
        apply(policy)
        apply(prepare("expire", "--receipt-id", first["receipt_id"], "--receipt-id", last["receipt_id"]))
        collected = apply(prepare("collect"))
        obj = collected["objects"][0]
        self.assertTrue(Path(obj["quarantine_path"]).exists())
        self.assertEqual(self.cli("retention", "recover", collected["transaction_id"])["state"], "completed")
        apply(prepare("restore", "--object-id", obj["quarantine_id"]))
        collected = apply(prepare("collect"))
        obj = collected["objects"][0]
        purge = prepare("purge", "--object-id", obj["quarantine_id"], "--grace-seconds", "0")
        self.cli("retention", "apply", path, "--approve-plan-id", purge["plan_id"], expected=3)
        self.assertTrue(Path(obj["quarantine_path"]).exists())
        script = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention
plan = json.loads(sys.argv[2])
def die(name, value):
    if name == 'retention:0:delete_effect': os._exit(73)
apply_retention(Manager(sys.argv[1]), plan, approve_plan_id=plan['plan_id'], permanent_delete=True, transaction_id='installed-purge', checkpoint=die)
"""
        died = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", script, str(self.project), json.dumps(purge)], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(died.returncode, 73, died.stderr)
        interrupted = self.cli("retention", "recover", "installed-purge")
        self.assertNotEqual(interrupted["state"], "completed")
        self.assertIsNone(interrupted["receipt_id"])
        self.cli("retention", "recover", "installed-purge", "--mode", "resume", "--approve-plan-id", purge["plan_id"], expected=3)
        self.cli("retention", "recover", "installed-purge", "--mode", "resume", "--permanent-delete", expected=3)
        result = self.cli("retention", "recover", "installed-purge", "--mode", "resume", "--approve-plan-id", purge["plan_id"], "--permanent-delete")
        self.assertTrue(result["permanently_deleted"])
        self.assertFalse(Path(obj["quarantine_path"]).exists())
        self.assertEqual(self.cli("inspect", "receipt", first["receipt_id"]), first)

    def test_installed_batch_process_death_resume_and_inverse_exact_approval(self):
        paths = []
        targets = [self.project / ("batch-target-" + str(index)) for index in range(2)]
        for index, target in enumerate(targets):
            path = self.project / ("child-" + str(index) + ".json")
            self.cli("plan", "install", "--source", self.source, "--target", target, "--plan-out", path)
            paths.append(path)
        path = self.project / "batch.json"
        batch = self.cli("batch", "plan", *paths, "--plan-out", path)
        self.cli("batch", "apply", path, expected=3)
        script = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
plan = json.loads(sys.argv[2])
def die(name, value):
    if name == 'batch:child:0:step:0:effect': os._exit(73)
BatchManager(Manager(sys.argv[1])).apply(plan, approve_plan_id=plan['plan_id'], batch_id='installed-batch', checkpoint=die)
"""
        died = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", script, str(self.project), json.dumps(batch)], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(died.returncode, 73, died.stderr)
        before = targets[0].lstat().st_ino
        self.assertNotEqual(self.cli("batch", "inspect", "installed-batch")["state"], "completed")
        self.cli("batch", "resume", "installed-batch", expected=3)
        receipt = self.cli("batch", "resume", "installed-batch", "--approve-plan-id", batch["plan_id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(targets[0].lstat().st_ino, before)
        self.assertTrue(all(target.is_symlink() for target in targets))
        inverse = self.cli("batch", "compensate-plan", "installed-batch", "--plan-out", path)
        self.cli("batch", "apply", path, expected=3)
        self.assertEqual(self.cli("batch", "apply", path, "--approve-plan-id", inverse["plan_id"])["status"], "completed")
        self.assertFalse(any(target.is_symlink() for target in targets))

    def test_installed_text_failures_expose_copyable_scoped_read_only_recovery_commands(self):
        core_path = self.project / "error-core.json"
        core = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", core_path)
        batch_path = self.project / "error-batch.json"
        batch = self.cli("batch", "plan", core_path, "--plan-out", batch_path)
        retention_path = self.project / "error-retention.json"
        retained = self.cli("retention", "plan", "policy", "--keep-recent", "0", "--plan-out", retention_path)
        script = """
import sys
from unittest.mock import patch
from skills_auditor.cli import main
fault, arguments = sys.argv[1], sys.argv[2:]
sys.argv = ['skills-audit', 'lifecycle', *arguments]
with patch(fault, side_effect=OSError('PRIVATE fixture detail')):
    result = main()
sys.exit(result)
"""
        cases = (
            (["retention", "apply", str(retention_path), "--approve-plan-id", retained["plan_id"]], "skills_auditor.lifecycle.retention._complete", "retention", "transaction_id"),
            (["apply", str(core_path), "--approve-plan-id", core["plan_id"]], "skills_auditor.lifecycle.engine.Manager._effect", "core", "transaction_id"),
            (["batch", "apply", str(batch_path), "--approve-plan-id", batch["plan_id"]], "skills_auditor.lifecycle.engine.Manager._effect", "batch", "batch_id"),
        )
        # Each generated failure is explicitly compensated before the next case,
        # keeping the later reviewed core plan's target absent and unmodified.
        for arguments, fault, kind, id_key in cases:
            with self.subTest(kind=kind):
                result = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", script, fault, "--project-root", str(self.project), "--format", "text", *arguments], cwd=self.project, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertNotIn("PRIVATE fixture detail", result.stdout)
                next_line = next(line for line in result.stdout.splitlines() if line.startswith("Next (read-only): "))
                command = shlex.split(next_line.partition(": ")[2])
                self.assertEqual(command[:4], ["skills-audit", "lifecycle", "--project-root", str(self.project)])
                inspected = self.cli(*command[4:])
                self.assertEqual(inspected["state"], "recovery_needed")
                self.assertIsNone(inspected["receipt_id"])
                self.assertIn(inspected[id_key], result.stdout)
                if kind == "retention":
                    self.cli("retention", "recover", inspected[id_key], "--mode", "resume", "--approve-plan-id", retained["plan_id"])
                elif kind == "core":
                    self.cli("recover", inspected[id_key], "--mode", "compensate", "--approve-plan-id", core["plan_id"])

    def test_installed_completed_retry_denial_survives_pointer_restore_without_renewing_freshness(self):
        path = self.project / "retry-install.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        receipt = self.cli("apply", path, "--approve-plan-id", plan["plan_id"], "--transaction-id", "completed-retry")
        identifier = receipt["installation_id"]
        observation = self.cli("verify", identifier)
        before = self.cli("status", identifier)
        saved_plan = path.read_bytes()
        inode = self.target.lstat().st_ino
        retry_arguments = ["apply", path, "--approve-plan-id", plan["plan_id"], "--transaction-id", "completed-retry"]
        self.assertEqual(self.cli(*retry_arguments), receipt)
        after = self.cli("status", identifier)
        self.assertEqual(after["verification_id"], observation["verification_id"])
        self.assertEqual(after["observed_at"], before["observed_at"])
        self.assertGreaterEqual(after["freshness"]["age_seconds"], before["freshness"]["age_seconds"])
        self.assertEqual(self.target.lstat().st_ino, inode)
        raw = os.readlink(self.target)
        self.target.unlink()
        error = self.cli(*retry_arguments, expected=3)
        self.assertEqual(error["code"], "approval_invalidated")
        self.assertEqual(error["details"]["transaction_id"], "completed-retry")
        self.assertIn("target_link", error["details"]["reason_codes"])
        denied = self.cli("inspect", "installation", identifier)
        self.assertEqual(denied["authorization"]["state"], "invalidated")
        self.assertEqual(denied["authorization"]["grant_id"], receipt["grant_id"])
        self.target.symlink_to(raw)
        restored = self.cli("verify", identifier, expected=3)
        self.assertTrue(restored["integrity"]["valid"])
        self.assertEqual(restored["approval"]["state"], "invalidated")
        self.assertEqual(restored["grant_id"], receipt["grant_id"])
        self.assertIn("target_link", restored["approval"]["reason_codes"])
        self.assertEqual(self.cli("inspect", "receipt", receipt["receipt_id"]), receipt)
        self.assertEqual(self.cli("inspect", "transaction", "completed-retry")["state"], "completed")
        self.assertEqual(path.read_bytes(), saved_plan)
        self.assertEqual((self.source / "payload").read_text(), "H1")

    def test_installed_pending_renewal_fences_new_apply_until_explicit_compensation(self):
        first = self.approved("install", source=self.source, target=self.target)
        identifier = first["installation_id"]
        self.cli("verify", identifier)
        pending_path = self.project / "pending-renew.json"
        pending = self.cli("plan", "renew", "--installation-id", identifier, "--plan-out", pending_path)
        other_path = self.project / "other-renew.json"
        other = self.cli("plan", "renew", "--installation-id", identifier, "--plan-out", other_path)
        before = self.cli("inspect", "installation", identifier)
        inode, raw = self.target.lstat().st_ino, os.readlink(self.target)
        script = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
plan = json.loads(sys.argv[2])
def die(name, value):
    if name == 'transaction:prepared': os._exit(73)
Manager(sys.argv[1]).apply(plan, approve_plan_id=plan['plan_id'], transaction_id='pending-renew', checkpoint=die)
"""
        died = subprocess.run([os.environ["SKILLS_AUDITOR_PYTHON"], "-I", "-c", script, str(self.project), json.dumps(pending)], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(died.returncode, 73, died.stderr)
        unfinished = self.cli("recover", "pending-renew")
        self.assertEqual(unfinished["state"], "prepared")
        self.assertIsNone(unfinished["receipt_id"])
        arguments = ["apply", other_path, "--approve-plan-id", other["plan_id"], "--transaction-id", "blocked-new-renew"]
        blocked = self.cli(*arguments, expected=3)
        self.assertEqual(blocked["code"], "pending_transaction")
        self.assertEqual(blocked["details"]["transaction_id"], "pending-renew")
        text_result = subprocess.run([str(CLI), "lifecycle", "--project-root", str(self.project), "--format", "text", *map(str, arguments)], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(text_result.returncode, 3, text_result.stdout + text_result.stderr)
        self.assertIn("recover pending-renew --mode inspect", text_result.stdout)
        self.assertEqual(self.cli("inspect", "transaction", "blocked-new-renew", expected=3)["code"], "record_missing")
        self.assertEqual(self.cli("inspect", "installation", identifier), before)
        self.assertEqual((self.target.lstat().st_ino, os.readlink(self.target)), (inode, raw))
        self.cli("recover", "pending-renew", "--mode", "compensate", expected=3)
        compensated = self.cli("recover", "pending-renew", "--mode", "compensate", "--approve-plan-id", pending["plan_id"])
        self.assertEqual(compensated["state"], "compensated")
        self.assertIsNone(compensated["receipt_id"])
        renewed = self.approved("renew", identifier)
        self.assertNotEqual(renewed["grant_id"], first["grant_id"])
        self.assertTrue(self.cli("verify", identifier)["valid"])
        self.assertEqual(self.cli("inspect", "receipt", first["receipt_id"]), first)
        self.assertEqual((self.target.lstat().st_ino, os.readlink(self.target)), (inode, raw))

    def test_candidate_update_rollback_noop_renewal_and_durable_warning(self):
        first = self.approved("install", source=self.source, target=self.target)
        identifier = first["installation_id"]
        self.cli("verify", identifier)
        (self.source / "payload").write_text("H2")
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertEqual(self.cli("preflight", identifier)["decision"], "proceed")
        second = self.approved("update", identifier, source=self.source)
        self.assertEqual((self.target / "payload").read_text(), "H2")
        self.approved("rollback", identifier, version_id=first["version_id"])
        self.assertEqual((self.target / "payload").read_text(), "H1")
        raw = os.readlink(self.target)
        self.target.unlink()
        self.assertEqual(self.cli("verify", identifier, expected=3)["approval"]["state"], "invalidated")
        status = self.cli("status", identifier, expected=3)
        self.assertEqual(len(status["incident_ids"]), 1)
        self.assertEqual(self.cli("inspect", "incident", status["incident_ids"][0])["grant_id"], status["grant_id"])
        self.target.symlink_to(raw, target_is_directory=True)
        self.assertEqual(self.cli("verify", identifier, expected=3)["approval"]["state"], "invalidated")
        before = self.target.lstat()
        renewed = self.approved("renew", identifier)
        self.assertEqual(self.target.lstat().st_ino, before.st_ino)
        self.assertTrue(self.cli("verify", identifier)["valid"])
        self.assertEqual(self.cli("inspect", "receipt", first["receipt_id"]), first)
        self.assertNotEqual(renewed["grant_id"], first["grant_id"])
        self.assertNotEqual(second["version_id"], first["version_id"])

    def test_identity_through_rename_move_disable_enable_archive_uninstall(self):
        first = self.approved("install", source=self.source, target=self.target)
        identifier = first["installation_id"]
        self.approved("rename", identifier, name="renamed")
        destination = self.project / "moved"
        self.approved("move", identifier, target=destination)
        self.assertFalse(self.target.is_symlink())
        self.assertTrue(destination.is_symlink())
        self.approved("disable", identifier)
        self.assertFalse(destination.is_symlink())
        self.assertEqual(self.cli("preflight", identifier, expected=3)["decision"], "block")
        self.approved("enable", identifier)
        self.assertEqual(self.cli("preflight", identifier)["decision"], "proceed")
        self.approved("archive", identifier)
        self.approved("uninstall", identifier)
        final = self.cli("inspect", "installation", identifier)
        self.assertEqual(final["state"], "uninstalled")
        self.assertEqual(final["skill_id"], first["skill_id"])
        self.assertEqual(final["name"], "renamed")
        self.assertFalse(destination.is_symlink())

    def test_missing_approval_bad_checksum_and_uninitialized_readers(self):
        self.assertEqual(self.cli("status", "unknown", expected=3)["approval"]["state"], "unknown")
        self.assertFalse((self.project / ".skills-auditor-local").exists())
        path = self.project / "plan.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.assertEqual(self.cli("apply", path, expected=3)["code"], "approval_required")
        plan["operation"] = "uninstall"
        path.write_text(json.dumps(plan))
        self.assertEqual(self.cli("apply", path, "--approve-plan-id", plan["plan_id"], expected=2)["code"], "invalid_plan")
        self.assertFalse(self.target.is_symlink())

    def test_legacy_migration_requires_clean_receipt_and_cached_status_is_explicit(self):
        sources = self.project / "legacy-sources"
        skill = sources / "legacy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: legacy\ndescription: migration fixture\n---\n# Legacy\n")
        host = self.project / "legacy-host"
        plan_file = self.project / "legacy-plan.json"
        receipt_file = self.project / "legacy-receipt.json"
        for arguments in (
            ["integrate", "--source", str(sources), "--target-root", "fixture=" + str(host), "--plan-out", str(plan_file), "--format", "json"],
            ["apply", str(plan_file), "--receipt-out", str(receipt_file), "--format", "json"],
        ):
            result = subprocess.run([str(CLI), *arguments], cwd=self.project, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            json.loads(result.stdout)
        receipt_bytes = receipt_file.read_bytes()
        migrated = self.approved("migrate", source=skill, target=host / "legacy", legacy_receipt=receipt_file)
        self.assertNotEqual((host / "legacy").resolve(), skill)
        self.assertTrue(self.cli("verify", migrated["installation_id"])["valid"])
        self.assertEqual(self.cli("status", migrated["installation_id"])["severity"], "ok")
        self.assertEqual(self.cli("status", migrated["installation_id"], "--max-age-seconds", "300")["severity"], "ok")
        time.sleep(1.05)
        cached = self.cli("preflight", migrated["installation_id"], "--cached", "--max-age-seconds", "1", expected=4)
        self.assertEqual(cached["decision"], "warn")
        self.assertEqual(self.cli("preflight", migrated["installation_id"])["decision"], "proceed")
        self.assertEqual(receipt_file.read_bytes(), receipt_bytes)

    def test_installed_process_death_requires_explicit_resume_or_compensation(self):
        installed_python = os.environ["SKILLS_AUDITOR_PYTHON"]
        crash_code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
plan = json.loads(sys.argv[2])
def die(name, transaction):
    if name == 'step:0:effect':
        os._exit(73)
Manager(sys.argv[1]).apply(plan, approve_plan_id=plan['plan_id'], transaction_id=sys.argv[3], checkpoint=die)
"""
        path = self.project / "crash-plan.json"
        first_plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        died = subprocess.run([installed_python, "-I", "-c", crash_code, str(self.project), json.dumps(first_plan), "resume-crash"], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(died.returncode, 73, died.stderr)
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertIsNone(self.cli("recover", "resume-crash")["receipt_id"])
        self.cli("recover", "resume-crash", "--mode", "resume", expected=3)
        receipt = self.cli("recover", "resume-crash", "--mode", "resume", "--approve-plan-id", first_plan["plan_id"])
        self.assertEqual(receipt["status"], "completed")
        (self.source / "payload").write_text("H2")
        next_plan = self.cli("plan", "update", "--installation-id", receipt["installation_id"], "--source", self.source, "--plan-out", path)
        died = subprocess.run([installed_python, "-I", "-c", crash_code, str(self.project), json.dumps(next_plan), "compensate-crash"], cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(died.returncode, 73, died.stderr)
        self.assertEqual((self.target / "payload").read_text(), "H2")
        compensated = self.cli("recover", "compensate-crash", "--mode", "compensate", "--approve-plan-id", next_plan["plan_id"])
        self.assertEqual(compensated["state"], "compensated")
        self.assertIsNone(compensated["receipt_id"])
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertEqual(self.cli("inspect", "receipt", receipt["receipt_id"]), receipt)

    def test_installed_alias_output_and_legacy_mutation_guards_preserve_managed_data(self):
        for source, alias in ((self.source, self.project / "CANDIDATE"), (self.project / "caf\u00e9", self.project / unicodedata.normalize("NFD", "caf\u00e9"))):
            source.mkdir(exist_ok=True)
            (source / "SKILL.md").write_text("# Candidate fixture\n")
            original = (source / "SKILL.md").read_bytes()
            result = self.cli("plan", "install", "--source", source, "--target", self.target, "--plan-out", alias / "SKILL.md", expected=3)
            self.assertEqual(result["code"], "unsafe_plan_output")
            self.assertEqual((source / "SKILL.md").read_bytes(), original)
        host = self.project / "host"
        host.mkdir()
        target = host / "managed"
        receipt = self.approved("install", source=self.source, target=target)
        inode, raw, original = target.lstat().st_ino, os.readlink(target), (target / "SKILL.md").read_bytes()
        mapping = self.project / "map.json"
        mapping.write_text(json.dumps({"managed": str(self.source)}))
        commands = [
            ["metadata-repair", "--skills-dir", str(host), "--apply"],
            ["sync", "--skills-dir", str(host), "--map-file", str(mapping), "--apply"],
            ["dedup", "--skills-dir", str(host), "--apply"],
            ["route", "--skills-dir", str(host), "--platform", "codex", "--trace-dir", str(self.project / "traces"), "--apply"],
        ]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(CLI), *arguments], cwd=self.project, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("managed_boundary", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(target.lstat().st_ino, inode)
                self.assertEqual(os.readlink(target), raw)
                self.assertEqual((target / "SKILL.md").read_bytes(), original)
        self.assertFalse((self.project / "traces").exists())
        self.assertEqual(self.cli("inspect", "receipt", receipt["receipt_id"]), receipt)

    def test_installed_deep_json_and_rehashed_invalid_containers_are_single_errors(self):
        import hashlib
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target)
        path = self.project / "malformed.json"
        for field, value in (("operation", []), ("operation", {}), ("legacy_receipt", []), ("legacy_receipt", "")):
            with self.subTest(field=field, value=value):
                changed = dict(plan, **{field: value})
                body = {key: item for key, item in changed.items() if key != "plan_id"}
                changed["plan_id"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                path.write_text(json.dumps(changed))
                self.assertEqual(self.cli("apply", path, "--approve-plan-id", changed["plan_id"], expected=2)["code"], "invalid_plan")
                self.assertFalse(self.target.exists() or self.target.is_symlink())
        path.write_text('{"deep":' + '[' * 10000 + '0' + ']' * 10000 + '}')
        self.assertEqual(self.cli("apply", path, expected=2)["code"], "invalid_input")

    def test_installed_legacy_explicit_and_default_outputs_are_guarded_before_link_creation(self):
        managed = self.approved("install", source=self.source, target=self.target)
        original = (self.target / "SKILL.md").read_bytes()
        sources = self.project / "legacy-output-sources"
        skill = sources / "normal"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: normal\ndescription: fixture\n---\n# Normal\n")
        host = self.project / "legacy-output-host"
        plan_file = self.project / "legacy-output-plan.json"

        def legacy(*arguments, expected):
            result = subprocess.run([str(CLI), *map(str, arguments), "--format", "json"], cwd=self.project, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            return json.loads(result.stdout)

        base = ("integrate", "--source", sources, "--target-root", "fixture=" + str(host))
        blocked = legacy(*base, "--plan-out", self.target / "SKILL.md", expected=3)
        self.assertEqual(blocked["error"]["code"], "managed_boundary")
        legacy(*base, "--plan-out", plan_file, expected=0)
        blocked = legacy("apply", plan_file, "--receipt-out", self.target / "SKILL.md", expected=3)
        self.assertEqual(blocked["error"]["code"], "managed_boundary")
        self.assertFalse(host.exists())
        receipts = self.project / ".skills-auditor-local" / "receipts"
        self.approved("install", source=self.source, target=receipts)
        blocked = legacy("apply", plan_file, expected=3)
        self.assertEqual(blocked["error"]["code"], "managed_boundary")
        self.assertFalse(host.exists())
        legacy("apply", plan_file, "--receipt-out", self.project / "safe-legacy-receipt.json", expected=0)
        self.assertTrue((host / "normal").is_symlink())
        self.assertEqual((self.target / "SKILL.md").read_bytes(), original)
        self.assertEqual(self.cli("inspect", "receipt", managed["receipt_id"]), managed)
