"""End-to-end runner scenarios with fake `claude`/`codex` executables; no network, no real agents."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import team
import team_runner

# One script serves both engines (argv[0] tells which). Per task/role, FAKE_PLAN_DIR/<ID>.<role>
# holds whitespace-separated modes consumed one per attempt: ok | fail | limit | enotfound | crash | stall | ask.
FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
engine = Path(sys.argv[0]).name
src = Path.cwd(); root = Path(os.environ.get('AI_TEAM_ROOT') or src); ident = os.environ['AI_TEAM_TASK']; role = os.environ['AI_TEAM_ROLE']; run = os.environ['AI_TEAM_RUN_ID']
plans = Path(os.environ['FAKE_PLAN_DIR']); plan = plans / f'{ident}.{role}'
mode = 'ok'
if plan.is_file():
    modes = plan.read_text().split()
    if modes:
        mode = modes[0]; plan.write_text(' '.join(modes[1:]))
with open(plans / f'{ident}.{role}.argv', 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
if mode == 'crash':
    sys.exit(137)  # killed: no output at all
out = lambda ev: print(json.dumps(ev), flush=True)
clock = __import__('time').time() + float(os.environ.get('TEAM_NOW_OFFSET') or 0)
def window(util, status='allowed'):
    out({'type': 'rate_limit_event', 'rate_limit_info': {'status': status, 'rateLimitType': 'five_hour', 'resetsAt': int(clock + 3600),
         'unifiedWindows': {'five_hour': {'utilization': util, 'resetsAt': int(clock + 3600)},
                            'seven_day': {'utilization': 0.2, 'resetsAt': int(clock + 5 * 86400)}}}})
if mode.startswith('usage:'):
    window(0.5); window(float(mode.split(':')[1])); mode = 'ok'
if engine == 'codex':
    print('Reading additional input from stdin...', flush=True)
    out({'type': 'thread.started', 'thread_id': 'thr-' + run}); out({'type': 'turn.started'})
else:
    out({'type': 'system', 'subtype': 'init', 'session_id': run, 'model': 'claude-fake'})
if mode == 'limit':
    out({'type': 'result', 'subtype': 'success', 'is_error': True,
         'result': "You've hit your session limit · resets 9:10pm"}); sys.exit(1)
if mode == 'enotfound':
    print('API Error: getaddrinfo ENOTFOUND api.anthropic.com', flush=True); sys.exit(1)
if mode == 'limit429':  # some work done, then the 5 h window is cut
    window(0.6)
    out({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'file_path': 'x'}}]}})
    window(1.01, 'rejected')
    out({'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
         'result': "You've hit your session limit · resets 9:10pm"}); sys.exit(1)

def team(*args):
    r = subprocess.run([sys.executable, os.environ['FAKE_TEAM'], '--project', str(root), *args],
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stderr, flush=True); sys.exit(r.returncode)

task = root / 'tasks' / ident
state = json.loads((task / 'state.json').read_text())
path = state['stage_path']; nxt = path[path.index(state['stage']) + 1]
report = lambda status: f'STATUS: {status}\nKomut: fake test\nOrtam: e2e\nSonuc: {status}\n' + 'ayrinti satiri\n' * 12
if mode == 'stall':  # exits cleanly without advancing (e.g. waits for a Lead decision)
    (task / 'STALL.md').write_text(f'{role} BLOCKED pending Lead decision\n')
elif role == 'lead' and state['stage'] not in ('NEW', 'KNOWLEDGE_UPDATE'):  # Lead decision turn
    with (task / 'TASK.md').open('a') as f:
        f.write('\n## Lead sorusu\nHangi kural?\n' if mode == 'ask' else '\n## Lead kararı\nfake karar\n')
    if mode != 'ask':
        team('message', ident, '--from', 'lead', '--to', 'verifier', '--body', 'fake karar')
elif role == 'lead' and state['stage'] == 'NEW' and mode == 'ask':
    with (task / 'TASK.md').open('a') as f: f.write('\n## Lead sorusu\nHangi ekran?\n')
elif role == 'lead' and state['stage'] == 'NEW':
    with (task / 'TASK.md').open('a') as f: f.write('\n## Kabul kriterleri\n- fake\n')
    team('advance', ident, nxt)
elif role in ('analyst', 'designer'):
    (task / ('ANALYSIS.md' if role == 'analyst' else 'DESIGN.md')).write_text('STATUS: DONE\nfake\n')
    team('advance', ident, nxt)
elif role == 'developer':
    team('claim', ident, '--session', run)
    files = (plans / f'{ident}.files').read_text().split() if (plans / f'{ident}.files').is_file() else [f'src/{ident}.txt']
    for rel in files:
        (src / rel).parent.mkdir(parents=True, exist_ok=True); (src / rel).write_text(f'{ident} {run}\n')
    (task / 'IMPLEMENTATION.md').write_text('STATUS: DONE\n' + ' '.join(files))
    team('release', ident, '--session', run); team('advance', ident, nxt)
elif role == 'tester' and mode == 'fail':
    (task / 'TEST_CLAUDE.md').write_text(report('FAIL'))
    team('evidence', ident, '--role', 'tester', '--result', 'FAIL', '--session', run, '--note', 'fake fail')
    team('rework', ident, '--reason', os.environ.get('FAKE_FAIL_REASON') or 'fake test failure ' + run)
elif role == 'tester':
    (src / 'test').mkdir(exist_ok=True); (src / 'test' / f'{ident}_test.txt').write_text('tester test\n')
    out({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Write',
         'input': {'file_path': str(src / 'test' / f'{ident}_test.txt')}}]}})  # the runner log shows the Tester wrote a test
    (task / 'TEST_CLAUDE.md').write_text(report('PASS'))
    team('evidence', ident, '--role', 'tester', '--result', 'PASS', '--session', run, '--note', 'fake pass')
    team('advance', ident, nxt)
elif role == 'verifier':
    (task / 'VERIFY_CODEX.md').write_text(report('PASS') + '## Bağımsız kontroller\n- fake kontrol\n')
    (task / 'FINAL_REPORT.md').write_text(report('PASS'))
    team('evidence', ident, '--role', 'verifier', '--result', 'PASS', '--session', run, '--note', 'fake verify')
    team('advance', ident, nxt)
elif role == 'lead':
    with (root / '.ai/CURRENT_STATE.md').open('a') as f: f.write(f'\n{ident} kapandi')
    team('knowledge', ident, '--summary', 'fake knowledge'); team('advance', ident, nxt)
if engine == 'codex':
    out({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'bitti'}})
    out({'type': 'turn.completed', 'usage': {'input_tokens': 500, 'cached_input_tokens': 400,
                                             'output_tokens': 30, 'reasoning_output_tokens': 5}})
else:
    out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'bitti'}]}})
    out({'type': 'result', 'subtype': 'success', 'num_turns': 3, 'total_cost_usd': 0.01,
         'usage': {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 300,
                   'cache_creation_input_tokens': 40}, 'modelUsage': {'claude-fake': {}}})
'''

TIMING = (1500, 600, 600)


class E2E(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / 'proj'; self.root.mkdir()
        team.init_project(self.root)
        (self.root / '.ai/project.json').write_text(json.dumps({'id': 'e2e', 'source_roots': [str(self.root)]}))
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        fakebin = base / 'fakebin'; fakebin.mkdir()
        for name in ('claude', 'codex'):
            (fakebin / name).write_text(FAKE); (fakebin / name).chmod(0o755)
        self.plans = base / 'plans'; self.plans.mkdir()
        env = {'PATH': f'{fakebin}{os.pathsep}{os.environ["PATH"]}', 'FAKE_TEAM': str(BIN / 'team.py'),
               'FAKE_PLAN_DIR': str(self.plans), 'TEAM_NOW_OFFSET': '0',
               'XDG_CONFIG_HOME': str(base / 'cfg'), 'CODEX_HOME': str(base / 'codex')}
        p = patch.dict(os.environ, env); p.start(); self.addCleanup(p.stop)
        for k in ('TEAM_VERIFIER_ENGINE', 'CMUX_WORKSPACE_ID', 'CMUX_SURFACE_ID', 'AI_TEAM_RUN_ID'):
            os.environ.pop(k, None)
        p = patch.object(team_runner, 'CLAUDE_ROLES', {'lead', 'analyst', 'designer', 'developer', 'tester'})
        p.start(); self.addCleanup(p.stop)
        team_runner._warned.clear()  # warn-once memory is per runner process
        team_runner._preflight.clear(); team_runner._inputs.clear()
        p = patch.object(team_runner, 'sandbox_probe', return_value=None); p.start(); self.addCleanup(p.stop)
        self.log = io.StringIO()  # runner log (stamp output from all threads)
        out = contextlib.redirect_stdout(self.log); out.__enter__(); self.addCleanup(out.__exit__, None, None, None)

    def task(self, ident, profile='small', scope=None, plan=None, files=None):
        p = team.create(self.root, ident, 'Title ' + ident, profile, True, scope)
        with team.locked(self.root):  # Lead's first step: NEW -> first work stage
            s = team.read(p / 'state.json'); team.advance(self.root, p, s, s['stage_path'][1]); team.atomic(p / 'state.json', s)
        for role, modes in (plan or {}).items():
            (self.plans / f'{ident}.{role}').write_text(modes)
        if files:
            (self.plans / f'{ident}.files').write_text(' '.join(files))
        return p

    def round(self):
        return team_runner.watch_loop(self.root, None, 0, TIMING, once_round=True)

    def drive(self, rounds=40):
        for _ in range(rounds):
            running = self.round()
            if not running:
                return
            for thread, _, _ in running.values():
                thread.join(30)

    def state(self, ident):
        return team.read(self.root / 'tasks' / ident / 'state.json')

    def metrics(self):
        f = self.root / '.ai/metrics.jsonl'
        return [json.loads(l) for l in f.read_text().splitlines()] if f.is_file() else []

    def test_a_small_task_runs_to_done_by_itself(self):
        self.task('APP-001')
        self.drive()
        s = self.state('APP-001')
        self.assertEqual((s['stage'], s['automation']['status']), ('DONE', 'COMPLETED'), self.log.getvalue())
        self.assertEqual(s['changed_files'], ['src/APP-001.txt'])
        self.assertEqual(s['independence'], 'independent')
        self.assertEqual(s['codex_verify']['engine_id'], 'codex'); self.assertEqual(s['claude_test']['engine_id'], 'claude')
        runs = [m for m in self.metrics() if m['type'] == 'run']
        self.assertEqual([m['role'] for m in runs], ['developer', 'tester', 'verifier', 'lead'])
        self.assertEqual(runs[0]['tokens']['cache_read'], 300); self.assertEqual(runs[0]['cost_usd'], 0.01)
        self.assertEqual((runs[2]['tokens']['input'], runs[2]['tokens']['cache_read']), (100, 400))
        self.assertIsNone(runs[2]['cost_usd']); self.assertEqual(runs[2]['engine'], 'codex')
        self.assertFalse([m for m in self.metrics() if m['type'] == 'intervention'])  # runner flow only
        rep = team.report(self.root)
        self.assertIn('Kapanan görev: 1 (small 1)', rep); self.assertIn('bağımsız 1 / tek model 0', rep)
        self.assertIn('PASS/PASS (bağımsız)', self.log.getvalue())

    def test_b_fix_done_changelog_and_risk_escalation(self):
        cfg = team.read(self.root / '.ai/runner.json'); cfg['risk_paths'] = ['core/**']
        team.atomic(self.root / '.ai/runner.json', cfg)
        self.task('APP-002', 'fix')
        self.task('APP-003', 'fix', files=['core/db.txt'])
        self.drive()
        a, b = self.state('APP-002'), self.state('APP-003')
        self.assertEqual((a['stage'], a['profile'], a['independence']), ('DONE', 'fix', 'single-model'))
        self.assertIsNone(a['codex_verify'])
        log = (self.root / '.ai/CHANGELOG.md').read_text()
        self.assertIn('APP-002 — Title APP-002 — PASS', log); self.assertNotIn('APP-003', log)
        self.assertEqual((b['stage'], b['profile']), ('DONE', 'small'))
        self.assertEqual(b['escalated']['files'], ['core/db.txt'])
        self.assertEqual(b['codex_verify']['result'], 'PASS')
        self.assertIn('YÜKSELTİLDİ', self.log.getvalue())

    def wait_then_resume(self, ident, mode, offset, result='limit'):
        self.task(ident, plan={'developer': mode})
        self.drive()
        s = self.state(ident)
        self.assertEqual(s['automation']['status'], 'WAITING_LIMIT', self.log.getvalue())
        self.assertGreater(team_runner.datetime.fromisoformat(s['automation']['retry_at']), team.utcnow())
        self.assertEqual(s['stage'], 'IMPLEMENTATION')
        os.environ['TEAM_NOW_OFFSET'] = str(offset)  # time passes without waiting
        self.drive()
        self.assertEqual(self.state(ident)['stage'], 'DONE', self.log.getvalue())
        self.assertIn(result, [m['result'] for m in self.metrics() if m['type'] == 'run'])

    def test_c_session_limit_waits_until_reset_then_finishes(self):
        self.wait_then_resume('APP-004', 'limit', 2 * 86400)

    def test_d_network_error_waits_then_finishes(self):
        self.wait_then_resume('APP-005', 'enotfound', 3600, 'network')

    def test_e_tester_fail_reworks_then_blocks_after_three_rounds(self):
        self.task('APP-006', plan={'tester': 'fail fail fail fail fail'})
        self.drive()
        s = self.state('APP-006')
        self.assertEqual(s['automation']['status'], 'BLOCKED')
        self.assertEqual(s['rework_count'], 4)
        self.assertIn('3 düzeltme turu aşıldı', s['automation']['message'])
        self.assertEqual(sum(m['role'] == 'developer' for m in self.metrics() if m['type'] == 'run'), 4)

    def test_f_crashed_role_blocks_only_its_task(self):
        self.task('APP-007', plan={'developer': 'crash'})
        self.task('APP-008')
        self.drive()
        s = self.state('APP-007')
        self.assertEqual(s['automation']['status'], 'BLOCKED'); self.assertIn('137', s['automation']['message'])
        self.assertEqual(self.state('APP-008')['stage'], 'DONE')
        self.assertIn('crash', [m['result'] for m in self.metrics() if m['task'] == 'APP-007'])

    def test_g_disjoint_scoped_developers_start_together(self):
        self.task('APP-009', scope=['lib/a/**'], files=['lib/a/x.txt'])
        self.task('APP-010', scope=['lib/b/**'], files=['lib/b/x.txt'])
        self.task('APP-011', scope=['lib/a/**'], files=['lib/a/y.txt'])
        first = self.round()
        self.assertEqual(sorted(first), ['APP-009', 'APP-010'])  # APP-011 overlaps APP-009
        for thread, _, _ in first.values():
            thread.join(30)
        self.drive()
        for ident in ('APP-009', 'APP-010', 'APP-011'):
            self.assertEqual(self.state(ident)['stage'], 'DONE', ident)
        self.assertEqual(self.state('APP-011')['changed_files'], ['lib/a/y.txt'])

    def test_h_unreadable_state_is_skipped_not_fatal(self):
        bad = self.task('APP-012')
        self.task('APP-013')
        (bad / 'state.json').chmod(0)
        try:
            self.drive(5)  # must not raise
            self.assertIn('APP-012: ATLANDI', self.log.getvalue())
            # Fail-closed: the other Developer waits for the unknown lock instead of failing its claim.
            self.assertNotEqual(self.state('APP-013')['automation']['status'], 'BLOCKED')
        finally:
            (bad / 'state.json').chmod(0o644)
        with patch.object(team_runner, 'triage', side_effect=OSError('disk')):
            self.assertEqual(self.round(), {})  # per-task error: skipped, loop alive
        self.drive()
        for ident in ('APP-012', 'APP-013'):
            self.assertEqual(self.state(ident)['stage'], 'DONE', ident)

    def budget(self, **cfg):
        c = team.read(self.root / '.ai/runner.json'); c['budget'] = cfg
        team.atomic(self.root / '.ai/runner.json', c)

    def test_i_budget_warn_logs_thresholds_and_finishes(self):
        self.budget(daily_usd=0.025, action='warn')  # each fake Claude run costs $0.01; Codex has no cost
        self.task('APP-014')
        self.drive()
        self.assertEqual(self.state('APP-014')['stage'], 'DONE')
        log = self.log.getvalue()
        self.assertEqual(log.count('Bütçe UYARI: günlük'), 1)  # once per threshold, not per poll
        self.assertEqual(log.count('Bütçe AŞILDI: günlük'), 1)
        self.assertIn('Bütçe AŞILDI', team.budget_text(self.root))

    def test_j_budget_pause_stops_new_roles_until_resume(self):
        self.budget(daily_usd=0.02, action='pause')
        self.task('APP-015')
        self.drive()
        s = self.state('APP-015')
        self.assertEqual(s['stage'], 'VERIFY_CODEX')  # developer + tester spent $0.02; verifier not started
        out = subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'status'],
                             capture_output=True, text=True).stdout
        self.assertIn('Bütçe doldu — duraklatıldı', out.splitlines()[1])
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'budget', '--resume'],
                       check=True, capture_output=True)
        self.drive()
        self.assertEqual(self.state('APP-015')['stage'], 'DONE')

    def test_k_task_budget_parks_task_for_user(self):
        self.budget(task_usd=0.015)
        self.task('APP-016')
        self.drive()
        s = self.state('APP-016')
        self.assertEqual((s['stage'], s['automation']['status']), ('VERIFY_CODEX', 'AWAITING_USER'))
        self.assertIn('Görev bütçesi aşıldı', s['automation']['message'])
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'budget', '--resume',
                        '--task', 'APP-016'], check=True, capture_output=True)
        self.drive()
        self.assertEqual(self.state('APP-016')['stage'], 'DONE')


    # ---- AI Orchestra Personal V1: Command Center intake, adaptive flow, worktree isolation ----
    def intake(self, text, worktree=False, plan=None):
        import orchestra
        r = orchestra.submit_task(self.root, text, worktree=worktree, start=False)
        for role, modes in (plan or {}).items():
            (self.plans / f'{r["id"]}.{role}').write_text(modes)
        return r

    def roles(self, ident):
        return [m['role'] for m in self.metrics() if m['type'] == 'run' and m['task'] == ident]

    def test_l_dashboard_task_runs_lead_intake_then_full_flow(self):
        r = self.intake('Profil sayfasındaki kayıt butonunun yazım hatasını ve hizasını düzelt')
        s = self.state(r['id'])
        self.assertEqual((s['stage'], s['automation']['status']), ('NEW', 'WAITING_FOR_LEAD'))
        self.assertIn('## Kullanıcı isteği', (self.root / 'tasks' / r['id'] / 'TASK.md').read_text())
        self.drive()
        s = self.state(r['id'])
        self.assertEqual(s['stage'], 'DONE', self.log.getvalue())
        self.assertEqual(self.roles(r['id'])[0], 'lead')  # real Lead run at NEW, no human wait
        self.assertEqual(s['independence'], 'independent')
        self.assertTrue([m for m in self.metrics() if m['type'] == 'intake' and m['task'] == r['id']])

    def test_m_lead_question_parks_task_until_user_replies(self):
        r = self.intake('Bir şeyi düzelt lütfen, detay sonra', plan={'lead': 'ask ok'})
        self.drive()
        s = self.state(r['id'])
        self.assertEqual((s['stage'], s['automation']['status']), ('NEW', 'BLOCKED'))
        self.assertIn('Lead sorusu: Hangi ekran?', s['automation']['message'])
        team.reply(self.root, r['id'], 'Ayarlar ekranı')
        self.assertEqual(self.state(r['id'])['automation']['status'], 'WAITING_FOR_LEAD')
        self.drive()
        self.assertEqual(self.state(r['id'])['stage'], 'DONE', self.log.getvalue())
        self.assertIn('Ayarlar ekranı', (self.root / 'tasks' / r['id'] / 'TASK.md').read_text())

    def test_n_micro_task_starts_no_lead_or_verifier(self):
        self.task('APP-020', 'fix')
        self.drive()
        self.assertEqual(self.roles('APP-020'), ['developer', 'tester'])
        self.assertEqual(self.state('APP-020')['stage'], 'DONE')

    def test_o_micro_touching_dependency_manifest_escalates(self):
        self.task('APP-021', 'fix', files=['pubspec.yaml'])  # built-in risk path, no runner.json entry
        self.drive()
        s = self.state('APP-021')
        self.assertEqual((s['stage'], s['profile'], s['escalated']['files']), ('DONE', 'small', ['pubspec.yaml']))
        self.assertIn('verifier', self.roles('APP-021'))

    def test_p_repeated_identical_failure_blocks_early(self):
        os.environ['FAKE_FAIL_REASON'] = 'login testi yine kırmızı'
        self.addCleanup(os.environ.pop, 'FAKE_FAIL_REASON', None)
        self.task('APP-022', plan={'tester': 'fail fail fail fail'})
        self.drive()
        s = self.state('APP-022')
        self.assertEqual((s['automation']['status'], s['rework_count']), ('BLOCKED', 2))
        self.assertIn('Aynı hata tekrarlandı', s['automation']['message'])

    def git(self, *a, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.root), *a], capture_output=True, text=True, check=True).stdout

    def test_q_worktree_tasks_are_isolated_and_never_merged(self):
        data = Path(tempfile.mkdtemp()); self.addCleanup(__import__('shutil').rmtree, data, True)
        os.environ['XDG_DATA_HOME'] = str(data); self.addCleanup(os.environ.pop, 'XDG_DATA_HOME', None)
        self.git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'init')
        (self.root / 'user_wip.txt').write_text('kullanıcının commit edilmemiş işi')
        (self.root / '.claude').mkdir(); (self.root / '.claude/settings.local.json').write_text('{"permissions":{}}')
        head = self.git('rev-parse', 'HEAD').strip()
        a = self.intake('Ayarlar ekranındaki metni düzelt', worktree=True)
        b = self.intake('Profil ekranındaki metni düzelt', worktree=True)
        self.drive()
        for r in (a, b):
            s = self.state(r['id'])
            self.assertEqual(s['stage'], 'DONE', self.log.getvalue())
            wt = Path(s['worktree']['src'])
            self.assertTrue((wt / 'src' / f'{r["id"]}.txt').is_file())
            self.assertFalse((self.root / 'src' / f'{r["id"]}.txt').exists())  # main checkout untouched
            self.assertTrue((wt / '.claude/settings.local.json').is_file())  # role permissions follow the task
            out = team.commit(self.root, r['id'], apply=True)
            self.assertIn(s['worktree']['branch'], out)
            files = self.git('show', '--name-only', '--format=', cwd=wt).split()
            self.assertEqual(sorted(files), sorted([f'src/{r["id"]}.txt', f'test/{r["id"]}_test.txt']))  # tester tests kept
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), head)  # no merge into the user's branch
        self.assertEqual((self.root / 'user_wip.txt').read_text(), 'kullanıcının commit edilmemiş işi')
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'HEAD').strip(), self.git('branch', '--show-current').strip())

    def test_r_restart_recovers_interrupted_run_and_finishes(self):
        p = self.task('APP-023')
        with team.locked(self.root):
            s = team.read(p / 'state.json')
            s['developer'] = {'session': 'dead-run', 'at': team.now()}
            s['automation'].update(status='RUNNING', role='developer', run_id='dead-run')
            team.atomic(p / 'state.json', s)
        self.assertEqual(self.round(), {})  # a RUNNING task is never started twice
        self.assertEqual(team_runner.recover(self.root), ['APP-023'])
        s = self.state('APP-023')
        self.assertEqual((s['developer'], s['automation']['status']), (None, 'IDLE'))
        self.assertEqual(s['automation']['warnings'][-1]['kind'], 'INTERRUPTED')
        self.drive()
        self.assertEqual(self.state('APP-023')['stage'], 'DONE')


    def test_s_stalled_role_gets_a_lead_decision_not_a_user_wait(self):
        self.task('APP-030', plan={'verifier': 'stall ok'})
        self.drive()
        s = self.state('APP-030')
        self.assertEqual(s['stage'], 'DONE', self.log.getvalue())
        self.assertEqual(self.roles('APP-030'), ['developer', 'tester', 'verifier', 'lead', 'verifier', 'lead'])
        kinds = [(m['role'], m['result']) for m in self.metrics() if m['type'] == 'run']
        self.assertIn(('verifier', 'handoff'), kinds); self.assertIn(('lead', 'decided'), kinds)
        self.assertIn('## Lead kararı', (self.root / 'tasks/APP-030/TASK.md').read_text())
        self.assertIsNone(s['automation'].get('lead_turn'))

    def test_t_lead_turns_are_capped_then_user_reply_goes_to_lead_first(self):
        self.task('APP-031', plan={'verifier': 'stall stall stall ok'})
        self.drive()
        s = self.state('APP-031')
        self.assertEqual((s['stage'], s['automation']['status']), ('VERIFY_CODEX', 'BLOCKED'), self.log.getvalue())
        self.assertEqual(self.roles('APP-031').count('lead'), team_runner.LEAD_HANDOFFS)
        team.reply(self.root, 'APP-031', 'Bu kontrol kapsam dışı, geç')
        self.assertEqual(self.state('APP-031')['automation']['lead_turn']['role'], 'user')
        self.drive()
        self.assertEqual(self.state('APP-031')['stage'], 'DONE', self.log.getvalue())
        self.assertEqual(self.roles('APP-031')[-3:], ['lead', 'verifier', 'lead'])  # reply -> Lead decides -> verifier

    def argv(self, ident, role):
        f = self.plans / f'{ident}.{role}.argv'
        return [json.loads(l) for l in f.read_text().splitlines()] if f.is_file() else []

    def test_v_limit_cut_run_resumes_its_own_session_after_the_reset(self):
        self.task('APP-030', plan={'developer': 'limit429 ok'})
        self.drive()
        s = self.state('APP-030')
        first = self.argv('APP-030', 'developer')[0]
        sid = first[first.index('--session-id') + 1]
        self.assertEqual(s['automation']['status'], 'WAITING_LIMIT', self.log.getvalue())
        self.assertEqual((s['automation']['resume']['session'], s['automation']['resume']['kind']), (sid, 'limit'))
        reset = team_runner.datetime.fromisoformat(s['automation']['retry_at']) - team.utcnow()
        self.assertTrue(3600 < reset.total_seconds() < 3600 + 180, reset)  # the engine's own reset, not a guess
        self.assertEqual(__import__('quota').load()['claude']['status'], 'rejected')
        os.environ['TEAM_NOW_OFFSET'] = str(2 * 3600)
        self.drive()
        self.assertEqual(self.state('APP-030')['stage'], 'DONE', self.log.getvalue())
        again = self.argv('APP-030', 'developer')[1]
        self.assertEqual(again[again.index('--resume') + 1], sid)  # same transcript, not a fresh start
        self.assertIn('DEVAM TURU', again[-1]); self.assertNotIn('--append-system-prompt', again)
        self.assertEqual([m['result'] for m in self.metrics() if m['role'] == 'developer'], ['limit', 'advanced'])

    def test_w_role_that_would_not_fit_the_window_waits_for_the_reset(self):
        self.task('APP-031', plan={'developer': 'usage:0.97'})
        self.drive()
        s = self.state('APP-031')
        self.assertEqual((s['stage'], s['automation']['status']), ('TEST_CLAUDE', 'WAITING_LIMIT'), self.log.getvalue())
        self.assertIn('Kota: claude 5 saat penceresi %97', s['automation']['message'])
        self.assertFalse(self.argv('APP-031', 'tester'))  # deferred before it could die at the limit
        dev = [m for m in self.metrics() if m['role'] == 'developer'][0]
        self.assertEqual(dev['quota'], {'engine': 'claude', 'five_hour': 0.47, 'seven_day': 0.0})
        os.environ['TEAM_NOW_OFFSET'] = str(2 * 3600)
        self.drive()
        self.assertEqual(self.state('APP-031')['stage'], 'DONE', self.log.getvalue())

    def test_x_dependent_task_waits_for_its_dependency(self):
        self.task('APP-032')
        dep = self.task('APP-033')
        with (dep / 'TASK.md').open('a') as f:
            f.write('\n## Bağımlılıklar\n- APP-032 (aynı ekran)\n')
        first = self.round()
        self.assertEqual(sorted(first), ['APP-032'])
        self.assertEqual(self.state('APP-033')['automation']['status'], 'WAITING_DEP')
        for thread, _, _ in first.values():
            thread.join(30)
        self.drive()
        runs = [m for m in self.metrics() if m['type'] == 'run']
        a_end = max(m['ts_end'] for m in runs if m['task'] == 'APP-032')
        self.assertGreaterEqual(min(m['ts_start'] for m in runs if m['task'] == 'APP-033'), a_end)
        self.assertEqual(self.state('APP-033')['stage'], 'DONE', self.log.getvalue())

    def test_y_failed_preflight_parks_the_task_without_spending_a_run(self):
        cfg = team.read(self.root / '.ai/runner.json'); cfg['preflight'] = {'developer': ['false']}
        team.atomic(self.root / '.ai/runner.json', cfg)
        self.task('APP-034')
        self.drive(3)
        s = self.state('APP-034')
        self.assertEqual(s['automation']['status'], 'WAITING_ENV'); self.assertIn('`false` başarısız', s['automation']['message'])
        self.assertFalse(self.metrics())
        cfg['preflight'] = {'developer': ['true']}; team.atomic(self.root / '.ai/runner.json', cfg)
        team_runner._preflight.clear()
        self.drive()
        self.assertEqual(self.state('APP-034')['stage'], 'DONE', self.log.getvalue())

    def test_z_stopped_verifier_is_not_rerun_on_unchanged_inputs(self):
        p = self.task('APP-035')
        with team.locked(self.root):
            s = team.read(p / 'state.json'); s['stage'] = 'VERIFY_CODEX'; team.atomic(p / 'state.json', s)
        team_runner.update_runner_state(self.root, 'APP-035', status='IDLE', stalled={
            'role': 'verifier', 'stage': 'VERIFY_CODEX', 'reason': 'cihaz kanıtı yok',
            'inputs': team_runner.stage_inputs(self.root, 'APP-035', s)})
        self.assertIsNone(team_runner.triage(self.root, 'APP-035', self.state('APP-035')))
        a = self.state('APP-035')['automation']
        self.assertEqual(a['status'], 'AWAITING_USER'); self.assertIn('Engel: cihaz kanıtı yok', a['message'])
        self.assertIsNone(team_runner.triage(self.root, 'APP-035', self.state('APP-035')))  # still parked
        with (p / 'TASK.md').open('a') as f: f.write('\n## Kullanıcı yanıtı\ncihaz ölçümü LEAD_PERF.md içinde\n')
        self.assertEqual(team_runner.triage(self.root, 'APP-035', self.state('APP-035')), 'verifier')
        # unchanged inputs again, but the user forces a run with "devam"
        team_runner.update_runner_state(self.root, 'APP-035', stalled={
            'role': 'verifier', 'stage': 'VERIFY_CODEX', 'inputs': team_runner.stage_inputs(self.root, 'APP-035', s)})
        self.assertIsNone(team_runner.triage(self.root, 'APP-035', self.state('APP-035')))
        subprocess.run([sys.executable, str(BIN / 'team.py'), '--project', str(self.root), 'automate', 'APP-035', '--resume'], check=True)
        self.assertIsNone(self.state('APP-035')['automation'].get('stalled'))
        self.assertEqual(team_runner.triage(self.root, 'APP-035', self.state('APP-035')), 'verifier')

    def test_u_lead_decision_turn_can_ask_the_user(self):
        self.task('APP-032', plan={'verifier': 'stall ok', 'lead': 'ask ok'})
        self.drive()
        s = self.state('APP-032')
        self.assertEqual((s['stage'], s['automation']['status']), ('VERIFY_CODEX', 'BLOCKED'))
        self.assertIn('Lead sorusu: Hangi kural?', s['automation']['message'])
        team.reply(self.root, 'APP-032', 'A kuralı')
        self.assertIsNone(team_runner.lead_question(self.root, 'APP-032'))  # answered
        self.drive()
        self.assertEqual(self.state('APP-032')['stage'], 'DONE', self.log.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
