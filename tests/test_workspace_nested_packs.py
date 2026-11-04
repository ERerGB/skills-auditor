"""Nested entries in one owned immutable package are not independent installs."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.workspace.node import Node
from tests.test_workspace_service import WorkspaceFixture


class NestedManagedPackRegression(WorkspaceFixture):
    def package(self, *, parent=True):
        pack=self.source/'skills/pack'
        nested=pack/'subskills/nested'
        nested.mkdir(parents=True)
        (pack/'SKILL.md').write_text('---\nname: pack\ndescription: complete pack\n---\nUse subskills/nested/SKILL.md\n')
        (nested/'SKILL.md').write_text('---\nname: nested\ndescription: nested entry\n---\nRead ../../../../helper\n')
        entries={'nested':'skills/pack/subskills/nested'}
        if parent:entries['pack']='skills/pack'
        self.registry.put('sources','shared',dict(self.binding,entries=entries))
        self.write_declaration(self.one,platforms=['codex'])

    def test_initial_full_pack_and_flat_nested_alias_complete_and_noop(self):
        self.package()
        plan=self.service.plan(['one'])
        self.assertEqual(plan['status'],'planned',plan)
        self.assertEqual(plan['summary']['changes'],2)
        result=self.apply(plan)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual((self.one/'.agents/skills/pack/subskills/nested').resolve(),(self.one/'.agents/skills/nested').resolve())
        self.assertTrue(self.service.verify(['one'])['valid'])
        self.assertEqual(self.service.plan(['one'])['summary']['changes'],0)

    def test_independent_native_same_physical_pointer_still_blocks(self):
        self.package(parent=False)
        self.assertEqual(self.apply(self.service.plan(['one']))['status'],'completed')
        target=self.one/'.agents/skills/nested'
        foreign=self.home/'.codex/skills/nested'
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to(target.resolve(),target_is_directory=True)
        self.assertEqual(foreign.resolve(),target.resolve())
        plan=self.service.plan(['one'])
        self.assertEqual(plan['status'],'blocked')
        self.assertTrue(any(f.get('path')==str(foreign) for f in plan['targets'][0]['plan']['findings']))

    def test_independent_native_pack_alias_same_physical_nested_entry_still_blocks(self):
        self.package(parent=False)
        self.assertEqual(self.apply(self.service.plan(['one']))['status'],'completed')
        target=self.one/'.agents/skills/nested'
        foreign=self.home/'.codex/skills/foreign-pack'
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to(target.resolve().parents[1],target_is_directory=True)
        self.assertEqual((foreign/'subskills/nested').resolve(),target.resolve())
        plan=self.service.plan(['one'])
        self.assertEqual(plan['status'],'blocked')
        self.assertTrue(any(f.get('path')==str(foreign/'subskills/nested') for f in plan['targets'][0]['plan']['findings']))

    def test_selected_but_unmanaged_pack_does_not_own_nested_discovery(self):
        self.package(parent=False)
        self.assertEqual(self.apply(self.service.plan(['one']))['status'],'completed')
        nested=self.one/'.agents/skills/nested'
        pack=self.one/'.agents/skills/pack'
        pack.symlink_to(nested.resolve().parents[1],target_is_directory=True)
        self.registry.put('sources','shared',dict(self.binding,entries={
            'pack':'skills/pack','nested':'skills/pack/subskills/nested'}))
        plan=self.service.plan(['one'])
        self.assertEqual(plan['status'],'blocked')
        self.assertTrue(any(f.get('path')==str(pack/'subskills/nested')
                            for f in plan['targets'][0]['plan']['findings']))

    def named_package(self):
        self.package()
        pack=self.source/'skills/pack'
        (pack/'SKILL.md').write_text('---\nname: a-pack\ndescription: parent first\n---\n')
        (pack/'subskills/nested/SKILL.md').write_text('---\nname: z-nested\ndescription: pending child\n---\n')
        self.registry.put('sources','shared',dict(self.binding,entries={
            'a-pack':'skills/pack','z-nested':'skills/pack/subskills/nested'}))

    def partial_parent_upgrade(self):
        self.named_package()
        self.assertEqual(self.apply(self.service.plan(['one']))['status'],'completed')
        (self.source/'helper').write_text('H2')
        plan=self.service.plan(['one'])
        self.assertEqual([child['name'] for child in plan['targets'][0]['plan']['children']],['a-pack','z-nested'])
        original=Manager.apply

        def stop_before_child(manager,child,**kwargs):
            if Path(child['after']['target']).name=='z-nested':
                raise LifecycleError('fixture_interrupt','Stop after parent publication.')
            return original(manager,child,**kwargs)

        with patch.object(Manager,'apply',stop_before_child):
            self.assertEqual(self.apply(plan)['status'],'partial')
        parent=self.one/'.agents/skills/a-pack'
        child=self.one/'.agents/skills/z-nested'
        self.assertNotEqual((parent/'subskills/nested').resolve(),child.resolve())
        return plan,parent,child

    def partial_parent_publication(self, *, committed=False):
        self.named_package()
        plan=self.service.plan(['one'])
        if committed:
            original=Manager.apply

            def lose_response(manager,core,**kwargs):
                receipt=original(manager,core,**kwargs)
                if Path(core['after']['target']).name=='a-pack':
                    raise LifecycleError('committed_response_failed','Stop before workspace mapping.')
                return receipt

            boundary=patch.object(Manager,'apply',lose_response)
        else:
            original=Manager._effect

            def interrupt_effect(manager,path,*args):
                original(manager,path,*args)
                if Path(path).name=='a-pack':
                    raise RuntimeError('Stop after pointer publication, before core metadata.')

            boundary=patch.object(Manager,'_effect',interrupt_effect)
        with boundary:
            self.assertEqual(self.apply(plan)['status'],'partial')
        parent=self.one/'.agents/skills/a-pack'
        child=self.one/'.agents/skills/z-nested'
        self.assertTrue(parent.is_symlink())
        self.assertFalse(child.exists())
        journal_path=self.root/'node/transactions'/(plan['targets'][0]['plan']['plan_id']+'.json')
        journal=json.loads(journal_path.read_text())
        self.assertEqual(len(journal['results']),0)
        self.assertEqual(journal['in_flight']['index'],0)
        return plan,parent,child

    def assert_published_parent_resumes(self, *, committed):
        plan,parent,child=self.partial_parent_publication(committed=committed)
        before_parent=(os.readlink(parent),parent.lstat().st_ino)
        resumed=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(resumed['status'],'completed',resumed)
        self.assertEqual((os.readlink(parent),parent.lstat().st_ino),before_parent)
        self.assertEqual((parent/'subskills/nested').resolve(),child.resolve())
        self.assertTrue(self.service.verify(['one'])['valid'])
        self.assertEqual(self.service.plan(['one'])['summary']['changes'],0)

    def test_resume_inflight_parent_publication_before_core_commit(self):
        self.assert_published_parent_resumes(committed=False)

    def test_resume_committed_parent_before_workspace_mapping(self):
        self.assert_published_parent_resumes(committed=True)

    def test_inflight_parent_does_not_hide_foreign_alias_to_pending_entry(self):
        plan,parent,child=self.partial_parent_publication()
        foreign=self.home/'.codex/skills/z-nested'
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to((parent/'subskills/nested').resolve(),target_is_directory=True)
        with patch.object(Manager,'_effect',side_effect=AssertionError('No pending effect before foreign conflict.')) as effect:
            result=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['observations'][0]['code'],'workspace_stale')
        effect.assert_not_called()
        self.assertFalse(child.exists())

    def test_resume_parent_upgraded_before_pending_flat_child(self):
        plan,parent,child=self.partial_parent_upgrade()
        before_parent=(os.readlink(parent),parent.lstat().st_ino)
        registry_before=self.registry.path.read_bytes()
        # Ordinary discovery stays strict about mismatched versions; only the
        # exact approved pending recovery can account for the reviewed next one.
        self.assertEqual(self.service.plan(['one'])['status'],'blocked')
        resumed=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(resumed['status'],'completed',resumed)
        self.assertEqual(self.registry.path.read_bytes(),registry_before)
        self.assertEqual((os.readlink(parent),parent.lstat().st_ino),before_parent)
        self.assertEqual((parent/'subskills/nested').resolve(),child.resolve())
        self.assertTrue(self.service.verify(['one'])['valid'])
        self.assertEqual(self.service.plan(['one'])['summary']['changes'],0)

    def test_pending_upgrade_does_not_hide_independent_alias_to_future_entry(self):
        plan,parent,child=self.partial_parent_upgrade()
        before=(os.readlink(child),child.lstat().st_ino)
        foreign=self.home/'.codex/skills/z-nested'
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to((parent/'subskills/nested').resolve(),target_is_directory=True)
        with patch.object(Manager,'_effect',side_effect=AssertionError('Foreign discovery must block before pending effects.')) as effect:
            result=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['observations'][0]['code'],'workspace_stale')
        effect.assert_not_called()
        self.assertEqual((os.readlink(child),child.lstat().st_ino),before)

    def test_resume_completed_117_children_without_pointer_effects_or_new_plan(self):
        pack=self.source/'skills/pack'
        pack.mkdir()
        (pack/'SKILL.md').write_text('---\nname: pack\ndescription: full package\n---\n')
        entries={'pack':'skills/pack'}
        for index in range(116):
            name='entry-{:03d}'.format(index)
            folder=pack/'subskills'/name
            folder.mkdir(parents=True)
            (folder/'SKILL.md').write_text('---\nname: '+name+'\ndescription: nested\n---\n')
            entries[name]='skills/pack/subskills/'+name
        self.registry.put('sources','shared',dict(self.binding,entries=entries))
        self.write_declaration(self.one,platforms=['codex'])
        plan=self.service.plan(['one'])
        self.assertEqual(plan['summary']['changes'],117)
        node_plan=plan['targets'][0]['plan']
        node_path=self.root/'node/plans'/(node_plan['plan_id']+'.json')
        saved_plan=node_path.read_bytes()
        registry_before=self.registry.path.read_bytes()
        original=Node._conflicts

        def released_final_failure(node,workspace,desired,**kwargs):
            if desired and all(Path(item['target']).is_symlink() for item in desired):
                raise LifecycleError('workspace_shadow_conflict','Released final discovery failure.')
            return original(node,workspace,desired,**kwargs)

        # Reproduce the released failure after every child has completed, before
        # the effective workspace view is published. Preserve the original plan.
        with patch.object(Node,'_conflicts',released_final_failure):
            self.assertEqual(self.apply(plan)['status'],'partial')
        journal_path=self.root/'node/transactions'/(node_plan['plan_id']+'.json')
        journal=json.loads(journal_path.read_text())
        self.assertEqual(journal['status'],'partial')
        self.assertEqual(len(journal['results']),117)
        self.assertNotIn('in_flight',journal)
        pointers={item['target']:(os.readlink(item['target']),Path(item['target']).lstat().st_ino)
                  for item in node_plan['desired']}
        manager=Manager(self.root/'node/managed',create=False)
        try:
            self.assertIsNone(manager.repository.get('workspace_view','one'))
            installations={row['id']:row['data'] for row in manager.repository.list('installation')}
            self.assertEqual(len(installations),117)
            self.assertTrue(all(value['state']=='active' for value in installations.values()))
        finally:
            manager.repository.close()
        with patch.object(Manager,'_effect',side_effect=AssertionError('Completed children must not republish pointers.')) as effect:
            resumed=self.service.recover(plan,mode='resume',approve_plan_id=plan['plan_id'])
        self.assertEqual(resumed['status'],'completed',resumed)
        effect.assert_not_called()
        self.assertEqual(self.registry.path.read_bytes(),registry_before)
        self.assertEqual(node_path.read_bytes(),saved_plan)
        self.assertEqual({path:(os.readlink(path),Path(path).lstat().st_ino) for path in pointers},pointers)
        manager=Manager(self.root/'node/managed',create=False)
        try:
            self.assertEqual({row['id']:row['data'] for row in manager.repository.list('installation')},installations)
        finally:
            manager.repository.close()
        self.assertTrue(self.service.verify(['one'])['valid'])
        self.assertEqual(self.service.plan(['one'])['summary']['changes'],0)


if __name__=='__main__':unittest.main()
