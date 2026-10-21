import importlib.util
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('team',Path(__file__).with_name('team.py'))
team=importlib.util.module_from_spec(spec);spec.loader.exec_module(team)

class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        (self.root/'.ai').mkdir()
        (self.root/'.ai/PROJECT.md').write_text('Mock only')
        (self.root/'.ai/CURRENT_STATE.md').write_text('Initial')
        (self.root/'.ai/project.json').write_text(json.dumps({'id':'fixture','source_roots':[str(self.root)]}))
        (self.root/'source.txt').write_text('v1')
        self.templates=Path(self.tmp.name)/'central/templates/task';self.templates.mkdir(parents=True)
        for doc in team.DOCS:(self.templates/(doc+'.md')).write_text('{{TASK_ID}} {{TITLE}}\nSTATUS: NOT_RUN\n'+'x'*140)
        self.mock=patch.object(team,'ROOT',self.templates.parent.parent);self.mock.start();self.addCleanup(self.mock.stop)
        self.p=team.create(self.root,'DEMO-001','Fixture')
        self.s=team.read(self.p/'state.json')

    def pass_report(self,role,session,p=None,s=None):
        p=p or self.p;s=s if s is not None else self.s
        name='TEST_CLAUDE' if role=='tester' else 'VERIFY_CODEX'
        (p/(name+'.md')).write_text('STATUS: PASS\nEngine: '+team.ROLES[role]+'\nMock local tests only.\n'+'Evidence results\n'*15)
        team.evidence(self.root,p,s,role,'PASS',session,'Fixture only')

    def finish_report(self,p):
        (p/'FINAL_REPORT.md').write_text('STATUS: PASS\nFixture report; mock evidence only.')

    def knowledge_update_cli(self,ident,text):
        (self.root/'.ai/CURRENT_STATE.md').write_text(text)
        self.cli('knowledge',ident,'--summary','Current state updated for '+ident)

    def cli(self,*args,ok=True):
        result=subprocess.run([sys.executable,str(Path(team.__file__)), '--project',str(self.root),*args],text=True,capture_output=True)
        self.assertEqual(result.returncode,0 if ok else 2,result.stdout+result.stderr)
        return result

    def test_task_path_traversal_rejected(self):
        for bad in ['../escape','/tmp/escape','a/b','..','']:
            with self.assertRaises(ValueError):team.create(self.root,bad,'x')

    def test_duplicate_task_does_not_overwrite(self):
        original=(self.p/'TASK.md').read_bytes()
        with self.assertRaises(FileExistsError):team.create(self.root,'DEMO-001','overwrite')
        self.assertEqual(original,(self.p/'TASK.md').read_bytes())

    def test_auto_task_is_opt_in_and_can_be_resumed_cli(self):
        self.assertFalse(team.read(self.p/'state.json')['automation']['enabled'])
        p=team.create(self.root,'AUTO-001','automatic fixture','small',True)
        state=team.read(p/'state.json')
        self.assertTrue(state['automation']['enabled'])
        self.assertEqual(state['automation']['status'],'WAITING_FOR_LEAD')
        self.cli('automate','AUTO-001','--disable')
        self.assertFalse(team.read(p/'state.json')['automation']['enabled'])
        self.cli('automate','AUTO-001','--resume')
        self.assertTrue(team.read(p/'state.json')['automation']['enabled'])

    def test_stage_skips_and_source_lock_rejected(self):
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'DONE')
        self.s['developer']={'session':'other'}
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'ANALYSIS')

    def test_verifier_cannot_precede_claude(self):
        self.s['stage']='VERIFY_CODEX'
        with self.assertRaises(ValueError):self.pass_report('verifier','codex-1')

    def test_unrun_report_not_evidence(self):
        self.s['stage']='TEST_CLAUDE'
        with self.assertRaises(ValueError):team.evidence(self.root,self.p,self.s,'tester','PASS','claude-1','none')

    def test_same_session_not_independent(self):
        self.s['stage']='TEST_CLAUDE';self.pass_report('tester','same')
        team.advance(self.root,self.p,self.s,'VERIFY_CODEX')
        with self.assertRaises(ValueError):self.pass_report('verifier','same')

    def test_source_change_invalidates_verification(self):
        self.s['stage']='TEST_CLAUDE';self.pass_report('tester','claude-1')
        (self.root/'source.txt').write_text('v2')
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'VERIFY_CODEX')

    def test_report_tamper_invalidates_stage(self):
        self.s['stage']='TEST_CLAUDE';self.pass_report('tester','claude-1')
        with (self.p/'TEST_CLAUDE.md').open('a') as f:f.write('modified')
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'VERIFY_CODEX')

    def test_end_to_end_and_open_gate_blocks_done(self):
        for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION','TEST_CLAUDE']:team.advance(self.root,self.p,self.s,stage)
        self.pass_report('tester','claude-1');team.advance(self.root,self.p,self.s,'VERIFY_CODEX')
        self.pass_report('verifier','codex-1');team.advance(self.root,self.p,self.s,'KNOWLEDGE_UPDATE')
        (self.root/'.ai/CURRENT_STATE.md').write_text('Updated fixture outcome')
        self.s['knowledge']={'fingerprint':team.snapshot(self.root),'documents':team.knowledge_hashes(self.root)}
        (self.p/'FINAL_REPORT.md').write_text('STATUS: PASS\nFixture report')
        self.s['gates']=[{'status':'AWAITING_USER'}]
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'DONE')
        self.s['gates'][0]['status']='RESOLVED_BY_LEAD'
        team.advance(self.root,self.p,self.s,'DONE')
        self.assertEqual(self.s['stage'],'DONE')

    def test_message_unique_and_data_not_approval(self):
        a=team.message(self.root,'DEMO-001','analyst','lead','message one')
        b=team.message(self.root,'DEMO-001','analyst','lead','message two')
        self.assertNotEqual(a['id'],b['id'])
        self.assertIn('never user approval',a['trust'])
        self.assertEqual(len(list((self.root/'.ai/messages/lead').glob('*.json'))),2)

    def test_launch_delivery_requires_task_and_stage(self):
        with self.assertRaises(ValueError):team.launch(self.root,'developer',None,True)
        with self.assertRaises(ValueError):team.launch(self.root,'verifier','DEMO-001',True)

    def test_backend_source_change_invalidates_fingerprint(self):
        backend=Path(self.tmp.name)/'backend';backend.mkdir();(backend/'api.cs').write_text('v1')
        (self.root/'.ai/project.json').write_text(json.dumps({'source_roots':[str(self.root),str(backend)]}))
        before=team.snapshot(self.root);(backend/'api.cs').write_text('v2')
        self.assertNotEqual(before,team.snapshot(self.root))

    def test_cross_task_lock_and_owner_release_cli(self):
        other=team.create(self.root,'DEMO-002','second')
        for ident in ['DEMO-001','DEMO-002']:
            for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION']:self.cli('advance',ident,stage)
        self.cli('claim','DEMO-001','--session','dev-1')
        self.cli('claim','DEMO-002','--session','dev-2',ok=False)
        self.cli('release','DEMO-001','--session','wrong',ok=False)
        self.cli('release','DEMO-001','--session','dev-1')
        self.cli('claim','DEMO-002','--session','dev-2')

    def test_db_approval_never_authorizes_agent_execution_cli(self):
        self.cli('gate','DEMO-001','--kind','DB','--action','draft migration','--target','fixture','--environment','mock')
        gate=team.read(self.p/'state.json')['gates'][0]['id']
        self.cli('resolve-gate','DEMO-001',gate,'--user-message-ref','fixture-only','--outcome','explicitly-approved',ok=False)
        self.cli('resolve-gate','DEMO-001',gate,'--user-message-ref','fixture-only','--outcome','user-applied')

    def test_knowledge_requires_actual_document_change_cli(self):
        self.s['stage']='KNOWLEDGE_UPDATE';team.atomic(self.p/'state.json',self.s)
        self.cli('knowledge','DEMO-001','--summary','none',ok=False)
        (self.root/'.ai/CURRENT_STATE.md').write_text('Fixture completed')
        self.cli('knowledge','DEMO-001','--summary','Current state updated')
        self.assertIsNotNone(team.read(self.p/'state.json')['knowledge'])

    def test_rework_resets_all_evidence_cli(self):
        self.s.update(stage='VERIFY_CODEX',claude_test={'result':'PASS'},codex_verify={'result':'FAIL'},knowledge={'summary':'old'})
        team.atomic(self.p/'state.json',self.s)
        self.cli('rework','DEMO-001','--reason','negative fixture fails')
        state=team.read(self.p/'state.json')
        self.assertEqual(state['stage'],'IMPLEMENTATION')
        for key in ['claude_test','codex_verify','knowledge']:self.assertIsNone(state[key])

    def test_configuration_change_invalidates_tests(self):
        config=self.root/'appsettings.json';config.write_text('{"env":"mock"}')
        before=team.snapshot(self.root);config.write_text('{"env":"changed"}')
        self.assertNotEqual(before,team.snapshot(self.root))

    def test_failed_test_cannot_advance(self):
        self.s['stage']='TEST_CLAUDE';self.pass_report('tester','claude-1')
        self.s['claude_test']['result']='FAIL'
        with self.assertRaises(ValueError):team.advance(self.root,self.p,self.s,'VERIFY_CODEX')

    def test_small_profile_skips_analysis_and_design_but_keeps_two_tests(self):
        p=team.create(self.root,'SMALL-001','small task','small')
        s=team.read(p/'state.json')
        self.assertEqual(s['stage_path'],team.WORKFLOWS['small'])
        team.advance(self.root,p,s,'IMPLEMENTATION')
        team.advance(self.root,p,s,'TEST_CLAUDE')
        self.assertIn('VERIFY_CODEX',s['stage_path'])
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')

    def test_ui_profile_requires_design_but_not_analysis(self):
        p=team.create(self.root,'UI-001','ui task','ui')
        s=team.read(p/'state.json')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'ANALYSIS')
        team.advance(self.root,p,s,'DESIGN')
        team.advance(self.root,p,s,'IMPLEMENTATION')

    def test_tweak_profile_keeps_design_and_two_tests_without_knowledge(self):
        p=team.create(self.root,'TWEAK-001','narrow visual fix','tweak')
        s=team.read(p/'state.json')
        # Tweak skips only the stages that add overhead for an already-understood visual fix.
        self.assertEqual(s['stage_path'],[
            'NEW','DESIGN','IMPLEMENTATION','TEST_CLAUDE','VERIFY_CODEX','DONE'])
        team.advance(self.root,p,s,'DESIGN')
        team.advance(self.root,p,s,'IMPLEMENTATION')
        team.advance(self.root,p,s,'TEST_CLAUDE')
        self.pass_report('tester','claude-tweak',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('verifier','codex-tweak',p,s)
        self.finish_report(p)
        self.assertIsNone(s['knowledge'])
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')

    def test_full_and_risky_profiles_keep_complete_flow(self):
        self.assertEqual(team.WORKFLOWS['full'],team.STAGES)
        self.assertEqual(team.WORKFLOWS['risky'],team.STAGES)

    def test_role_launch_respects_profile_stage(self):
        p=team.create(self.root,'SMALL-002','small task','small')
        with self.assertRaises(ValueError):team.launch(self.root,'analyst','SMALL-002',True)
        with self.assertRaises(ValueError):team.launch(self.root,'designer','SMALL-002',True)
        s=team.read(p/'state.json');team.advance(self.root,p,s,'IMPLEMENTATION');team.atomic(p/'state.json',s)
        self.assertIn('Claude',team.launch(self.root,'developer','SMALL-002',True))

    def test_reclassify_keeps_shared_stage_and_resets_evidence_cli(self):
        self.s.update(stage='DESIGN',claude_test={'result':'PASS'},codex_verify={'result':'PASS'},knowledge={'summary':'old'})
        team.atomic(self.p/'state.json',self.s)
        (self.root/'.ai/CURRENT_STATE.md').write_text('Written during the old profile')
        self.cli('reclassify','DEMO-001','tweak','--reason','narrow visual follow-up')
        state=team.read(self.p/'state.json')
        self.assertEqual(state['profile'],'tweak')
        # DESIGN exists in both paths, so the completed design is not repeated.
        self.assertEqual(state['stage'],'DESIGN')
        self.assertEqual(state['stage_path'],team.WORKFLOWS['tweak'])
        for key in ['claude_test','codex_verify','knowledge']:self.assertIsNone(state[key])
        self.assertEqual(state['knowledge_baseline'],team.knowledge_hashes(self.root))
        self.assertNotEqual(state['knowledge_baseline'],self.s['knowledge_baseline'])

    def test_reclassify_uses_nearest_previous_common_stage_cli(self):
        self.s.update(stage='KNOWLEDGE_UPDATE',claude_test={'result':'PASS'},
                      codex_verify={'result':'PASS'},knowledge={'summary':'old'})
        team.atomic(self.p/'state.json',self.s)
        self.cli('reclassify','DEMO-001','tweak','--reason','narrow visual follow-up')
        state=team.read(self.p/'state.json')
        # KNOWLEDGE_UPDATE is absent from tweak; VERIFY_CODEX is its closest prior shared stage.
        self.assertEqual(state['stage'],'VERIFY_CODEX')
        for key in ['claude_test','codex_verify','knowledge']:self.assertIsNone(state[key])

    def test_reclassify_baseline_forces_new_knowledge_update_cli(self):
        (self.root/'.ai/CURRENT_STATE.md').write_text('Outcome written before reclassification')
        self.cli('reclassify','DEMO-001','ui','--reason','ui impact discovered')
        state=team.read(self.p/'state.json');state['stage']='KNOWLEDGE_UPDATE'
        team.atomic(self.p/'state.json',state)
        # The pre-reclassification document must no longer satisfy the new workflow.
        self.cli('knowledge','DEMO-001','--summary','stale document',ok=False)
        self.knowledge_update_cli('DEMO-001','Outcome written after reclassification')
        self.assertIsNotNone(team.read(self.p/'state.json')['knowledge'])

    def test_small_profile_end_to_end_two_independent_tests_cli(self):
        p=team.create(self.root,'SMALL-E2E','small end to end','small')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'IMPLEMENTATION')
        team.advance(self.root,p,s,'TEST_CLAUDE')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('tester','claude-small',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.pass_report('verifier','codex-small',p,s)
        team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.finish_report(p)
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        team.atomic(p/'state.json',s)
        self.knowledge_update_cli('SMALL-E2E','Small fixture task completed')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')
        self.assertEqual(s['claude_test']['engine'],'Claude Code')
        self.assertEqual(s['codex_verify']['engine'],'Codex')
        self.assertNotEqual(s['claude_test']['session'],s['codex_verify']['session'])
        self.assertNotIn('STATUS: NOT_RUN',(p/'FINAL_REPORT.md').read_text())

    def test_ui_profile_end_to_end_and_analyst_rejected_cli(self):
        p=team.create(self.root,'UI-E2E','ui end to end','ui')
        s=team.read(p/'state.json')
        with self.assertRaises(ValueError):team.launch(self.root,'analyst','UI-E2E',True)
        team.advance(self.root,p,s,'DESIGN')
        with self.assertRaises(ValueError):team.launch(self.root,'analyst','UI-E2E',True)
        team.advance(self.root,p,s,'IMPLEMENTATION')
        team.advance(self.root,p,s,'TEST_CLAUDE')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('tester','claude-ui',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.pass_report('verifier','codex-ui',p,s)
        team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.finish_report(p)
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        team.atomic(p/'state.json',s)
        self.knowledge_update_cli('UI-E2E','Ui fixture task completed')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')
        self.assertEqual(s['codex_verify']['engine'],'Codex')
        self.assertNotEqual(s['claude_test']['session'],s['codex_verify']['session'])

    def test_risky_profile_gate_blocks_done_after_valid_evidence_cli(self):
        p=team.create(self.root,'RISKY-E2E','risky end to end','risky')
        s=team.read(p/'state.json')
        self.assertEqual(s['stage_path'],team.STAGES)
        for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION','TEST_CLAUDE']:team.advance(self.root,p,s,stage)
        self.pass_report('tester','claude-risky',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('verifier','codex-risky',p,s)
        team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.finish_report(p)
        team.atomic(p/'state.json',s)
        self.cli('gate','RISKY-E2E','--kind','HIGH_IMPACT','--action','fixture push','--target','single mock token','--environment','mock')
        self.knowledge_update_cli('RISKY-E2E','Risky fixture task completed')
        s=team.read(p/'state.json')
        # Evidence and knowledge are valid; only the open human gate remains.
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        gate=s['gates'][0]['id']
        self.cli('resolve-gate','RISKY-E2E',gate,'--user-message-ref','fixture-only','--outcome','cancelled')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')

    def test_risky_profile_requires_recorded_human_gate_before_done_cli(self):
        p=team.create(self.root,'RISKY-GATE','risky without gate','risky')
        s=team.read(p/'state.json')
        for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION','TEST_CLAUDE']:team.advance(self.root,p,s,stage)
        self.pass_report('tester','claude-gate',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('verifier','codex-gate',p,s)
        team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.finish_report(p)
        team.atomic(p/'state.json',s)
        self.knowledge_update_cli('RISKY-GATE','Risky fixture task without gate')
        s=team.read(p/'state.json')
        # Evidence, knowledge and report are valid; no human gate was ever recorded.
        self.assertEqual(s['gates'],[])
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'KNOWLEDGE_UPDATE')
        self.cli('gate','RISKY-GATE','--kind','HIGH_IMPACT','--action','fixture push','--target','single mock token','--environment','mock')
        s=team.read(p/'state.json')
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        gate=s['gates'][0]['id']
        self.cli('resolve-gate','RISKY-GATE',gate,'--user-message-ref','fixture-only','--outcome','cancelled')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')

    def test_workflow_stage_paths_are_independent_copies(self):
        for profile in ['full','risky']:
            self.assertEqual(team.WORKFLOWS[profile],team.STAGES)
            self.assertIsNot(team.WORKFLOWS[profile],team.STAGES)
        self.assertIsNot(team.WORKFLOWS['full'],team.WORKFLOWS['risky'])

    def test_verifier_tests_do_not_invalidate_claude_evidence(self):
        """A verifier must be able to record findings after adding its own tests.

        Gating on the full tree deadlocked the role: writing the regression test
        that proved a defect also changed the fingerprint it was checked against.
        """
        before_full = team.snapshot(self.root)
        before_prod = team.production_snapshot(self.root)

        test_dir = self.root / 'test'
        test_dir.mkdir(exist_ok=True)
        (test_dir / 'verifier_probe_test.txt').write_text('independent check')

        self.assertEqual(team.production_snapshot(self.root), before_prod)
        self.assertNotEqual(team.snapshot(self.root), before_full)

    def test_production_change_still_invalidates_evidence(self):
        """The safety property survives: touching production source breaks the match."""
        before = team.production_snapshot(self.root)
        (self.root / 'source.txt').write_text('v2')
        self.assertNotEqual(team.production_snapshot(self.root), before)

    def git(self,*args):
        subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)

    def write(self,rel,text):
        f=self.root/rel;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(text);return f

    def test_untracked_xcuserdata_and_package_resolved_are_tool_state(self):
        self.git('init','-q')
        user='App.xcodeproj/xcuserdata/dev.xcuserdatad/UserInterfaceState.xcuserstate'
        resolved='App.xcworkspace/xcshareddata/swiftpm/Package.resolved'
        self.write(user,'a');self.write(resolved,'a')
        before=team.snapshot(self.root)
        self.write(user,'b');self.write(resolved,'b')
        self.assertEqual(team.snapshot(self.root),before)
        self.assertEqual(team.production_snapshot(self.root),team.production_snapshot(self.root))

    def test_tracked_xcuserdata_and_package_resolved_still_count(self):
        user='App.xcodeproj/xcuserdata/dev.xcuserdatad/UserInterfaceState.xcuserstate'
        resolved='App.xcworkspace/xcshareddata/swiftpm/Package.resolved'
        for rel in (user,resolved):
            self.git('init','-q') if not (self.root/'.git').exists() else None
            self.write(rel,'a');self.git('add','-f',rel)
            before=team.snapshot(self.root);self.write(rel,'b')
            self.assertNotEqual(team.snapshot(self.root),before,rel)

    def test_other_package_resolved_paths_and_sources_still_count(self):
        self.git('init','-q')
        for rel in ('Package.resolved','Other/xcshareddata/swiftpm/Package.resolved','Sources/main.swift'):
            self.write(rel,'a');before=team.snapshot(self.root);self.write(rel,'b')
            self.assertNotEqual(team.snapshot(self.root),before,rel)

    def test_small_profile_rejects_design_stage(self):
        p=team.create(self.root,'SMALL-NODESIGN','small task','small')
        s=team.read(p/'state.json')
        self.assertNotIn('DESIGN',s['stage_path'])
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DESIGN')
        with self.assertRaises(ValueError):team.launch(self.root,'designer','SMALL-NODESIGN',True)
        self.assertEqual(s['stage'],'NEW')
        team.advance(self.root,p,s,'IMPLEMENTATION')

    def test_reclassify_rejected_while_developer_lock_active_cli(self):
        for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION']:self.cli('advance','DEMO-001',stage)
        self.cli('claim','DEMO-001','--session','dev-lock')
        self.cli('reclassify','DEMO-001','risky','--reason','locked source',ok=False)
        state=team.read(self.p/'state.json')
        self.assertEqual(state['profile'],'full')
        self.assertEqual(state['stage'],'IMPLEMENTATION')
        self.cli('release','DEMO-001','--session','dev-lock')
        self.cli('reclassify','DEMO-001','risky','--reason','blast radius discovered')
        self.assertEqual(team.read(self.p/'state.json')['profile'],'risky')

    def test_rework_baseline_forces_new_knowledge_update_cli(self):
        (self.root/'.ai/CURRENT_STATE.md').write_text('Outcome written before rework')
        self.cli('rework','DEMO-001','--reason','negative fixture fails')
        state=team.read(self.p/'state.json')
        self.assertEqual(state['knowledge_baseline'],team.knowledge_hashes(self.root))
        self.assertNotEqual(state['knowledge_baseline'],self.s['knowledge_baseline'])
        state['stage']='KNOWLEDGE_UPDATE';team.atomic(self.p/'state.json',state)
        # The document written before the rework must no longer satisfy the new cycle.
        self.cli('knowledge','DEMO-001','--summary','stale document',ok=False)
        self.knowledge_update_cli('DEMO-001','Outcome written after rework')
        self.assertIsNotNone(team.read(self.p/'state.json')['knowledge'])

    def test_reclassify_invalidates_gates_but_keeps_history_cli(self):
        self.cli('gate','DEMO-001','--kind','HIGH_IMPACT','--action','fixture push','--target','single mock token','--environment','mock')
        gate=team.read(self.p/'state.json')['gates'][0]['id']
        self.cli('resolve-gate','DEMO-001',gate,'--user-message-ref','fixture-only','--outcome','explicitly-approved')
        self.cli('reclassify','DEMO-001','risky','--reason','blast radius discovered')
        state=team.read(self.p/'state.json')
        self.assertEqual(state['gates'],[])
        self.assertEqual(len(state['gate_history']),1)
        old=state['gate_history'][0]
        self.assertEqual(old['id'],gate)
        self.assertEqual(old['status'],'INVALIDATED_BY_RECLASSIFY')
        self.assertEqual(old['previous_status'],'RESOLVED_BY_LEAD')
        self.assertEqual(old['invalidation_reason'],'blast radius discovered')
        self.assertIn('invalidated',old)

    def test_reclassified_risky_task_requires_a_new_gate_cli(self):
        self.cli('gate','DEMO-001','--kind','HIGH_IMPACT','--action','fixture push','--target','single mock token','--environment','mock')
        gate=team.read(self.p/'state.json')['gates'][0]['id']
        self.cli('resolve-gate','DEMO-001',gate,'--user-message-ref','fixture-only','--outcome','cancelled')
        self.cli('reclassify','DEMO-001','risky','--reason','blast radius discovered')
        p=self.p;s=team.read(p/'state.json')
        for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION','TEST_CLAUDE']:team.advance(self.root,p,s,stage)
        self.pass_report('tester','claude-reclass',p,s)
        team.advance(self.root,p,s,'VERIFY_CODEX')
        self.pass_report('verifier','codex-reclass',p,s)
        team.advance(self.root,p,s,'KNOWLEDGE_UPDATE')
        self.finish_report(p)
        team.atomic(p/'state.json',s)
        self.knowledge_update_cli('DEMO-001','Reclassified fixture task completed')
        s=team.read(p/'state.json')
        # The old approval is history only; the risky profile still needs its own gate.
        with self.assertRaises(ValueError):team.advance(self.root,p,s,'DONE')
        self.cli('gate','DEMO-001','--kind','HIGH_IMPACT','--action','fixture push','--target','single mock token','--environment','mock')
        s=team.read(p/'state.json')
        new_gate=s['gates'][0]['id']
        self.assertNotEqual(new_gate,gate)
        self.cli('resolve-gate','DEMO-001',new_gate,'--user-message-ref','fixture-only','--outcome','cancelled')
        s=team.read(p/'state.json')
        team.advance(self.root,p,s,'DONE')
        self.assertEqual(s['stage'],'DONE')

    def test_developer_launch_rejected_while_other_task_holds_lock_cli(self):
        other=team.create(self.root,'DEMO-002','second')
        for ident in ['DEMO-001','DEMO-002']:
            for stage in ['ANALYSIS','DESIGN','IMPLEMENTATION']:self.cli('advance',ident,stage)
        self.cli('claim','DEMO-002','--session','dev-2')
        with self.assertRaises(ValueError):team.launch(self.root,'developer','DEMO-001',True)
        self.cli('release','DEMO-002','--session','dev-2')
        self.assertIn('Claude',team.launch(self.root,'developer','DEMO-001',True))
        # The lock holder may still resume its own task.
        self.cli('claim','DEMO-002','--session','dev-2')
        self.assertIn('Claude',team.launch(self.root,'developer','DEMO-002',True))

    def test_every_role_except_lead_requires_task_id(self):
        for role in ['analyst','designer','developer','tester','verifier']:
            with self.assertRaises(ValueError):team.launch(self.root,role,None,True)
        self.assertIn('Claude',team.launch(self.root,'lead',None,True))

    def test_full_profile_rejects_wrong_stage_role_launch(self):
        p=team.create(self.root,'FULL-002','full task','full')
        s=team.read(p/'state.json')
        for role in ['designer','developer','tester','verifier']:
            with self.assertRaises(ValueError):team.launch(self.root,role,'FULL-002',True)
        team.advance(self.root,p,s,'ANALYSIS');team.atomic(p/'state.json',s)
        for role in ['designer','developer','tester','verifier']:
            with self.assertRaises(ValueError):team.launch(self.root,role,'FULL-002',True)
        team.advance(self.root,p,s,'DESIGN');team.atomic(p/'state.json',s)
        for role in ['analyst','developer','tester','verifier']:
            with self.assertRaises(ValueError):team.launch(self.root,role,'FULL-002',True)
        team.advance(self.root,p,s,'IMPLEMENTATION');team.atomic(p/'state.json',s)
        for role in ['analyst','designer','tester','verifier']:
            with self.assertRaises(ValueError):team.launch(self.root,role,'FULL-002',True)
        self.assertIn('Claude',team.launch(self.root,'developer','FULL-002',True))
        team.advance(self.root,p,s,'TEST_CLAUDE');team.atomic(p/'state.json',s)
        with self.assertRaises(ValueError):team.launch(self.root,'developer','FULL-002',True)
        with self.assertRaises(ValueError):team.launch(self.root,'verifier','FULL-002',True)
        self.assertIn('Claude',team.launch(self.root,'tester','FULL-002',True))

    def set_scope(self,p,*globs):
        (p/'TASK.md').write_text('# T\n\n## Dosyalar\n'+''.join(f'- `{g}`\n' for g in globs)+'\n## Sonraki\nx\n')

    def test_scoped_fingerprint_ignores_other_tasks_files(self):
        (self.root/'a').mkdir();(self.root/'b').mkdir()
        (self.root/'a/x.txt').write_text('1');(self.root/'b/y.txt').write_text('1')
        self.set_scope(self.p,'a/**')
        self.assertEqual(team.task_scope(self.p),['a/**'])
        before=team.production_snapshot(self.root,['a/**'])
        (self.root/'b/y.txt').write_text('2')
        self.assertEqual(before,team.production_snapshot(self.root,['a/**']))
        self.assertNotEqual(team.production_snapshot(self.root),team.production_snapshot(self.root,['a/**']))
        (self.root/'a/x.txt').write_text('2')
        self.assertNotEqual(before,team.production_snapshot(self.root,['a/**']))

    def test_scoped_evidence_survives_other_task_change_but_unscoped_does_not(self):
        (self.root/'a').mkdir();(self.root/'b').mkdir();(self.root/'b/y.txt').write_text('1')
        (self.root/'a/x.txt').write_text('1')
        for scope in (['a/**'],None):
            p=team.create(self.root,'S-%d'%(scope is None),'scoped','small');s=team.read(p/'state.json')
            if scope: self.set_scope(p,*scope)
            s['stage']='TEST_CLAUDE';self.pass_report('tester','t1',p,s)
            s['stage']='VERIFY_CODEX';(self.root/'b/y.txt').write_text(str(scope))
            if scope:
                self.pass_report('verifier','v1',p,s)
            else:
                with self.assertRaises(ValueError):self.pass_report('verifier','v1',p,s)

    def test_task_without_scope_section_is_whole_repo(self):
        self.assertIsNone(team.task_scope(self.p))
        self.set_scope(self.p)
        self.assertIsNone(team.task_scope(self.p))

    def test_disjoint_scopes_may_hold_developer_locks_together_cli(self):
        a=team.create(self.root,'LK-A','a','small');b=team.create(self.root,'LK-B','b','small');c=team.create(self.root,'LK-C','c','small')
        self.set_scope(a,'lib/leave/**');self.set_scope(b,'lib/expense/**','test/tester_mob_141*')
        for ident in ('LK-A','LK-B','LK-C'):
            self.cli('advance',ident,'IMPLEMENTATION')
        self.cli('claim','LK-A','--session','s1')
        self.cli('claim','LK-B','--session','s2')
        self.cli('claim','LK-C','--session','s3',ok=False)  # unscoped = global lock
        self.set_scope(b,'lib/leave/sub/**')
        self.cli('release','LK-B','--session','s2')
        self.cli('claim','LK-B','--session','s2',ok=False)  # overlaps LK-A

    def test_fourth_rework_blocks_task_and_resume_resets_cli(self):
        p=team.create(self.root,'RW-1','rw','small',True);self.cli('advance','RW-1','IMPLEMENTATION')
        for _ in range(3):
            self.cli('rework','RW-1','--reason','x')
        self.assertNotEqual(team.read(p/'state.json')['automation']['status'],'BLOCKED')
        self.cli('rework','RW-1','--reason','x')
        auto=team.read(p/'state.json')['automation']
        self.assertEqual(auto['status'],'BLOCKED');self.assertIn('3 düzeltme turu aşıldı',auto['message'])
        self.cli('automate','RW-1','--resume')
        self.assertEqual(team.read(p/'state.json')['rework_count'],0)

    def git_repo(self):
        env={'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.com','GIT_COMMITTER_NAME':'t','GIT_COMMITTER_EMAIL':'t@example.com'}
        patcher=patch.dict('os.environ',env);patcher.start();self.addCleanup(patcher.stop)
        g=lambda *a: subprocess.run(['git','-C',str(self.root),*a],capture_output=True,text=True,check=True).stdout
        g('init','-q');g('add','-A');g('commit','-qm','init')
        return g

    def test_changed_files_and_commit_dry_run_apply_cli(self):
        g=self.git_repo()
        p=team.create(self.root,'CM-1','Commit fixture','small');self.cli('advance','CM-1','IMPLEMENTATION')
        (self.root/'preexisting.txt').write_text('other task dirt')  # dirty before claim: not ours
        self.cli('claim','CM-1','--session','s1')
        (self.root/'source.txt').write_text('v2');(self.root/'new.txt').write_text('n')
        (self.root/'.ai/scratch.md').write_text('coordination')
        self.cli('release','CM-1','--session','s1')
        self.assertEqual(team.read(p/'state.json')['changed_files'],['new.txt','source.txt'])
        o=team.create(self.root,'CM-2','other','small');s=team.read(o/'state.json');s['changed_files']=['source.txt'];team.atomic(o/'state.json',s)
        out=self.cli('commit','CM-1').stdout
        self.assertIn('DRY-RUN',out);self.assertIn('UYARI: açık görev CM-2',out);self.assertIn('CM-1: Commit fixture',out)
        self.assertIn('UYARI: görev IMPLEMENTATION',out);self.assertEqual(g('log','--oneline').count('\n'),1)
        self.cli('commit','CM-1','--apply',ok=False)  # not DONE without --force
        g('add','preexisting.txt')
        self.assertIn('staged',self.cli('commit','CM-1','--apply','--force',ok=False).stderr)
        g('reset','-q','preexisting.txt')
        self.cli('commit','CM-1','--apply','--force')
        self.assertEqual(g('show','--name-only','--format=').split(),['new.txt','source.txt'])
        self.assertEqual(team.read(p/'state.json')['commit'],g('rev-parse','HEAD').strip())
        self.assertIn('preexisting.txt',g('status','--porcelain'))
        self.assertIn('Commit edilecek değişiklik yok',self.cli('commit','CM-1').stdout)

class PortabilityTests(unittest.TestCase):
    def test_init_creates_defaults_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'.ai').mkdir(); (Path(d)/'.ai/models.json').write_text('{"x":1}')
            team.init_project(d)
            self.assertEqual(json.loads((Path(d)/'.ai/models.json').read_text()),{'x':1})
            self.assertEqual(json.loads((Path(d)/'.ai/runner.json').read_text())['max_parallel'],3)
            self.assertIn('nothing to create',team.init_project(d))

    def test_init_full_skeleton_idempotent_and_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); team.init_project(d,dry_run=True); self.assertEqual(list(d.iterdir()),[])
            team.init_project(d)
            for f in ['CLAUDE.md','AGENTS.md','.ai/PROJECT.md','.ai/OWNERSHIP.md','.ai/CURRENT_STATE.md','.ai/KNOWN_ISSUES.md','.ai/DECISIONS.md','.ai/TEAM_RULES.md','.ai/models.json','.ai/runner.json']+['roles/%s.md'%r for r in team.ROLES]:
                self.assertTrue((d/f).is_file(),f)
            for r in team.ROLES: self.assertTrue((d/'.ai/messages'/r).is_dir())
            self.assertTrue((d/'tasks').is_dir())
            snap={p:p.read_bytes() for p in d.rglob('*') if p.is_file()}
            self.assertIn('nothing to create',team.init_project(d))
            self.assertEqual(snap,{p:p.read_bytes() for p in d.rglob('*') if p.is_file()})

    def test_init_keeps_existing_claude_md_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'CLAUDE.md').write_text('mine')
            out=team.init_project(d)
            self.assertEqual((Path(d)/'CLAUDE.md').read_text(),'mine'); self.assertIn('CLAUDE.md mevcut',out)

    def test_install_copies_skills_and_backs_up_changed(self):
        with tempfile.TemporaryDirectory() as h:
            env={**__import__('os').environ,'HOME':h}
            run=lambda: subprocess.run(['bash',str(team.ROOT/'install.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(run().returncode,0)
            f=Path(h)/'.claude/skills/orchestra-setup/SKILL.md'
            self.assertIn('name: orchestra-setup',f.read_text())
            f.write_text('edited'); out=run()
            self.assertIn('SKILL.md.bak',out.stdout); self.assertEqual((f.parent/'SKILL.md.bak').read_text(),'edited')
            self.assertIn('name: orchestra-setup',f.read_text())

    def test_doctor_reports_project_files(self):
        with tempfile.TemporaryDirectory() as d:
            team.init_project(d)
            text='\n'.join(team.doctor(Path(d))[0])
            self.assertIn('.ai/runner.json',text); self.assertNotIn('WARN .ai/',text)

    def test_projects_file_prefers_user_config(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'XDG_CONFIG_HOME':d}):
            self.assertEqual(team.projects_file(),team.ROOT/'projects.json')
            (Path(d)/'ai-team').mkdir(); (Path(d)/'ai-team/projects.json').write_text('{"a":{}}')
            self.assertEqual(team.load_projects(),{'a':{}})

    def test_no_hardcoded_home_paths(self):
        for f in list((team.ROOT/'bin').glob('*.py'))+list((team.ROOT/'bin').glob('*.sh'))+list((team.ROOT/'templates').rglob('*.md')):
            if f.name.startswith('test_'): continue
            self.assertNotIn(str(Path.home()),f.read_text(),str(f))

if __name__=='__main__':unittest.main(verbosity=2)
