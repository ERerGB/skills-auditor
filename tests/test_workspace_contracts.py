import base64
import copy
import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jsonschema

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.workspace import worker
from skills_auditor.workspace.common import PREFIX, declaration, decode_json, read_json, seal
from skills_auditor.workspace.node import Node
from skills_auditor.workspace.providers import PotologyProvider, SSHProvider
from skills_auditor.workspace.sources import capture, source_binding, stage, unpack
from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceContracts(WorkspaceFixture):
    def test_public_source_declaration_owns_package_selection(self):
        spec = {k: v for k, v in self.binding.items() if k != 'root'}
        spec['required_files'] = ['helper']
        self.write_declaration(self.one, sources={'shared': spec})
        plan = self.service.plan(['one'])
        self.assertEqual(plan['source_specs']['shared'], spec)
        self.assertNotIn(str(self.source), json.dumps(spec))
        self.assertEqual(self.apply(plan)['status'], 'completed')
        self.assertEqual(self.service.plan(['one'])['summary']['changes'], 0)
        other = dict(spec, include=['skills'])
        self.write_declaration(self.two, sources={'shared': other})
        with self.assertRaises(LifecycleError): self.service.plan()
        with self.assertRaises(LifecycleError): self.service.plan(['two'])

    def test_schema_example_matches_runtime_and_rejects_machine_paths(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root/'skills_auditor/schemas/workspace-declaration-v1.schema.json').read_text())
        example = json.loads((root/'config/skills-auditor.workspace.example.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(example, schema)
        declaration(example)
        example['sources']['project']['root'] = '/machine/private'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(example, schema)
        with self.assertRaises(LifecycleError): declaration(example)

    def test_dependency_selection_relative_links_and_executable_bits(self):
        (self.source/'script').write_text('print(1)\n')
        (self.source/'script').chmod(0o755)
        (self.source/'helper-link').symlink_to('helper')
        binding = dict(self.binding, include=['.'], required_files=['helper-link', 'script'])
        packet = capture(binding)
        staged = stage(packet, self.root/'cache')
        self.assertEqual((staged/'script').stat().st_mode & 0o777, 0o755)
        self.assertEqual((staged/'helper-link').read_text(), 'H1')
        with self.assertRaises(LifecycleError): capture(dict(binding, required_files=['not-selected']))
        with self.assertRaises(LifecycleError): capture(dict(binding, include=['skills'], required_files=['helper']))
        bad = copy.deepcopy(packet); bad['required_files'] = ['missing']
        with self.assertRaises(LifecycleError): stage(bad, self.root/'cache')
        bad = copy.deepcopy(packet)
        next(item for item in bad['files'] if item['path']=='helper')['data']=base64.b64encode(b'corrupt envelope').decode()
        with self.assertRaises(LifecycleError): stage(bad, self.root/'cache')
        (staged/'helper').write_text('cache corruption')
        with self.assertRaises(LifecycleError): stage(packet, self.root/'cache')

    def test_portable_packets_reject_ambiguous_topology_and_provenance(self):
        packet = capture(self.binding)
        for mutation in ('origin', 'local-mark', 'entries', 'platform', 'files', 'kind', 'parent', 'unicode', 'plugin'):
            bad = copy.deepcopy(packet)
            if mutation == 'origin': bad['origin'] = []
            if mutation == 'local-mark': bad['origin'] = {'kind':'local'}
            if mutation == 'entries': bad['entries'] = []
            if mutation == 'platform': bad['platforms'] = ['unsupported']
            if mutation == 'files': bad['files'] = {}
            if mutation == 'kind': bad['files'][0]['kind'] = 'device'
            if mutation == 'parent': bad['files'] += [{'path':'helper/child','kind':'file','mode':0o644,'data':''}]
            if mutation == 'unicode':
                bad['files'] += [{'path':name,'kind':'file','mode':0o644,'data':''} for name in ['caf\u00e9','cafe\u0301']]
            if mutation == 'plugin': bad['files'] += [{'path':'.codex-plugin/plugin.json','kind':'file','mode':0o644,'data':''}]
            with self.subTest(mutation=mutation), self.assertRaises(LifecycleError):
                unpack(bad, self.root/('bad-'+mutation))
        with self.assertRaises(LifecycleError): unpack(packet, self.source)
        bad=copy.deepcopy(packet); bad['inspection']['source_tree_sha256']='not-a-hash'
        with self.assertRaises(LifecycleError): stage(bad,self.root/'cache')

    def test_strict_public_source_contracts_and_json(self):
        for change in ({'kind':'unknown'}, {'root':'relative'}, {'include':[]}, {'entries':{}}, {'required_files':{}},
                       {'kind':'git'}, {'commit':'a'*40}, {'kind':'git','commit':'a'*40,'repository':'https://token:secret@example.com/repo'},
                       {'kind':'git','commit':'a'*40,'repository':'file:///machine/repo'}):
            with self.subTest(change=change), self.assertRaises(LifecycleError): source_binding(dict(self.binding, **change))
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', '{broken']:
            with self.subTest(raw=raw), self.assertRaises(LifecycleError): decode_json(raw)
        with self.assertRaises(LifecycleError): read_json(self.root/'missing')
        for value in ([{}], ['x','x']):
            with self.assertRaises(LifecycleError): declaration({'schema_version':PREFIX+'declaration/v1','baseline':value})
        with self.assertRaises(LifecycleError): declaration({'schema_version':PREFIX+'declaration/v1','baseline':['x'],'sources':[]})

    def test_pinned_git_failure_and_special_files_never_stage(self):
        with self.assertRaises(LifecycleError): capture(dict(self.binding,kind='git',commit='a'*40))
        with patch('skills_auditor.workspace.sources.MAX_BYTES',1), self.assertRaises(LifecycleError): capture(self.binding)
        import os
        os.mkfifo(self.source/'pipe')
        with self.assertRaises(LifecycleError): capture(dict(self.binding,include=['.']))

    def test_nested_and_user_level_conflicts_are_not_precedence_overrides(self):
        for root in [self.one/'.cursor/skills/pack/nested',self.home/'.claude/skills/alpha']:
            root.mkdir(parents=True)
            (root/'SKILL.md').write_text('---\nname: alpha\ndescription: foreign\n---\n')
        plan=self.service.plan(['one'])
        self.assertEqual(plan['status'],'blocked')
        self.assertEqual(len(plan['targets'][0]['plan']['findings']),2)
        self.assertFalse((self.one/'.agents/skills/alpha').exists())

    def test_generation_directory_and_registration_boundaries(self):
        for path in [self.home,self.root/'node',self.root/'absent']:
            with self.assertRaises(LifecycleError): self.service.register('invalid','local',path,self.one/'skills-auditor.workspace.json')
        with self.assertRaises(LifecycleError): self.service.register('one','local',self.two,self.two/'skills-auditor.workspace.json')
        outside=self.root/'outside'; outside.mkdir()
        project=self.root/'escaping'; project.mkdir(); self.write_declaration(project)
        (project/'.agents').symlink_to(outside)
        with self.assertRaises(LifecycleError): self.service.register('escaping','local',project,project/'skills-auditor.workspace.json')
        with self.assertRaises(LifecycleError): self.service.register('bad','missing',self.one,self.one/'skills-auditor.workspace.json')
        with self.assertRaises(LifecycleError): Node(self.home)
        before=self.registry.read()['revision']
        self.registry.put('environments','local',self.environment)
        self.assertEqual(self.registry.read()['revision'],before)

    def test_reviewed_noop_and_completed_apply_cannot_bless_superseded_state(self):
        initial=self.service.plan(); self.apply(initial)
        noop=self.service.plan(); self.apply(noop)
        (self.source/'helper').write_text('H2')
        updated=self.service.plan(); self.apply(updated)
        for plan in [noop,initial]:
            with self.subTest(plan=plan['plan_id']), self.assertRaises(LifecycleError): self.apply(plan)
        self.assertTrue(self.service.verify()['valid'])

    def test_capacity_rechecked_after_review_and_recovery_missing_journal(self):
        plan=self.service.plan(['one'])
        usage=shutil.disk_usage(self.root)
        with patch('skills_auditor.workspace.node.shutil.disk_usage',return_value=type(usage)(usage.total,usage.total,0)):
            result=self.apply(plan)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['targets'][0]['code'],'workspace_capacity')
        self.assertFalse((self.one/'.agents/skills/alpha').exists())
        self.assertEqual(self.service.recover(plan)['observations'][0]['status'],'not_started')
        recovered=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(recovered['status'],'completed')
        node_plan=plan['targets'][0]['plan']
        (self.root/'node/transactions'/(node_plan['plan_id']+'.json')).unlink()
        observed=self.service.recover(plan)
        self.assertEqual(observed['observations'][0]['code'],'workspace_recovery')

    def test_rollback_enable_and_stale_target_before_first_effect(self):
        self.apply(self.service.plan(['one']))
        self.write_declaration(self.one,overrides={'alpha':None})
        self.apply(self.service.plan(['one']))
        self.write_declaration(self.one)
        enabled=self.service.plan(['one']); self.apply(enabled)
        inverse=self.service.rollback(enabled)
        self.assertEqual(self.apply(inverse)['status'],'completed')
        self.assertFalse((self.one/'.agents/skills/alpha').exists())
        plan=self.service.plan(['two'])
        path=self.two/'.claude/skills/alpha'; path.mkdir()
        result=self.apply(plan)
        self.assertEqual(result['status'],'partial')
        self.assertFalse((self.two/'.agents/skills/alpha').exists())

    def test_worker_roundtrip_and_invalid_wire_do_not_execute_shell(self):
        for value in [{'method':'inspect','environment':'local','generation':'one','state_dir':str(self.root/'node')},
                      {'method':'unknown','environment':'local','generation':'one','state_dir':str(self.root/'node')}]:
            out=io.StringIO()
            with patch('sys.stdin',type('Input',(),{'buffer':io.BytesIO(json.dumps(value).encode())})()),redirect_stdout(out): code=worker.main()
            self.assertEqual(code,0 if value['method']=='inspect' else 3)
            self.assertIsInstance(json.loads(out.getvalue()),dict)
        with patch('sys.stdin',type('Input',(),{'buffer':io.BytesIO(b'{}{}')})()),redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(),3)

    def test_unrecognized_runtime_documents_and_approvals_fail_closed(self):
        plan=self.service.plan(['one'])
        changed=copy.deepcopy(plan); changed['plan_id']='wrong'
        with self.assertRaises(LifecycleError): self.apply(changed)
        target=plan['targets'][0]['plan']
        (self.root/'node/plans'/(target['plan_id']+'.json')).unlink()
        self.assertEqual(self.apply(plan)['status'],'partial')
        with self.assertRaises(LifecycleError): self.service.rollback(plan)
        with self.assertRaises(LifecycleError): self.service.export_release(dict(plan,release_id=None),self.root/'release.json')
        with self.assertRaises(LifecycleError): self.service.recover(plan,mode='compensate')
        with self.assertRaises(LifecycleError): self.service.recover(self.service.plan(['two']))
        with self.assertRaises(LifecycleError): self.service.plan(['one','one'])
        with self.assertRaises(LifecycleError): self.service.verify(['missing'])
