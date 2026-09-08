"""S05/S06: owner-scoped navigation and recovery after a lost process response.

Each action is followed by an independent SQLite/history oracle. Navigation is
executed from a different working directory; these are not string-only tests.
"""

import json
import copy
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.snapshots import inspect_source, materialize
from lifecycle_model import ModelOracle, normalized_tree, tree_state


_CHECKOUT = Path(__file__).resolve().parents[1]


class TestLifecycleModelContext(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-model-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "owner's project with spaces"
        self.outside = self.root / "unrelated working directory"
        self.project.mkdir()
        self.outside.mkdir()
        self.source = self.project / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Model fixture\n")
        (self.source / "payload").write_text("H1")
        self.candidate_before = tree_state(self.source)
        self.target = self.project / "host entry"
        self.manager = Manager(self.project)
        self.addCleanup(self.manager.repository.close)
        self.oracle = ModelOracle(self, self.manager)
        self.oracle.watch_pointer(self.target, None)
        self.expected_snapshot = normalized_tree(self.candidate_before)
        self.environment = {**os.environ, "PYTHONPATH": str(_CHECKOUT),
                            "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": tempfile.gettempdir()}
        self.oracle.check()

    def after_action(self):
        self.oracle.check()
        self.assertEqual(tree_state(self.source), self.candidate_before)
        self.assertFalse((self.outside / ".skills-auditor-local").exists())

    def expect_effect(self, plan):
        if "children" in plan:
            for child in plan["children"]:
                self.expect_effect(child["plan"])
            return
        snapshot = plan["version"]["snapshot"]["path"]
        self.oracle.watch_tree(snapshot, self.expected_snapshot)
        self.oracle.watch_pointer(plan["after"]["target"], snapshot if plan["after"]["state"] == "active" else None)

    def cli(self, *arguments, expected=0, text=False, project=None):
        result = subprocess.run([sys.executable, "-m", "skills_auditor", "lifecycle",
                                 "--project-root", str(project or self.project), "--format", "text" if text else "json",
                                 *map(str, arguments)], cwd=self.outside, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.after_action()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout if text else json.loads(result.stdout)

    def install(self):
        path = self.project / "install.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.expect_effect(plan)
        receipt = self.cli("apply", path, "--approve-plan-id", plan["plan_id"])
        self.oracle.expect_installation(receipt["installation_id"], state="active", authorization="valid",
                                        version_id=receipt["version_id"], target=self.target)
        self.cli("verify", receipt["installation_id"])
        return receipt

    def command(self, rendered, *, expected=0, readonly=False, project=None):
        arguments = shlex.split(rendered)
        self.assertEqual(arguments[:2], ["skills-audit", "lifecycle"])
        self.assertIn("--project-root", arguments, rendered)
        self.assertEqual(Path(arguments[arguments.index("--project-root") + 1]), project or self.project)
        if readonly:
            self.assertFalse(set(arguments) & {"apply", "resume", "compensate", "--approve-plan-id", "--permanent-delete"})
        result = subprocess.run([sys.executable, "-m", "skills_auditor", "lifecycle", "--format", "json", *arguments[2:]],
                                cwd=self.outside, env=self.environment, capture_output=True, text=True, timeout=30)
        self.after_action()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def context(self, value, *, verified=True, project=None):
        self.assertEqual(value.get("project_root"), str(project or self.project))
        self.assertIs(value.get("context_verified"), verified)

    def test_s05_status_preflight_and_invocation_export_owner_in_json_and_text(self):
        receipt = self.install()
        identifier = receipt["installation_id"]
        for command in (("status", identifier), ("preflight", identifier, "--cached"),
                        ("preflight", identifier), ("invocation", "select", identifier, "--cached")):
            with self.subTest(command=command):
                value = self.cli(*command)
                status = value.get("status", value)
                self.context(status)
                self.assertEqual(status["approval"]["state"], "valid")
                recommendation = status["recommended_next_action"]
                self.assertEqual(shlex.split(recommendation["command"]), recommendation["argv"])
                self.assertEqual(recommendation["required_inputs"], [])
                observed = self.command(recommendation["command"])
                self.assertEqual(observed["installation_id"], identifier)
                text = self.cli(*command, text=True)
                emitted = next(line[6:] for line in text.splitlines() if line.startswith("Next: "))
                self.assertEqual(self.command(emitted)["installation_id"], identifier)

    def test_s05_raw_verification_text_entry_is_read_only_and_owner_scoped(self):
        receipt = self.install()
        text = self.cli("verify", receipt["installation_id"], text=True)
        emitted = next(line[6:] for line in text.splitlines() if line.startswith("Next: "))
        result = self.command(emitted, readonly=True)
        self.context(result)
        self.assertEqual(result["installation_id"], receipt["installation_id"])

    def test_s05_failed_status_and_incident_links_do_not_switch_project_or_renew_grant(self):
        receipt = self.install()
        identifier = receipt["installation_id"]
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        self.after_action()
        self.cli("verify", identifier, expected=3)
        # An observed damaged active installation intentionally has no pointer;
        # do not ask the healthy-installation oracle to require a live link.
        current = self.oracle.check()["records"]["installation"][identifier]
        self.assertEqual(current["state"], "active")
        self.assertEqual(current["authorization"]["state"], "invalidated")
        self.assertEqual(current["authorization"]["grant_id"], receipt["grant_id"])
        self.assertFalse(self.target.is_symlink())
        text = self.cli("status", identifier, expected=3, text=True)
        links = [line[len("Investigate: "):] for line in text.splitlines() if line.startswith("Investigate: ")]
        self.assertTrue(links)
        for link in links:
            incident = self.command(link, readonly=True)
            self.assertEqual(incident["installation_id"], identifier)
            self.assertEqual(incident["grant_id"], receipt["grant_id"])
        for command in (("status", identifier), ("preflight", identifier, "--cached"),
                        ("invocation", "select", identifier, "--cached")):
            value = self.cli(*command, expected=3)
            status = value.get("status", value)
            self.context(status)
            self.assertEqual(status["approval"]["state"], "invalidated")
            self.assertEqual(status["grant_id"], receipt["grant_id"])
            self.assertIn("--project-root", shlex.split(status["recommended_next_action"]["command"]))

    def test_s05_investigation_continuation_executes_in_owner_and_keeps_page_binding(self):
        receipt = self.install()
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        self.after_action()
        failed = self.cli("verify", receipt["installation_id"], expected=3)
        identifier = self.cli("incidents")["incidents"][0]["incident_id"]
        self.cli("append-note", identifier, "--text", "Bounded owner-local evidence", "--actor", "model-reviewer",
                 "--tool", "model-test", "--evidence-ref", "verification:" + failed["verification_id"])
        packet = self.cli("investigate", identifier, "--limit", "1")
        self.context(packet)
        self.assertTrue(packet["has_more"])
        continuation = packet["continuation"]
        self.assertEqual(continuation["project_root"], str(self.project))
        self.assertEqual(continuation["incident_id"], identifier)
        self.assertEqual(continuation["limit"], 1)
        following = self.command(packet["next_action"], readonly=True)
        self.context(following)
        self.assertEqual(following["incident"]["incident_id"], identifier)
        self.assertGreater(following["events"][0]["sequence"], packet["events"][-1]["sequence"])
        self.assertEqual(following["events"][0]["event_type"], "note")
        self.assertEqual(len(following["events"]), 1)
        text = self.cli("investigate", identifier, "--limit", "1", text=True)
        self.assertEqual(json.loads(text)["next_action"], packet["next_action"])

    def test_s05_unknown_and_missing_database_keep_requested_context_without_initialization(self):
        missing = self.root / "missing owner's project"
        missing.mkdir()
        for project, verified in ((self.project, True), (missing, False)):
            for command in (("status", "unknown"), ("preflight", "unknown", "--cached"),
                            ("invocation", "select", "unknown", "--cached")):
                with self.subTest(project=project, command=command):
                    value = self.cli(*command, expected=3, project=project)
                    status = value.get("status", value)
                    self.context(status, verified=verified, project=project)
                    if "approval" in status:
                        self.assertNotEqual(status["approval"]["state"], "valid")
                        action = status["recommended_next_action"]
                        if action["argv"]:
                            self.command(action["command"], expected=3, project=project)
                    text = self.cli(*command, expected=3, project=project, text=True)
                    self.assertIn(str(project), text)
                    self.assertFalse((missing / ".skills-auditor-local").exists())

    def test_s05_missing_candidate_recommendation_is_a_template_not_executable_shell(self):
        receipt = self.install()
        path = self.project / "uninstall.json"
        plan = self.cli("plan", "uninstall", "--installation-id", receipt["installation_id"], "--plan-out", path)
        self.expect_effect(plan)
        self.cli("apply", path, "--approve-plan-id", plan["plan_id"])
        status = self.cli("status", receipt["installation_id"], expected=3)
        self.context(status)
        recommendation = status["recommended_next_action"]
        self.assertIsNone(recommendation["argv"])
        self.assertEqual(set(recommendation["required_inputs"]), {"source", "target"})
        self.assertIn("--project-root", shlex.split(recommendation["command"]))
        self.assertFalse(self.target.is_symlink())

    def crash(self, kind, plan_path, boundary):
        script = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.retention import apply_retention
manager = Manager(sys.argv[1], create=False)
with open(sys.argv[3]) as handle:
    plan = json.load(handle)
def checkpoint(name, value):
    if name == sys.argv[4]:
        os._exit(73)
if sys.argv[2] == 'transaction':
    manager.apply(plan, approve_plan_id=plan['plan_id'], checkpoint=checkpoint)
elif sys.argv[2] == 'batch':
    BatchManager(manager).apply(plan, approve_plan_id=plan['plan_id'], checkpoint=checkpoint)
else:
    apply_retention(manager, plan, approve_plan_id=plan['plan_id'], checkpoint=checkpoint)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.project), kind, str(plan_path), boundary],
                                cwd=self.outside, env=self.environment, capture_output=True, text=True, timeout=30)
        self.after_action()
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)

    def pending_page(self, *arguments):
        before = self.oracle.check()
        value = self.cli("list", "--pending", *arguments)
        self.assertEqual(self.oracle.check(), before, "Pending discovery must not write records or events")
        self.context(value)
        self.assertEqual(value["schema_version"], "skills-auditor-lifecycle-pending-list/v1")
        for entry in value["pending"]:
            self.assertEqual(shlex.split(entry["inspection"]["command"]), entry["inspection"]["argv"])
            inspected = self.command(entry["inspection"]["command"], readonly=True)
            self.assertEqual(inspected["plan"]["plan_id"], entry["plan_id"])
            self.assertEqual(inspected["state"], entry["state"])
        self.assertEqual(self.oracle.check(), before, "Inspecting emitted recovery entries must be read-only")
        return value

    def test_s06_generated_core_id_is_discoverable_after_death_before_installation_exists(self):
        path = self.project / "install.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.oracle.watch_tree(plan["version"]["snapshot"]["path"], None)
        self.crash("transaction", path, "transaction:prepared")
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.manager.list_installations(), [])
        page = self.pending_page()
        self.assertEqual(len(page["pending"]), 1)
        entry = page["pending"][0]
        self.assertEqual(entry["kind"], "transaction")
        self.assertEqual(entry["parents"], [])
        self.assertEqual(entry["plan_id"], plan["plan_id"])
        self.oracle.expect_pending("transaction", entry["id"])
        for options in ((), ("--transaction-id", "different-id")):
            failed = self.cli("apply", path, "--approve-plan-id", plan["plan_id"], *options, expected=3)
            self.assertEqual(failed["details"]["transaction_id"], entry["id"])
        replanned_path = self.project / "fresh install.json"
        replanned = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", replanned_path)
        blocked = self.cli("apply", replanned_path, "--approve-plan-id", replanned["plan_id"], expected=3)
        self.assertEqual(blocked["details"]["transaction_id"], entry["id"])
        self.assertEqual([row["id"] for row in self.manager.repository.list("transaction")], [entry["id"]])
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.expect_effect(plan)
        self.cli("recover", entry["id"], "--mode", "resume", "--approve-plan-id", plan["plan_id"])
        self.oracle.expect_installation(plan["installation_id"], state="active", authorization="valid", target=self.target)
        self.assertEqual(self.pending_page()["pending"], [])

    def test_s06_pending_batch_pages_preserve_parent_child_relationships_then_resume(self):
        child = self.project / "child.json"
        self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", child)
        path = self.project / "batch.json"
        plan = self.cli("batch", "plan", child, "--plan-out", path)
        self.oracle.watch_tree(plan["children"][0]["plan"]["version"]["snapshot"]["path"], None)
        self.crash("batch", path, "batch:child:0:transaction:prepared")
        first = self.pending_page("--limit", "1")
        self.assertTrue(first["has_more"])
        following = self.pending_page("--limit", "1", "--after-kind", first["continuation"]["after_kind"],
                                      "--after-id", first["continuation"]["after_id"])
        entries = first["pending"] + following["pending"]
        self.assertEqual([item["kind"] for item in entries], ["batch", "transaction"])
        parent, child = entries
        self.assertEqual(parent["children"], [{"kind": "transaction", "id": child["id"]}])
        self.assertEqual(child["parents"], [{"kind": "batch", "id": parent["id"]}])
        self.assertFalse(following["has_more"])
        before = self.oracle.check()["records"]
        for options in ((), ("--batch-id", "different-parent")):
            blocked = self.cli("batch", "apply", path, "--approve-plan-id", plan["plan_id"], *options, expected=3)
            self.assertEqual(blocked["details"].get("batch_id"), parent["id"], blocked)
        replanned_path = self.project / "fresh batch.json"
        replanned = self.cli("batch", "plan", self.project / "child.json", "--plan-out", replanned_path)
        blocked = self.cli("batch", "apply", replanned_path, "--approve-plan-id", replanned["plan_id"], expected=3)
        self.assertEqual(blocked["details"].get("batch_id"), parent["id"], blocked)
        after = self.oracle.check()["records"]
        for kind in ("batch", "transaction", "receipt", "batch-receipt", "installation"):
            self.assertEqual(after.get(kind), before.get(kind), "Conflicting admission must not supersede pending intent")
        self.expect_effect(plan)
        self.cli("batch", "resume", parent["id"], "--approve-plan-id", plan["plan_id"])
        self.assertEqual(self.pending_page()["pending"], [])
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def retention_intent(self):
        inspected = inspect_source(self.source)
        self.oracle.watch_tree(self.manager.store_root / inspected["snapshot_tree_sha256"] / "tree", self.expected_snapshot)
        materialize(self.source, self.manager.store_root, inspected["source_tree_sha256"], inspected["snapshot_tree_sha256"])
        self.after_action()
        path = self.project / "collect.json"
        plan = self.cli("retention", "plan", "collect", "--plan-out", path)
        self.oracle.watch_tree(Path(plan["objects"][0]["quarantine_path"]) / "tree", None)
        self.crash("retention-transaction", path, "retention:prepared")
        return path, plan

    def test_s06_generated_retention_id_is_discoverable_then_explicitly_resumable(self):
        _, plan = self.retention_intent()
        page = self.pending_page()
        self.assertEqual(len(page["pending"]), 1)
        entry = page["pending"][0]
        self.assertEqual(entry["kind"], "retention-transaction")
        self.assertEqual(entry["plan_id"], plan["plan_id"])
        obj = plan["objects"][0]
        self.oracle.watch_tree(self.manager.store_root / obj["name"] / "tree", None)
        self.oracle.watch_tree(Path(obj["quarantine_path"]) / "tree", self.expected_snapshot)
        self.cli("retention", "recover", entry["id"], "--mode", "resume", "--approve-plan-id", plan["plan_id"])
        self.assertEqual(self.pending_page()["pending"], [])
        self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).is_dir())

    def test_s06_retention_new_plan_and_new_ids_report_original_blocking_intent(self):
        path, plan = self.retention_intent()
        pending = self.oracle.pending()
        identifier = pending[0]["id"]
        for command in (("retention", "plan", "collect"),
                        ("retention", "apply", path, "--approve-plan-id", plan["plan_id"]),
                        ("retention", "apply", path, "--approve-plan-id", plan["plan_id"], "--transaction-id", "different-id")):
            with self.subTest(command=command):
                failed = self.cli(*command, expected=3)
                self.assertEqual(failed["code"], "retention_recovery_required")
                self.assertEqual(failed["details"].get("transaction_id"), identifier)
                text = self.cli(*command, expected=3, text=True)
                hint = next(line[len("Next (read-only): "):] for line in text.splitlines() if line.startswith("Next (read-only): "))
                self.assertEqual(self.command(hint, readonly=True)["transaction_id"], identifier)
        self.assertEqual(self.oracle.pending(), pending)
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])

    def test_s06_pending_bounds_missing_store_and_terminal_exclusion(self):
        self.install()
        self.assertEqual(self.pending_page()["pending"], [])
        for options in (("--limit", "0"), ("--limit", "101"), ("--after-kind", "transaction"),
                        ("--after-id", "identifier"), ("--after-kind", "foreign", "--after-id", "identifier")):
            self.cli("list", "--pending", *options, expected=2)
        missing = self.root / "missing pending owner"
        missing.mkdir()
        error = self.cli("list", "--pending", project=missing, expected=3)
        self.assertEqual(error["code"], "repository_missing")
        self.context(error, project=missing, verified=False)
        self.assertFalse((missing / ".skills-auditor-local").exists())

    def test_s06_candidate_retarget_does_not_poison_history_or_other_pending_discovery(self):
        from skills_auditor.lifecycle.pending import list_pending
        receipt = self.install()
        original = self.source.with_name("original candidate")
        self.source.rename(original)
        self.oracle.watch_tree(original, self.candidate_before)
        replacement = self.project / "replacement candidate"
        replacement.mkdir()
        (replacement / "SKILL.md").write_text("# Different candidate\n")
        (replacement / "payload").write_text("H2")
        self.oracle.watch_tree(replacement, tree_state(replacement))
        self.source.symlink_to(replacement, target_is_directory=True)
        self.candidate_before = tree_state(self.source)
        before = self.oracle.check()
        self.assertEqual(list_pending(self.manager)["pending"], [])
        self.after_action()
        self.assertEqual(self.oracle.check(), before)
        # A separate, genuinely unfinished intent remains discoverable even
        # though a completed plan's source spelling now resolves elsewhere.
        path = self.project / "other install.json"
        other_target = self.project / "other host"
        self.oracle.watch_pointer(other_target, None)
        plan = self.cli("plan", "install", "--source", original, "--target", other_target, "--plan-out", path)
        self.crash("transaction", path, "transaction:prepared")
        page = self.pending_page()
        self.assertEqual([item["plan_id"] for item in page["pending"]], [plan["plan_id"]])
        self.assertEqual(self.cli("inspect", "receipt", receipt["receipt_id"]), receipt)
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_repository_cursor_bounds_and_pending_reader_do_not_load_full_record_lists(self):
        repository = self.manager.repository
        for identifier in ("c", "a", "b"):
            repository.put("model-fixture", identifier, {"value": identifier})
            self.after_action()
        self.assertEqual([row["id"] for row in repository.page("model-fixture", limit=2)], ["a", "b"])
        self.assertEqual([row["id"] for row in repository.page("model-fixture", after_id="b", limit=2)], ["c"])
        for values in ({"limit": 0}, {"limit": True}, {"limit": 1001}, {"after_id": ""}, {"after_id": []}):
            with self.subTest(values=values), self.assertRaises(Exception) as caught:
                repository.page("model-fixture", **values)
            self.assertEqual(caught.exception.code, "invalid_record")
        from skills_auditor.lifecycle.pending import list_pending
        with patch.object(repository, "list", side_effect=AssertionError("unbounded record fetch")):
            self.assertEqual(list_pending(self.manager)["pending"], [])
        self.after_action()

    def test_s05_invalid_or_unresolvable_project_context_fails_once_before_repository_initialization(self):
        from skills_auditor.lifecycle import cli
        for project, exception in ((str(self.project) + "\ninvalid", None), (str(self.project), OSError("cannot resolve")),
                                   (str(self.project), RuntimeError("symlink loop"))):
            with self.subTest(project=project, exception=exception), patch.object(cli, "Manager") as manager:
                output = io.StringIO()
                with redirect_stdout(output):
                    if exception:
                        with patch("skills_auditor.lifecycle.context.Path.resolve", side_effect=exception):
                            code = cli.main(["--project-root", project, "--format", "json", "plan", "install",
                                             "--source", str(self.source), "--target", str(self.target)])
                    else:
                        code = cli.main(["--project-root", project, "--format", "json", "plan", "install",
                                         "--source", str(self.source), "--target", str(self.target)])
                value = json.loads(output.getvalue())
                self.assertEqual(code, 2)
                self.assertEqual(value["code"], "invalid_context")
                self.assertIs(value["context_verified"], False)
                manager.assert_not_called()
                self.after_action()

    def test_s05_navigation_accepts_path_objects_but_rejects_control_character_inputs(self):
        from skills_auditor.lifecycle.context import action
        from skills_auditor.lifecycle.common import LifecycleError
        self.assertEqual(action(self.project, ["recover", "--mode", "inspect", "--", "-id"])["argv"][3], str(self.project))
        for root, arguments in ((str(self.project) + "\x00", ["list"]), (self.project, ["inspect", "receipt", "bad\nid"])):
            with self.subTest(root=root, arguments=arguments), self.assertRaises(LifecycleError) as caught:
                action(root, arguments)
            self.assertEqual(caught.exception.code, "invalid_context")

    def test_s05_open_manager_refuses_owner_retarget_in_every_navigation_consumer(self):
        from skills_auditor.lifecycle.common import LifecycleError
        from skills_auditor.lifecycle.status import read_status, preflight
        from skills_auditor.lifecycle.pending import list_pending
        from skills_auditor.lifecycle.incidents import investigate
        from skills_auditor.lifecycle.invocation import select
        receipt = self.install()
        raw = os.readlink(self.target)
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        self.cli("verify", receipt["installation_id"], expected=3)
        incident = self.cli("incidents")["incidents"][0]["incident_id"]
        self.target.symlink_to(raw)
        self.oracle.watch_pointer(self.target, raw)
        renewed_path = self.project / "renew.json"
        plan = self.cli("plan", "renew", "--installation-id", receipt["installation_id"], "--plan-out", renewed_path)
        self.cli("apply", renewed_path, "--approve-plan-id", plan["plan_id"])
        self.cli("verify", receipt["installation_id"])
        owner = self.project
        moved = owner.with_name("moved original project")
        foreign = owner.with_name("another legitimate project")
        foreign.mkdir()
        other = Manager(foreign)
        other.repository.close()
        owner.rename(moved)
        owner.symlink_to(foreign, target_is_directory=True)
        relative_db = Path(".skills-auditor-local/lifecycle/state.sqlite3")
        before = {(root / relative_db): (root / relative_db).read_bytes() for root in (moved, foreign)}
        consumers = (lambda: read_status(self.manager, receipt["installation_id"]),
                     lambda: preflight(self.manager, receipt["installation_id"], refresh=True),
                     lambda: list_pending(self.manager), lambda: investigate(self.manager, incident),
                     lambda: select(self.manager, receipt["installation_id"]))
        try:
            for consumer in consumers:
                with self.assertRaises(LifecycleError) as caught:
                    consumer()
                self.assertEqual(caught.exception.code, "project_context_changed")
                self.assertEqual(caught.exception.details["project_root"], str(owner))
                self.assertIs(caught.exception.details["context_verified"], False)
                from skills_auditor.lifecycle.cli import _render
                text = _render({**caught.exception.to_dict(), **caught.exception.details}, project_root=str(owner))
                self.assertNotIn("Next", text)
                for path, contents in before.items():
                    self.assertEqual(path.read_bytes(), contents, "Context refusal must not write either database")
                self.assertEqual(tree_state(moved / "candidate"), self.candidate_before)
            from skills_auditor.lifecycle import cli
            with patch.object(cli, "Manager", return_value=self.manager), redirect_stdout(io.StringIO()) as output:
                code = cli.main(["--project-root", str(owner), "--format", "json", "status", receipt["installation_id"]])
            error = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(error["code"], "project_context_changed")
            self.assertEqual(error["project_root"], str(owner))
            self.assertIs(error["context_verified"], False)
            self.assertNotIn("Next", cli._render(error, project_root=str(owner)))
        finally:
            owner.unlink()
            moved.rename(owner)
        self.after_action()

    def test_s05_owner_is_rechecked_after_status_reads_before_export(self):
        from skills_auditor.lifecycle import status
        from skills_auditor.lifecycle.common import LifecycleError
        receipt = self.install()
        original_read = status._read_status
        owner = self.project
        moved = owner.with_name("moved during read")
        foreign = owner.with_name("foreign during read")
        foreign.mkdir()
        other = Manager(foreign)
        other.repository.close()

        def read_then_retarget(*arguments, **options):
            result = original_read(*arguments, **options)
            owner.rename(moved)
            owner.symlink_to(foreign, target_is_directory=True)
            return result

        try:
            with patch.object(status, "_read_status", side_effect=read_then_retarget), self.assertRaises(LifecycleError) as caught:
                status.read_status(self.manager, receipt["installation_id"])
            self.assertEqual(caught.exception.code, "project_context_changed")
        finally:
            owner.unlink()
            moved.rename(owner)
        self.after_action()

    def test_s05_owner_identity_read_failures_are_unverified_and_do_not_refresh(self):
        from skills_auditor.lifecycle.common import LifecycleError
        from skills_auditor.lifecycle.status import preflight
        receipt = self.install()
        for error in (OSError("owner unreadable"), RuntimeError("owner loop")):
            with self.subTest(error=error), patch.object(self.manager, "_state_safety", side_effect=error), \
                    patch.object(self.manager, "verify", side_effect=AssertionError("unverified owner refreshed")):
                with self.assertRaises(LifecycleError) as caught:
                    preflight(self.manager, receipt["installation_id"], refresh=True)
                self.assertEqual(caught.exception.code, "project_context_changed")
                self.assertIs(caught.exception.details["context_verified"], False)
        self.after_action()

    def test_s06_a_forged_terminal_projection_cannot_hide_a_pending_core_intent(self):
        from skills_auditor.lifecycle.pending import list_pending
        from skills_auditor.lifecycle.common import LifecycleError
        path = self.project / "unfinished.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.oracle.watch_tree(plan["version"]["snapshot"]["path"], None)
        self.crash("transaction", path, "transaction:prepared")
        row = self.manager.repository.list("transaction")[0]
        for state, close_steps in (("completed", False), ("completed", True), ("compensated", False), ("compensated", True)):
            changed = copy.deepcopy(row["data"])
            changed.update(state=state, receipt_id="missing-receipt" if state == "completed" else None)
            if close_steps:
                for step in changed["steps"]:
                    step["state"] = state
            current = self.manager.repository.get("transaction", row["id"])
            self.manager.repository.put("transaction", row["id"], changed, expected_revision=current["revision"])
            try:
                with self.assertRaises(LifecycleError) as caught:
                    list_pending(self.manager)
                self.assertEqual(caught.exception.code, "pending_corrupt")
                from skills_auditor.lifecycle import cli
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli.main(["--project-root", str(self.project), "list", "--pending"])
                self.assertEqual(code, 3)
                self.assertIn("transaction_id=" + row["id"], output.getvalue())
                hint = next(line[len("Next (read-only): "):] for line in output.getvalue().splitlines()
                            if line.startswith("Next (read-only): "))
            finally:
                current = self.manager.repository.get("transaction", row["id"])
                self.manager.repository.put("transaction", row["id"], row["data"], expected_revision=current["revision"])
            self.assertEqual(self.command(hint, readonly=True)["transaction_id"], row["id"])
        self.after_action()
        self.cli("recover", row["id"], "--mode", "compensate", "--approve-plan-id", plan["plan_id"], "--actor", "another-local-actor")
        self.assertEqual(self.pending_page()["pending"], [])

    def test_s06_corrupt_retention_discovery_keeps_a_scoped_read_only_recovery_entry(self):
        from skills_auditor.lifecycle import cli
        self.retention_intent()
        row = self.manager.repository.list("retention-transaction")[0]
        changed = copy.deepcopy(row["data"])
        changed.update(state="completed", receipt_id="missing-receipt", completion_event_sequence=1)
        self.manager.repository.put("retention-transaction", row["id"], changed, expected_revision=row["revision"])
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["--project-root", str(self.project), "list", "--pending"])
            self.assertEqual(code, 3)
            self.assertIn("pending_corrupt", output.getvalue())
            self.assertIn("transaction_id=" + row["id"], output.getvalue())
            hint = next(line[len("Next (read-only): "):] for line in output.getvalue().splitlines()
                        if line.startswith("Next (read-only): "))
            self.assertIn("retention", shlex.split(hint))
        finally:
            current = self.manager.repository.get("retention-transaction", row["id"])
            self.manager.repository.put("retention-transaction", row["id"], row["data"], expected_revision=current["revision"])
        self.assertEqual(self.command(hint, readonly=True)["transaction_id"], row["id"])
        self.after_action()

    def test_generated_navigation_and_pending_schemas_are_additive_and_bounded(self):
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        schema_root = _CHECKOUT / "skills_auditor/schemas"
        schemas = {kind: json.loads((schema_root / ("lifecycle-" + kind + "-v1.schema.json")).read_text())
                   for kind in ("status", "invocation", "investigation", "core")}
        registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas.values())
        validators = {kind: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()) for kind, schema in schemas.items()}
        receipt = self.install()
        identifier = receipt["installation_id"]
        status = self.cli("status", identifier)
        selection = self.cli("invocation", "select", identifier, "--cached")
        for kind, value in (("status", status), ("invocation", selection)):
            validators[kind].validate(value)
            for changes in ({"context_verified": "yes"}, {"project_root": []}):
                self.assertFalse(validators[kind].is_valid({**value, **changes}))
            old = copy.deepcopy(value)
            old.pop("project_root")
            old.pop("context_verified")
            nested = old.get("status", old)
            nested.pop("project_root", None)
            nested.pop("context_verified", None)
            nested["recommended_next_action"].pop("argv")
            nested["recommended_next_action"].pop("required_inputs")
            validators[kind].validate(old)
        path = self.project / "other.json"
        other_target = self.project / "other host"
        self.oracle.watch_pointer(other_target, None)
        self.cli("plan", "install", "--source", self.source, "--target", other_target, "--plan-out", path)
        self.crash("transaction", path, "transaction:prepared")
        pending = self.pending_page()
        validators["core"].validate(pending)
        for changes in ({"context_verified": False}, {"limit": 101}, {"has_more": True}, {"pending": [{}]}):
            self.assertFalse(validators["core"].is_valid({**pending, **changes}))
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        self.after_action()
        self.cli("verify", identifier, expected=3)
        incident = self.cli("incidents")["incidents"][0]["incident_id"]
        self.cli("append-note", incident, "--text", "Next page", "--actor", "schema-fixture", "--tool", "test")
        packet = self.cli("investigate", incident, "--limit", "1")
        validators["investigation"].validate(packet)
        old = copy.deepcopy(packet)
        old.pop("project_root")
        old.pop("context_verified")
        for field in ("project_root", "incident_id", "limit"):
            old["continuation"].pop(field)
        validators["investigation"].validate(old)
        for changes in ({"project_root": []}, {"limit": 1000}, {"incident_id": False}):
            self.assertFalse(validators["investigation"].is_valid({**packet, "continuation": {**packet["continuation"], **changes}}))


if __name__ == "__main__":
    unittest.main()
