"""Smoke tests for an installed skills-auditor wheel.

These tests deliberately do not import ``skills_auditor`` from the test process.
The artifact runner supplies paths to a clean virtual environment so every probe
crosses the installed distribution boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be provided by the artifact test runner")
    return Path(value).resolve()


CLI = required_path("SKILLS_AUDITOR_CLI")
PYTHON = required_path("SKILLS_AUDITOR_PYTHON")
PROJECT_ROOT = required_path("SKILLS_AUDITOR_PROJECT_ROOT")
EXPECTED_VERSION = os.environ.get("SKILLS_AUDITOR_EXPECTED_VERSION", "")


class TestInstalledDistributionSmoke(unittest.TestCase):
    def run_process(self, *command: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as base:
            return subprocess.run(
                list(command),
                cwd=base,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

    def test_console_entry_point_reports_built_version(self) -> None:
        result = self.run_process(str(CLI), "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), f"skills-audit {EXPECTED_VERSION}")

    def test_module_entry_point_matches_console_entry_point(self) -> None:
        console = self.run_process(str(CLI), "--version")
        module = self.run_process(str(PYTHON), "-m", "skills_auditor", "--version")
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertEqual(module.stderr, "")
        self.assertEqual(module.stdout, console.stdout)

    def test_help_exposes_the_transactional_lifecycle(self) -> None:
        result = self.run_process(str(CLI), "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        for command in ("integrate", "apply", "verify"):
            with self.subTest(command=command):
                self.assertIn(command, result.stdout)

    def test_import_resolves_outside_the_source_checkout(self) -> None:
        probe = (
            "import json, pathlib, skills_auditor; "
            "print(json.dumps({'path': str(pathlib.Path(skills_auditor.__file__).resolve()), "
            "'version': skills_auditor.__version__}))"
        )
        result = self.run_process(str(PYTHON), "-I", "-c", probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        installed_path = Path(payload["path"])
        self.assertEqual(payload["version"], EXPECTED_VERSION)
        self.assertFalse(
            installed_path.is_relative_to(PROJECT_ROOT),
            f"artifact smoke imported the source checkout: {installed_path}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
