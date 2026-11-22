import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace import catalog
from skills_auditor.workspace.cli import main
from skills_auditor.workspace.common import PREFIX, seal


def example():
    return {"schema": "skills-auditor-catalog/v1", "workspace": "example",
            "items": {"native/nested": {"disposition": "held"}},
            "attestation": {"names": 1, "review_sha256": "a" * 64, "entry_validator": "unit"},
            "release": seal({"schema_version": PREFIX + "release/v1", "sources": {}}, "release_id")}


class CatalogTest(unittest.TestCase):
    def test_client_and_pinned_resolution(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            profile = Path(d) / "profile"
            argv = ["catalog", "--profile", str(profile)]
            self.assertEqual(main(argv + ["enroll", "--origin", "https://example.test", "--workspace", "example"]), 0)
            self.assertNotEqual(main(argv + ["enroll", "--origin", "https://example.test", "--workspace", "example"]), 0)
            binding = catalog.profile_at(profile)
            wire = catalog.encode(example())
            pin = catalog.sha(wire)
            root = "example/releases/" + pin
            network = {"me": b'{"workspace":"example"}', "example/current": json.dumps({"generation": 3, "digest": pin}).encode(),
                       root: json.dumps({"size": len(wire), "parts": 1}).encode(), root + "/0": wire}
            with patch.object(catalog, "fetch", side_effect=lambda p, route, *args: network[route]):
                self.assertEqual(main(argv + ["discover"]), 0)
                out = Path(d) / "catalog.json"
                self.assertEqual(main(argv + ["resolve", "--out", str(out)]), 0)
                self.assertEqual(main(argv + ["lint", str(out)]), 0)
                self.assertEqual(json.loads(out.read_text()), example())
                self.assertEqual(catalog.resolve(binding, pin)[1]["digest"], pin)
                network[root + "/0"] = b"X" * len(wire)
                with self.assertRaisesRegex(LifecycleError, "differs from pin"):
                    catalog.resolve(binding, pin)
                network["me"] = b'{"workspace":"wrong"}'
                with self.assertRaisesRegex(LifecycleError, "binding"):
                    catalog.discover(binding)
            os.chmod(profile, 0o644)
            with self.assertRaises(LifecycleError):
                catalog.profile_at(profile)

    def test_lint_and_enrollment_failures(self):
        for field, value in [("schema", "old"), ("items", {}), ("items", {"a": {"disposition": "available"}}),
                             ("items", {"a": {"disposition": "available", "source": "missing"}})]:
            with self.subTest(field=field, value=value), self.assertRaises(LifecycleError):
                catalog.lint({**example(), field: value})
        for endpoint in ("http://example.test", "https://u@example.test", "https://example.test/path"):
            with self.assertRaises(LifecycleError):
                catalog.origin(endpoint)

    def test_http_errors_suppress_remote_response_and_token(self):
        binding = {"origin": "https://example.test", "token": "f" * 64}
        with patch.object(catalog, "build_opener") as client:
            client.return_value.open.side_effect = HTTPError("", 403, "denied", {}, None)
            with self.assertRaises(LifecycleError) as failure:
                catalog.fetch(binding, "me")
            self.assertEqual(failure.exception.details, {"http_status": 403})
            self.assertNotIn(binding["token"], str(failure.exception))
