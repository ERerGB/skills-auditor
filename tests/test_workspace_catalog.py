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


class DirectoryTest(unittest.TestCase):
    def test_local_identity_and_no_implicit_native_observation(self):
        from skills_auditor.workspace import directory
        from skills_auditor.workspace.common import declaration
        from uuid import uuid4
        first, second = str(uuid4()), str(uuid4())
        doc = {"schema_version": PREFIX + "declaration/v1", "baseline": ["private"],
               "skill_ids": {"one": first, "two": second}}
        self.assertEqual(declaration(doc)["skill_ids"]["one"], first)
        with self.assertRaises(LifecycleError):
            declaration({**doc, "skill_ids": {"one": first, "two": first}})
        with self.assertRaises(LifecycleError):
            directory.skill_uuid("not-a-uuid")
        skills = [{"name": "one", "entry_name": "one", "skill_id": first, "subscribed": False},
                  {"name": "two", "entry_name": None, "skill_id": second, "subscribed": True}]
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "SKILL.md"
            md.write_text("local version")
            one = {"name": "one", "path": str(md), "enabled": True}
            private = {"name": "private", "path": str(md), "enabled": True}
            self.assertEqual(directory.summarize({"skills": skills}, [one, private]), {
                first: {"state": "present", "entry_sha256": catalog.sha(md.read_bytes())},
                second: {"state": "absent", "entry_sha256": None}})
            self.assertEqual(directory.summarize({"skills": skills}, [one, one])[first]["state"], "multiple")
            with self.assertRaises(LifecycleError):
                directory.summarize({"skills": skills}, [{**one, "enabled": "false"}])

    def test_reader_lists_registry_without_a_release_and_pins_approval(self):
        from skills_auditor.workspace import directory
        from uuid import uuid4
        pin, key = "1" * 64, str(uuid4())
        profile = {"workspace": "company", "origin": "https://example.test", "token": "2" * 64}
        row = {"name": "one", "entry_name": None, "skill_id": key, "subscribed": False,
               "declared_source_file": None, "source_file": None, "repository": None, "revision": None}
        current = {"digest": None, "workspace": "company", "environment": str(uuid4()), "skills": [row]}
        with patch.object(catalog, "fetch", return_value=json.dumps(current).encode()):
            self.assertEqual(directory.listing(profile, None), current)
            with self.assertRaises(LifecycleError):
                directory.listing(profile, pin)
        published = {**current, "digest": pin}
        saved = directory.consent(profile, "/tmp/example", published)
        with patch.object(catalog, "profile_at", return_value=profile), patch.object(directory, "listing", return_value=published):
            directory.recheck(saved)
            row["subscribed"] = True
            with self.assertRaisesRegex(LifecycleError, "subscriptions"):
                directory.recheck(saved)

    def test_account_commands_use_explicit_registration_and_current_reader(self):
        from types import SimpleNamespace
        from uuid import uuid4
        from skills_auditor.workspace import directory
        key = str(uuid4())
        profile = {"workspace": "orchard"}
        row = {"name": "review", "skill_id": key, "subscribed": False}
        data = {"skills": [row], "digest": "c" * 64}
        with patch.object(catalog, "fetch", return_value=b"") as request:
            args = SimpleNamespace(catalog_command="register", skill_id=key, name="review", source_file="review/SKILL.md")
            self.assertTrue(directory.run(args, profile, None)["registered"])
            request.assert_called_once_with(profile, "orchard/registrations/" + key, method="PUT",
                                            body={"name": "review", "source_file": "review/SKILL.md"})
            args.source_file = "/home/private/SKILL.md"
            with self.assertRaises(LifecycleError):
                directory.run(args, profile, None)
            args.catalog_command = "unregister"
            self.assertFalse(directory.run(args, profile, None)["registered"])
            args.catalog_command = "unsubscribe"
            self.assertFalse(directory.run(args, profile, None)["subscribed"])
            request.assert_called_with(profile, "orchard/subscriptions/" + key, method="DELETE")
            with patch.object(directory, "listing", return_value=data), patch("builtins.input", return_value="1"):
                args.catalog_command, args.pin = "select", None
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(directory.run(args, profile, None)["subscribed"])
                request.assert_called_with(profile, "orchard/subscriptions/" + key, method="PUT")
            args.catalog_command, args.project = "list", "project"
            with patch.object(directory, "listing", return_value=data):
                request.return_value = b'{"observed_at":null,"observations":{}}'
                listing = directory.run(args, profile, None)
                self.assertEqual(listing["skills"][0]["native_discovery"], {"state": "unverified"})
