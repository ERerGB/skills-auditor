import copy
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.common import PREFIX, declaration, digest, fields, platforms, read_json, relative
from skills_auditor.workspace.node import Node
from skills_auditor.workspace.providers import SSHProvider, PotologyProvider
from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceEdges(WorkspaceFixture):
    def test_exclude_and_reenable_preserve_identity_and_require_approval(self):
        self.apply(self.service.plan())
        original = (self.one / '.agents/skills/alpha').resolve()
        self.write_declaration(self.one, overrides={'alpha': None})
        disabled = self.service.plan(['one'])
        self.assertEqual(self.apply(disabled)['status'],'completed')
        self.assertFalse((self.one / '.agents/skills/alpha').exists())
        self.assertTrue(self.service.verify(['one'])['valid'])
        self.write_declaration(self.one)
        enabled = self.service.plan(['one'])
        self.assertEqual(self.apply(enabled)['status'],'completed')
        self.assertEqual((self.one / '.agents/skills/alpha').resolve(),original)

    def test_capacity_and_platform_failure_have_no_pointer_effects(self):
        usage = shutil._ntuple_diskusage(total=100,used=100,free=0)
        with patch('skills_auditor.workspace.node.shutil.disk_usage',return_value=usage):
            blocked = self.service.plan()
        self.assertEqual(blocked['status'],'blocked')
        self.assertTrue(all(t['error']['code']=='workspace_capacity' for t in blocked['targets']))
        self.assertFalse((self.one/'.agents/skills/alpha').exists())
        self.registry.put('sources','shared',dict(self.binding,platforms=['codex']))
        blocked = self.service.plan()
        self.assertEqual(blocked['status'],'blocked')

    def test_conflicting_baselines_require_explicit_override(self):
        other=self.root/'other';self.write_package(other,'different')
        self.registry.put('sources','other',dict(self.binding,root=str(other)))
        self.write_declaration(self.one,baseline=['shared','other'])
        blocked=self.service.plan(['one'])
        self.assertEqual(blocked['targets'][0]['error']['code'],'workspace_source_conflict')
        self.write_declaration(self.one,baseline=['shared','other'],overrides={'alpha':{'source':'other','entry':'missing'}})
        self.assertEqual(self.service.plan(['one'])['status'],'blocked')

    def test_completed_retries_cannot_hide_tampered_targets(self):
        plan=self.service.plan();self.apply(plan)
        entry=self.one/'.agents/skills/alpha';entry.unlink();entry.symlink_to(self.source/'skills/alpha')
        self.assertFalse(self.service.verify()['valid'])
        with self.assertRaises(LifecycleError):self.apply(plan)
        self.assertEqual(self.service.plan()['status'],'blocked')

    def test_lost_reply_recovery_and_read_only_inspection(self):
        plan=self.service.plan()
        original=Node.apply
        def lose_reply(node,request):
            value=original(node,request)
            if request['plan']['workspace']=='one':raise LifecycleError('workspace_uncertain','response lost')
            return value
        with patch.object(Node,'apply',lose_reply):result=self.apply(plan)
        self.assertEqual(result['status'],'partial')
        before=(self.one/'.agents/skills/alpha').lstat().st_ino
        self.assertEqual(self.service.recover(plan)['observations'][0]['status'],'completed')
        recovered=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(recovered['status'],'completed')
        self.assertEqual((self.one/'.agents/skills/alpha').lstat().st_ino,before)

    def test_unreachable_environment_does_not_erase_known_results(self):
        self.registry.put('environments','offline',{'provider':'ssh','generation':'one','state_dir':'/private/state','connection':'existing-alias'})
        with patch('skills_auditor.workspace.providers.subprocess.run',side_effect=OSError('offline')):
            status=self.service.status()
        self.assertEqual(status['environments']['offline']['status'],'unknown')
        self.assertEqual(status['environments']['local']['status'],'observed')

    def test_replaced_workspace_and_runtime_bindings_fail_closed(self):
        plan=self.service.plan()
        self.one.rename(self.root/'old-one');self.one.mkdir()
        self.write_declaration(self.one)
        self.assertEqual(self.apply(plan)['status'],'partial')
        with patch('skills_auditor.workspace.node.fingerprint',return_value='other-runtime'):
            self.assertEqual(self.service.status()['environments']['local']['status'],'unknown')


