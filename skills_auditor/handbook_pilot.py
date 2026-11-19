"""Bounded, local-only handbook calibration pilot; observations are not efficacy claims."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .skill_trace import _read_regular_file

CONFIG_ENV = 'SKILLS_AUDITOR_HANDBOOK_PILOT_CONFIG'
MAX_FILE = 1024 * 1024
MAX_CALLS = 2000
ISSUES = ('none', 'ambiguity', 'missing_fact', 'invalid_command', 'unnecessary_step', 'boundary')
OUTCOMES = ('completed', 'blocked', 'unknown')


def now():
    return datetime.now(timezone.utc).isoformat()


def config_path():
    return Path(os.environ.get(CONFIG_ENV) or Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'skill-handbook-pilot.json').expanduser()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pilot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(path=None):
    raw = _read_regular_file(path or config_path(), MAX_FILE)
    if raw is None:
        return None
    value = json.loads(raw)
    if value.get('schema_version') != 1 or type(value.get('enabled')) is not bool:
        raise ValueError('Invalid handbook pilot configuration')
    if value.get('max_sessions') not in (2, 3) or not Path(value['state_dir']).is_absolute():
        raise ValueError('Invalid bounded pilot configuration')
    return value


def _connect(config):
    path = Path(config['state_dir'])/'pilot.sqlite3'
    if not path.is_file() or path.is_symlink():
        raise ValueError('Pilot database missing or replaced')
    connection = sqlite3.connect(str(path), timeout=.15)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(catalog, directory, maximum=3, excluded_sessions=(), excluded_cwds=(), path=None):
    path = path or config_path()
    if os.path.lexists(path) or directory.exists():
        raise ValueError('Existing pilot preserved; choose a fresh explicit config and directory')
    if maximum not in (2, 3) or not directory.is_absolute():
        raise ValueError('Choose two or three sessions and an absolute state directory')
    skills = json.loads(_read_regular_file(catalog, MAX_FILE))
    if not isinstance(skills, list) or not 1 <= len(skills) <= 256:
        raise ValueError('Catalog must contain 1–256 explicitly maintained Skill entries')
    ids, paths = set(), {}
    for item in skills:
        if item.get('maintenance_scope') != 'user_maintained':
            raise ValueError('Only explicitly maintained Skills may enter this pilot')
        identity = item['id']
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', identity) or identity in ids:
            raise ValueError('Duplicate or invalid Skill identity')
        ids.add(identity)
        revision = item.get('source_revision')
        if (not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', ''))
                or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision)):
            raise ValueError('Pinned source revision and SHA-256 are required')
        if not isinstance(item.get('paths'), list) or not item['paths']:
            raise ValueError('Explicit installed paths are required')
        for alias in item['paths']:
            target = Path(alias).expanduser()
            if not target.is_absolute() or target.name != 'SKILL.md':
                raise ValueError('Only absolute SKILL.md paths can be observed')
            data = _read_regular_file(target, MAX_FILE)
            if data is None or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('Installed bytes do not match the pinned catalog')
            resolved = str(target.resolve())
            if resolved in paths and paths[resolved] != identity:
                raise ValueError('Aliases must share one canonical Skill identity')
            paths[resolved] = identity
    directory.mkdir(parents=True, mode=0o700)
    connection = sqlite3.connect(str(directory/'pilot.sqlite3'))
    connection.executescript('''
      CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, first_at TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE notices (id TEXT PRIMARY KEY);
      CREATE TABLE calls (session TEXT NOT NULL, call_id TEXT NOT NULL, skill TEXT NOT NULL, revision TEXT NOT NULL, sha256 TEXT NOT NULL,
        model TEXT NOT NULL, pre INTEGER NOT NULL DEFAULT 0, post INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(session,call_id,skill));
      CREATE TABLE outcomes (session TEXT NOT NULL, skill TEXT NOT NULL, outcome TEXT NOT NULL, issue_type TEXT NOT NULL,
        issue_code TEXT NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, reported_at TEXT NOT NULL,
        PRIMARY KEY(session,skill));
    ''')
    connection.close()
    os.chmod(directory/'pilot.sqlite3', 0o600)
    config = {'schema_version':1, 'enabled':True, 'created_at':now(), 'state_dir':str(directory), 'max_sessions':maximum,
              'exclude_session_ids':list(excluded_sessions), 'exclude_cwds':[str(Path(x).resolve()) for x in excluded_cwds], 'skills':skills}
    _write(path, config)
    return config


def observe_hook(event, payload):
    """Called only by the real hook wrapper (not --dry-run); bounded and fail-open there.

    Metadata is observational, not authenticated. Presence of a post event is not
    successful semantic use. Outcomes require a separate explicit task report.
    """
    config = read_config()
    if not config or not config['enabled']:
        return
    session, call = event.session_id, event.call_id
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',session) or session in config['exclude_session_ids'] or not event.cwd:
        return
    cwd = Path(event.cwd).resolve()
    if any(cwd.is_relative_to(Path(x)) for x in config['exclude_cwds']):
        return
    phase = payload.get('hook_event_name')
    if phase not in ('PreToolUse', 'PostToolUse', 'Stop'):
        return
    with _connect(config) as db:
        db.execute('BEGIN IMMEDIATE')
        known = db.execute('SELECT cwd FROM sessions WHERE id=?', (session,)).fetchone()
        if known and known['cwd'] != str(cwd):
            return
        if phase == 'Stop':
            db.execute('UPDATE sessions SET stopped=1 WHERE id=?', (session,))
            count=db.execute('SELECT count(DISTINCT s.id) FROM sessions s JOIN outcomes o ON s.id=o.session WHERE s.stopped=1').fetchone()[0]
            if count>=2 and not db.execute("SELECT 1 FROM notices WHERE id='review_ready'").fetchone():
                db.execute("INSERT INTO notices VALUES('review_ready')")
                return {'systemMessage':'Skill Trace handbook pilot has at least two task reports ready for independent review. Run skills-audit handbook-pilot status. No effectiveness decision or automatic update has been made.'}
            return
        if not call or len(call)>160:
            return
        item = None
        if event.path and event.operation == 'read':
            observed = Path(event.path).expanduser()
            observed = observed if observed.is_absolute() else cwd/observed
            resolved = str(observed.resolve())
            for candidate in config['skills']:
                if any(resolved == str(Path(p).resolve()) for p in candidate['paths']):
                    data = _read_regular_file(observed, MAX_FILE)
                    if data is not None and hashlib.sha256(data).hexdigest() == candidate['sha256']:
                        item = candidate
                    break
        previous = db.execute('SELECT * FROM calls WHERE session=? AND call_id=?', (session,call)).fetchall()
        if item is None and phase == 'PostToolUse' and not event.path and len(previous)==1:
            item = next((x for x in config['skills'] if x['id']==previous[0]['skill']), None)
            if item and not any(hashlib.sha256(_read_regular_file(Path(p),MAX_FILE) or b'').hexdigest()==item['sha256'] for p in item['paths']):
                item=None
        if item is None:
            return
        if not known:
            if phase != 'PreToolUse' or db.execute('SELECT count(*) FROM sessions').fetchone()[0]>=config['max_sessions']:
                return
            db.execute('INSERT INTO sessions(id,cwd,first_at) VALUES(?,?,?)', (session,str(cwd),now()))
        if not previous and db.execute('SELECT count(*) FROM calls WHERE session=?',(session,)).fetchone()[0]>=MAX_CALLS:
            return
        model = payload.get('model')
        model = model if isinstance(model,str) and len(model)<=128 else 'unknown'
        response = payload.get('tool_response', payload.get('result'))
        failed = event.status.lower() in ('error','failed') or (isinstance(response,dict) and (response.get('isError') is True or response.get('is_error') is True or response.get('exit_code',0) not in (None,0)))
        pre, post = int(phase=='PreToolUse'), int(phase=='PostToolUse')
        db.execute('INSERT OR IGNORE INTO calls(session,call_id,skill,revision,sha256,model) VALUES(?,?,?,?,?,?)',
                   (session,call,item['id'],item['source_revision'],item['sha256'],model))
        db.execute('UPDATE calls SET pre=max(pre,?),post=max(post,?),failed=max(failed,?) WHERE session=? AND call_id=? AND skill=?',
                   (pre,post,int(failed),session,call,item['id']))
        row=db.execute('SELECT pre,post,failed FROM calls WHERE session=? AND call_id=? AND skill=?',(session,call,item['id'])).fetchone()
        notified=db.execute('SELECT notified FROM sessions WHERE id=?',(session,)).fetchone()[0]
        if row['pre'] and row['post'] and not row['failed'] and not notified:
            db.execute('UPDATE sessions SET notified=1 WHERE id=?',(session,))
            return {'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':
                'A user-authorized, experimental handbook pilot observed this Skill access: '+item['id']+' at '+item['sha256'][:12]+'. '
                'Continue the actual user task normally. If this Skill was used, after finishing record its actual outcome with '
                'skills-audit handbook-pilot record --session-id '+session+' --skill-id '+item['id']+
                ' --outcome completed --artifact /absolute/local-evidence-file --real-task. '
                'Choose completed, blocked, or unknown to match the actual outcome; the command is an example. '
                'Use --issue-type and --issue-code for a concrete problem. See handbook-pilot --help. '
                'Keep private prompts/content out of the pilot; an existing task artifact or redacted verification receipt suffices. '
                'Do not create extra tasks, repeat reads, rewrite Skills, or assert effectiveness to fill the sample quota. '
                'If this is a synthetic evaluation or merely source inspection, skip recording a real use.'}}


def record_outcome(config, session, skill, outcome, issue_type, issue_code, artifact, real_task):
    if not real_task:
        raise ValueError('Explicit --real-task attestation required; synthetic trials stay separate')
    if outcome not in OUTCOMES or issue_type not in ISSUES:
        raise ValueError('Invalid outcome or issue type')
    if not re.fullmatch(r'[a-z0-9_-]{1,80}',issue_code) or (issue_type=='none') != (issue_code=='none'):
        raise ValueError('Use a bounded issue code; none pairs with none')
    data = _read_regular_file(artifact, MAX_FILE)
    if data is None:
        raise ValueError('A readable bounded task artifact is required')
    artifact_hash=hashlib.sha256(data).hexdigest()
    row=(session,skill,outcome,issue_type,issue_code,str(artifact.resolve()),artifact_hash)
    with _connect(config) as db:
        observed=db.execute('SELECT count(*) FROM calls WHERE session=? AND skill=? AND pre=1 AND post=1 AND failed=0',(session,skill)).fetchone()[0]
        if not observed:
            raise ValueError('No correlated non-error hook observation for that task and Skill')
        old=db.execute('SELECT session,skill,outcome,issue_type,issue_code,artifact_path,artifact_sha256 FROM outcomes WHERE session=? AND skill=?',(session,skill)).fetchone()
        if old:
            if tuple(old)!=row:
                raise ValueError('Existing outcome preserved; explicit later review must resolve differing reports')
            return
        db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?)',row+(now(),))


def report(config):
    if not config:
        return {'status':'not_configured','effectiveness':'unassessed'}
    with _connect(config) as db:
        sessions=[dict(x) for x in db.execute('SELECT * FROM sessions ORDER BY first_at')]
        calls=[dict(x) for x in db.execute('SELECT * FROM calls')]
        outcomes=[dict(x) for x in db.execute('SELECT * FROM outcomes')]
    completed={x['id'] for x in sessions if x['stopped']} & {x['session'] for x in outcomes}
    repeated={}
    for item in outcomes:
        if item['issue_type']!='none':
            repeated.setdefault((item['skill'],item['issue_type'],item['issue_code']),set()).add(item['session'])
    triggers=[{'skill':k[0],'issue_type':k[1],'issue_code':k[2],'distinct_tasks':len(v),
               'action':'review_for_correction' if k[1] in ('invalid_command','boundary') else 'review_for_local_rewrite'}
              for k,v in repeated.items() if len(v)>=2 or k[1] in ('invalid_command','boundary')]
    return {'enabled':config['enabled'],'suggested_review_triggers':triggers,'status':'ready_for_review' if len(completed)>=2 else 'collecting' if config['enabled'] else 'paused',
            'effectiveness':'unassessed; hook observations and agent-reported outcomes need independent review',
            'max_sessions':config['max_sessions'],'observed_sessions':len(sessions),'reported_stopped_sessions':len(completed),
            'admission_full':len(sessions)>=config['max_sessions'],'sessions':sessions,'observations':calls,'outcomes':outcomes,
            'promotion':'No automatic Skill edits, publication, or standard-practice promotion.',
            'limits':['Stop is a turn boundary, not guaranteed session completion.','Outcome labels are reporter attestations, not independent proof.','Two or three tasks are a pilot, not a general effectiveness estimate.','Missing model or call identity remains unknown; parser coverage is limited.']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    sub=parser.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init');init.add_argument('--catalog',type=Path,required=True);init.add_argument('--state-dir',type=Path,required=True)
    init.add_argument('--max-sessions',type=int,choices=(2,3),default=3);init.add_argument('--exclude-session-id',action='append',default=[]);init.add_argument('--exclude-cwd',action='append',default=[])
    sub.add_parser('status');sub.add_parser('pause')
    record=sub.add_parser('record');record.add_argument('--session-id',required=True);record.add_argument('--skill-id',required=True)
    record.add_argument('--outcome',choices=OUTCOMES,required=True);record.add_argument('--issue-type',choices=ISSUES,default='none');record.add_argument('--issue-code',default='none');record.add_argument('--artifact',type=Path,required=True);record.add_argument('--real-task',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.action=='init':
            config=initialize(args.catalog,args.state_dir,args.max_sessions,args.exclude_session_id,args.exclude_cwd,args.config)
        else:
            config=read_config(args.config)
            if args.action!='status' and config is None:raise ValueError('No pilot configured')
            if args.action=='pause':config['enabled']=False;_write(args.config or config_path(),config)
            elif args.action=='record':record_outcome(config,args.session_id,args.skill_id,args.outcome,args.issue_type,args.issue_code,args.artifact,args.real_task)
        print(json.dumps(report(config),ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as exc:
        print('Handbook pilot: '+str(exc),file=sys.stderr);return 2
