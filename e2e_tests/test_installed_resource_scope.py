"""Exercise the installed scope CLI outside source and plugin directories."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

CLI = os.environ['SKILLS_AUDITOR_CLI']


class InstalledResourceScopeTests(unittest.TestCase):
    def test_two_projects_with_disabled_capture_and_missing_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            env = dict(os.environ, SKILLS_AUDITOR_SKILL_TRACE='0')
            def call(project, action, *extra, expected=0, text=None):
                command = [CLI, 'skill-trace', 'scope', action,
                           '--project-root', str(root / project), '--state-dir', str(root / 'state'),
                           '--environment', 'fixture-host', '--generation', 'one',
                           '--workspace', project, '--skill', 'design', *extra]
                result = subprocess.run(command, input=text, text=True, capture_output=True,
                                        env=env, cwd=root, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result
            for project in ('one', 'two'):
                (root / project).mkdir()
                (root / project / 'DESIGN.md').write_text(project)
                call(project, 'init', '--project-id', project, '--context', 'DESIGN.md')
            call('one', 'write-private', '--name', 'draft.txt', text='first-private')
            self.assertEqual(call('one', 'read-private', '--name', 'draft.txt').stdout, 'first-private')
            call('two', 'read-private', '--name', 'draft.txt', expected=2)
            read = json.loads(call('two', 'read-context').stdout)
            self.assertEqual(read['contexts'][0]['text'], 'two')
            (root / 'two' / 'DESIGN.md').unlink()
            (root / 'DESIGN.md').write_text('foreign-parent')
            denied = call('two', 'read-context', expected=2)
            self.assertEqual(denied.stdout, '')
            self.assertEqual(json.loads(denied.stderr)['status'], 'denied')
            self.assertFalse((root / 'state' / 'scope-decisions.jsonl').exists())