class TestProviderContracts(unittest.TestCase):
    def test_ssh_retains_native_alias_and_never_retries_uncertain_writes(self):
        provider=SSHProvider('remote',{'generation':'one','state_dir':'/private/runtime','connection':'native-alias','python_path':'/path with spaces'})
        response=subprocess.CompletedProcess([],0,stdout='{"status":"completed"}',stderr='')
        with patch('skills_auditor.workspace.providers.subprocess.run',return_value=response) as call:
            self.assertEqual(provider.call('apply',plan={})['status'],'completed')
        args=call.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes',args)
        self.assertIn('ForwardAgent=no',args)
        self.assertIn('native-alias',args)
        self.assertNotIn('plan',args[-1])
        for method,code in [('inspect','workspace_unreachable'),('apply','workspace_uncertain')]:
            with patch('skills_auditor.workspace.providers.subprocess.run',side_effect=subprocess.TimeoutExpired('ssh',1)) as call:
                with self.assertRaises(LifecycleError) as caught:provider.call(method)
                self.assertEqual(caught.exception.code,code)
                self.assertEqual(call.call_count,1)
        bad=SSHProvider('remote',{'generation':'one','state_dir':'/state','connection':'-ProxyCommand=bad'})
        with self.assertRaises(LifecycleError):bad._argv()

    def test_ssh_reports_native_errors_and_malformed_responses(self):
        provider=SSHProvider('remote',{'generation':'one','state_dir':'/runtime','connection':'native-alias'})
        for response,code in [('{"status":"error","code":"workspace_generation","message":"changed"}','workspace_generation'),('not json','workspace_uncertain')]:
            with patch('skills_auditor.workspace.providers.subprocess.run',return_value=subprocess.CompletedProcess([],1,stdout=response,stderr='')):
                with self.assertRaises(LifecycleError) as caught:provider.call('apply')
                self.assertEqual(caught.exception.code,code)

    def test_potology_requires_current_generation_and_owner_executable_route(self):
        config={'generation':'one','state_dir':'/runtime','potology_tools':'/tools','potology_state':'/state','potology_target':'target','pairing':'selected'}
        provider=PotologyProvider('target',config)
        status={'environments':[{'id':'target','generation':'one'}]}
        routes={'routes':[{'requires_owner_execution':False,'connections':[{'mode':'ssh','profile_ref':'selected'}]}]}
        with patch.object(provider,'_query',side_effect=[status,routes,{'argv':['ssh','-o','StrictHostKeyChecking=yes','native-target']}]):
            self.assertEqual(provider._argv()[-1],'native-target')
        for responses in [[{'environments':[]}],[status,{'routes':[]}],[status,routes,{'argv':['sh','bad']}]]:
            with patch.object(provider,'_query',side_effect=responses),self.assertRaises(LifecycleError):provider._argv()

    def test_strict_declarations_reject_ambiguous_or_host_specific_shapes(self):
        for value in ['', '../up', '/absolute', 'a/../b', 'a\\b']:
            with self.subTest(value=value),self.assertRaises(LifecycleError):relative(value)
        for value in [[],['unknown'],['codex','codex'],[{}]]:
            with self.subTest(value=value),self.assertRaises(LifecycleError):platforms(value)
        with self.assertRaises(LifecycleError):declaration({'schema_version':PREFIX+'declaration/v1','baseline':[]})
        with self.assertRaises(LifecycleError):declaration({'schema_version':PREFIX+'declaration/v1','baseline':['x'],'overrides':{'alpha':{'source':'x','entry':'/host/path'}}})
