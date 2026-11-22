"""Project/environment isolation regressions; all inputs are synthetic."""
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor.resource_scope import Scope, ScopeError, main, LIMIT


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / 'a'
        self.b = self.root / 'b'
        self.a.mkdir()
        self.b.mkdir()
        (self.a / '.impeccable.md').write_text('context-A')
        (self.b / '.impeccable.md').write_text('context-B')
        self.settings = patch('skills_auditor.resource_scope.read_settings', return_value={'enabled': False})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.one = self.scope(self.a, 'env-one', 'a')
        self.two = self.scope(self.b, 'env-one', 'b')
        self.one.init('project-a', ['.impeccable.md'])
        self.two.init('project-b', ['.impeccable.md'])

    def scope(self, root, env, workspace, generation='v1'):
        return Scope(root, self.root / env, env, generation, workspace, 'impeccable')

    def test_context_and_private_are_separate(self):
        self.assertEqual(self.one.read_context()['contexts'][0]['text'], 'context-A')
        self.assertEqual(self.two.read_context()['contexts'][0]['text'], 'context-B')
        self.one.write_private('draft.txt', b'private-A')
        with self.assertRaises(ScopeError):
            self.two.read_private('draft.txt')
        self.assertNotEqual(self.one.resolve()['private_root'], self.two.resolve()['private_root'])

    def test_same_project_two_environments_keep_private_separate(self):
        other = self.scope(self.a, 'env-two', 'a')
        other.init('project-a', ['.impeccable.md'])
        self.one.write_private('draft.txt', b'first')
        with self.assertRaises(ScopeError):
            other.read_private('draft.txt')
        other.write_private('draft.txt', b'second')
        self.assertEqual(self.one.read_private('draft.txt'), b'first')

    def test_missing_context_does_not_fallback(self):
        (self.a / '.impeccable.md').unlink()
        (self.root / '.impeccable.md').write_text('foreign-parent')
        with self.assertRaises(ScopeError):
            self.one.read_context()

    def test_unknown_project_has_no_implicit_enrollment(self):
        with self.assertRaises(ScopeError):
            self.scope(self.b, 'env-one', 'a').resolve()

    def test_generation_mismatch(self):
        with self.assertRaises(ScopeError):
            self.scope(self.a, 'env-one', 'a', 'v2').resolve()

    def test_copied_private_state_is_not_adopted(self):
        copied = self.root / 'copy'
        shutil.copytree(self.root / 'env-one', copied)
        foreign = Scope(self.a, copied, 'env-one', 'v1', 'a', 'impeccable')
        with self.assertRaises(ScopeError):
            foreign.resolve()

    def test_replaced_project_directory_is_rejected(self):
        self.a.rename(self.root / 'old-a')
        shutil.copytree(self.root / 'old-a', self.a)
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_manifest_foreign_project_rejected(self):
        shutil.copyfile(self.b / '.skill-context.json', self.a / '.skill-context.json')
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_symlink_context_rejected(self):
        (self.a / '.impeccable.md').unlink()
        (self.a / '.impeccable.md').symlink_to(self.b / '.impeccable.md')
        with self.assertRaises(ScopeError):
            self.one.read_context()

    def test_hardlink_context_rejected(self):
        (self.a / '.impeccable.md').unlink()
        os.link(self.b / '.impeccable.md', self.a / '.impeccable.md')
        with self.assertRaises(ScopeError):
            self.one.read_context()

    def test_manifest_symlink_rejected(self):
        (self.a / '.skill-context.json').unlink()
        (self.a / '.skill-context.json').symlink_to(self.b / '.skill-context.json')
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_private_symlink_rejected(self):
        private = Path(self.one.resolve()['private_root'])
        private.rmdir()
        private.symlink_to(self.b, target_is_directory=True)
        with self.assertRaises(ScopeError):
            self.one.write_private('draft.txt', b'not-written')
        self.assertFalse((self.b / 'draft.txt').exists())

    def test_private_file_symlink_rejected(self):
        private = Path(self.one.resolve()['private_root'])
        (private / 'draft.txt').symlink_to(self.b / '.impeccable.md')
        with self.assertRaises(ScopeError):
            self.one.write_private('draft.txt', b'not-written')
        self.assertEqual((self.b / '.impeccable.md').read_text(), 'context-B')

    def test_traversal_and_absolute_paths_rejected(self):
        for name in ('../b/x', '/tmp/x', 'x/../../x', '.', 'x//y', 'x\\y'):
            with self.subTest(name=name), self.assertRaises(ScopeError):
                self.one.write_private(name, b'no')

    def test_nested_project_context_rejected(self):
        nested = self.a / 'nested'
        nested.mkdir()
        (nested / '.skill-context.json').write_text('{}')
        (nested / 'context.md').write_text('foreign')
        manifest = json.loads((self.a / '.skill-context.json').read_text())
        manifest['skills']['impeccable']['context'] = ['nested/context.md']
        (self.a / '.skill-context.json').write_text(json.dumps(manifest))
        with self.assertRaises(ScopeError):
            self.one.read_context()

    def test_nested_target_cannot_use_parent_scope(self):
        nested = self.a / 'nested'
        nested.mkdir()
        (nested / '.skill-context.json').write_text('{}')
        with self.assertRaises(ScopeError):
            self.one.resolve(target=nested)

    def test_outside_target_rejected(self):
        with self.assertRaises(ScopeError):
            self.one.resolve(target=self.b)

    def test_init_is_idempotent_and_does_not_replace_manifest(self):
        original = (self.a / '.skill-context.json').read_bytes()
        self.one.init('project-a', ['.impeccable.md'])
        self.assertEqual(original, (self.a / '.skill-context.json').read_bytes())
        with self.assertRaises(ScopeError):
            self.one.init('other-project', ['.impeccable.md'])

    def test_state_inside_project_rejected(self):
        scope = Scope(self.a, self.a / 'private', 'e', 'v1', 'a', 'impeccable')
        with self.assertRaises(ScopeError):
            scope.init('project-a', ['.impeccable.md'])

    def test_missing_manifest_denied(self):
        (self.a / '.skill-context.json').unlink()
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_invalid_manifest_denied(self):
        (self.a / '.skill-context.json').write_text('{"schema_version": 999}')
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_capture_off_has_no_decision_log(self):
        self.one.resolve()
        self.assertFalse((self.root / 'env-one' / 'scope-decisions.jsonl').exists())

    def test_capture_has_decisions_without_context_or_private_bodies(self):
        with patch('skills_auditor.resource_scope.read_settings', return_value={'enabled': True}):
            self.one.read_context()
            self.one.write_private('draft.txt', b'secret-body')
            (self.a / '.impeccable.md').unlink()
            with self.assertRaises(ScopeError):
                self.one.read_context()
        raw = (self.root / 'env-one' / 'scope-decisions.jsonl').read_text()
        self.assertNotIn('context-A', raw)
        self.assertNotIn('secret-body', raw)
        self.assertNotIn(str(self.a), raw)
        self.assertIn('denied', raw)
        self.assertIn('allowed', raw)

    def test_capture_error_does_not_disable_isolation(self):
        with patch('skills_auditor.resource_scope.read_settings', side_effect=ValueError('bad config')):
            self.assertEqual(self.one.read_context()['contexts'][0]['text'], 'context-A')
            with self.assertRaises(ScopeError):
                self.one.write_private('../bad', b'no')

    def test_scope_enrollment_never_initializes_managed_lifecycle(self):
        state = self.root / 'env-new'
        state.mkdir()
        outside = self.root / 'foreign-managed'
        outside.mkdir()
        (state / 'managed').symlink_to(outside, target_is_directory=True)
        scope = self.scope(self.a, 'env-new', 'a')
        scope.init('project-a', ['.impeccable.md'])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(scope.read_context()['contexts'][0]['text'], 'context-A')

    def test_existing_multiplatform_workspace_is_reused_unchanged(self):
        from skills_auditor.workspace.node import Node
        state = self.root / 'existing-state'
        Node(state).register({'environment': 'existing-host', 'generation': 'v1',
                              'workspace': 'a', 'path': str(self.a)})
        original = (state / 'workspaces.json').read_bytes()
        scope = Scope(self.a, state, 'existing-host', 'v1', 'a', 'impeccable')
        scope.init('project-a', ['.impeccable.md'])
        self.assertEqual((state / 'workspaces.json').read_bytes(), original)
        self.assertEqual(scope.read_context()['contexts'][0]['text'], 'context-A')

    def test_malformed_enrollment_metadata_is_safe_denial(self):
        path = self.root / 'env-one' / 'workspaces.json'
        for value in ([], {'a': {}}, {'a': {'path': 1}}, {'a': None}):
            path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ScopeError):
                self.one.resolve()

    def test_missing_environment_binding_cannot_be_reenrolled_implicitly(self):
        (self.root / 'env-one' / 'environment.json').unlink()
        other = Scope(self.a, self.root / 'env-one', 'env-one', 'v1', 'a', 'another-skill')
        manifest = json.loads((self.a / '.skill-context.json').read_text())
        manifest['skills']['another-skill'] = {'context': ['.impeccable.md']}
        (self.a / '.skill-context.json').write_text(json.dumps(manifest))
        with self.assertRaises(ScopeError):
            other.init('project-a', ['.impeccable.md'])
        self.assertFalse(other.base.exists())

    def test_oversized_and_nonregular_inputs_are_rejected(self):
        (self.a / '.impeccable.md').write_bytes(b'x' * (LIMIT + 1))
        with self.assertRaises(ScopeError):
            self.one.read_context()
        with self.assertRaises(ScopeError):
            self.one.write_private('large.txt', b'x' * (LIMIT + 1))
        (self.a / '.impeccable.md').unlink()
        os.mkfifo(self.a / '.impeccable.md')
        with self.assertRaises(ScopeError):
            self.one.read_context()

    def test_private_hardlink_and_permissions_do_not_mutate_other_file(self):
        private = Path(self.one.resolve()['private_root'])
        os.link(self.b / '.impeccable.md', private / 'linked')
        with self.assertRaises(ScopeError):
            self.one.write_private('linked', b'no')
        self.assertEqual((self.b / '.impeccable.md').read_text(), 'context-B')
        private.chmod(0o755)
        with self.assertRaises(ScopeError):
            self.one.resolve()

    def test_cli_roundtrip_and_denials(self):
        flags = ['--project-root', str(self.a), '--state-dir', str(self.root / 'env-one'),
                 '--environment', 'env-one', '--generation', 'v1', '--workspace', 'a', '--skill', 'impeccable']
        def run(action, *extra, data=b''):
            out = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
            inp = io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')
            with patch('sys.stdout', out), patch('sys.stdin', inp), patch('sys.stderr', io.StringIO()):
                code = main([action, *flags, *extra])
            out.flush()
            return code, out.buffer.getvalue()
        self.assertEqual(run('init', '--project-id', 'project-a', '--context', '.impeccable.md')[0], 0)
        self.assertEqual(run('resolve')[0], 0)
        self.assertEqual(json.loads(run('read-context')[1])['contexts'][0]['text'], 'context-A')
        self.assertEqual(run('write-private', '--name', 'cli.txt', data=b'cli-state')[0], 0)
        self.assertEqual(run('read-private', '--name', 'cli.txt'), (0, b'cli-state'))
        self.assertEqual(run('read-private', '--name', '../no')[0], 2)
        for action in ('init', 'read-private', 'write-private'):
            with self.assertRaises(SystemExit):
                run(action)

    def test_invalid_init_inputs_leave_no_registration(self):
        c = self.root / 'c'
        c.mkdir()
        for contexts in ([], ['../outside'], ['.skill-context.json'], ['x', 'x']):
            scope = self.scope(c, 'new-env', 'c')
            with self.subTest(contexts=contexts), self.assertRaises(ScopeError):
                scope.init('project-c', contexts)
            self.assertFalse(scope.state.exists())
        for root, state in ((Path('relative'), self.root / 's'), (self.a, Path('relative'))):
            with self.assertRaises(ScopeError):
                Scope(root, state, 'env', 'v1', 'c', 'design')
        with self.assertRaises(ScopeError):
            Scope(self.a, self.root / 's', '../env', 'v1', 'c', 'design')
        with self.assertRaises(ScopeError):
            Scope(self.a, Path.home(), 'env', 'v1', 'c', 'design')

    def test_subdirectory_context_and_private_access(self):
        (self.a / 'docs').mkdir()
        (self.a / 'docs' / 'context.md').write_text('scoped context')
        manifest = json.loads((self.a / '.skill-context.json').read_text())
        manifest['skills']['impeccable']['context'] = ['docs/context.md']
        (self.a / '.skill-context.json').write_text(json.dumps(manifest))
        self.assertEqual(self.one.read_context(self.a / 'docs' / 'new-file')['contexts'][0]['text'], 'scoped context')
        private = Path(self.one.resolve()['private_root'])
        (private / 'drafts').mkdir(mode=0o700)
        self.one.write_private('drafts/one', b'one')
        self.assertEqual(self.one.read_private('drafts/one'), b'one')
        with self.assertRaises(ScopeError):
            self.one.resolve('relative')

    def test_unsupported_platform_is_explicit(self):
        with patch('skills_auditor.resource_scope.os', object()), self.assertRaisesRegex(ScopeError, 'unsupported_platform'):
            self.scope(self.a, 'env-one', 'a')

    def test_private_permissions(self):
        private = Path(self.one.resolve()['private_root'])
        self.one.write_private('draft.txt', b'ok')
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private / 'draft.txt').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.one.read_private('draft.txt'), b'ok')


if __name__ == '__main__':
    unittest.main()
