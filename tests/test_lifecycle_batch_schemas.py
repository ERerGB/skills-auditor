"""The batch contract validates real recovery records and explicit inverses."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager


class TestLifecycleBatchSchemas(unittest.TestCase):
    def setUp(self):
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from skills_auditor.lifecycle.batch import BatchManager
        temporary = tempfile.TemporaryDirectory(prefix="batch-schema-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)
        self.batch = BatchManager(self.manager)
        self.plans = []
        for index in range(2):
            source = self.root / ("source-" + str(index))
            source.mkdir()
            (source / "SKILL.md").write_text("# Candidate " + str(index), encoding="utf-8")
            self.plans.append(self.manager.plan("install", source=source, target=self.root / ("target-" + str(index))))
        schemas = Path(__file__).parents[1] / "skills_auditor/schemas"
        bundle = json.loads((schemas / "lifecycle-batch-v1.schema.json").read_text())
        registry = Registry()
        for name in ("lifecycle-core-v1.schema.json", "lifecycle-status-v1.schema.json"):
            document = json.loads((schemas / name).read_text())
            registry = registry.with_resource(document["$id"], Resource.from_contents(document))
        Draft202012Validator.check_schema(bundle)
        self.validator = Draft202012Validator(bundle, registry=registry, format_checker=FormatChecker())

    def check(self, value):
        errors = list(self.validator.iter_errors(value))
        if errors:
            self.fail(str(errors[0])[:1200])

    def test_completed_batch_and_explicit_inverse_match_contract(self):
        plan = self.batch.plan(self.plans)
        self.check(plan)
        result = self.batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="schema-batch")
        self.check(result)
        self.check(self.batch.inspect("schema-batch"))
        inverse = self.batch.plan_compensation("schema-batch")
        self.check(inverse)
        compensated = self.batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="schema-inverse")
        self.check(compensated)
        original = self.batch.inspect("schema-batch")
        self.check(original)
        self.assertEqual(original["state"], "compensated")

    def test_journal_boundaries_never_masquerade_as_completed_receipts(self):
        plan = self.batch.plan(self.plans)
        captured = []

        def inspect_and_stop(name, transaction):
            self.check(transaction)
            captured.append(name)
            raise OSError("schema proof stops at first durable batch boundary")

        with self.assertRaises(LifecycleError):
            self.batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="schema-interrupted", checkpoint=inspect_and_stop)
        self.assertTrue(captured)
        transaction = self.batch.inspect("schema-interrupted")
        self.check(transaction)
        self.assertIsNone(transaction["receipt_id"])
        inverse = self.batch.plan_compensation("schema-interrupted")
        self.check(inverse)
        result = self.batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="schema-cancel")
        self.check(result)
        self.check(self.batch.inspect("schema-interrupted"))

    def test_wrong_shapes_cannot_advertise_approved_or_completed_batch(self):
        plan = self.batch.plan(self.plans)
        for changes in ({"children": []}, {"compensates_revision": True}, {"implicitly_approved": True}):
            self.assertFalse(self.validator.is_valid({**plan, **changes}))
        result = self.batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="schema-malformed")
        pending = copy.deepcopy(result)
        pending["children"][0]["state"] = "running"
        self.assertFalse(self.validator.is_valid(pending))
        self.assertFalse(self.validator.is_valid({**result, "status": "partial"}))
        transaction = self.batch.inspect("schema-malformed")
        self.assertFalse(self.validator.is_valid({**transaction, "receipt_id": None}))


if __name__ == "__main__":
    unittest.main()
