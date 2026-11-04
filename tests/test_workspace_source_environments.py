"""Authoring and installation environments are independent source identities."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.providers import SSHProvider
from skills_auditor.workspace.sources import capture
from tests.test_workspace_service import WorkspaceFixture


class TestSourceEnvironments(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.remote = self.root/'authoring-environment'
        self.remote.mkdir()
        home = self.remote/'home'; home.mkdir()
        project = self.remote/'project'; project.mkdir(); self.write_declaration(project)
        self.source = self.remote/'source'
        self.write_package(self.source, 'REMOTE_H1')
        shim = self.root/'source-ssh-fixture.py'
        checkout = Path(__file__).resolve().parents[1]
        shim.write_text('import os,sys\n'
            f'env=dict(os.environ,HOME={str(home)!r},PYTHONPATH={str(checkout)!r},PYTHONDONTWRITEBYTECODE="1")\n'
            'os.execve(sys.executable,[sys.executable,"-B","-m","skills_auditor.workspace.worker"],env)\n')
        self.registry.put('environments','author',{'provider':'ssh','generation':'author-v1','connection':'native-author-alias',
                          'state_dir':str(self.remote/'state')})
        transport = patch.object(SSHProvider,'_argv',return_value=[sys.executable,str(shim)])
        transport.start(); self.addCleanup(transport.stop)
        self.service.register('author','author',project,project/'skills-auditor.workspace.json')
        self.binding = dict(self.binding, root=str(self.source), environment='author')
        self.registry.put('sources','shared',self.binding)

    def test_capture_routes_to_the_authoring_environment_and_retains_fixed_bytes(self):
        # A remote-owned binding must never silently read a same-named local path.
        with patch('skills_auditor.workspace.service.capture',side_effect=AssertionError('controller capture used')):
            plan = self.service.plan(['one'])
            self.assertEqual(self.apply(plan)['status'],'completed')
        self.assertEqual(plan['source_owners']['shared']['environment'],'author')
        self.assertEqual(plan['source_owners']['shared']['generation'],'author-v1')
        active = (self.one/'.agents/skills/alpha').resolve()
        self.assertEqual((active.parent.parent/'helper').read_text(),'REMOTE_H1')
        (self.source/'helper').write_text('REMOTE_H2')
        self.assertEqual((active.parent.parent/'helper').read_text(),'REMOTE_H1')
        update = self.service.plan(['one'])
        self.assertEqual(self.apply(update)['status'],'completed')
        self.assertEqual(((self.one/'.agents/skills/alpha').resolve().parent.parent/'helper').read_text(),'REMOTE_H2')

    def test_remote_source_drift_is_rechecked_before_any_target_effect(self):
        plan = self.service.plan(['one'])
        (self.source/'helper').write_text('unreviewed change')
        with self.assertRaises(LifecycleError) as caught: self.apply(plan)
        self.assertEqual(caught.exception.code,'workspace_stale')
        self.assertFalse((self.one/'.agents/skills/alpha').exists())

    def test_source_generation_or_connection_loss_does_not_activate_targets(self):
        plan = self.service.plan(['one'])
        binding = self.remote/'state/environment.json'
        value = json.loads(binding.read_text());value['generation']='replacement'
        binding.write_text(json.dumps(value))
        with self.assertRaises(LifecycleError) as caught: self.apply(plan)
        self.assertEqual(caught.exception.code,'workspace_generation')
        self.assertFalse((self.one/'.agents/skills/alpha').exists())
        with patch('skills_auditor.workspace.providers.subprocess.run',side_effect=subprocess.TimeoutExpired('ssh',1)), self.assertRaises(LifecycleError) as caught:
            self.service.plan(['one'])
        self.assertEqual(caught.exception.code,'workspace_unreachable')

    def test_retained_release_does_not_export_owner_addresses_or_grants(self):
        plan = self.service.plan(['one'])
        path = self.root/'portable.json';self.service.export_release(plan,path)
        raw = path.read_text()
        for private in [str(self.source),'native-author-alias','source_owners','state_dir']:
            self.assertNotIn(private,raw)
        self.assertNotIn('environment', json.loads(raw)['sources']['shared'])
        restored = self.service.plan(['one'],release=json.loads(raw))
        self.assertEqual(self.apply(restored)['status'],'completed')

    def test_remote_binding_requires_registered_owner_and_cannot_use_local_capture(self):
        with self.assertRaises(LifecycleError):self.registry.put('sources','unknown',dict(self.binding,environment='missing'))
        with self.assertRaises(LifecycleError) as caught:capture(self.binding)
        self.assertEqual(caught.exception.code,'workspace_source_environment')

    def test_cli_preserves_the_author_local_path_instead_of_resolving_controller_aliases(self):
        from tests.test_workspace_cli import TestWorkspaceCli
        local_alias = self.root/'alias'
        local_alias.symlink_to(self.remote/'source')
        TestWorkspaceCli.call(self,'source','add','owner-alias','--environment','author','--root',local_alias,
                              '--kind','local','--include','skills','--entry','alpha=skills/alpha')
        self.assertEqual(self.registry.read()['sources']['owner-alias']['root'],str(local_alias))
        result = TestWorkspaceCli.call(self,'source','add','invalid','--environment','author','--root','relative',
                                       '--kind','local','--include','skills','--entry','alpha=skills/alpha',expected=3)
        self.assertEqual(result['code'],'workspace_source')

    def test_local_root_alias_cannot_expand_capture_to_an_entire_filesystem(self):
        alias = self.root/'root-alias';alias.symlink_to('/')
        value = {k:v for k,v in self.binding.items() if k != 'environment'}
        value['root'] = str(alias)
        with patch('skills_auditor.workspace.sources.os.walk',side_effect=AssertionError('filesystem walked')), self.assertRaises(LifecycleError) as caught:
            capture(value)
        self.assertEqual(caught.exception.code,'workspace_source')
