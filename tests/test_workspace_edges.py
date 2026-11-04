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

    def shared_package_capacity_plan(self):
        beta = self.source / "skills/beta"
        beta.mkdir()
        (beta / "SKILL.md").write_text("---\nname: beta\ndescription: shared capacity fixture\n---\n")
        (self.source / "helper").write_bytes(b"P" * (4 * 1024 * 1024))
        self.registry.put("sources", "shared", dict(self.binding,
            entries={"alpha": "skills/alpha", "beta": "skills/beta"}))
        return self.service.plan(["one"])

    def test_apply_capacity_counts_one_shared_payload_and_per_installation_metadata(self):
        plan = self.shared_package_capacity_plan()
        free = 12 * 1024 * 1024
        usage = shutil._ntuple_diskusage(total=100 * 1024 * 1024, used=100 * 1024 * 1024 - free, free=free)
        with patch("skills_auditor.workspace.node.shutil.disk_usage", return_value=usage):
            result = self.apply(plan)
        self.assertEqual(result["status"], "completed", result)
        roots = {(self.one / platform / "skills" / name).resolve().parent.parent
                 for platform in (".agents", ".cursor", ".claude") for name in ("alpha", "beta")}
        self.assertEqual(len(roots), 1)
        self.assertTrue(self.service.verify(["one"])["valid"])

    def test_shared_payload_capacity_still_blocks_before_any_installation_effect(self):
        plan = self.shared_package_capacity_plan()
        usage = shutil._ntuple_diskusage(total=100 * 1024 * 1024, used=93 * 1024 * 1024, free=7 * 1024 * 1024)
        with patch("skills_auditor.workspace.node.shutil.disk_usage", return_value=usage):
            result = self.apply(plan)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["targets"][0]["code"], "workspace_capacity")
        for platform in (".agents", ".cursor", ".claude"):
            for name in ("alpha", "beta"):
                self.assertFalse((self.one / platform / "skills" / name).exists())

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

    def test_recovery_rechecks_native_shadows_before_pending_pointer_effects(self):
        from skills_auditor.lifecycle.engine import Manager
        plan = self.service.plan(["one"])
        original_apply = Manager.apply
        count = 0
        def interrupt(manager, child, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise LifecycleError("fixture_interruption", "Stop before the second child effect.")
            return original_apply(manager, child, **kwargs)
        with patch.object(Manager, "apply", interrupt):
            self.assertEqual(self.apply(plan)["status"], "partial")
        first = self.one / ".agents/skills/alpha"
        inode = first.lstat().st_ino
        shadow = self.home / ".agents/skills/alpha"
        shadow.mkdir(parents=True)
        (shadow / "SKILL.md").write_text("---\nname: alpha\ndescription: foreign shadow\n---\n")
        result = self.service.recover(plan, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["observations"][0]["code"], "workspace_stale")
        self.assertEqual(first.lstat().st_ino, inode)
        self.assertFalse((self.one / ".cursor/skills/alpha").exists())
        self.assertFalse((self.one / ".claude/skills/alpha").exists())
        self.assertIn("foreign shadow", (shadow / "SKILL.md").read_text())

    def test_enrolled_native_root_cannot_be_redirected_outside_the_workspace(self):
        outside = self.root / "outside-root"
        outside.mkdir()
        native = self.one / ".agents/skills"
        native.rmdir()
        native.symlink_to(outside, target_is_directory=True)
        plan = self.service.plan(["one"])
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["targets"][0]["error"]["code"], "workspace_replaced")
        self.assertEqual(list(outside.iterdir()), [])

    def test_native_root_drift_blocks_saved_apply_verify_and_recovery(self):
        plan = self.service.plan(["one"])
        outside = self.root / "outside-root"
        outside.mkdir()
        native = self.one / ".agents/skills"
        native.rmdir()
        native.symlink_to(outside, target_is_directory=True)
        result = self.apply(plan)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["targets"][0]["code"], "workspace_replaced")
        self.assertFalse(self.service.verify(["one"])["valid"])
        recovered = self.service.recover(plan, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(recovered["status"], "partial")
        self.assertEqual(recovered["observations"][0]["code"], "workspace_replaced")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.one / ".cursor/skills/alpha").exists())

    def test_enrolled_internal_native_root_alias_remains_supported(self):
        project = self.root / "internal-alias-project"
        (project / ".cursor/skills").mkdir(parents=True)
        (project / ".agents").mkdir()
        (project / ".agents/skills").symlink_to("../.cursor/skills", target_is_directory=True)
        self.write_declaration(project, platforms=["codex"])
        self.service.register("alias-project", "local", project, project / "skills-auditor.workspace.json")
        plan = self.service.plan(["alias-project"])
        self.assertEqual(self.apply(plan)["status"], "completed")
        self.assertTrue(self.service.verify(["alias-project"])["valid"])
        self.assertTrue((project / ".cursor/skills/alpha").is_symlink())

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
