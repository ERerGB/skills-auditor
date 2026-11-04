import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import main
from skills_auditor.workspace.service import WorkspaceService
from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceCli(WorkspaceFixture):
    def call(self, *args, expected=0):
        stream = io.StringIO()
        argv = ['skills-audit', 'workspace', '--registry-root', str(self.registry.root), *map(str, args)]
        with patch('sys.argv', argv), redirect_stdout(stream):
            code = main()
        value = json.loads(stream.getvalue())
        self.assertEqual(code, expected, value)
        return value

    def test_cli_registration_plan_apply_verify_export_and_inverse(self):
        self.call('environment', 'add', 'local', '--provider', 'local', '--generation', 'one', '--state-dir', self.root/'node')
        self.call('source', 'add', 'shared', '--root', self.source, '--kind', 'local', '--include', 'skills', '--include', 'helper', '--entry', 'alpha=skills/alpha')
        self.call('register', 'one', '--environment', 'local', '--path', self.one, '--declaration', self.one/'skills-auditor.workspace.json')
        self.assertIn('local', self.call('list')['environments'])
        self.assertEqual(self.call('status')['environments']['local']['status'], 'observed')
        out = self.root/'plan.json'
        plan = self.call('plan', '--plan-out', out)
        self.assertEqual(json.loads(out.read_text()), plan)
        self.call('apply', out, expected=3)
        self.call('apply', out, '--approve-plan-id', plan['plan_id'])
        self.assertTrue(self.call('verify')['valid'])
        release = self.root/'release.json'
        self.assertFalse(self.call('export-release', out, '--out', release)['contains_approvals'])
        inverse = self.call('rollback', out, '--plan-out', self.root/'inverse.json')
        self.call('apply', self.root/'inverse.json', '--approve-plan-id', inverse['plan_id'])
        self.assertTrue(self.call('verify')['valid'])

    def test_invalid_cli_shapes_are_one_json_error(self):
        for args in [('unknown',), ('source', 'add', 'bad', '--root', self.source, '--kind', 'local', '--include', '.', '--entry', 'alpha'),
                     ('source', 'add', 'bad', '--root', self.source, '--kind', 'git', '--include', '.', '--entry', 'alpha=skills/alpha'),
                     ('plan', '--legacy-receipt', 'missing-separator')]:
            with self.subTest(args=args):
                value = self.call(*args, expected=2 if args[0]=='unknown' else 3)
                self.assertEqual(value['status'], 'error')

    def test_fresh_environment_restores_retained_sources_without_old_approvals(self):
        original = self.service.plan()
        self.apply(original)
        file = self.root/'release.json'
        self.service.export_release(original, file)
        retained = json.loads(file.read_text())
        self.assertNotIn('targets', retained)
        self.assertNotIn('registry_digest', retained)
        # Simulate loss of the original editable source and a replacement node.
        self.source.rename(self.root/'source-unavailable')
        new = self.root/'replacement'
        new.mkdir()
        self.write_declaration(new)
        self.registry.put('environments','replacement',{'provider':'local','generation':'two','state_dir':str(self.root/'new-node')})
        self.service.register('replacement','replacement',new,new/'skills-auditor.workspace.json')
        restored = self.service.plan(['replacement'],release=retained)
        self.assertNotEqual(restored['plan_id'], original['plan_id'])
        self.assertEqual(self.apply(restored)['status'],'completed')
        self.assertTrue(self.service.verify(['replacement'])['valid'])
        self.assertEqual(self.service.plan(['replacement'],release=retained)['summary']['changes'],0)

    def test_worker_rejects_commands_and_missing_state_as_json(self):
        commands = [{'method':'shell','environment':'x','generation':'one','state_dir':str(self.root/'bad')},
                    {'method':'inspect','environment':'x','generation':'one','state_dir':str(self.root/'missing')},
                    {'method':'inspect'}]
        for value in commands:
            with self.subTest(value=value):
                result = subprocess.run([sys.executable,'-B','-m','skills_auditor.workspace.worker'],input=json.dumps(value),text=True,capture_output=True)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(json.loads(result.stdout)['status'],'error')
        self.assertFalse((self.root/'missing').exists())
