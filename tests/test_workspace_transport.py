"""Real stdio worker boundary using a fixture in place of native SSH transport."""
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
