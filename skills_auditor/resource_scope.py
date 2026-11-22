"""Explicit project resources, independent of optional Skill Trace capture.

The local account remains the trust boundary. This checks resolver-mediated
access, not arbitrary shell tools. Environment/workspace enrollment uses Node.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

from .lifecycle.common import LifecycleError, utc_now
from .skill_trace import read_settings
from .workspace.common import decode_json, digest, fingerprint, identifier
from .workspace.node import Node

MANIFEST = '.skill-context.json'
LIMIT = 1024 * 1024


class ScopeError(ValueError):
    """Stable, body-free denial code."""


def deny(code):
    raise ScopeError(code)


def relative(value):
    if (not isinstance(value, str) or not value or value == '.' or '\\' in value
            or any(ord(c) < 32 for c in value) or PurePosixPath(value).is_absolute()
            or '..' in PurePosixPath(value).parts or str(PurePosixPath(value)) != value):
        deny('invalid_relative_path')
    return value


def identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def checked_tree(path):
    """Reject redirection below the explicitly resolved root, including dangling links."""
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            deny('symlink_path')
    return path


@contextmanager
def directory(path):
    checked_tree(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def parent_fd(root, name, *, nested=False):
    parts = PurePosixPath(relative(name)).parts
    with directory(root) as base:
        fd = os.dup(base)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if nested:
                    try:
                        os.stat(MANIFEST, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        deny('nested_project')
            yield fd, parts[-1]
        finally:
            os.close(fd)


def read_file(root, name, *, nested=False):
    with parent_fd(root, name, nested=nested) as (parent, leaf):
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT:
                deny('unsafe_file')
            data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                deny('resource_too_large')
            return data


def write_file(root, name, data, *, exclusive=False, append=False):
    if not isinstance(data, bytes) or len(data) > LIMIT:
        deny('resource_too_large')
    with parent_fd(root, name) as (parent, leaf):
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        if exclusive:
            flags |= os.O_EXCL
        if append:
            flags |= os.O_APPEND
        fd = os.open(leaf, flags, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                deny('unsafe_private_file')
            if append:
                stream.seek(0, os.SEEK_END)
            else:
                stream.truncate(0)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


class Scope:
    def __init__(self, project_root, state_dir, environment, generation, workspace, skill):
        if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
            deny("unsupported_platform")
        raw_root, raw_state = Path(project_root).expanduser(), Path(state_dir).expanduser()
        if not raw_root.is_absolute() or not raw_state.is_absolute():
            deny('absolute_roots_required')
        # Host aliases such as /tmp are resolved once. Later calls retain this binding.
        self.project = raw_root.resolve()
        self.state = raw_state.resolve()
        try:
            for value in (environment, generation, workspace, skill):
                identifier(value)
        except LifecycleError:
            deny('invalid_identity')
        self.env, self.generation, self.workspace, self.skill = environment, generation, workspace, skill
        self.key = digest([workspace, skill])
        self.base = self.state / 'resource-scopes' / self.key
        self.private = self.base / 'private'
        try:
            self.node = Node(self.state)
        except LifecycleError:
            deny('invalid_state_root')

    def _request(self):
        return {'environment': self.env, 'generation': self.generation,
                'workspace': self.workspace, 'path': str(self.project), 'platforms': ['codex']}

    def _roots(self):
        checked_tree(self.project)
        checked_tree(self.state)
        if (not self.project.is_dir() or self.project == Path.home().resolve()
                or self.project == self.project.parent or self.state == self.state.parent
                or self.state == Path.home().resolve() or self.state == self.project
                or self.state.is_relative_to(self.project) or self.project.is_relative_to(self.state)):
            deny('invalid_roots')

    def _manifest(self):
        value = decode_json(read_file(self.project, MANIFEST))
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'project_id', 'skills'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not isinstance(value['skills'], dict) or not value['skills']):
            deny('invalid_manifest')
        identifier(value['project_id'])
        for name, spec in value['skills'].items():
            identifier(name)
            if (not isinstance(spec, dict) or set(spec) != {'context'} or not isinstance(spec['context'], list)
                    or not spec['context'] or len(spec['context']) > 32):
                deny('invalid_manifest')
            paths = [relative(p) for p in spec['context']]
            if len(paths) != len(set(paths)) or MANIFEST in paths:
                deny('invalid_manifest')
        if self.skill not in value['skills']:
            deny('skill_not_declared')
        return value

    def _binding_value(self, project_id):
        return {'schema_version': 1, 'project_id': project_id, 'environment': self.env,
                'generation': self.generation, 'runtime_fingerprint': fingerprint(),
                'workspace': self.workspace, 'skill': self.skill,
                'project_root': str(self.project), 'project_identity': identity(self.project),
                'state_root': str(self.state), 'state_identity': identity(self.state)}

    def _validate_node_files(self):
        for name in ('environment.json', 'workspaces.json'):
            if (self.state / name).exists() or (self.state / name).is_symlink():
                value = decode_json(read_file(self.state, name))
                if not isinstance(value, dict):
                    deny('invalid_enrollment_metadata')
                if name == 'workspaces.json':
                    for key, item in value.items():
                        identifier(key)
                        if (not isinstance(item, dict)
                                or set(item) != {'path', 'path_identity', 'platforms', 'roots'}
                                or not isinstance(item['path'], str) or not Path(item['path']).is_absolute()
                                or not isinstance(item['path_identity'], list) or len(item['path_identity']) != 2
                                or any(type(v) is not int or v < 0 for v in item['path_identity'])
                                or not isinstance(item['platforms'], list) or not item['platforms']
                                or any(not isinstance(v, str) for v in item['platforms'])
                                or not isinstance(item['roots'], dict)
                                or set(item['roots']) != set(item['platforms'])
                                or any(not isinstance(v, str) or not Path(v).is_absolute()
                                       for v in item['roots'].values())):
                            deny('invalid_enrollment_metadata')

    def _bound(self):
        self._roots()
        self._validate_node_files()
        self.node._binding(self._request())
        workspace = self.node._workspace(self.workspace)
        if workspace['path'] != str(self.project):
            deny('foreign_workspace')
        manifest = self._manifest()
        binding = decode_json(read_file(self.base, 'binding.json'))
        if binding != self._binding_value(manifest['project_id']):
            deny('foreign_binding')
        checked_tree(self.private)
        if not self.private.is_dir() or self.private.stat().st_mode & 0o077:
            deny('unsafe_private_directory')
        return manifest

    def _audit(self, action, decision, reason, result=None):
        # Optional diagnostic failure never changes a resource decision. No path/body logging.
        try:
            if not read_settings()['enabled']:
                return
            self._roots()
            self._validate_node_files()
            self.node._binding(self._request())
            event = {'schema_version': 'skill-resource-decision/v1', 'at': utc_now(),
                     'scope_id': digest([fingerprint(), identity(self.state), self.env, self.generation, self.workspace, self.skill]),
                     'action': action, 'decision': decision, 'reason': reason}
            if isinstance(result, dict) and 'contexts' in result:
                event['context_hashes'] = [item['sha256'] for item in result['contexts']]
            write_file(self.state, 'scope-decisions.jsonl', (json.dumps(event) + '\n').encode(), append=True)
        except (OSError, ValueError, LifecycleError, RuntimeError):
            print('Skill Trace: scope decision capture unavailable.', file=sys.stderr)

    def _run(self, action, operation):
        try:
            result = operation()
        except (OSError, ValueError, LifecycleError, RuntimeError) as error:
            code = str(error) if isinstance(error, ScopeError) else 'resource_unavailable_or_invalid'
            self._audit(action, 'denied', code)
            raise ScopeError(code) from None
        self._audit(action, 'allowed', 'verified_scope', result)
        return result

    def init(self, project_id, contexts):
        def operation():
            self._roots()
            identifier(project_id)
            desired = {'schema_version': 1, 'project_id': project_id,
                       'skills': {self.skill: {'context': contexts}}}
            # Validate all user input before enrollment writes.
            if not contexts or len(contexts) > 32 or len(set(contexts)) != len(contexts):
                deny('invalid_contexts')
            for item in contexts:
                relative(item)
                if item == MANIFEST:
                    deny('invalid_contexts')
            self._validate_node_files()
            if (self.project / MANIFEST).exists() or (self.project / MANIFEST).is_symlink():
                manifest = self._manifest()
                if manifest['project_id'] != project_id or manifest['skills'][self.skill]['context'] != contexts:
                    deny('existing_manifest_differs')
            else:
                manifest = desired
            checked_tree(self.base)
            # Check an existing scope before any registration or writes.
            if self.base.exists():
                self._bound()
                return self._result(manifest, [])
            if self.node.binding_path.exists():
                self.node._binding(self._request())
            if self.workspace in self.node._workspaces():
                self.node._binding(self._request())
                existing = self.node._workspace(self.workspace)
                if existing['path'] != str(self.project):
                    deny('foreign_workspace')
            else:
                self.node.register(self._request(), initialize_manager=False)
            if not (self.project / MANIFEST).exists():
                write_file(self.project, MANIFEST, json_bytes(manifest), exclusive=True)
            self.base.mkdir(parents=True, mode=0o700)
            self.base.chmod(0o700)
            self.private.mkdir(mode=0o700)
            write_file(self.base, 'binding.json', json_bytes(self._binding_value(project_id)), exclusive=True)
            return self._result(manifest, [])
        return self._run('init', operation)

    def _target(self, target):
        target = Path(target) if target is not None else self.project
        if not target.is_absolute():
            deny('absolute_target_required')
        checked_tree(target)
        target = target.resolve()
        if not target.is_relative_to(self.project):
            deny('foreign_target')
        current = target if target.is_dir() else target.parent
        while current != self.project:
            if (current / MANIFEST).exists() or (current / MANIFEST).is_symlink():
                deny('nested_project')
            current = current.parent

    def _result(self, manifest, contexts):
        return {'schema_version': 1, 'project_id': manifest['project_id'], 'environment': self.env,
                'generation': self.generation, 'workspace': self.workspace, 'skill': self.skill,
                'project_root': str(self.project), 'private_root': str(self.private), 'contexts': contexts}

    def _resolve(self, target=None, include_text=False):
        manifest = self._bound()
        self._target(target)
        contexts = []
        for name in manifest['skills'][self.skill]['context']:
            data = read_file(self.project, name, nested=True)
            record = {'path': str(self.project / name), 'sha256': hashlib.sha256(data).hexdigest()}
            if include_text:
                record['text'] = data.decode('utf-8')
            contexts.append(record)
        return self._result(manifest, contexts)

    def resolve(self, target=None):
        return self._run('resolve', lambda: self._resolve(target))

    def read_context(self, target=None):
        return self._run('read-context', lambda: self._resolve(target, True))

    def read_private(self, name, target=None):
        def operation():
            self._bound()
            self._target(target)
            return read_file(self.private, name)
        return self._run('read-private', operation)

    def write_private(self, name, data, target=None):
        def operation():
            self._bound()
            self._target(target)
            write_file(self.private, name, data)
            return {'status': 'written', 'sha256': hashlib.sha256(data).hexdigest()}
        return self._run('write-private', operation)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='skills-audit skill-trace scope')
    parser.add_argument('action', choices=('init', 'resolve', 'read-context', 'read-private', 'write-private'))
    for name in ('project-root', 'state-dir', 'environment', 'generation', 'workspace', 'skill'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--project-id')
    parser.add_argument('--context', action='append')
    parser.add_argument('--target')
    parser.add_argument('--name', help='Relative private file; existing parent directories only.')
    args = parser.parse_args(argv)
    if args.action == 'init' and (args.target or args.name):
        parser.error('init does not accept --target or --name')
    if args.action != 'init' and (args.project_id or args.context):
        parser.error('--project-id and --context are only accepted by init')
    if args.action not in ('read-private', 'write-private') and args.name:
        parser.error('--name is only accepted by private operations')
    try:
        scope = Scope(args.project_root, args.state_dir, args.environment, args.generation, args.workspace, args.skill)
        if args.action == 'init':
            if not args.project_id or not args.context:
                parser.error('init requires --project-id and at least one --context')
            result = scope.init(args.project_id, args.context)
        elif args.action in ('read-private', 'write-private'):
            if not args.name:
                parser.error('private operations require --name')
            if args.action == 'read-private':
                sys.stdout.buffer.write(scope.read_private(args.name, args.target))
                return 0
            result = scope.write_private(args.name, sys.stdin.buffer.read(LIMIT + 1), args.target)
        elif args.action == 'read-context':
            result = scope.read_context(args.target)
        else:
            result = scope.resolve(args.target)
        print(json.dumps(result, indent=2))
        return 0
    except ScopeError as error:
        print(json.dumps({'status': 'denied', 'reason': str(error)}), file=sys.stderr)
        return 2
