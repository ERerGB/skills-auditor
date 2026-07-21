"""Repository-level test-layer wiring must remain an enforced contract."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


class TestCiContract(unittest.TestCase):
    def test_ci_keeps_independent_unit_smoke_e2e_and_distribution_jobs(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for job in ("unit", "smoke", "e2e", "distribution"):
            with self.subTest(job=job):
                self.assertIn(f"  {job}:\n", workflow)
        self.assertIn("python -m coverage run -m unittest discover -s tests -v", workflow)
        self.assertIn("python -m coverage report", workflow)
        self.assertIn("python scripts/run_artifact_tests.py smoke dist", workflow)
        self.assertIn("python scripts/run_artifact_tests.py e2e dist", workflow)
        self.assertIn('python-version: ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]', workflow)
        self.assertIn("os: [ubuntu-latest, macos-latest]", workflow)

    def test_coverage_and_sdist_keep_all_test_layers(self) -> None:
        project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("fail_under = 90", project)
        self.assertIn('source = ["skills_auditor"]', project)
        self.assertIn('"/smoke_tests"', project)
        self.assertIn('"/e2e_tests"', project)

        distribution_check = (PROJECT_ROOT / "scripts" / "check_distribution.py").read_text(
            encoding="utf-8"
        )
        for path in (
            "scripts/run_artifact_tests.py",
            "smoke_tests/test_installed_distribution.py",
            "e2e_tests/test_installed_cli_lifecycle.py",
        ):
            with self.subTest(path=path):
                self.assertIn(path, distribution_check)


if __name__ == "__main__":
    unittest.main()
