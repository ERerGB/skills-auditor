#!/usr/bin/env python3
"""Disposable Linux acceptance probe; deliberately not a package manager."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback


WORK = Path('/work')
INPUT = Path('/input')
PAYLOAD = WORK / 'payload'
SKILL = '.agents/skills/task-handoff'
VENDIR = '/input/vendir'
EVIDENCE = {'checks': [], 'commands': []}


def run(*args, cwd=WORK, ok=True):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=90)
    EVIDENCE['commands'].append({'argv': list(args), 'cwd': str(cwd),
                                'exit': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr})
    if ok and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr[-2000:]}')
    return result


def check(name, condition, **details):
    EVIDENCE['checks'].append({'name': name, 'passed': bool(condition), **details})
    if not condition:
        raise AssertionError(name)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, cwd=WORK, ok=True):
    return run('git', '-c', 'core.hooksPath=/dev/null', *args, cwd=cwd, ok=ok)


def expected_tree(commit):
    tree = git('--git-dir=/input/source.git', 'ls-tree', '-r', commit).stdout
    expected = {}
    for line in tree.splitlines():
        meta, name = line.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'Fixture requires regular Git files: {name}')
        data = subprocess.check_output(['git', '--git-dir=/input/source.git',
                                        'cat-file', 'blob', oid])
        expected[name] = {'sha256': sha(data), 'executable': mode == '100755'}
    return expected


def actual_tree(root):
    return {p.relative_to(root).as_posix(): {
                'sha256': sha(p.read_bytes()), 'executable': bool(p.stat().st_mode & 0o111)}
            for p in sorted(root.rglob('*')) if p.is_file() and '.git' not in p.parts}


def required_dependencies(root):
    # This is a reviewed, task-handoff-specific acceptance list, not inferred dependencies.
    required = [f'{SKILL}/agents/openai.yaml', f'{SKILL}/references/handoff-packet.md',
                'AGENTS.md', 'docs/repo-context.md', 'docs/context-migration-standard.md',
                'docs/remote-work.md', 'tools/repo-context.py', 'tools/context_migration.py',
                'tools/context_archive.py', 'tools/context_protocol.py',
                'tools/context-schema.json', 'tools/context-migration-schema.json']
    missing = [name for name in required if not (root / name).is_file()]
    # Resolve the actual Markdown file links in SKILL.md relative to the actual skill root.
    for link in re.findall(r'\]\(([^)]+)\)', (root / SKILL / 'SKILL.md').read_text()):
        if '://' not in link and not link.startswith('#'):
            target = (root / SKILL / link.split('#', 1)[0]).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                missing.append(link)
    return missing


def main():
    meta = json.loads((INPUT / 'source.json').read_text())
    phase = sys.argv[1]
    EVIDENCE.update({'phase': phase, 'source_commit': meta['commit'],
                     'vendir': run(VENDIR, '--version').stdout.strip()})
    shutil.copyfile('/input/probe/vendir.yml', WORK / 'vendir.yml')
    if phase == 'restore':
        shutil.copyfile(INPUT / 'baseline.lock.yml', WORK / 'vendir.lock.yml')
    run(VENDIR, 'sync', *(['--locked'] if phase == 'restore' else []))
    lock = (WORK / 'vendir.lock.yml').read_text()
    EVIDENCE['native_lock'] = lock
    check('native_lock_pins_original_commit', meta['commit'] in lock)
    expected = expected_tree(meta['commit'])
    observed = actual_tree(PAYLOAD)
    check('all_committed_files_and_executable_bits_match', observed == expected,
          file_count=len(observed), expected_count=len(expected))
    EVIDENCE['payload_sha256'] = sha(json.dumps(observed, sort_keys=True).encode())
    mounts = [line for line in Path('/proc/self/mountinfo').read_text().splitlines()
              if line.split()[4] in ('/input', '/work')]
    check('different_filesystems', INPUT.stat().st_dev != PAYLOAD.stat().st_dev,
          source_device=INPUT.stat().st_dev, target_device=PAYLOAD.stat().st_dev,
          mounts=mounts)
    check('declared_dependencies_and_skill_links_present', not required_dependencies(PAYLOAD))

    # Observe native behavior: this unfiltered Git source retains a usable checkout.
    # Do not manufacture Git metadata or assume artifact sources behave the same way.
    origin = git('remote', 'get-url', 'origin', cwd=PAYLOAD).stdout.strip()
    check('native_git_checkout_retained',
          git('rev-parse', 'HEAD', cwd=PAYLOAD).stdout.strip() == meta['commit']
          and not git('status', '--porcelain', cwd=PAYLOAD).stdout.strip(),
          transport_origin=origin)
    # Potology fingerprints origin. The file:// mirror is only this proof's transport.
    git('remote', 'set-url', 'origin', meta['project_origin'], cwd=PAYLOAD)
    check('canonical_project_identity_bound',
          git('remote', 'get-url', 'origin', cwd=PAYLOAD).stdout.strip() == meta['project_origin'])
    run('python3', 'tools/repo-context.py', '--help', cwd=PAYLOAD)
    run('python3', 'tools/context_migration.py', '--help', cwd=PAYLOAD)
    inventory = json.loads(run('python3', 'tools/context_migration.py', '--repo', str(PAYLOAD),
                               'inventory', '--source', str(PAYLOAD), cwd=PAYLOAD).stdout)
    check('real_migration_inventory_executes', inventory['source_head'] == meta['commit'])
    tests = run('python3', '-m', 'unittest', 'discover', '-s', 'tests',
                '-p', 'test_context_migration.py', '-v', cwd=PAYLOAD)
    check('upstream_migration_behavior_tests', tests.returncode == 0,
          summary=tests.stderr[-180:])

    # Exercise existing Auditor as-is; all registration writes stay in /work.
    cli = ['python3', '-m', 'skills_auditor']
    run(*cli, 'integrate', '--source', str(PAYLOAD / '.agents/skills'),
        '--target-root', 'codex=/work/registered/skills', '--plan-out', '/work/plan.json',
        '--format', 'json')
    run(*cli, 'apply', '/work/plan.json', '--receipt-out', '/work/receipt.json', '--format', 'json')
    receipt = json.loads(run(*cli, 'verify', '/work/receipt.json', '--format', 'json').stdout)
    check('auditor_registration_and_receipt', receipt['status'] == 'passed'
          and (WORK / 'registered/skills/task-handoff').resolve() == PAYLOAD / SKILL)

    # Negative control: sync the whole skill directory, without its enclosing repo.
    partial = WORK / 'partial'
    partial.mkdir()
    config = Path('/input/probe/vendir.yml').read_text()
    config += f'    includePaths:\n    - {SKILL}/**/*\n'
    (partial / 'vendir.yml').write_text(config)
    shutil.copyfile(WORK / 'vendir.lock.yml', partial / 'vendir.lock.yml')
    run(VENDIR, 'sync', '--locked', cwd=partial)
    check('skill_directory_negative_control_keeps_its_own_files',
          (partial / 'payload' / SKILL / 'references/handoff-packet.md').is_file()
          and (partial / 'payload' / SKILL / 'agents/openai.yaml').is_file())
    missing = required_dependencies(partial / 'payload')
    check('skill_only_transfer_is_not_dependency_complete', bool(missing), missing=missing)

    # Negative control: a runtime module outside the skill tree disappears.
    module = PAYLOAD / 'tools/context_protocol.py'
    original = module.read_bytes()
    module.unlink()
    broken = run('python3', 'tools/repo-context.py', '--help', cwd=PAYLOAD, ok=False)
    auditor = json.loads(run(*cli, 'verify', '/work/receipt.json', '--format', 'json').stdout)
    check('external_dependency_loss_detected_by_behavior_not_skill_hash',
          broken.returncode != 0 and auditor['status'] == 'passed'
          and 'tools/context_protocol.py' in required_dependencies(PAYLOAD),
          runtime_exit=broken.returncode, auditor_status=auditor['status'])
    # Restore the experiment's deliberate mutation, keeping the original content exact.
    module.write_bytes(original)

    if phase == 'restore':
        baseline = json.loads((INPUT / 'baseline.json').read_text())
        check('fresh_environment_recovers_identical_payload',
              EVIDENCE['payload_sha256'] == baseline['payload_sha256'])
        latest = WORK / 'latest'
        latest.mkdir()
        shutil.copyfile('/input/probe/vendir.yml', latest / 'vendir.yml')
        run(VENDIR, 'sync', cwd=latest)
        latest_lock = (latest / 'vendir.lock.yml').read_text()
        latest_skill = (latest / 'payload' / SKILL / 'SKILL.md').read_bytes()
        original_skill = (PAYLOAD / SKILL / 'SKILL.md').read_bytes()
        check('unlocked_control_observes_advanced_branch', meta['next_commit'] in latest_lock
              and latest_skill != original_skill)
        EVIDENCE['unlocked_control_lock'] = latest_lock
        shallow = WORK / 'shallow'
        shallow.mkdir()
        (shallow / 'vendir.yml').write_text(Path('/input/probe/vendir.yml').read_text()
                                           + '      depth: 1\n')
        shutil.copyfile(INPUT / 'baseline.lock.yml', shallow / 'vendir.lock.yml')
        failure = run(VENDIR, 'sync', '--locked', cwd=shallow, ok=False)
        check('shallow_fetch_does_not_guarantee_locked_restore', failure.returncode != 0
              and 'reference is not a tree' in failure.stderr, exit=failure.returncode)
    EVIDENCE['status'] = 'passed'


if __name__ == '__main__':
    try:
        main()
    except Exception:
        EVIDENCE['status'] = 'failed'
        EVIDENCE['error'] = traceback.format_exc()
    print(json.dumps(EVIDENCE, ensure_ascii=False, indent=2))
    sys.exit(0 if EVIDENCE['status'] == 'passed' else 1)
