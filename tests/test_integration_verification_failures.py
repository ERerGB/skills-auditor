"""Filesystem verification failures must remain actionable approval failures."""

from __future__ import annotations

import errno
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor import integration
from skills_auditor.cli import main


class TestVerificationFilesystemFailures(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-verify-failures-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "source"
        self.target = self.project / "target"
        for name in ("alpha", "beta"):
            skill = self.source / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: verification fixture\n---\n\nbody\n",
                encoding="utf-8",
            )
        plan = integration.build_integration_plan(
            integration.IntegrationSpec(
                project_root=self.project,
                sources=(self.source,),
                targets=(integration.IntegrationTarget("test", root=self.target),),
            )
        )
        self.receipt, self.receipt_path = integration.apply_integration_plan(plan)

    @contextmanager
    def filesystem_fault(self, operation: str, paths: set[Path]):
        """Fail only the selected temporary entries, delegating all other I/O."""
        if operation in {"is_symlink", "resolve", "resolve_actual", "is_dir"}:
            owner = Path
            attribute = "resolve" if operation == "resolve_actual" else operation
        else:
            owner = integration
            attribute = "directory_tree_hash"
        original = getattr(owner, attribute)
        selected_calls: dict[Path, int] = {}

        def fail_selected(path, *args, **kwargs):
            if path in paths:
                selected_calls[path] = selected_calls.get(path, 0) + 1
                if operation == "resolve_actual" and selected_calls[path] == 1:
                    return original(path, *args, **kwargs)
                if operation == "hash_value_error":
                    raise ValueError("source contains an unsupported filesystem entry")
                raise PermissionError(errno.EACCES, "verification probe denied", str(path))
            return original(path, *args, **kwargs)

        with patch.object(owner, attribute, autospec=True, side_effect=fail_selected) as mocked:
            yield mocked

    def assert_invalidated(self, verification: dict, reason_codes: list[str]) -> None:
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["approval"],
            {
                "state": "invalidated",
                "requires_reapproval": True,
                "reason_codes": reason_codes,
            },
        )

    def fault_cases(self):
        return (
            ("is_symlink", self.target / "alpha", "target_link"),
            ("resolve", self.target / "alpha", "target_link"),
            ("resolve_actual", self.target / "alpha", "target_link"),
            ("is_dir", self.source / "alpha", "source_tree"),
            ("hash_os_error", self.source / "alpha", "source_tree"),
            ("hash_value_error", self.source / "alpha", "source_tree"),
        )

    def test_successful_verification_is_the_control_for_fault_probes(self) -> None:
        verification = integration.verify_receipt(self.receipt)
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(
            verification["approval"],
            {"state": "valid", "requires_reapproval": False, "reason_codes": []},
        )
        self.assertEqual(verification["summary"], {"checks": 4, "passed": 4, "failed": 0})

    def test_filesystem_errors_invalidate_instead_of_escaping_verification(self) -> None:
        for operation, path, reason_code in self.fault_cases():
            with self.subTest(operation=operation):
                with self.filesystem_fault(operation, {path}) as fault:
                    verification = integration.verify_receipt(self.receipt)
                    self.assertTrue(fault.called)
                self.assert_invalidated(verification, [reason_code])
                failed = [check for check in verification["checks"] if not check["ok"]]
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0]["code"], reason_code)
                self.assertEqual(failed[0]["name"], "alpha")
                self.assertIsNone(failed[0]["actual"])
                self.assertEqual(
                    verification["summary"], {"checks": 4, "passed": 3, "failed": 1}
                )
                self.assertEqual(integration.verify_receipt(self.receipt)["status"], "passed")

    def test_repeated_io_failures_deduplicate_reasons_in_check_order(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(
                self.filesystem_fault("resolve", {self.target / "alpha", self.target / "beta"})
            )
            stack.enter_context(
                self.filesystem_fault(
                    "hash_os_error", {self.source / "alpha", self.source / "beta"}
                )
            )
            verification = integration.verify_receipt(self.receipt)
        self.assert_invalidated(verification, ["target_link", "source_tree"])
        self.assertEqual(
            [check["code"] for check in verification["checks"]],
            ["target_link", "source_tree", "target_link", "source_tree"],
        )
        self.assertTrue(all(check["actual"] is None for check in verification["checks"]))
        self.assertEqual(verification["summary"], {"checks": 4, "passed": 0, "failed": 4})

    def test_reason_order_follows_first_failure_even_when_source_fails_first(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(self.filesystem_fault("hash_os_error", {self.source / "alpha"}))
            stack.enter_context(self.filesystem_fault("resolve", {self.target / "beta"}))
            verification = integration.verify_receipt(self.receipt)
        self.assert_invalidated(verification, ["source_tree", "target_link"])
        self.assertEqual(
            [(check["name"], check["code"]) for check in verification["checks"] if not check["ok"]],
            [("alpha", "source_tree"), ("beta", "target_link")],
        )
        self.assertEqual(verification["summary"], {"checks": 4, "passed": 2, "failed": 2})

    def test_cli_reports_filesystem_failures_in_json_and_human_output(self) -> None:
        for operation, path, reason_code in self.fault_cases():
            for output_format in ("json", "human"):
                with self.subTest(operation=operation, output_format=output_format):
                    stdout, stderr = StringIO(), StringIO()
                    argv = ["skills-audit", "verify", str(self.receipt_path)]
                    if output_format == "json":
                        argv.extend(["--format", "json"])
                    with self.filesystem_fault(operation, {path}), patch(
                        "sys.argv", argv
                    ), redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main()
                    self.assertEqual(exit_code, 3)
                    self.assertEqual(stderr.getvalue(), "")
                    output = stdout.getvalue()
                    if output_format == "json":
                        verification = json.loads(output)
                        self.assert_invalidated(verification, [reason_code])
                        failed = [check for check in verification["checks"] if not check["ok"]]
                        self.assertEqual(len(failed), 1)
                        self.assertEqual(failed[0]["code"], reason_code)
                        self.assertIsNone(failed[0]["actual"])
                    else:
                        self.assertIn("status: failed", output)
                        self.assertIn("approval: invalidated | re-approval required: yes", output)
                        self.assertIn(f"FAIL {reason_code}:", output)
                        self.assertIn(f"approval reasons: {reason_code}", output)
                        self.assertIn("Run skills-audit integrate", output)
                        self.assertIn("Review the emitted plan", output)
                        self.assertIn("Explicitly re-approve", output)


if __name__ == "__main__":
    unittest.main()
