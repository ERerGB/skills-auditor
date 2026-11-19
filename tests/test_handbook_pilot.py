"""Offline fixtures only: never real-use or host-dispatch evidence."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from skills_auditor import handbook_pilot as pilot
from skills_auditor.observability import sensor_event_from_payload

ROOT=Path(__file__).resolve().parents[1]


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.skill=self.root/'owned/SKILL.md';self.skill.parent.mkdir();self.skill.write_text('---\nname: owned\ndescription: Fixture\n---\nUse actual evidence.\n')
        self.config_path=self.root/'config.json'
        env=patch.dict(os.environ,{pilot.CONFIG_ENV:str(self.config_path),'SKILLS_AUDITOR_SKILL_TRACE':'1'})
        env.start();self.addCleanup(env.stop)
        self.catalog=self.root/'catalog.json';self.catalog.write_text(json.dumps([{'id':'repo/owned','maintenance_scope':'user_maintained','source_revision':'a'*40,'sha256':hashlib.sha256(self.skill.read_bytes()).hexdigest(),'paths':[str(self.skill)]}]))
        self.config=pilot.initialize(self.catalog,self.root/'state',3,['rewrite'],[str(self.root/'synthetic')],self.config_path)
        self.artifact=self.root/'result.json';self.artifact.write_text('{"result":"synthetic fixture only"}')

    def event(self,session='one',phase='PreToolUse',call='call1',path=True,**extra):
        payload={'session_id':session,'hook_event_name':phase,'tool_use_id':call,'cwd':str(self.root),'tool_name':'Read','model':'fixture-model'}
        if path:payload['tool_input']={'file_path':str(self.skill)}
        if phase=='PostToolUse':payload['tool_response']={'exit_code':0}
        payload.update(extra);event=sensor_event_from_payload(payload,provider='codex')
        self.assertEqual(event.call_id,call)
        return pilot.observe_hook(event,payload)

    def pair(self,session='one',call='call1'):
        self.event(session,call=call);return self.event(session,'PostToolUse',call,path=False)

    def record(self,session='one',**kw):
        values={'outcome':'completed','issue_type':'none','issue_code':'none','artifact':self.artifact,'real_task':True};values.update(kw)
        pilot.record_outcome(self.config,session,'repo/owned',**values)

    def test_correlated_read_reminder_is_once_and_not_a_success_claim(self):
        self.assertIsNone(self.event())
        notice=self.event(phase='PostToolUse',path=False)
        self.assertEqual(notice['hookSpecificOutput']['hookEventName'],'PostToolUse')
        self.assertIsNone(self.pair(call='again'))
        result=pilot.report(self.config)
        self.assertEqual(result['observed_sessions'],1)
        self.assertEqual(result['reported_stopped_sessions'],0)
        self.assertIn('unassessed',result['effectiveness'])
        self.assertNotIn(self.skill.read_text(),json.dumps(result))

    def test_pre_only_post_only_and_error_cannot_be_reported(self):
        self.event('pre')
        self.event('post','PostToolUse')
        self.event('error');self.event('error','PostToolUse',tool_response={'isError':True})
        for session in ['pre','post','error']:
            with self.assertRaises(ValueError):self.record(session)

    def test_revision_change_between_phases_does_not_correlate(self):
        self.event();self.skill.write_text('changed')
        self.event(phase='PostToolUse',path=False)
        with self.assertRaises(ValueError):self.record()

    def test_normalized_failure_alias_blocks_outcome_and_reminder(self):
        self.event()
        notice=self.event(phase='PostToolUse',path=False,outcome='failed')
        self.assertIsNone(notice)
        self.assertEqual(pilot.report(self.config)['observations'][0]['failed'],1)
        with self.assertRaises(ValueError):self.record()

    def test_catalog_requires_immutable_full_source_revision(self):
        rows=json.loads(self.catalog.read_text())
        for revision in ['main','a'*7,'','x'*40,123]:
            rows[0]['source_revision']=revision
            self.catalog.write_text(json.dumps(rows))
            with self.assertRaises(ValueError):
                pilot.initialize(self.catalog,self.root/'new-state',path=self.root/'new-config')
            self.assertFalse((self.root/'new-state').exists())

    def test_outcomes_idempotent_conflicts_preserved(self):
        self.pair();self.record();self.record()
        with self.assertRaises(ValueError):self.record(outcome='blocked')
        with self.assertRaises(ValueError):self.record(real_task=False)
        self.assertEqual(len(pilot.report(self.config)['outcomes']),1)

    def test_review_needs_two_distinct_reported_turns_and_notice_once(self):
        self.pair();self.record();self.event(phase='Stop')
        self.assertEqual(pilot.report(self.config)['status'],'collecting')
        self.pair('two');self.record('two');notice=self.event('two','Stop')
        self.assertIn('systemMessage',notice)
        self.assertIsNone(self.event('two','Stop'))
        r=pilot.report(self.config);self.assertEqual(r['status'],'ready_for_review');self.assertIn('unassessed',r['effectiveness'])

    def test_three_session_cap_and_repeated_turns(self):
        for s in ['one','two','three','four']:self.pair(s)
        self.pair('one',call='later-turn')
        r=pilot.report(self.config);self.assertEqual(r['observed_sessions'],3);self.assertTrue(r['admission_full'])
        self.assertNotIn('four',[s['id'] for s in r['sessions']])

    def test_exclusions_foreign_paths_and_paused_capture(self):
        self.pair('rewrite');self.event(cwd=str(self.root/'synthetic'))
        other=self.root/'vendor/SKILL.md';other.parent.mkdir();other.write_text('vendor')
        self.event(tool_input={'file_path':str(other)})
        self.assertEqual(pilot.report(self.config)['observed_sessions'],0)
        self.config['enabled']=False;pilot._write(self.config_path,self.config);self.pair()
        self.assertEqual(pilot.report(self.config)['observed_sessions'],0)

    def test_same_task_issue_does_not_meet_repeat_trigger(self):
        self.pair();self.record(issue_type='ambiguity',issue_code='missing-route')
        self.assertEqual(pilot.report(self.config)['suggested_review_triggers'],[])
        self.pair('two');self.record('two',issue_type='ambiguity',issue_code='missing-route')
        self.assertEqual(pilot.report(self.config)['suggested_review_triggers'][0]['distinct_tasks'],2)

    def test_concurrent_admission_never_exceeds_cap(self):
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda i:self.event('task'+str(i)),range(8)))
        self.assertEqual(pilot.report(self.config)['observed_sessions'],3)

    def test_catalog_rejects_third_party_and_changed_source(self):
        rows=json.loads(self.catalog.read_text());rows[0]['maintenance_scope']='third_party';self.catalog.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):pilot.initialize(self.catalog,self.root/'new-state',path=self.root/'new-config')
        rows[0]['maintenance_scope']='user_maintained';rows[0]['sha256']='0'*64;self.catalog.write_text(json.dumps(rows))
        with self.assertRaises(ValueError):pilot.initialize(self.catalog,self.root/'new-state',path=self.root/'new-config')
        self.assertFalse((self.root/'new-state').exists())

    def test_wrapper_failure_is_nonblocking_and_dryrun_never_pilot_evidence(self):
        payload={'hook_event_name':'PreToolUse','session_id':'fixture','tool_use_id':'x','cwd':str(self.root),'tool_name':'Read','tool_input':{'file_path':str(self.skill)}}
        cmd=[sys.executable,str(ROOT/'plugins/skill-trace/scripts/sensor_hook.py'),'--provider','codex','--log-dir',str(self.root/'logs')]
        proc=subprocess.run(cmd+['--dry-run'],input=json.dumps(payload),text=True,capture_output=True)
        self.assertEqual(proc.returncode,0);self.assertEqual(pilot.report(self.config)['observed_sessions'],0)
        self.config_path.write_text('broken')
        proc=subprocess.run(cmd,input=json.dumps(payload),text=True,capture_output=True)
        self.assertEqual(proc.returncode,0);self.assertIn('pilot skipped',proc.stderr)
        self.assertEqual(len(list((self.root/'logs/sensors').glob('*/*.jsonl'))),1)

    def test_cli_control_and_local_artifact_hash(self):
        self.pair();self.record()
        r=pilot.report(self.config);self.assertEqual(r['outcomes'][0]['artifact_sha256'],hashlib.sha256(self.artifact.read_bytes()).hexdigest())
        proc=subprocess.run([sys.executable,str(ROOT/'scripts/skills_audit.py'),'handbook-pilot','--config',str(self.config_path),'status'],text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr);self.assertEqual(json.loads(proc.stdout)['observed_sessions'],1)


if __name__=='__main__':unittest.main()
