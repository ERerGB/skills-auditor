"""Exercise the real atomic receipt writer below the public apply boundary."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from skills_auditor import integration
from tests.test_integration_validation import IntegrationValidationFixture


class TestAtomicReceiptWrites(IntegrationValidationFixture):
    @contextmanager
    def writer_fault(self, destination: Path, stage: str, *, cleanup_fails=False):
        """Keep real temporary handles; fail one I/O operation on those handles."""
        destination = destination.resolve()
        real_temporary = tempfile.NamedTemporaryFile
        real_fsync = os.fsync
        real_replace = os.replace
        real_unlink = Path.unlink
        handles = []
        temporary_paths = []
        failure = OSError(f"injected {stage} failure")

        with ExitStack() as stack:
            def open_temporary(*args, **kwargs):
                handle = real_temporary(*args, **kwargs)
                handles.append(handle)
                temporary = Path(handle.name)
                temporary_paths.append(temporary)
                self.assertEqual(temporary.parent, destination.parent)
                self.assertTrue(temporary.name.startswith(f".{destination.name}."))
                if stage == "write":
                    write = handle.write

                    def partial_write_then_fail(value):
                        write(value[:1])
                        raise failure

                    stack.enter_context(patch.object(handle, "write", partial_write_then_fail))
                elif stage == "flush":
                    stack.enter_context(patch.object(handle, "flush", side_effect=failure))
                return handle

            def fail_fsync(fd):
                if any(not handle.closed and fd == handle.fileno() for handle in handles):
                    raise failure
                return real_fsync(fd)

            def fail_replace(source, target):
                if Path(source) in temporary_paths and Path(target) == destination:
                    raise failure
                return real_replace(source, target)

            def fail_cleanup(path, *args, **kwargs):
                if path in temporary_paths:
                    raise OSError("injected temporary cleanup failure")
                return real_unlink(path, *args, **kwargs)

            stack.enter_context(
                patch.object(integration.tempfile, "NamedTemporaryFile", side_effect=open_temporary)
            )
            if stage == "fsync":
                stack.enter_context(patch.object(integration.os, "fsync", side_effect=fail_fsync))
            elif stage == "replace":
                stack.enter_context(patch.object(integration.os, "replace", side_effect=fail_replace))
            if cleanup_fails:
                stack.enter_context(patch.object(Path, "unlink", fail_cleanup))
            yield handles, temporary_paths, failure

    def test_success_replaces_document_without_leaving_a_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base).resolve()
            destination = root / "receipt.json"
            destination.write_bytes(b"previous receipt bytes\n")
            value = {"message": "new receipt", "results": []}
            result = integration._atomic_write_json(destination, value)
            self.assertEqual(result, destination)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), value)
            self.assertEqual(list(root.iterdir()), [destination])

    def test_io_failures_close_and_remove_temporary_files_without_publishing(self) -> None:
        for stage in ("write", "flush", "fsync", "replace"):
            for existing in (False, True):
                with self.subTest(stage=stage, existing=existing), tempfile.TemporaryDirectory() as base:
                    root = Path(base).resolve()
                    destination = root / "receipt.json"
                    previous = b"previous receipt bytes\n"
                    if existing:
                        destination.write_bytes(previous)
                    with self.writer_fault(destination, stage) as (handles, paths, failure):
                        with self.assertRaises(OSError) as caught:
                            integration._atomic_write_json(destination, {"results": []})
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(len(handles), 1)
                    self.assertTrue(handles[0].closed)
                    self.assertFalse(paths[0].exists())
                    self.assertEqual(list(root.iterdir()), [destination] if existing else [])
                    if existing:
                        self.assertEqual(destination.read_bytes(), previous)
                    else:
                        self.assertFalse(destination.exists())

    def test_cleanup_failure_does_not_mask_original_error_or_replace_old_receipt(self) -> None:
        for stage in ("write", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as base:
                root = Path(base).resolve()
                destination = root / "receipt.json"
                previous = b"previous receipt bytes\n"
                destination.write_bytes(previous)
                with self.writer_fault(destination, stage, cleanup_fails=True) as (
                    handles, paths, failure
                ):
                    with self.assertRaises(OSError) as caught:
                        integration._atomic_write_json(destination, {"results": []})
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(handles), 1)
                self.assertTrue(handles[0].closed)
                self.assertEqual(destination.read_bytes(), previous)
                self.assertEqual(set(root.iterdir()), {destination, paths[0]})
                # Cleanup is best-effort: this is retained staging data, not a
                # published receipt. TemporaryDirectory removes it after mocks exit.
                self.assertTrue(paths[0].is_file())

    def test_noop_receipt_write_failure_preserves_previous_approved_receipt(self) -> None:
        for stage in ("write", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as base:
                project = Path(base).resolve()
                plan = self.valid_plan(project)
                receipt_path = project / "receipt.json"
                old_receipt, _ = integration.apply_integration_plan(plan, receipt_path)
                old_bytes = receipt_path.read_bytes()
                renewal = integration.build_integration_plan(
                    integration.IntegrationSpec(
                        project_root=project,
                        sources=(project / "source",),
                        targets=(integration.IntegrationTarget("test", root=project / "target"),),
                    )
                )
                self.assertEqual(renewal["targets"][0]["actions"][0]["action"], "noop")
                entry = project / "target" / "alpha"
                link_before = entry.lstat()
                raw_target = os.readlink(entry)
                source = project / "source" / "alpha" / "SKILL.md"
                source_before = source.read_bytes()
                with self.writer_fault(receipt_path, stage) as (handles, paths, failure):
                    with self.assertRaises(integration.IntegrationError) as caught:
                        integration.apply_integration_plan(renewal, receipt_path)
                self.assertEqual(caught.exception.code, "receipt_write_failed")
                self.assertEqual(caught.exception.exit_code, 3)
                self.assertIn(str(failure), str(caught.exception))
                self.assertEqual(receipt_path.read_bytes(), old_bytes)
                self.assertEqual(integration.verify_receipt(old_receipt)["status"], "passed")
                self.assertTrue(entry.is_symlink())
                self.assertEqual(entry.resolve(), source.parent)
                link_after = entry.lstat()
                for field in ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns"):
                    self.assertEqual(getattr(link_after, field), getattr(link_before, field))
                self.assertEqual(os.readlink(entry), raw_target)
                self.assertEqual(source.read_bytes(), source_before)
                self.assertTrue(handles[0].closed)
                self.assertFalse(paths[0].exists())
                self.assertEqual(set(project.iterdir()), {project / "source", project / "target", receipt_path})

    def test_failed_apply_and_failed_receipt_write_report_both_failures(self) -> None:
        for stage in ("write", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as base:
                project = Path(base).resolve()
                plan = self.valid_plan(project)
                receipt_path = project / "failed.json"
                source = project / "source" / "alpha" / "SKILL.md"
                source_before = source.read_bytes()
                with self.writer_fault(receipt_path, stage) as (handles, paths, failure), patch(
                    "skills_auditor.integration.os.symlink", side_effect=OSError("link denied")
                ):
                    with self.assertRaises(integration.IntegrationError) as caught:
                        integration.apply_integration_plan(plan, receipt_path)
                self.assertEqual(caught.exception.code, "apply_failed_without_receipt")
                self.assertEqual(caught.exception.exit_code, 3)
                self.assertIn(str(failure), str(caught.exception))
                self.assertEqual(caught.exception.details, [{"apply_error": "link denied"}])
                self.assertTrue(handles[0].closed)
                self.assertFalse(paths[0].exists())
                self.assertFalse(receipt_path.exists())
                self.assertEqual(list((project / "target").iterdir()), [])
                self.assertEqual(source.read_bytes(), source_before)
                self.assertEqual(set(project.iterdir()), {project / "source", project / "target"})


if __name__ == "__main__":
    unittest.main()
