import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from skills_auditor.environments import BUILTIN_ENVIRONMENTS
from skills_auditor.integration import IntegrationSpec, IntegrationTarget, build_integration_plan
from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.common import DEFAULT_PLATFORMS, declaration
from skills_auditor.workspace.grok import configuration
from tests import test_workspace_cli
from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceGrok(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        # Never inherit the developer machine's Grok home into fixture discovery.
        environment = patch.dict('os.environ', {'GROK_HOME': str(self.home/'.grok')})
        environment.start()
        self.addCleanup(environment.stop)
        self.binding['platforms'].append('grok')
        self.registry.put('sources', 'shared', self.binding)
        self.project = self.root/'grok-project'
        self.project.mkdir()
        self.write_declaration(self.project, platforms=['grok'])
        self.service.register('grok-project', 'local', self.project, self.project/'skills-auditor.workspace.json')

    def test_explicit_grok_install_update_inverse_and_default_scope(self):
        self.assertEqual(tuple(self.service.status()['environments']['local']['workspaces']['one']['platforms']), DEFAULT_PLATFORMS)
        plan = self.service.plan(['grok-project'])
        self.assertEqual(plan['summary']['changes'], 1)
        self.assertEqual(self.apply(plan)['status'], 'completed')
        target = self.project/'.grok/skills/alpha'
        first = target.resolve()
        self.assertEqual((first.parent.parent/'helper').read_text(), 'H1')
        self.assertFalse((self.one/'.grok').exists())
        self.assertFalse((self.project/'.agents').exists())
        self.assertEqual(self.service.plan(['grok-project'])['summary']['changes'], 0)
        (self.source/'helper').write_text('H2')
        update = self.service.plan(['grok-project'])
        self.assertEqual(self.apply(update)['status'], 'completed')
        self.assertEqual((first.parent.parent/'helper').read_text(), 'H1')
        inverse = self.service.rollback(update)
        self.assertEqual(self.apply(inverse)['status'], 'completed')
        self.assertEqual(target.resolve(), first)
        self.assertTrue(self.service.verify(['grok-project'])['valid'])

    def test_existing_workspace_can_explicitly_add_grok_without_rebinding_installations(self):
        original = self.service.plan(['one'])
        self.apply(original)
        before = {r['platform']: r['installation_id'] for r in self.service.verify(['one'])['targets'][0]['results']}
        stale = self.service.plan(['one'])
        self.write_declaration(self.one, platforms=[*DEFAULT_PLATFORMS, 'grok'])
        self.service.register('one', 'local', self.one, self.one/'skills-auditor.workspace.json')
        with self.assertRaises(LifecycleError): self.apply(stale)
        updated = self.service.plan(['one'])
        self.assertEqual(updated['summary']['changes'], 1)
        self.assertEqual(self.apply(updated)['status'], 'completed')
        after = {r['platform']: r['installation_id'] for r in self.service.verify(['one'])['targets'][0]['results']}
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertIn('grok', after)
        self.write_declaration(self.one, platforms=['grok'])
        with self.assertRaises(LifecycleError):
            self.service.register('one', 'local', self.one, self.one/'skills-auditor.workspace.json')

    def test_grok_compatibility_user_and_configured_roots_block_different_same_name_copies(self):
        roots = [self.project/'.claude/skills', self.home/'.agents/skills', self.home/'.cursor/skills', self.home/'.grok/skills']
        for index, root in enumerate(roots):
            with self.subTest(root=root):
                entry = root/'alpha'
                entry.mkdir(parents=True)
                (entry/'SKILL.md').write_text('---\nname: alpha\ndescription: foreign\n---\n')
                plan = self.service.plan(['grok-project'])
                self.assertEqual(plan['status'], 'blocked')
                self.assertEqual(plan['targets'][0]['plan']['findings'][0]['code'], 'workspace_shadow_conflict')
                (entry/'SKILL.md').unlink(); entry.rmdir()
        extra = self.root/'extra'; (extra/'alpha').mkdir(parents=True)
        (extra/'alpha/SKILL.md').write_text('---\nname: alpha\ndescription: extra\n---\n')
        home = self.home/'.grok'; home.mkdir(exist_ok=True)
        (home/'config.toml').write_text('[skills]\npaths = ['+json.dumps(str(extra))+']\n')
        plan = self.service.plan(['grok-project'])
        self.assertEqual(plan['status'], 'blocked')
        self.assertFalse((self.project/'.grok/skills/alpha').exists())

    def test_config_or_grok_home_changes_stale_the_exact_plan_before_effects(self):
        home = self.home/'.grok';home.mkdir()
        config = home/'config.toml';config.write_text('[skills]\npaths=[]\n')
        plan = self.service.plan(['grok-project'])
        config.write_text('[skills]\npaths=[]\n# changed after review\n')
        self.assertEqual(self.apply(plan)['targets'][0]['code'], 'workspace_stale')
        plan = self.service.plan(['grok-project'])
        with patch.dict('os.environ', {'GROK_HOME': str(self.root/'replacement-home')}):
            self.assertEqual(self.apply(plan)['targets'][0]['code'], 'workspace_stale')
        self.assertFalse((self.project/'.grok/skills/alpha').exists())

    def test_custom_home_is_used_by_global_legacy_integration_and_local_inventory(self):
        custom = self.root/'custom-home'
        with patch.dict('os.environ', {'GROK_HOME': str(custom)}):
            native = BUILTIN_ENVIRONMENTS.get('grok')
            self.assertEqual(native.primary_global_root(self.home), custom/'skills')
            spec = IntegrationSpec(project_root=self.root, sources=(self.source/'skills',),
                                   targets=(IntegrationTarget('grok', scope='global'),))
            plan = build_integration_plan(spec)
            self.assertEqual(plan['targets'][0]['root'], str(custom/'skills'))
            dormant = self.home/'.grok/skills/alpha'; dormant.mkdir(parents=True)
            (dormant/'SKILL.md').write_text('---\nname: alpha\ndescription: inactive Grok profile\n---\n')
            self.assertEqual(self.service.plan(['grok-project'])['status'], 'planned')
        with patch.dict('os.environ', {'GROK_HOME': 'relative'}):
            plan = self.service.plan(['grok-project'])
            self.assertEqual(plan['targets'][0]['error']['code'], 'workspace_grok_config')

    def test_source_platform_compatibility_is_explicit(self):
        self.binding['platforms'].remove('grok')
        self.registry.put('sources','shared',self.binding)
        plan = self.service.plan(['grok-project'])
        self.assertEqual(plan['targets'][0]['error']['code'], 'workspace_platform')
        declaration({'schema_version':'skills-auditor-workspace-declaration/v1','baseline':['shared'],'platforms':['grok']})

    def test_config_errors_block_with_paths_without_exposing_config_values(self):
        home = self.home/'.grok';home.mkdir()
        config = home/'config.toml'
        for body in ['invalid secret =', '[skills]\npaths="secret"', '[skills]\npaths=["relative-secret"]', 'skills=42']:
            config.write_text(body)
            with self.subTest(body=body), self.assertRaises(LifecycleError) as caught:
                configuration(home, [self.project])
            self.assertNotIn('relative-secret', str(caught.exception))
        config.unlink();config.symlink_to('missing')
        with self.assertRaises(LifecycleError): configuration(home, [self.project])
        config.unlink();config.mkdir()
        with self.assertRaises(LifecycleError): configuration(home, [self.project])

    def test_missing_target_toml_dependency_is_a_clear_block(self):
        home = self.home/'.grok'; home.mkdir()
        (home/'config.toml').write_text('[skills]\npaths=[]\n')
        with patch.dict('sys.modules', {'tomllib':None, 'tomli':None}), self.assertRaises(LifecycleError) as caught:
            configuration(home, [self.project])
        self.assertEqual(caught.exception.code, 'workspace_grok_dependency')

    def test_native_catalog_and_reads_verify_without_granting_trust_or_issuing_model_calls(self):
        self.apply(self.service.plan(['grok-project']))
        md = self.project/'.grok/skills/alpha/SKILL.md'
        response = {'grokVersion':'1.0.70','projectTrusted':False,
                    'skills':[{'name':'alpha','source':{'type':'configToml','path':str(md)}}]}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(response).encode(), stderr=b'')
        with patch('skills_auditor.workspace.grok.shutil.which', return_value='/reviewed/grok'), \
             patch('skills_auditor.workspace.grok.subprocess.run', return_value=completed) as process:
            result = self.service.verify(['grok-project'], native_platforms=['grok'])
        self.assertTrue(result['valid'])
        native = result['targets'][0]['native_discovery']['grok']
        self.assertEqual(native['level'], 'catalog_and_reads')
        self.assertFalse(native['project_trusted'])
        self.assertEqual(native['entries'][0]['bytes'], len(md.read_bytes()))
        argv = process.call_args.args[0]
        self.assertEqual(argv[-2:], ['inspect','--json'])
        self.assertNotIn('--trust', argv)
        self.assertNotIn('--always-approve', argv)

    def test_native_untrusted_missing_and_ambiguous_hosts_stay_unverified(self):
        self.apply(self.service.plan(['grok-project']))
        cases = [{'grokVersion':'1.0.70','projectTrusted':False,'skills':[]},
                 {'grokVersion':'1.0.70','skills':[{'name':'alpha','userInvocable':False}]},
                 {'grokVersion':'1.0.70','skills':[{'name':'alpha','source':{'path':str(self.source/'skills/alpha/SKILL.md')}}]},
                 {'cursorVersion':'new','skills':[]}, [], 'not json']
        for response in cases:
            raw = response.encode() if isinstance(response,str) else json.dumps(response).encode()
            completed = subprocess.CompletedProcess([], 0, stdout=raw, stderr=b'private configuration details')
            with self.subTest(response=response), patch('skills_auditor.workspace.grok.shutil.which', return_value='/reviewed/grok'), \
                 patch('skills_auditor.workspace.grok.subprocess.run', return_value=completed):
                result = self.service.verify(['grok-project'], native_platforms=['grok'])
            self.assertFalse(result['valid'])
            self.assertNotIn('private configuration details', json.dumps(result))
        with patch('skills_auditor.workspace.grok.shutil.which', return_value=None):
            result = self.service.verify(['grok-project'], native_platforms=['grok'])
        self.assertEqual(result['targets'][0]['native_discovery']['grok']['code'], 'grok_cli_unavailable')
        self.assertTrue(self.service.verify(['grok-project'])['valid'], 'A native discovery gap must not revoke healthy filesystem approval')
        with self.assertRaises(LifecycleError): self.service.verify(['one'],native_platforms=['cursor'])

    def test_grok_marker_and_parent_repo_configuration_are_included(self):
        from skills_auditor.workspace.sources import capture
        (self.source/'.grok-plugin').mkdir()
        with self.assertRaises(LifecycleError): capture(self.binding)
        (self.source/'.grok-plugin').rmdir()
        (self.root/'.git').mkdir()
        extra = self.root/'parent-extra'; extra.mkdir()
        (self.root/'.grok').mkdir()
        (self.root/'.grok/config.toml').write_text('[skills]\npaths=['+json.dumps(str(extra))+']\n')
        plan = self.service.plan(['grok-project'])
        self.assertIn(str(extra), plan['targets'][0]['plan']['discovery']['roots'])


class TestGrokCli(WorkspaceFixture):
    call = test_workspace_cli.TestWorkspaceCli.call
    def test_grok_is_explicit_cli_choice_and_defaults_are_unchanged(self):
        self.call('source','add','grok-source','--root',self.source,'--kind','local','--include','skills','--include','helper',
                  '--entry','alpha=skills/alpha','--platform','grok')
        self.assertEqual(self.registry.read()['sources']['grok-source']['platforms'], ['grok'])
        self.call('source','add','default-source','--root',self.source,'--kind','local','--include','skills','--entry','alpha=skills/alpha')
        self.assertEqual(tuple(self.registry.read()['sources']['default-source']['platforms']), DEFAULT_PLATFORMS)
