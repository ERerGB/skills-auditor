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


CLI = Path(os.environ["SKILLS_AUDITOR_CLI"])


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
