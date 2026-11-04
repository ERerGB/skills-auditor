"""Real stdio worker boundary using a fixture in place of native SSH transport."""
import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from skills_auditor.integration import IntegrationSpec, IntegrationTarget, apply_integration_plan, build_integration_plan
from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.providers import SSHProvider
from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceTransport(WorkspaceFixture):
    def test_worker_process_has_independent_runtime_binding_and_reproduced_package(self):
        remote=self.root/'remote'; remote.mkdir()
        home=remote/'home';home.mkdir()
        project=remote/'project';project.mkdir();self.write_declaration(project)
        shim=self.root/'ssh-fixture.py'
        checkout=Path(__file__).resolve().parents[1]
        shim.write_text('import os,sys\n'
            f'env=dict(os.environ,HOME={str(home)!r},PYTHONPATH={str(checkout)!r},PYTHONDONTWRITEBYTECODE="1")\n'
            'os.execve(sys.executable,[sys.executable,"-B","-m","skills_auditor.workspace.worker"],env)\n')
        self.registry.put('environments','remote',{'provider':'ssh','generation':'remote-v1','connection':'fixture','state_dir':str(remote/'state')})
        with patch.object(SSHProvider,'_argv',return_value=[sys.executable,str(shim)]):
            self.service.register('remote','remote',project,project/'skills-auditor.workspace.json')
            plan=self.service.plan(['one','remote'])
            self.assertEqual(self.apply(plan)['status'],'completed')
            self.assertTrue(self.service.verify(['one','remote'])['valid'])
            self.assertEqual(self.service.plan(['one','remote'])['summary']['changes'],0)
            local=(self.one/'.agents/skills/alpha').resolve()
            foreign=(project/'.agents/skills/alpha').resolve()
            self.assertNotEqual(local,foreign)
            self.assertEqual((foreign.parent.parent/'helper').read_text(),'H1')
            self.assertNotIn(str(self.source),str(foreign))
            status=self.service.status()['environments']
            self.assertNotEqual(status['local']['binding']['runtime_fingerprint'],status['remote']['binding']['runtime_fingerprint'])

    def multi_entry_legacy(self):
        beta = self.source / "skills/beta"
        beta.mkdir()
        (beta / "SKILL.md").write_text("---\nname: beta\ndescription: migration fixture\n---\nRead ../../helper\n")
        self.registry.put("sources", "shared", dict(self.binding,
            entries={"alpha": "skills/alpha", "beta": "skills/beta"}))
        roots = [self.one / name / "skills" for name in (".agents", ".cursor", ".claude")]
        spec = IntegrationSpec(project_root=self.root, sources=(self.source / "skills",),
            targets=tuple(IntegrationTarget("fixture", root=root) for root in roots))
        receipt, _ = apply_integration_plan(build_integration_plan(spec))
        return receipt, roots

    def test_one_original_receipt_migrates_multiple_entries_without_invalidating_siblings(self):
        receipt, roots = self.multi_entry_legacy()
        original = copy.deepcopy(receipt)
        plan = self.service.plan(["one"], legacy_receipts={"local": [receipt]})
        children = plan["targets"][0]["plan"]["children"]
        self.assertEqual(len(children), 6)
        self.assertTrue(all(child["plan"]["legacy_receipt"] == original for child in children))
        self.assertEqual(self.apply(plan)["status"], "completed")
        self.assertEqual(receipt, original)
        self.assertTrue(self.service.verify(["one"])["valid"])
        self.assertEqual(self.service.plan(["one"])["summary"]["changes"], 0)
        for root in roots:
            for name in ("alpha", "beta"):
                self.assertNotEqual((root / name).resolve(), self.source / "skills" / name)
                self.assertEqual(((root / name).resolve().parent.parent / "helper").read_text(), "H1")

    def test_multi_entry_legacy_migration_resumes_after_a_completed_sibling(self):
        from skills_auditor.lifecycle.engine import Manager
        receipt, roots = self.multi_entry_legacy()
        plan = self.service.plan(["one"], legacy_receipts={"local": [receipt]})
        original_apply = Manager.apply
        calls = []
        def interrupt(manager, child, **kwargs):
            calls.append(child["installation_id"])
            if len(calls) == 2:
                raise LifecycleError("fixture_interruption", "Stop before the second child effect.")
            return original_apply(manager, child, **kwargs)
        with patch.object(Manager, "apply", interrupt):
            self.assertEqual(self.apply(plan)["status"], "partial")
        self.assertNotEqual((roots[0] / "alpha").resolve(), self.source / "skills/alpha")
        self.assertEqual((roots[0] / "beta").resolve(), self.source / "skills/beta")
        resumed = self.service.recover(plan, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(self.service.verify(["one"])["valid"])

    def test_multi_entry_legacy_target_drift_blocks_before_any_new_pointer_effect(self):
        receipt, roots = self.multi_entry_legacy()
        plan = self.service.plan(["one"], legacy_receipts={"local": [receipt]})
        beta = roots[0] / "beta"
        beta.unlink()
        beta.symlink_to(self.source / "skills/alpha")
        result = self.apply(plan)
        self.assertEqual(result["status"], "partial")
        self.assertEqual((roots[0] / "alpha").resolve(), self.source / "skills/alpha")
        self.assertEqual(beta.resolve(), self.source / "skills/alpha")
        invalid = self.service.plan(["one"], legacy_receipts={"local": [receipt]})
        self.assertEqual(invalid["status"], "blocked")
        self.assertEqual(invalid["targets"][0]["error"]["code"], "workspace_legacy")

    def test_clean_same_target_legacy_receipt_migrates_whole_package_only(self):
        native=self.one/'.agents/skills'
        spec=IntegrationSpec(project_root=self.root,sources=(self.source/'skills',),targets=(IntegrationTarget('fixture',root=native),))
        receipt,_=apply_integration_plan(build_integration_plan(spec))
        legacy=(native/'alpha').resolve()
        observed=self.service.status()['environments']['local']['legacy_installations']['one']
        self.assertTrue(any(v.get('name')=='alpha' and v.get('verification')=='unverified' for v in observed))
        blocked=self.service.plan(['one'])
        self.assertEqual(blocked['status'],'blocked')
        plan=self.service.plan(['one'],legacy_receipts={'local':[receipt]})
        self.assertEqual(plan['status'],'planned',plan)
        self.assertEqual(next(a['action'] for a in plan['targets'][0]['plan']['actions'] if a['platform']=='codex'),'migrate')
        self.assertEqual(self.apply(plan)['status'],'completed')
        self.assertNotEqual((native/'alpha').resolve(),legacy)
        self.assertEqual(((native/'alpha').resolve().parent.parent/'helper').read_text(),'H1')
        self.assertTrue(self.service.verify(['one'])['valid'])
