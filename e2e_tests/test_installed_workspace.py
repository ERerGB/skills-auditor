"""Portable package restoration through the wheel's public console interface."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CLI = os.environ['SKILLS_AUDITOR_CLI']


class TestInstalledWorkspace(unittest.TestCase):
    def test_isolated_baseline_overrides_update_inverse_and_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            home=root/'home'; home.mkdir()
            registry=root/'controller'
            def cli(*args):
                result=subprocess.run([CLI,'workspace','--registry-root',str(registry),*map(str,args)],
                    env=dict(os.environ,HOME=str(home)),cwd=root,text=True,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return json.loads(result.stdout)
            def prepare(source,token):
                (source/'skills/probe').mkdir(parents=True)
                (source/'skills/probe/SKILL.md').write_text('---\nname: probe\ndescription: installed fixture\n---\nRead ../../template\n')
                (source/'template').write_text(token)
            for name,token in [('baseline','H1'),('project','PROJECT')]:
                source=root/name; prepare(source,token)
                cli('source','add',name,'--root',source,'--kind','local','--include','skills','--include','template',
                    '--require-file','template','--entry','probe=skills/probe')
            cli('environment','add','host','--provider','local','--generation','first','--state-dir',root/'node')
            for name in ['one','two']:
                path=root/name; path.mkdir()
                declaration={'schema_version':'skills-auditor-workspace-declaration/v1','baseline':['baseline']}
                if name=='two':declaration['overrides']={'probe':{'source':'project','entry':'skills/probe'}}
                (path/'declaration.json').write_text(json.dumps(declaration))
                cli('register',name,'--environment','host','--path',path,'--declaration',path/'declaration.json')
            plan=cli('plan','--plan-out',root/'plan.json')
            cli('apply',root/'plan.json','--approve-plan-id',plan['plan_id'])
            for name,token in [('one','H1'),('two','PROJECT')]:
                for platform in ['.agents','.cursor','.claude']:
                    entry=(root/name/platform/'skills/probe').resolve()
                    self.assertEqual((entry.parent.parent/'template').read_text(),token)
            self.assertTrue(cli('verify')['valid'])
            self.assertEqual(cli('plan')['summary']['changes'],0)
            first=(root/'one/.agents/skills/probe').resolve()
            (root/'baseline/template').write_text('H2')
            self.assertEqual((first.parent.parent/'template').read_text(),'H1')
            update=cli('plan','one','--plan-out',root/'update.json')
            cli('apply',root/'update.json','--approve-plan-id',update['plan_id'])
            inverse=cli('rollback',root/'update.json','--plan-out',root/'inverse.json')
            cli('apply',root/'inverse.json','--approve-plan-id',inverse['plan_id'])
            self.assertEqual((root/'one/.agents/skills/probe').resolve(),first)
            cli('export-release',root/'plan.json','--out',root/'release.json')
            (root/'baseline').rename(root/'source-gone')
            replacement=root/'replacement'; replacement.mkdir()
            (replacement/'declaration.json').write_text(json.dumps({'schema_version':'skills-auditor-workspace-declaration/v1','baseline':['baseline']}))
            cli('environment','add','replacement','--provider','local','--generation','second','--state-dir',root/'new-node')
            cli('register','replacement','--environment','replacement','--path',replacement,'--declaration',replacement/'declaration.json')
            restored=cli('plan','replacement','--release',root/'release.json','--plan-out',root/'restored.json')
            cli('apply',root/'restored.json','--approve-plan-id',restored['plan_id'])
            self.assertTrue(cli('verify','replacement')['valid'])
            self.assertEqual(cli('plan','replacement','--release',root/'release.json')['summary']['changes'],0)
