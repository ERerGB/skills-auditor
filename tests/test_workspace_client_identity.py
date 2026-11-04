"""An ambiguous generic executable is not evidence of a specific native host."""
from unittest.mock import patch

from tests.test_workspace_service import WorkspaceFixture


class TestWorkspaceClientIdentity(WorkspaceFixture):
    def test_generic_agent_does_not_prove_cursor_is_installed(self):
        def locate(name):
            return '/vendor/grok/agent' if name == 'agent' else None
        with patch('skills_auditor.workspace.node.shutil.which', side_effect=locate) as lookup:
            status = self.service.status()['environments']['local']
        self.assertFalse(status['clients']['cursor'])
        self.assertNotIn('agent', [call.args[0] for call in lookup.call_args_list])

    def test_explicit_cursor_command_is_availability_not_native_acceptance(self):
        with patch('skills_auditor.workspace.node.shutil.which', side_effect=lambda name: '/vendor/cursor/cursor-agent' if name == 'cursor-agent' else None):
            status = self.service.status()['environments']['local']
        self.assertTrue(status['clients']['cursor'])
        self.apply(self.service.plan(['one']))
        self.assertEqual(self.service.verify(['one'])['targets'][0]['native_discovery'], 'unverified')
