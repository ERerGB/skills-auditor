"""Failure contracts for native host descriptors."""

from __future__ import annotations

import unittest
from pathlib import Path

from skills_auditor.environments import NativeEnvironment, NativeEnvironmentRegistry


class TestNativeEnvironmentEdges(unittest.TestCase):
    def test_empty_roots_raise_explicit_errors(self) -> None:
        environment = NativeEnvironment("empty", (), ())
        with self.assertRaisesRegex(ValueError, "no project skill root"):
            environment.primary_project_root(Path("/project"))
        with self.assertRaisesRegex(ValueError, "no global skill root"):
            environment.primary_global_root(Path("/home"))

    def test_registry_rejects_empty_duplicate_and_unknown_keys(self) -> None:
        registry = NativeEnvironmentRegistry()
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            registry.register(NativeEnvironment(" ", ("skills",), ("skills",)))
        registry.register(NativeEnvironment("known", ("skills",), ("skills",)))
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(NativeEnvironment("known", ("other",), ("other",)))
        with self.assertRaisesRegex(ValueError, "Unknown environment.*known"):
            registry.get("missing")


if __name__ == "__main__":
    unittest.main()
