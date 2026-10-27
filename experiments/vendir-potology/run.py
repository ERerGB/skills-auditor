#!/usr/bin/env python3
"""Run the pinned Potology/vendir proof using disposable, network-disabled containers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
import uuid


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VERSION = 'v0.46.3'
# GitHub release asset digests observed 2026-10-27; integrity pins, not signature verification.
DIGESTS = {'arm64': '821a9da837a017270743597a2e9b796d2d256385ad85450d6c5d89f05dd5aac9',
           'amd64': '5af8823ecc67b3ce2359bb0b70e72a5dbd765135722ca0ff42c16bc4d7409847'}


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def git(*args):
    return run('git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-url', required=True, help='Read-only Git fetch URL for Potology')
    parser.add_argument('--commit', required=True, help='Exact reviewed Potology commit')
    parser.add_argument('--project-origin', required=True, help='Canonical target repository identity')
    parser.add_argument('--image', required=True, help='Existing local Linux image with Python 3.12 and Git')
    parser.add_argument('--output', type=Path, required=True, help='New private evidence directory')
    args = parser.parse_args()
    if len(args.commit) != 40 or any(c not in '0123456789abcdef' for c in args.commit):
        parser.error('--commit must be a full lowercase SHA')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    image = json.loads(run('docker', 'image', 'inspect', args.image))[0]
    arch = image['Architecture']
    if arch not in DIGESTS:
        raise ValueError(f'Unsupported proof image architecture: {arch}')
    asset = f'vendir-linux-{arch}'
    url = f'https://github.com/carvel-dev/vendir/releases/download/{VERSION}/{asset}'
    print(f'Downloading pinned vendir {VERSION} ({arch})', flush=True)
    data = urllib.request.urlopen(url, timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != DIGESTS[arch]:
        raise ValueError('vendir release asset digest mismatch')
    (out / 'vendir').write_bytes(data)
    (out / 'vendir').chmod(0o755)

    source = out / 'source.git'
    git('init', '--bare', '-q', str(source))
    git('--git-dir=' + str(source), 'fetch', '-q', args.source_url, args.commit)
    git('--git-dir=' + str(source), 'update-ref', 'refs/heads/probe-main', args.commit)
    git('--git-dir=' + str(source), 'symbolic-ref', 'HEAD', 'refs/heads/probe-main')
    fixture = out / 'fixture'
    git('clone', '-q', source.as_uri(), str(fixture))
    skill = fixture / '.agents/skills/task-handoff/SKILL.md'
    with skill.open('a') as stream:
        stream.write('\n<!-- Isolated vendir probe: simulated upstream v2; not a Potology change. -->\n')
    git('-C', str(fixture), 'add', '.agents/skills/task-handoff/SKILL.md')
    git('-C', str(fixture), '-c', 'user.name=Vendir Probe',
        '-c', 'user.email=probe@example.invalid', 'commit', '-qm', 'Synthetic upstream change')
    next_commit = git('-C', str(fixture), 'rev-parse', 'HEAD')
    git('-C', str(fixture), 'push', '-q', 'origin', 'HEAD:refs/heads/probe-next')
    metadata = {'commit': args.commit, 'next_commit': next_commit,
                'source_url': args.source_url, 'project_origin': args.project_origin,
                'image_id': image['Id'], 'vendir_version': VERSION, 'vendir_sha256': digest,
                'probe_sha256': hashlib.sha256((HERE / 'probe.py').read_bytes()).hexdigest(),
                'runner_sha256': hashlib.sha256((HERE / 'run.py').read_bytes()).hexdigest(),
                'config_sha256': hashlib.sha256((HERE / 'vendir.yml').read_bytes()).hexdigest(),
                'auditor_head': git('-C', str(ROOT), 'rev-parse', 'HEAD')}
    (out / 'source.json').write_text(json.dumps(metadata, indent=2) + '\n')

    # Docker may run on a remote daemon with no host bind mounts. Copy only this
    # fixture into a newly owned volume; every acceptance container mounts it read-only.
    resource = 'skills-auditor-vendir-' + uuid.uuid4().hex[:12]
    helper = resource + '-loader'
    metadata['temporary_volume'] = resource
    (out / 'source.json').write_text(json.dumps(metadata, indent=2) + '\n')
    options = ['--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--tmpfs', '/tmp:rw,size=128m',
               '--tmpfs', '/workspace:rw,size=1m', '--tmpfs', '/home/coder/.codex:rw,size=1m',
               '--tmpfs', '/home/coder/.ssh-host:rw,size=1m']
    run('docker', 'volume', 'create', '--label', 'skills-auditor.experiment=vendir-potology', resource)
    try:
        run('docker', 'run', '-d', '--name', helper, *options, '--mount',
            f'type=volume,src={resource},dst=/input', '--entrypoint', 'python3',
            image['Id'], '-c', 'import time; time.sleep(3600)')
        # docker cp can retain source ownership on directory entries. Re-materialize
        # the fixture as the container user so Git's ownership check stays enabled.
        run('docker', 'cp', str(source), helper + ':/input/transport-source.git')
        run('docker', 'exec', helper, 'python3', '-c',
            "import shutil; shutil.copytree('/input/transport-source.git', '/input/source.git')")
        for path in (out / 'vendir', out / 'source.json'):
            run('docker', 'cp', str(path), helper + ':/input/' + path.name)
        run('docker', 'cp', str(HERE), helper + ':/input/probe')
        run('docker', 'cp', str(ROOT / 'skills_auditor'), helper + ':/input/skills_auditor')
        for phase in ('baseline', 'restore'):
            if phase == 'restore':
                git('--git-dir=' + str(source), 'update-ref', 'refs/heads/probe-main', next_commit)
                run('docker', 'exec', helper, 'python3', '-c',
                    "from pathlib import Path; import sys; "
                    "Path('/input/source.git/refs/heads/probe-main').write_text(sys.argv[1] + '\\n')",
                    next_commit)
                for path in (out / 'baseline.json', out / 'baseline.lock.yml'):
                    run('docker', 'cp', str(path), helper + ':/input/' + path.name)
            print(f'Running {phase} in a fresh Linux tmpfs', flush=True)
            cmd = ['docker', 'run', '--rm', '--name', resource + '-' + phase, *options,
                   '--tmpfs', '/work:rw,exec,size=256m',
                   '--mount', f'type=volume,src={resource},dst=/input,readonly',
                   '-e', 'HOME=/work/home', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '-e', 'PYTHONPATH=/input', '-e', 'GIT_CONFIG_NOSYSTEM=1',
                   '-e', 'GIT_CONFIG_GLOBAL=/dev/null', '-e', 'GIT_CONFIG_COUNT=1',
                   '-e', 'GIT_CONFIG_KEY_0=safe.directory', '-e', 'GIT_CONFIG_VALUE_0=/input/source.git',
                   '--entrypoint', 'python3', image['Id'], '/input/probe/probe.py', phase]
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
            (out / f'{phase}.json').write_text(result.stdout)
            (out / f'{phase}.stderr').write_text(result.stderr)
            if result.returncode:
                raise RuntimeError(f'{phase} failed; inspect {out / (phase + ".json")}')
            report = json.loads(result.stdout)
            if report['status'] != 'passed':
                raise RuntimeError(f'{phase} did not pass')
            if phase == 'baseline':
                (out / 'baseline.lock.yml').write_text(report['native_lock'])
            print(f'{phase}: {len(report["checks"])} acceptance checks passed', flush=True)
    finally:
        for name in (resource + '-baseline', resource + '-restore', helper):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        removal = subprocess.run(['docker', 'volume', 'rm', resource], text=True, capture_output=True)
        (out / 'cleanup.json').write_text(json.dumps({'volume': resource,
            'exit': removal.returncode, 'stdout': removal.stdout, 'stderr': removal.stderr}, indent=2))
        if removal.returncode:
            raise RuntimeError(f'Failed to remove owned temporary volume {resource}; see cleanup.json')
    print(f'Evidence: {out}', flush=True)


if __name__ == '__main__':
    main()
